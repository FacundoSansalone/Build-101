"""Helpers sobre el JSON de A2A (JSON-RPC 0.3). Funciones puras, sin I/O.

El proxy trabaja con el JSON crudo y no con los modelos del SDK: así lo que
no entiende pasa igual, y lo firmado (checkout_jwt, mandatos) nunca se
re-arma desde un modelo.
"""

from __future__ import annotations

import base64
import codecs
import copy
import hashlib
import json
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from contracts import GUARD_VEREDICTO_KEY

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

TipoPedido = Literal["find_products", "create_checkout", "otro"]


def dumps(obj: Any) -> bytes:
    """Mismo formato que JSONResponse de Starlette (lo que usa el SDK)."""
    return json.dumps(
        obj, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def _constante_no_estandar(nombre: str) -> Any:
    raise ValueError(f"constante JSON no estándar: {nombre}")


def parsear_json(contenido: bytes | str) -> tuple[Any, str | None]:
    """JSON estricto (sin NaN ni Infinity). Devuelve (objeto, None) o (None, motivo)."""
    try:
        return json.loads(contenido, parse_constant=_constante_no_estandar), None
    except (ValueError, UnicodeDecodeError, RecursionError) as e:
        return None, f"{type(e).__name__}: {e}"[:200]


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


def menciona_checkout(contenido: bytes) -> bool:
    """¿Una respuesta que no se pudo leer en estricto trae un ap2.checkout?

    Prueba el parser permisivo (el mismo que usaría el cliente, que acepta
    NaN) y, si tampoco puede, busca el texto.
    """
    try:
        return contar_clave(json.loads(contenido), CHECKOUT_KEY) > 0
    except (ValueError, UnicodeDecodeError, RecursionError):
        return CHECKOUT_KEY.encode() in contenido


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
    if contar_clave(obj, GUARD_VEREDICTO_KEY) == 0:
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
        and GUARD_VEREDICTO_KEY in parte["data"]
    )


def es_final(result: dict[str, Any]) -> bool:
    """¿Este evento cierra la tarea para el cliente?"""
    kind = result.get("kind")
    if kind == "status-update":
        return bool(result.get("final"))
    if kind == "task":
        estado = (result.get("status") or {}).get("state")
        return estado in ESTADOS_TERMINALES
    return kind == "message"


def sha256_hex(texto: Any) -> str:
    return hashlib.sha256(str(texto).encode("utf-8")).hexdigest()


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
    )


# ---------------------------------------------------------------------------
# Ofertas (lo que devuelve el comercio a find_products, en uno o varios eventos)
# ---------------------------------------------------------------------------


@dataclass
class Respuesta:
    task_id: str | None = None
    contexto_id: str | None = None
    carts: list[dict[str, Any]] = field(default_factory=list)
    offer_details: list[dict[str, Any]] | None = None
    escenario: str | None = None

    def ingerir(self, result: dict[str, Any]) -> None:
        """Acepta un Task, un TaskArtifactUpdateEvent o un TaskStatusUpdateEvent."""
        kind = result.get("kind")
        if kind == "task":
            self.task_id = result.get("id") or self.task_id
            self.contexto_id = result.get("contextId") or self.contexto_id
            # Un Task es una foto completa: reemplaza lo acumulado.
            self.carts = []
            for artifact in result.get("artifacts") or []:
                self._artefacto(artifact)
        elif kind in ("artifact-update", "status-update"):
            self.task_id = result.get("taskId") or self.task_id
            self.contexto_id = result.get("contextId") or self.contexto_id
            if kind == "artifact-update":
                self._artefacto(result.get("artifact"))
        elif kind == "message":
            self.contexto_id = result.get("contextId") or self.contexto_id

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
# Monto del checkout (para atar la aprobación humana)
# ---------------------------------------------------------------------------


def monto_checkout(checkout: dict[str, Any]) -> tuple[int | None, str | None]:
    """(centavos, moneda) del checkout: lo firmado en el JWT si se puede leer
    (`totals` → total, si no la suma de `line_items`), si no el data part."""
    _, payload = decodificar_jwt(checkout.get("checkout_jwt"))
    centavos: int | None = None
    moneda: str | None = None
    if payload:
        moneda = payload.get("currency") if isinstance(payload.get("currency"), str) else None
        totales = payload.get("totals")
        if isinstance(totales, list):
            for t in totales:
                if isinstance(t, dict) and t.get("type") == "total" and _es_entero(t.get("amount")):
                    centavos = t["amount"]
        if centavos is None and isinstance(payload.get("line_items"), list):
            suma = 0
            for li in payload["line_items"]:
                precio = (li.get("item") or {}).get("price") if isinstance(li, dict) else None
                cantidad = li.get("quantity", 1) if isinstance(li, dict) else None
                if not (_es_entero(precio) and _es_entero(cantidad)):
                    suma = None
                    break
                suma += precio * cantidad
            centavos = suma
    if centavos is None:
        if _es_entero(checkout.get("amount_cents")):
            centavos = checkout["amount_cents"]
        elif isinstance(checkout.get("amount"), (int, float)) and not isinstance(
            checkout.get("amount"), bool
        ):
            centavos = int(round(checkout["amount"] * 100))
    if moneda is None and isinstance(checkout.get("currency"), str):
        moneda = checkout["currency"]
    return centavos, moneda


def _es_entero(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


# ---------------------------------------------------------------------------
# Lo que agrega el proxy (nunca toca lo que vino del comercio)
# ---------------------------------------------------------------------------


def artefacto_veredicto(veredicto_json: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifactId": f"guard-veredicto-{uuid.uuid4().hex}",
        "name": GUARD_VEREDICTO_KEY,
        "parts": [{"kind": "data", "data": {GUARD_VEREDICTO_KEY: veredicto_json}}],
    }


def tarea_con_artefacto(task: dict[str, Any], artefacto: dict[str, Any]) -> dict[str, Any]:
    """Copia de la tarea con un artefacto más al final. Lo demás, igual."""
    return {**task, "artifacts": [*(task.get("artifacts") or []), artefacto]}


def _sin_checkout(artifacts: list[Any]) -> list[Any]:
    """Saca las partes con ap2.checkout; descarta artefactos que quedan vacíos."""
    resultado = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            resultado.append(artifact)
            continue
        parts = [
            p
            for p in artifact.get("parts") or []
            if not (
                isinstance(p, dict)
                and isinstance(p.get("data"), dict)
                and CHECKOUT_KEY in p["data"]
            )
        ]
        if parts:
            resultado.append({**artifact, "parts": parts})
    return resultado


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
    task: dict[str, Any], texto: str, artefacto: dict[str, Any] | None
) -> dict[str, Any]:
    """La misma tarea (mismos ids), `failed`, sin el checkout y con el motivo."""
    artifacts = _sin_checkout(task.get("artifacts") or [])
    if artefacto is not None:
        artifacts.append(artefacto)
    nueva = {
        **task,
        "status": estado_fallido(texto, task.get("id"), task.get("contextId")),
    }
    if artifacts:
        nueva["artifacts"] = artifacts
    else:
        nueva.pop("artifacts", None)
    return nueva


def tarea_sintetica_bloqueada(
    task_id: str | None, contexto_id: str | None, texto: str, artefacto: dict[str, Any]
) -> dict[str, Any]:
    """Una tarea `failed` armada por el proxy cuando no hay una tarea legible
    del comercio (respuesta ilegible, error de red, timeout, etc.)."""
    task_id = task_id or f"guard-bloqueo-{uuid.uuid4().hex}"
    contexto_id = contexto_id or f"guard-contexto-{uuid.uuid4().hex}"
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


async def lineas_acotadas(trozos: AsyncIterator[bytes], limite: int) -> AsyncIterator[str]:
    """Líneas de un stream (CRLF, CR o LF), con tope de bytes en total."""
    decodificador = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pendiente = ""
    total = 0
    async for trozo in trozos:
        total += len(trozo)
        if total > limite:
            raise LimiteExcedido(limite)
        pendiente += decodificador.decode(trozo)
        while True:
            m = _FIN_DE_LINEA.search(pendiente)
            # Un \r al final puede ser la mitad de un \r\n: esperar más.
            if m is None or (m.group() == "\r" and m.end() == len(pendiente)):
                break
            yield pendiente[: m.start()]
            pendiente = pendiente[m.end():]
    pendiente += decodificador.decode(b"", final=True)
    while (m := _FIN_DE_LINEA.search(pendiente)) is not None:
        yield pendiente[: m.start()]
        pendiente = pendiente[m.end():]
    if pendiente:
        yield pendiente


@dataclass
class EventoSSE:
    data: str
    event: str | None = None
    id: str | None = None

    def serializar(self) -> bytes:
        lineas = []
        if self.event:
            lineas.append(f"event: {self.event}")
        if self.id is not None:
            lineas.append(f"id: {self.id}")
        lineas.extend(f"data: {linea}" for linea in self.data.split("\n"))
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
