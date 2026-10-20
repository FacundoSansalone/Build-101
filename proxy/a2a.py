"""Helpers sobre el JSON de A2A (JSON-RPC 0.3). Funciones puras, sin I/O.

El proxy trabaja con el JSON crudo y no con los modelos del SDK: así lo que
no entiende pasa igual, y lo firmado (checkout_jwt, mandatos) nunca se
re-arma desde un modelo.
"""

from __future__ import annotations

import asyncio
import base64
import codecs
import copy
import hashlib
import json
import math
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from contracts import GUARD_PARAMETROS_KEY, GUARD_VEREDICTO_KEY

# Data parts que solo pone el proxy: si llegan del comercio, se borran.
CLAVES_DEL_PROXY = (GUARD_VEREDICTO_KEY, GUARD_PARAMETROS_KEY)

CART_KEY = "ap2.cart"
OFFER_DETAILS_KEY = "demo.offer_details"
SCENARIO_KEY = "demo_scenario"
CHECKOUT_KEY = "ap2.checkout"
# Data parts con mandatos firmados por el usuario (common/constants.py y
# dpc_finish del sample). Se reenvían tal cual; solo se copian al evento.
MANDATO_KEYS = (
    "ap2.mandates.CheckoutMandateSdJwt",
    "ap2.mandates.PaymentMandateSdJwt",
    "checkout_mandate",
)
# Métodos que devuelven una tarea ya existente (no crean un checkout nuevo).
METODOS_DE_TAREA = ("tasks/get", "tasks/resubscribe")
ESTADOS_TERMINALES = {"completed", "failed", "canceled", "rejected"}
# Los `kind` de un result de A2A. Otro lo eligió el comercio: no va a ningún texto.
KINDS_A2A = ("task", "message", "status-update", "artifact-update")

TipoPedido = Literal["find_products", "create_checkout", "otro"]


def dumps(obj: Any) -> bytes:
    """Mismo formato que JSONResponse de Starlette (lo que usa el SDK)."""
    return json.dumps(
        obj, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def _constante_no_estandar(nombre: str) -> Any:
    raise ValueError(f"constante JSON no estándar: {nombre}")


def _float_finito(texto: str) -> float:
    valor = float(texto)
    if not math.isfinite(valor):
        raise ValueError(f"número no finito: {texto[:20]}")
    return valor


def parsear_json(contenido: bytes | str) -> tuple[Any, str | None]:
    """JSON estricto: sin NaN ni Infinity, y sin números que desbordan a
    infinito (`1e999`, que `json.loads` acepta como `inf` pero `dumps` no puede
    volver a escribir). Devuelve (objeto, None) o (None, motivo)."""
    try:
        return json.loads(
            contenido, parse_constant=_constante_no_estandar, parse_float=_float_finito
        ), None
    except (ValueError, UnicodeDecodeError, RecursionError) as e:
        return None, f"{type(e).__name__}: {e}"[:200]


# Profundidad máxima de una respuesta del comercio (objetos y listas). Una de
# A2A real anda por los 10 niveles. Más hondo, copiarla (`deepcopy`) o
# serializar su evento (pydantic corta cerca de los 100) falla: se trata como
# ilegible, antes de cualquier otra etapa.
PROFUNDIDAD_MAXIMA = 64


def anidado_de_mas(obj: Any, maximo: int) -> bool:
    """¿`obj` tiene más de `maximo` niveles de objetos o listas? Iterativo: no
    depende del límite de recursión."""
    pila = [(obj, 1)]
    while pila:
        actual, nivel = pila.pop()
        if not isinstance(actual, (dict, list)):
            continue
        if nivel > maximo:
            return True
        hijos = actual.values() if isinstance(actual, dict) else actual
        pila.extend((hijo, nivel + 1) for hijo in hijos)
    return False


def contar_clave(obj: Any, clave: str) -> int:
    """Cuántas veces aparece `clave` como clave de un objeto, a cualquier
    profundidad. Iterativo: no depende del límite de recursión."""
    n = 0
    pila = [obj]
    while pila:
        actual = pila.pop()
        if isinstance(actual, dict):
            if clave in actual:
                n += 1
            pila.extend(actual.values())
        elif isinstance(actual, list):
            pila.extend(actual)
    return n


def menciona_clave(contenido: bytes, clave: str) -> bool:
    """¿Una respuesta que no se pudo leer en estricto trae `clave`?

    Prueba el parser permisivo (el mismo que usaría el cliente, que acepta
    NaN y 1e999) y, si tampoco puede, busca el texto.
    """
    try:
        return contar_clave(json.loads(contenido), clave) > 0
    except (ValueError, UnicodeDecodeError, RecursionError):
        return clave.encode() in contenido


def menciona_checkout(contenido: bytes) -> bool:
    return menciona_clave(contenido, CHECKOUT_KEY)


def data_parts(parts: Any) -> list[dict[str, Any]]:
    if not isinstance(parts, list):
        return []
    return [
        p["data"]
        for p in parts
        if isinstance(p, dict)
        and p.get("kind") == "data"
        and isinstance(p.get("data"), dict)
    ]


def buscar(clave: str, datos: list[dict[str, Any]]) -> Any:
    for d in datos:
        if clave in d:
            return d[clave]
    return None


def checkouts_en_artefactos(artifacts: Any) -> list[Any]:
    """Los ap2.checkout de los data parts de una lista de artefactos (lo que
    lee el Shopping Agent: `_extract_first_data(task.artifacts, ...)`)."""
    if not isinstance(artifacts, list):
        return []
    return [
        datos[CHECKOUT_KEY]
        for artifact in artifacts
        if isinstance(artifact, dict)
        for datos in data_parts(artifact.get("parts"))
        if CHECKOUT_KEY in datos
    ]


def checkouts_del_result(result: dict[str, Any]) -> list[Any]:
    """Checkouts en el lugar esperado según el tipo de evento A2A."""
    kind = result.get("kind")
    if kind == "task":
        return checkouts_en_artefactos(result.get("artifacts"))
    if kind == "artifact-update":
        return checkouts_en_artefactos([result.get("artifact")])
    return []


def quitar_veredictos_ajenos(obj: Any) -> tuple[Any, bool]:
    """Saca toda parte con `guard.veredicto` (solo la puede poner el proxy).

    Devuelve (objeto, cambió). Si no había nada que sacar devuelve el mismo
    objeto; si no, una copia (lo demás, incluido el ap2.checkout, igual).
    """
    if all(contar_clave(obj, clave) == 0 for clave in CLAVES_DEL_PROXY):
        return obj, False
    copia = copy.deepcopy(obj)
    vaciados: set[int] = set()  # id() de los objetos que se quedaron sin partes
    cambio = False
    pila: list[Any] = [copia]
    while pila:
        actual = pila.pop()
        if isinstance(actual, dict):
            parts = actual.get("parts")
            if isinstance(parts, list):
                quedan = [p for p in parts if not _es_parte_veredicto(p)]
                if len(quedan) != len(parts):
                    cambio = True
                    actual["parts"] = quedan
                    if not quedan:
                        vaciados.add(id(actual))
            pila.extend(v for v in actual.values() if isinstance(v, (dict, list)))
        elif isinstance(actual, list):
            pila.extend(v for v in actual if isinstance(v, (dict, list)))
    if not cambio:
        return obj, False
    # Los artefactos que quedaron vacíos por sacar el veredicto se descartan.
    pila = [copia]
    while pila:
        actual = pila.pop()
        if isinstance(actual, dict):
            artifacts = actual.get("artifacts")
            if isinstance(artifacts, list):
                actual["artifacts"] = [a for a in artifacts if id(a) not in vaciados]
            pila.extend(v for v in actual.values() if isinstance(v, (dict, list)))
        elif isinstance(actual, list):
            pila.extend(v for v in actual if isinstance(v, (dict, list)))
    return copia, True


def _es_parte_veredicto(parte: Any) -> bool:
    return (
        isinstance(parte, dict)
        and isinstance(parte.get("data"), dict)
        and any(clave in parte["data"] for clave in CLAVES_DEL_PROXY)
    )


def tipo_de(result: Any) -> str:
    """El `kind` de un result si es uno de A2A (`KINDS_A2A`); si no, el tipo de
    Python (`dict`, `list`…). Para los motivos: nunca texto del comercio."""
    kind = result.get("kind") if isinstance(result, dict) else None
    return kind if isinstance(kind, str) and kind in KINDS_A2A else type(result).__name__


def es_final(result: dict[str, Any]) -> bool:
    """¿Este evento cierra la tarea para el cliente? Un `status` o un `state`
    de otro tipo (lo elige el comercio) no cierra nada."""
    kind = result.get("kind")
    if kind == "status-update":
        return bool(result.get("final"))
    if kind == "task":
        status = result.get("status")
        estado = status.get("state") if isinstance(status, dict) else None
        return isinstance(estado, str) and estado in ESTADOS_TERMINALES
    return kind == "message"


def sha256_hex(texto: Any) -> str:
    return hashlib.sha256(str(texto).encode("utf-8")).hexdigest()


def huella_checkout(checkout: Any) -> str:
    """SHA-256 del data part `ap2.checkout` COMPLETO en JSON canónico (claves
    ordenadas, sin espacios). Cambia si cambia cualquier campo (el JWT, el
    `checkout_hash`, `amount`, `amount_cents`, `currency`, `item_label`…), no
    si solo cambia el orden de las claves. Si no se puede serializar devuelve
    una huella única: nunca coincide con una decisión guardada."""
    try:
        canonico = json.dumps(
            checkout, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError):
        return f"no-serializable-{uuid.uuid4().hex}"
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Pedido (lo que manda el Shopping Agent)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Pedido:
    rpc_id: Any = None
    metodo_rpc: str | None = None
    tipo: TipoPedido = "otro"
    contexto_id: str | None = None
    catalog_search: str | None = None
    cart_id: str | None = None
    escenario: str | None = None
    mandatos: dict[str, Any] = field(default_factory=dict)
    task_id: str | None = None  # params.id de tasks/get y tasks/resubscribe
    # El mensaje tal cual (para reconocer su eco en el history: ver sin_eco_del_pedido)
    mensaje: dict[str, Any] | None = field(default=None, repr=False, compare=False)

    @property
    def es_de_tarea(self) -> bool:
        return self.metodo_rpc in METODOS_DE_TAREA


def leer_pedido(raw: bytes) -> Pedido:
    """Clasifica el JSON-RPC entrante sin modificarlo."""
    body, _ = parsear_json(raw)
    if not isinstance(body, dict):
        return Pedido()
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    message = params.get("message") if isinstance(params.get("message"), dict) else {}
    datos = data_parts(message.get("parts"))
    metodo = body.get("method") if isinstance(body.get("method"), str) else None

    catalog_search = buscar("catalog_search", datos)
    cart_id = buscar("cart_id", datos)
    tipo: TipoPedido = "otro"
    if catalog_search:
        tipo = "find_products"
    elif cart_id:
        tipo = "create_checkout"
    escenario = buscar(SCENARIO_KEY, datos)
    contexto = message.get("contextId")
    task_id = params.get("id") if metodo in METODOS_DE_TAREA else None
    return Pedido(
        rpc_id=body.get("id"),
        metodo_rpc=metodo,
        tipo=tipo,
        contexto_id=str(contexto) if contexto else None,
        catalog_search=str(catalog_search) if catalog_search else None,
        cart_id=str(cart_id) if cart_id else None,
        escenario=str(escenario) if escenario else None,
        mandatos={k: v for k in MANDATO_KEYS if (v := buscar(k, datos)) is not None},
        task_id=str(task_id) if task_id else None,
        mensaje=message or None,
    )


def _partes_canonicas(partes: Any) -> list[Any] | None:
    """Las partes de un mensaje con lo que importa (tipo, texto, datos)."""
    if not isinstance(partes, list):
        return None
    salida = []
    for parte in partes:
        if not isinstance(parte, dict):
            return None
        salida.append({k: parte.get(k) for k in ("kind", "text", "data") if k in parte})
    return salida


def sin_eco_del_pedido(result: Any, mensaje: dict[str, Any] | None) -> Any:
    """`result` sin los mensajes del `history` que son el eco exacto del
    pedido del comprador: mismo `messageId` y mismas partes que `mensaje`.

    Es para el filtro de seguridad: ese eco es texto nuestro (el pedido del
    usuario, "Find products…"), no del proveedor. El agente del cliente del
    sample manda `role: agent`, así que el rol no alcanza para reconocerlo. Un
    mensaje que el comercio alteró (aunque use el mismo `messageId`) no es el
    eco y se sigue revisando. Devuelve una copia; el original no cambia.
    """
    if not isinstance(result, dict) or not isinstance(mensaje, dict):
        return result
    historial = result.get("history")
    mensaje_id = mensaje.get("messageId")
    partes = _partes_canonicas(mensaje.get("parts"))
    if not isinstance(historial, list) or not mensaje_id or partes is None:
        return result

    def es_eco(m: Any) -> bool:
        return (
            isinstance(m, dict)
            and m.get("messageId") == mensaje_id
            and m.get("role") == mensaje.get("role")
            and _partes_canonicas(m.get("parts")) == partes
        )

    if not any(es_eco(m) for m in historial):
        return result
    copia = dict(result)
    copia["history"] = [m for m in historial if not es_eco(m)]
    return copia


# ---------------------------------------------------------------------------
# Ofertas (lo que devuelve el comercio a find_products, en uno o varios eventos)
# ---------------------------------------------------------------------------


def _texto(valor: Any) -> str | None:
    return valor if isinstance(valor, str) else None


@dataclass
class Respuesta:
    task_id: str | None = None
    contexto_id: str | None = None
    carts: list[dict[str, Any]] = field(default_factory=list)
    offer_details: list[dict[str, Any]] | None = None
    escenario: str | None = None

    def ingerir(self, result: dict[str, Any]) -> None:
        """Acepta un Task, un TaskArtifactUpdateEvent o un TaskStatusUpdateEvent.

        Los ids solo si son strings y los artefactos solo si son una lista:
        lo de otro tipo (lo elige el comercio) se ignora, como lo ilegible."""
        kind = result.get("kind")
        if kind == "task":
            self.task_id = _texto(result.get("id")) or self.task_id
            self.contexto_id = _texto(result.get("contextId")) or self.contexto_id
            # Un Task es una foto completa: reemplaza lo acumulado.
            self.carts = []
            artifacts = result.get("artifacts")
            for artifact in artifacts if isinstance(artifacts, list) else []:
                self._artefacto(artifact)
        elif kind in ("artifact-update", "status-update"):
            self.task_id = _texto(result.get("taskId")) or self.task_id
            self.contexto_id = _texto(result.get("contextId")) or self.contexto_id
            if kind == "artifact-update":
                self._artefacto(result.get("artifact"))
        elif kind == "message":
            self.contexto_id = _texto(result.get("contextId")) or self.contexto_id

    def _artefacto(self, artifact: Any) -> None:
        if not isinstance(artifact, dict):
            return
        for datos in data_parts(artifact.get("parts")):
            if CART_KEY in datos and isinstance(datos[CART_KEY], dict):
                self.carts.append(datos[CART_KEY])
            if OFFER_DETAILS_KEY in datos and isinstance(datos[OFFER_DETAILS_KEY], list):
                self.offer_details = datos[OFFER_DETAILS_KEY]
            if datos.get(SCENARIO_KEY):
                self.escenario = str(datos[SCENARIO_KEY])


# ---------------------------------------------------------------------------
# Monto firmado del checkout (para atar la aprobación humana)
# ---------------------------------------------------------------------------


def monto_firmado(checkout: dict[str, Any]) -> tuple[int, str] | None:
    """(centavos, moneda) que firmó el comercio en el `checkout_jwt`, o None.

    Es exactamente el `totals[type=total].amount` del payload (un entero
    JSON, en centavos) y la `currency` firmada. Sin fallbacks: ni la suma de
    `line_items` (la arma el comercio) ni el data part (`amount`,
    `amount_cents`). Si el JWT no se puede leer en estricto (claves
    duplicadas, números no finitos), falta el total, hay más de uno o no es un
    entero exacto, devuelve None y no se puede aprobar.
    """
    payload = _payload_estricto(checkout.get("checkout_jwt"))
    if payload is None:
        return None
    moneda = payload.get("currency")
    if not isinstance(moneda, str) or not 0 < len(moneda) <= 16:
        return None
    totales = payload.get("totals")
    if not isinstance(totales, list):
        return None
    montos = [t.get("amount") for t in totales if isinstance(t, dict) and t.get("type") == "total"]
    if len(montos) != 1 or not _es_entero(montos[0]) or montos[0] < 0:
        return None
    return montos[0], moneda


def _sin_duplicadas(pares: list[tuple[str, Any]]) -> dict[str, Any]:
    objeto: dict[str, Any] = {}
    for clave, valor in pares:
        if clave in objeto:
            raise ValueError(f"clave duplicada: {clave!r}")
        objeto[clave] = valor
    return objeto


def _payload_estricto(jwt: Any) -> dict[str, Any] | None:
    """Payload de un JWT leído en estricto (sin claves duplicadas, NaN ni
    números que desbordan). No verifica la firma: eso lo hace el Guard/AP2."""
    if not isinstance(jwt, str) or jwt.count(".") != 2:
        return None
    segmento = jwt.split(".")[1]
    try:
        raw = base64.urlsafe_b64decode(segmento + "=" * (-len(segmento) % 4))
        payload = json.loads(
            raw,
            object_pairs_hook=_sin_duplicadas,
            parse_constant=_constante_no_estandar,
            parse_float=_float_finito,
        )
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


def _es_entero(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


# ---------------------------------------------------------------------------
# Lo que agrega el proxy (nunca toca lo que vino del comercio)
# ---------------------------------------------------------------------------


_CART_ID_SEGURO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
# Contexto, cart, task, aprobación, merchant.id (`tipos_gateway.ID_SEGURO_RE`).
ID_SEGURO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}")


def cart_id_seguro(valor: Any) -> str | None:
    """El cart_id si es un identificador corto y sin texto libre; si no, None."""
    return valor if isinstance(valor, str) and _CART_ID_SEGURO.fullmatch(valor) else None


def id_seguro(valor: Any) -> str | None:
    """El id (de tarea o de contexto) si es un identificador sin texto libre;
    si no, None. Un id que eligió el comercio puede traer una inyección."""
    return valor if isinstance(valor, str) and ID_SEGURO_RE.fullmatch(valor) else None


def veredicto_para_agente(veredicto_json: dict[str, Any]) -> dict[str, Any]:
    """La versión del veredicto que va al agente, sin texto del comercio.

    Solo `status` y `motivo` (los arma guard/ con plantillas fijas), el
    `cart_id_elegido` si es un identificador seguro (si no, None) y los
    chequeos con `tipo` y `resultado`. Nada de `observado`, `esperado`,
    `detalle`, `campo`, ranking ni recibo (`verificaciones[].declarado`): eso
    va completo al dashboard en el evento `veredicto`.
    """
    cart_id = veredicto_json.get("cart_id_elegido")
    chequeos = veredicto_json.get("chequeos")
    return {
        "status": veredicto_json.get("status"),
        "motivo": veredicto_json.get("motivo"),
        "cart_id_elegido": cart_id
        if isinstance(cart_id, str) and _CART_ID_SEGURO.fullmatch(cart_id)
        else None,
        "chequeos": [
            {"tipo": c.get("tipo"), "resultado": c.get("resultado")}
            for c in (chequeos if isinstance(chequeos, list) else [])
            if isinstance(c, dict)
        ],
    }


def artefacto_veredicto(veredicto_json: dict[str, Any]) -> dict[str, Any]:
    """El artefacto `guard.veredicto` (para el agente: pasale la versión de
    `veredicto_para_agente`)."""
    return {
        "artifactId": f"guard-veredicto-{uuid.uuid4().hex}",
        "name": GUARD_VEREDICTO_KEY,
        "parts": [{"kind": "data", "data": {GUARD_VEREDICTO_KEY: veredicto_json}}],
    }


def tarea_con_artefacto(task: dict[str, Any], artefacto: dict[str, Any]) -> dict[str, Any]:
    """Copia de la tarea con un artefacto más al final. Lo demás, igual."""
    return {**task, "artifacts": [*(task.get("artifacts") or []), artefacto]}


def _ahora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def estado_fallido(texto: str, task_id: str | None, contexto_id: str | None) -> dict[str, Any]:
    message: dict[str, Any] = {
        "kind": "message",
        "messageId": uuid.uuid4().hex,
        "role": "agent",
        "parts": [{"kind": "text", "text": texto}],
    }
    if task_id:
        message["taskId"] = task_id
    if contexto_id:
        message["contextId"] = contexto_id
    return {"state": "failed", "message": message, "timestamp": _ahora_iso()}


def tarea_bloqueada(
    task: dict[str, Any], texto: str, artefacto: dict[str, Any]
) -> dict[str, Any]:
    """La tarea retenida o bloqueada: los mismos ids (si son seguros), `failed`,
    con el motivo y solo el artefacto del veredicto. Nada más de la tarea del
    comercio (ni el checkout, ni `history`, `metadata` u otros artefactos):
    el agente no tiene por qué leer texto del comercio en una tarea que no
    sigue. El agente del sample lee el veredicto de `task.artifacts`."""
    return tarea_sintetica_bloqueada(task.get("id"), task.get("contextId"), texto, artefacto)


def tarea_sintetica_bloqueada(
    task_id: str | None, contexto_id: str | None, texto: str, artefacto: dict[str, Any]
) -> dict[str, Any]:
    """Una tarea `failed` armada por el proxy cuando no hay una tarea legible
    del comercio (respuesta ilegible, error de red, timeout, etc.). Los ids
    solo si son seguros (`id_seguro`); si no, unos del proxy."""
    task_id = id_seguro(task_id) or f"guard-bloqueo-{uuid.uuid4().hex}"
    contexto_id = id_seguro(contexto_id) or f"guard-contexto-{uuid.uuid4().hex}"
    return {
        "kind": "task",
        "id": task_id,
        "contextId": contexto_id,
        "status": estado_fallido(texto, task_id, contexto_id),
        "artifacts": [artefacto],
    }


def evento_artefacto(
    task_id: str | None, contexto_id: str | None, artefacto: dict[str, Any]
) -> dict[str, Any]:
    return {
        "kind": "artifact-update",
        "taskId": task_id,
        "contextId": contexto_id,
        "artifact": artefacto,
    }


def evento_fallido(
    task_id: str | None, contexto_id: str | None, texto: str
) -> dict[str, Any]:
    return {
        "kind": "status-update",
        "taskId": task_id,
        "contextId": contexto_id,
        "status": estado_fallido(texto, task_id, contexto_id),
        "final": True,
    }


def respuesta_jsonrpc(rpc_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def error_jsonrpc(rpc_id: Any, mensaje: str, code: int = -32603) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": mensaje}}


# ---------------------------------------------------------------------------
# Agent Card y JWT
# ---------------------------------------------------------------------------


def reescribir_card(card: dict[str, Any], url_publica: str) -> dict[str, Any]:
    """La card con la URL del proxy, para que el cliente no lo saltee.

    La Agent Card no es un objeto firmado de AP2.
    """
    nueva = {**card, "url": url_publica}
    interfaces = card.get("additionalInterfaces")
    if isinstance(interfaces, list):
        nueva["additionalInterfaces"] = [
            {**i, "url": url_publica} if isinstance(i, dict) else i for i in interfaces
        ]
    return nueva


def _b64url_json(segmento: str) -> Any:
    raw = base64.urlsafe_b64decode(segmento + "=" * (-len(segmento) % 4))
    return json.loads(raw)


def decodificar_jwt(jwt: Any) -> tuple[dict | None, dict | None]:
    """Header y payload de un JWT, solo para mostrar. No verifica la firma."""
    if not isinstance(jwt, str) or jwt.count(".") != 2:
        return None, None
    header, payload, _ = jwt.split(".")
    try:
        h, p = _b64url_json(header), _b64url_json(payload)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None, None
    if not isinstance(h, dict) or not isinstance(p, dict):
        return None, None
    return h, p


# ---------------------------------------------------------------------------
# Lectura acotada (tope de bytes por respuesta del comercio) y SSE
# ---------------------------------------------------------------------------


class LimiteExcedido(Exception):
    """La respuesta del comercio superó el tope de bytes."""

    def __init__(self, limite: int, parcial: bytes = b"") -> None:
        super().__init__(f"la respuesta del comercio supera {limite} bytes")
        self.limite = limite
        self.parcial = parcial


async def leer_acotado(trozos: AsyncIterator[bytes], limite: int) -> bytes:
    partes: list[bytes] = []
    total = 0
    async for trozo in trozos:
        total += len(trozo)
        if total > limite:
            raise LimiteExcedido(limite, (b"".join(partes) + trozo)[:limite])
        partes.append(trozo)
    return b"".join(partes)


_FIN_DE_LINEA = re.compile(r"\r\n|\r|\n")
# Cada tantas líneas el lector cede el event loop: un trozo enorme lleno de
# líneas (un comercio hostil) no frena al resto del proxy.
_LINEAS_POR_TURNO = 1000


async def lineas_acotadas(trozos: AsyncIterator[bytes], limite: int) -> AsyncIterator[str]:
    """Líneas de un stream (CRLF, CR o LF), con tope de bytes en total.

    Lineal en el tamaño: cada trozo se recorre con un índice y lo que queda
    se recorta una vez por trozo (recortar por línea era cuadrático), y la
    búsqueda sigue desde donde quedó la anterior. Cede cada
    `_LINEAS_POR_TURNO` líneas."""
    decodificador = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pendiente = ""
    total = 0
    lineas = 0
    async for trozo in trozos:
        total += len(trozo)
        if total > limite:
            raise LimiteExcedido(limite)
        # Lo pendiente no tiene fines de línea, salvo un \r al final.
        desde = max(0, len(pendiente) - 1)
        pendiente += decodificador.decode(trozo)
        inicio = 0
        while True:
            m = _FIN_DE_LINEA.search(pendiente, max(inicio, desde))
            # Un \r al final puede ser la mitad de un \r\n: esperar más.
            if m is None or (m.group() == "\r" and m.end() == len(pendiente)):
                break
            yield pendiente[inicio: m.start()]
            inicio = m.end()
            lineas += 1
            if lineas % _LINEAS_POR_TURNO == 0:
                await asyncio.sleep(0)
        pendiente = pendiente[inicio:]
    pendiente += decodificador.decode(b"", final=True)
    inicio = 0
    while (m := _FIN_DE_LINEA.search(pendiente, inicio)) is not None:
        yield pendiente[inicio: m.start()]
        inicio = m.end()
    if inicio < len(pendiente):
        yield pendiente[inicio:]


# Todo lo que `str.splitlines()` toma como fin de línea. El proxy corta el
# SSE solo en CR/LF, pero el cliente A2A (httpx) corta con splitlines(): si
# uno de estos caracteres sale crudo, el cliente ve líneas (y eventos) que el
# proxy nunca vio.
_FINES_DE_LINEA = "\r\n\x0b\x0c\x1c\x1d\x1e\x85  "
_SIN_FINES_DE_LINEA = str.maketrans("", "", _FINES_DE_LINEA)
# En `data:` (JSON) se escapan: dentro de un string JSON valen lo mismo.
_ESCAPES_DATA = str.maketrans({c: f"\\u{ord(c):04x}" for c in _FINES_DE_LINEA[2:]})


def _sanear_campo(valor: str) -> str:
    """`id`/`event` del comercio sin ningún fin de línea (N7)."""
    return valor.translate(_SIN_FINES_DE_LINEA)


@dataclass
class EventoSSE:
    data: str
    event: str | None = None
    id: str | None = None

    def serializar(self) -> bytes:
        """El evento en SSE. `id` y `event` se sanean y los fines de línea
        que no son CR/LF se escapan en `data` (N7): el cliente ve exactamente
        las líneas que vio el proxy."""
        lineas = []
        event = _sanear_campo(self.event) if self.event else ""
        if event:
            lineas.append(f"event: {event}")
        if self.id is not None:
            lineas.append(f"id: {_sanear_campo(self.id)}")
        lineas.extend(
            f"data: {linea.translate(_ESCAPES_DATA)}" for linea in self.data.split("\n")
        )
        return ("\n".join(lineas) + "\n\n").encode("utf-8")


def bloque_sse(body: dict[str, Any]) -> bytes:
    return EventoSSE(dumps(body).decode("utf-8")).serializar()


async def leer_sse(lineas: AsyncIterator[str]) -> AsyncIterator[EventoSSE]:
    """Parser SSE mínimo. El JSON de cada `data:` queda tal cual llegó."""
    data: list[str] = []
    event: str | None = None
    event_id: str | None = None
    async for linea in lineas:
        linea = linea.rstrip("\r\n")
        if not linea:
            if data:
                yield EventoSSE(data="\n".join(data), event=event, id=event_id)
            data, event, event_id = [], None, None
            continue
        if linea.startswith(":"):
            continue
        nombre, _, valor = linea.partition(":")
        valor = valor[1:] if valor.startswith(" ") else valor
        if nombre == "data":
            data.append(valor)
        elif nombre == "event":
            event = valor
        elif nombre == "id":
            event_id = valor
    if data:
        yield EventoSSE(data="\n".join(data), event=event, id=event_id)
