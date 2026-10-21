"""Estado persistente del gateway de producción (diseño §1.7 y §1.10).

Guarda solo lo de las **compras de integrador** (`origen="api"`): la compra,
las ataduras `contextId` → compra, las ofertas de sus contextos, el último
veredicto por (contexto, cart), sus aprobaciones y las entregas de webhooks.
El tráfico anónimo de la demo no pasa por acá y el registro de veredictos
de `proxy/persistencia.py` no cambia.

Dos almacenes con la misma interfaz (`tipos_gateway.AlmacenEstado`):

- `EstadoMemoria`: el default. Con `durable=False` equivale a hoy (nada
  sobrevive a un reinicio). En los tests, con `durable=True` y la misma
  instancia en dos `create_app`, simula un reinicio.
- `EstadoSupabase`: PostgREST con httpx y timeout corto. Solo con
  `GATEWAY_ESTADO=supabase` explícito: las credenciales `SUPABASE_*` solas no
  lo activan, así la demo (que las tiene en `.env`) no escribe en tablas que
  todavía no existen. Las tablas están en `proxy/sql/003_gateway.sql` y
  `004_serverless.sql` (en ese orden), que se aplican a mano.

Fail-closed: cualquier falla del almacén es `ErrorAlmacen` y quien llama
bloquea (503 o corte); nunca se toma una falla por "no hay nada". Lo que se
escribe en segundo plano va por `TareasEnFondo.programar`, que nunca lanza: si
una escritura se pierde y hay un reinicio, falta el dato y el proxy bloquea.

Varias instancias sin memoria compartida (Vercel, `proxy/sql/004_serverless.sql`):
la base también guarda las esperas (el porqué y la decisión), los eventos de
cada compra y la cola de webhooks, y protege la fila de la compra (un trigger
descarta una escritura con `actualizada` menor que la guardada y no deja salir
de `bloqueada` ni de `vencida`). Lo que decide entre instancias es atómico en
la base: abrir una espera (una abierta por compra), responderla o vencerla
(PATCH condicionado a `estado=eq.abierta`), crear una aprobación pendiente
(una por contexto, cart y total firmado), una escritura condicionada de la
compra y la toma de una entrega de webhook (`tomada_hasta`). `EstadoMemoria`
imita el trigger y los índices, así los tests ven lo mismo que producción.
`TareasEnFondo.barrera` espera las escrituras en vuelo antes de responder y
avisa si falló una decisiva.

Este módulo no decide cuándo persistir: lo decide el núcleo del proxy.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import copy
import json
import logging
import re
import secrets
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final, get_args

import httpx
from pydantic import ValidationError

from contracts import Evento
from proxy.aprobaciones import Aprobacion
from proxy.persistencia import cabeceras, credenciales, fila_aprobacion
from proxy.tipos_gateway import (
    ID_COMPRA_RE,
    ID_ESPERA_RE,
    ID_SEGURO_RE,
    ID_SLUG_RE,
    AlmacenEstado,
    EntregaWebhook,
    ErrorAlmacen,
    EscrituraDecisivaFallida,
    EstadoEspera,
    TipoEspera,
    es_id,
)

logger = logging.getLogger("proxy.estado")

TIMEOUT_SEGUNDOS = 5.0
MAX_PENDIENTES_FONDO = 100
LIMITE_LISTADO_MAXIMO = 200

# Mismos valores que los `check` de 003_gateway.sql: un estado que la base
# rechazaría se rechaza antes (y en memoria también, para que los tests vean
# lo mismo que producción).
ESTADOS_COMPRA_SQL = frozenset(
    {
        "creada", "esperando_contexto", "negociando", "esperando_decision",
        "sin_oportunidad", "bloqueada", "aceptada", "rechazada", "vencida",
    }
)
ESTADOS_TERMINALES = ("bloqueada", "vencida")
# Las que cuentan para el límite de compras activas: las que todavía no
# resolvieron la negociación (`compras.ESTADOS_ACTIVOS_API`). Una sin
# oportunidad, aceptada o rechazada ya no ocupa lugar.
ESTADOS_ACTIVOS = ("creada", "esperando_contexto", "negociando", "esperando_decision")
STATUS_VEREDICTO_SQL = frozenset({"aprobado", "con_avisos", "escalar", "bloqueado"})

TABLA_COMPRAS = "gw_compras"
TABLA_CONTEXTOS = "gw_contextos"
TABLA_OFERTAS = "gw_ofertas"
TABLA_VEREDICTOS = "gw_ultimos_veredictos"
TABLA_APROBACIONES = "gw_aprobaciones"
TABLA_ENTREGAS = "gw_entregas_webhook"
TABLA_ESPERAS = "gw_esperas"        # 004_serverless.sql
TABLA_EVENTOS = "gw_eventos"        # 004_serverless.sql

# Los `check` de 004_serverless.sql.
TIPOS_ESPERA = frozenset(get_args(TipoEspera))
ESTADOS_ESPERA = frozenset(get_args(EstadoEspera))
COLUMNAS_ESPERA = (
    "id", "compra_id", "tipo", "estado", "oportunidad_id", "vence", "respuesta",
    "creada", "resuelta",
)
COLUMNAS_EVENTO = (
    "id", "ts", "integrador_id", "compra_id", "contexto_id", "escenario", "guard_activo",
    "tipo", "datos",
)
# Las de `EntregaWebhook.a_json()`: el listado no trae el cuerpo de la cola.
COLUMNAS_ENTREGA = (
    "id", "integrador_id", "tipo", "compra_id", "creada", "intentos", "estado",
    "status_http", "error", "destino_host", "actualizada",
)
MAX_BYTES_RESPUESTA = 16 * 1024          # la respuesta de una espera (jsonb)
MAX_BYTES_CUERPO_WEBHOOK = 1024 * 1024   # el cuerpo de una entrega en la cola
INTENTOS_APROBACION = 3                  # insertar o leer la pendiente que ya existe
TIPO_EVENTO_RE = re.compile(r"[a-z][a-z0-9_]{0,59}")
PG_UNICA = "23505"                       # unique_violation (PostgREST responde 409)
LARGO_REFERENCIA = 200                   # el de `api_v1.LARGO_REFERENCIA`
IDS_POR_CONSULTA = 100                   # `compra_id=in.(…)`: la URL queda corta
MAX_FILAS_LECTURA = 20_000               # varias compras: contextos o aprobaciones
MAX_EVENTOS_RECIENTES = 1000             # los más nuevos de una compra (la explicación)
# Lo que borra la purga (retención), en este orden: lo que apunta a una compra
# antes que la compra (los contextos y las esperas además se van en cascada).
TABLAS_PURGA = (
    "gw_eventos", "gw_ofertas", "gw_ultimos_veredictos", "gw_aprobaciones",
    "gw_entregas_webhook", "gw_esperas", "gw_contextos", "gw_compras",
)


class _Cualquiera:
    """Centinela de `guardar_compra_condicionada`: sin condición sobre `espera_id`."""

    def __repr__(self) -> str:
        return "CUALQUIERA"


CUALQUIERA: Final = _Cualquiera()


# -- validación de ids (antes de cada operación, en los dos almacenes) --------


def _id(regex: Any, valor: Any, nombre: str) -> str:
    """El id si cumple el formato; si no, `ErrorAlmacen` y no se hace nada.

    Los ids van en filtros de PostgREST (`?id=eq.<id>`): un id con `&`, `,` o
    paréntesis podría cambiar el filtro. El mensaje no incluye el valor."""
    if not es_id(regex, valor):
        raise ErrorAlmacen(f"{nombre} con formato inválido")
    return valor


def _id_compra(valor: Any) -> str:
    return _id(ID_COMPRA_RE, valor, "compra_id")


def _id_seguro(valor: Any, nombre: str) -> str:
    return _id(ID_SEGURO_RE, valor, nombre)


def _id_slug(valor: Any, nombre: str) -> str:
    return _id(ID_SLUG_RE, valor, nombre)


def _limite(limite: Any) -> int:
    if not isinstance(limite, int) or isinstance(limite, bool) or limite < 1:
        raise ErrorAlmacen("límite inválido")
    return min(limite, LIMITE_LISTADO_MAXIMO)


def _estado_compra(estado: Any) -> str:
    if estado not in ESTADOS_COMPRA_SQL:
        raise ErrorAlmacen("estado de compra desconocido")
    return estado


def _estados_filtro(estado: Any) -> tuple[str, ...] | None:
    """El filtro de estado del listado: uno, varios (tupla) o ninguno."""
    if estado is None:
        return None
    return _estados_esperados(estado)


def _referencia(valor: Any) -> str | None:
    if valor is None:
        return None
    if not isinstance(valor, str) or not 1 <= len(valor) <= LARGO_REFERENCIA or not valor.isprintable():
        raise ErrorAlmacen("referencia inválida")
    return valor


def _cursor(antes_de: Any) -> tuple[datetime, str] | None:
    """(creada, id) de la última compra vista: el listado sigue con las
    anteriores (orden `creada desc, id desc`)."""
    if antes_de is None:
        return None
    if not isinstance(antes_de, (tuple, list)) or len(antes_de) != 2:
        raise ErrorAlmacen("cursor inválido")
    return _fecha(antes_de[0]), _id_compra(antes_de[1])


def _ids_compras(compra_ids: Any) -> list[str]:
    """Los ids, validados antes de mandar nada y sin repetir (en orden)."""
    if isinstance(compra_ids, (str, bytes)) or not hasattr(compra_ids, "__iter__"):
        raise ErrorAlmacen("compra_ids inválidos")
    return list(dict.fromkeys(_id_compra(c) for c in compra_ids))


def _tipo_evento(tipo: Any) -> str:
    if not es_id(TIPO_EVENTO_RE, tipo):
        raise ErrorAlmacen("tipo de evento inválido")
    return tipo


def _antes_que(fila: dict[str, Any], cursor: tuple[datetime, str]) -> bool:
    creada = _fecha(fila["creada"])
    return creada < cursor[0] or (creada == cursor[0] and fila["id"] < cursor[1])


def _iso(fecha: datetime) -> str:
    """ISO 8601 en UTC con `Z`: un `+00:00` en la query string se lee como
    espacio si alguien no lo codifica."""
    if fecha.tzinfo is None:
        fecha = fecha.replace(tzinfo=timezone.utc)
    return fecha.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _fecha(valor: Any) -> datetime:
    if isinstance(valor, datetime):
        fecha = valor
    elif isinstance(valor, str):
        try:
            fecha = datetime.fromisoformat(valor)
        except ValueError:
            raise ErrorAlmacen("fecha inválida") from None
    else:
        raise ErrorAlmacen("fecha inválida")
    return fecha if fecha.tzinfo is not None else fecha.replace(tzinfo=timezone.utc)


def _validar_fila_compra(fila: Any) -> dict[str, Any]:
    """`espera_id` (004) es opcional: si la fila no lo trae, el upsert deja el
    que estaba (como PostgREST, que solo escribe las columnas del cuerpo)."""
    if not isinstance(fila, dict) or not isinstance(fila.get("datos"), dict):
        raise ErrorAlmacen("fila de compra inválida")
    _id_compra(fila.get("id"))
    _id_slug(fila.get("integrador_id"), "integrador_id")
    _id_slug(fila.get("comercio_id"), "comercio_id")
    _estado_compra(fila.get("estado"))
    _fecha(fila.get("creada"))
    _fecha(fila.get("actualizada"))
    if fila.get("espera_id") is not None:
        _id_espera(fila["espera_id"])
    return fila


def _id_espera(valor: Any) -> str:
    return _id(ID_ESPERA_RE, valor, "espera_id")


def _tipo_espera(tipo: Any) -> str:
    if tipo not in TIPOS_ESPERA:
        raise ErrorAlmacen("tipo de espera desconocido")
    return tipo


def _entero(valor: Any, nombre: str, *, minimo: int = 0) -> int:
    if not isinstance(valor, int) or isinstance(valor, bool) or valor < minimo:
        raise ErrorAlmacen(f"{nombre} inválido")
    return valor


def _estados_esperados(estado_esperado: Any) -> tuple[str, ...]:
    """Uno o varios estados de compra (validados), en orden fijo."""
    if isinstance(estado_esperado, str):
        return (_estado_compra(estado_esperado),)
    if isinstance(estado_esperado, (tuple, list, set, frozenset)) and estado_esperado:
        return tuple(sorted({_estado_compra(e) for e in estado_esperado}))
    raise ErrorAlmacen("estado esperado inválido")


def _espera_esperada(valor: Any) -> str | None | _Cualquiera:
    if valor is CUALQUIERA or valor is None:
        return valor
    return _id_espera(valor)


def _respuesta_json(respuesta: Any) -> Any:
    """La respuesta de una espera como la devolvería la base (jsonb): JSON
    válido, sin NaN y acotada. Se devuelve normalizada (tuplas → listas)."""
    try:
        texto = json.dumps(respuesta, allow_nan=False, ensure_ascii=False)
    except (TypeError, ValueError):
        raise ErrorAlmacen("respuesta de la espera no es JSON") from None
    if len(texto.encode("utf-8")) > MAX_BYTES_RESPUESTA:
        raise ErrorAlmacen("respuesta de la espera demasiado grande")
    return json.loads(texto)


def _validar_fila_espera(fila: Any) -> dict[str, Any]:
    """Una espera nueva: abierta, sin respuesta, y `oportunidad_id` solo (y
    siempre) en las de decisión, como los `check` de `gw_esperas`. Devuelve la
    fila con todas las columnas (los dos almacenes insertan lo mismo)."""
    if not isinstance(fila, dict):
        raise ErrorAlmacen("fila de espera inválida")
    _id_espera(fila.get("id"))
    _id_compra(fila.get("compra_id"))
    tipo = _tipo_espera(fila.get("tipo"))
    if fila.get("estado") != "abierta":
        raise ErrorAlmacen("una espera nueva está abierta")
    oportunidad_id = fila.get("oportunidad_id")
    if tipo == "decision":
        _id_seguro(oportunidad_id, "oportunidad_id")
    elif oportunidad_id is not None:
        raise ErrorAlmacen("solo una espera de decisión tiene oportunidad")
    _fecha(fila.get("vence"))
    _fecha(fila.get("creada"))
    if fila.get("respuesta") is not None or fila.get("resuelta") is not None:
        raise ErrorAlmacen("una espera nueva no tiene respuesta")
    return {columna: copy.deepcopy(fila.get(columna)) for columna in COLUMNAS_ESPERA}


def _validar_fila_aprobacion(fila: Any) -> dict[str, Any]:
    """Lo que la base exige de `gw_aprobaciones` (ids, estado, total firmado),
    antes de mandar nada."""
    if not isinstance(fila, dict):
        raise ErrorAlmacen("fila de aprobación inválida")
    _id_seguro(fila.get("id"), "aprobacion_id")
    _id_seguro(fila.get("contexto_id"), "contexto_id")
    _id_seguro(fila.get("cart_id"), "cart_id")
    if fila.get("estado") not in ("pendiente", "usada"):
        raise ErrorAlmacen("estado de aprobación desconocido")
    _entero(fila.get("centavos"), "centavos")
    _id_seguro(fila.get("moneda"), "moneda")
    return fila


def _misma_clave_aprobacion(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """La clave del índice único parcial de pendientes (004)."""
    return all(a.get(c) == b.get(c) for c in ("contexto_id", "cart_id", "centavos", "moneda"))


def _validar_fila_evento(fila: Any) -> dict[str, Any]:
    if not isinstance(fila, dict):
        raise ErrorAlmacen("fila de evento inválida")
    _id_seguro(fila.get("id"), "evento_id")
    _fecha(fila.get("ts"))
    _id_slug(fila.get("integrador_id"), "integrador_id")
    _id_compra(fila.get("compra_id"))
    if fila.get("contexto_id") is not None:
        _id_seguro(fila["contexto_id"], "contexto_id")
    if fila.get("escenario") is not None and not isinstance(fila["escenario"], str):
        raise ErrorAlmacen("escenario inválido")
    if not isinstance(fila.get("guard_activo"), bool):
        raise ErrorAlmacen("guard_activo inválido")
    if not es_id(TIPO_EVENTO_RE, fila.get("tipo")):
        raise ErrorAlmacen("tipo de evento inválido")
    if not isinstance(fila.get("datos"), dict):
        raise ErrorAlmacen("datos del evento inválidos")
    # `orden` lo pone la base (identity): no se manda.
    return {columna: copy.deepcopy(fila.get(columna)) for columna in COLUMNAS_EVENTO}


def _proteger_compra(vieja: dict[str, Any], nueva: dict[str, Any]) -> dict[str, Any] | None:
    """Lo que hace el trigger `gw_compras_proteger` (004): la fila que queda,
    o None si la base descarta la escritura sin error.

    El paso a un estado terminal entra siempre (aunque llegue con una
    `actualizada` menor: una copia vieja que llegó antes no puede dejar
    seguir una conversación cortada), con la `actualizada` mayor de las dos.
    Si no, una escritura con `actualizada` menor que la guardada llega tarde,
    y de `bloqueada` o `vencida` no se sale."""
    if nueva["estado"] in ESTADOS_TERMINALES and vieja["estado"] not in ESTADOS_TERMINALES:
        if _fecha(nueva["actualizada"]) < _fecha(vieja["actualizada"]):
            nueva = {**nueva, "actualizada": vieja["actualizada"]}
        return nueva
    if _fecha(nueva["actualizada"]) < _fecha(vieja["actualizada"]):
        return None
    if vieja["estado"] in ESTADOS_TERMINALES and nueva["estado"] != vieja["estado"]:
        return None
    return nueva


def _proyectar(fila: dict[str, Any], columnas: tuple[str, ...]) -> dict[str, Any]:
    return {c: copy.deepcopy(fila.get(c)) for c in columnas}


ESTADOS_ENTREGA_SQL = frozenset({"pendiente", "entregada", "fallida", "descartada"})


def _fila_cola(
    entrega: EntregaWebhook, cuerpo: Any, proximo_intento: datetime
) -> dict[str, Any]:
    """La fila de `gw_entregas_webhook` al encolar (004): el registro de la
    entrega, el cuerpo exacto (texto UTF-8: los mismos bytes en cada intento,
    en cualquier instancia) y cuándo intentarla, sin tomar."""
    _id_seguro(entrega.id, "entrega_id")
    _id_slug(entrega.integrador_id, "integrador_id")
    if entrega.estado != "pendiente":
        raise ErrorAlmacen("solo se encola una entrega pendiente")
    if not isinstance(cuerpo, (bytes, bytearray)) or len(cuerpo) > MAX_BYTES_CUERPO_WEBHOOK:
        raise ErrorAlmacen("cuerpo de webhook inválido")
    try:
        texto = bytes(cuerpo).decode("utf-8")
    except UnicodeDecodeError:
        raise ErrorAlmacen("cuerpo de webhook inválido") from None
    return {
        **entrega.a_json(),
        "cuerpo": texto,
        "proximo_intento": _iso(_fecha(proximo_intento)),
        "tomada_hasta": None,
    }


def _ventana_toma(ahora: Any, hasta: Any) -> tuple[datetime, datetime]:
    ahora, hasta = _fecha(ahora), _fecha(hasta)
    if hasta <= ahora:
        raise ErrorAlmacen("la toma tiene que vencer después de ahora")
    return ahora, hasta


def _cambios_marca(entrega: EntregaWebhook, proximo_intento: datetime | None) -> dict[str, Any]:
    """Lo que escribe `marcar_entrega`: el estado del intento y la entrega
    suelta. Una que sigue pendiente necesita su próximo intento (si no, nadie
    la volvería a tomar); una terminada no tiene."""
    _id_seguro(entrega.id, "entrega_id")
    if entrega.estado not in ESTADOS_ENTREGA_SQL:
        raise ErrorAlmacen("estado de entrega desconocido")
    if entrega.estado == "pendiente":
        if proximo_intento is None:
            raise ErrorAlmacen("una entrega pendiente necesita su próximo intento")
        proximo: str | None = _iso(_fecha(proximo_intento))
    else:
        proximo = None
    datos = entrega.a_json()
    return {
        **{c: datos[c] for c in ("estado", "intentos", "status_http", "error",
                                 "destino_host", "actualizada")},
        "proximo_intento": proximo,
        "tomada_hasta": None,
    }


# -- filas -------------------------------------------------------------------


def fila_ofertas(
    contexto_id: str,
    comercio_id: str | None,
    carts: list[dict],
    offer_details: list[dict],
    escenario: str | None,
    registradas: datetime,
) -> dict[str, Any]:
    """La fila de `gw_ofertas`: lo que vio el proxy en `find_products`, para
    evaluar el `create_checkout` aunque haya un reinicio en el medio."""
    return {
        "contexto_id": contexto_id,
        "comercio_id": comercio_id,
        "carts": copy.deepcopy(carts),
        "offer_details": copy.deepcopy(offer_details),
        "escenario": escenario,
        "registradas": registradas.isoformat(),
    }


def fila_ultimo_veredicto(
    contexto_id: str,
    cart_id: str,
    status: str,
    centavos: int | None,
    moneda: str | None,
    ts: datetime,
) -> dict[str, Any]:
    """La fila de `gw_ultimos_veredictos`: lo que necesita
    `RegistroAprobaciones.aprobar` (status y total firmado) después de un
    reinicio."""
    return {
        "contexto_id": contexto_id,
        "cart_id": cart_id,
        "status": status,
        "centavos": centavos,
        "moneda": moneda,
        "ts": ts.isoformat(),
    }


def fila_aprobacion_gw(
    aprobacion: Aprobacion, *, integrador_id: str | None, compra_id: str | None
) -> dict[str, Any]:
    """La fila de `gw_aprobaciones`: la de auditoría de `persistencia` más el
    dueño (integrador y compra)."""
    return {
        **fila_aprobacion(aprobacion),
        "integrador_id": integrador_id,
        "compra_id": compra_id,
    }


def nuevo_id_espera() -> str:
    return f"esp-{secrets.token_hex(8)}"


def fila_espera(
    compra_id: str,
    tipo: str,
    vence: datetime,
    *,
    creada: datetime,
    oportunidad_id: str | None = None,
    espera_id: str | None = None,
) -> dict[str, Any]:
    """La fila de `gw_esperas` (004) para abrir una espera: `abierta`, con su
    vencimiento y, si es de decisión, la oportunidad que espera. El id es
    `esp-<16 hex>` (nuevo si no se pasa)."""
    return {
        "id": espera_id or nuevo_id_espera(),
        "compra_id": compra_id,
        "tipo": tipo,
        "estado": "abierta",
        "oportunidad_id": oportunidad_id,
        "vence": _fecha(vence).isoformat(),
        "respuesta": None,
        "creada": _fecha(creada).isoformat(),
        "resuelta": None,
    }


def fila_evento(evento: Evento, *, integrador_id: str, compra_id: str) -> dict[str, Any]:
    """La fila de `gw_eventos` (004): el `Evento` tal cual (en JSON) más el
    dueño. `orden` lo pone la base."""
    datos = evento.model_dump(mode="json")
    return {
        "id": datos["id"],
        "ts": datos["ts"],
        "integrador_id": integrador_id,
        "compra_id": compra_id,
        "contexto_id": datos["contexto_id"],
        "escenario": datos["escenario"],
        "guard_activo": datos["guard_activo"],
        "tipo": datos["tipo"],
        "datos": datos["datos"],
    }


def evento_desde_fila(fila: dict[str, Any]) -> Evento:
    """El `Evento` de una fila de `gw_eventos`. Una fila que no valida da
    `ErrorAlmacen` (sin su contenido en el mensaje)."""
    if not isinstance(fila, dict):
        raise ErrorAlmacen("fila de evento inválida")
    try:
        return Evento.model_validate(
            {c: fila.get(c) for c in ("id", "ts", "contexto_id", "escenario", "guard_activo",
                                     "tipo", "datos")}
        )
    except ValidationError as e:
        raise ErrorAlmacen(f"fila de evento inválida ({e.error_count()} errores)") from None


# -- tareas en segundo plano --------------------------------------------------


@dataclass
class _Ambito:
    """Las escrituras decisivas de un pedido (ver `TareasEnFondo.ambito`):
    las que fallaron y todavía no se informaron, y las que siguen en vuelo."""

    fallas: list[str] = field(default_factory=list)
    en_vuelo: dict[asyncio.Task[None], str] = field(default_factory=dict)


class TareasEnFondo:
    """Escrituras que no demoran la respuesta (actualizar la compra, las
    ofertas, el último veredicto y las entregas).

    Tope de tareas en vuelo: si el almacén se cuelga, no crece sin límite;
    lo que no entra se descarta y se avisa. `programar` nunca lanza: una
    falla se loguea y va a `al_fallar` (el proxy la convierte en un evento
    `error`).

    Con varias instancias (sin `waitUntil`, lo que queda en vuelo al
    responder se puede perder) el núcleo llama a `barrera` antes de
    responder, y marca `decisiva=True` las escrituras sin las que la
    respuesta no vale (por ejemplo, las ofertas de un `find_products`): si
    una falla, la barrera lanza `EscrituraDecisivaFallida` y el pedido
    responde 503 `estado_no_disponible`. Las decisivas se agrupan por pedido
    (`ambito`): la falla de un pedido no la recibe otro."""

    def __init__(
        self,
        *,
        max_pendientes: int = MAX_PENDIENTES_FONDO,
        al_fallar: Callable[[str], None] | None = None,
    ) -> None:
        self._max = max_pendientes
        self.al_fallar = al_fallar
        self._tareas: set[asyncio.Task[None]] = set()
        self.errores = 0
        self.descartadas = 0
        self.fallas_decisivas = 0
        # Una variable por instancia: dos proxies en el mismo proceso (los
        # tests) no se mezclan las decisivas.
        self._ambito: contextvars.ContextVar[_Ambito | None] = contextvars.ContextVar(
            f"tareas_en_fondo_{id(self)}", default=None
        )

    def _avisar(self, mensaje: str) -> None:
        logger.warning("%s", mensaje)
        if self.al_fallar is not None:
            try:
                self.al_fallar(mensaje)
            except Exception:  # noqa: BLE001 — el aviso nunca rompe al que escribe
                logger.exception("al_fallar lanzó")

    @contextlib.contextmanager
    def ambito(self) -> Iterator[None]:
        """Un pedido: las decisivas programadas adentro (en esta tarea o en
        las que se creen desde acá) las informa la `barrera` de adentro.

        Sin `ambito` explícito, la primera decisiva de una tarea abre uno
        para esa tarea (cada pedido HTTP corre en su propia tarea)."""
        token = self._ambito.set(_Ambito())
        try:
            yield
        finally:
            self._ambito.reset(token)

    def _ambito_actual(self) -> _Ambito:
        ambito = self._ambito.get()
        if ambito is None:
            ambito = _Ambito()
            self._ambito.set(ambito)
        return ambito

    def programar(
        self,
        coro: Awaitable[None],
        descripcion: str,
        *,
        obligatoria: bool = False,
        decisiva: bool = False,
    ) -> None:
        """`obligatoria`: no se descarta por el tope de tareas en vuelo (el
        paso de una compra a un estado terminal: si se perdiera, la compra
        cortada por seguridad reviviría después de un reinicio).

        `decisiva`: además de no descartarse por el tope, si falla (o no se
        puede programar) queda registrada en el ámbito del pedido y la
        informa `barrera`. Sin `decisiva`, todo como siempre."""
        ambito = self._ambito_actual() if decisiva else None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _cerrar_coro(coro)
            self.descartadas += 1
            if ambito is not None:
                self._registrar_falla(ambito, f"{descripcion} (sin loop)")
            self._avisar(f"Estado: no se guardó {descripcion} (sin loop)")
            return
        if len(self._tareas) >= self._max and not (obligatoria or decisiva):
            _cerrar_coro(coro)
            self.descartadas += 1
            self._avisar(f"Estado: no se guardó {descripcion} (demasiadas escrituras en vuelo)")
            return
        tarea = loop.create_task(self._correr(coro, descripcion, ambito))
        self._tareas.add(tarea)
        tarea.add_done_callback(self._tareas.discard)
        if ambito is not None:
            ambito.en_vuelo[tarea] = descripcion

    def _registrar_falla(self, ambito: _Ambito, descripcion: str) -> None:
        ambito.fallas.append(descripcion)
        self.fallas_decisivas += 1

    def _falla_de_tarea(self, ambito: _Ambito | None, descripcion: str) -> None:
        """Una decisiva que falló, si todavía nadie la informó como "sin
        confirmar" (una barrera que venció antes)."""
        if ambito is not None and asyncio.current_task() in ambito.en_vuelo:
            self._registrar_falla(ambito, descripcion)

    async def _correr(
        self, coro: Awaitable[None], descripcion: str, ambito: _Ambito | None = None
    ) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            self._falla_de_tarea(ambito, f"{descripcion} (cancelada)")
            raise
        except ErrorAlmacen as e:
            self.errores += 1
            self._falla_de_tarea(ambito, descripcion)
            self._avisar(f"Estado: no se guardó {descripcion} ({e})")
        except Exception as e:  # noqa: BLE001 — nunca lanza
            self.errores += 1
            self._falla_de_tarea(ambito, descripcion)
            logger.exception("Estado: error inesperado guardando %s", descripcion)
            self._avisar(f"Estado: no se guardó {descripcion} ({type(e).__name__})")
        finally:
            if ambito is not None:
                ambito.en_vuelo.pop(asyncio.current_task(), None)  # type: ignore[arg-type]

    @property
    def pendientes(self) -> int:
        return len(self._tareas)

    async def barrera(self, timeout_s: float) -> bool:
        """Espera hasta `timeout_s` las escrituras que estaban en vuelo al
        llamarla (de cualquier pedido: sin `waitUntil`, lo que queda en vuelo
        al responder se puede perder). True si terminaron todas; no cancela
        las que no.

        Después, si en el ámbito de este pedido hay decisivas que fallaron,
        que no se pudieron programar o que siguen sin terminar, lanza
        `EscrituraDecisivaFallida` (un `ErrorAlmacen`) con sus descripciones,
        y las olvida: otra barrera del mismo ámbito no las repite."""
        pendientes = set(self._tareas)
        sin_terminar: set[asyncio.Task[None]] = set()
        if pendientes:
            _, sin_terminar = await asyncio.wait(pendientes, timeout=max(0.0, timeout_s))
            # Ceder una vuelta: los done callbacks (`discard`) corren antes de volver.
            await asyncio.sleep(0)
        ambito = self._ambito.get()
        if ambito is not None:
            fallas = list(ambito.fallas)
            ambito.fallas.clear()
            for tarea, descripcion in list(ambito.en_vuelo.items()):
                if not tarea.done():
                    fallas.append(f"{descripcion} (sin confirmar)")
                    del ambito.en_vuelo[tarea]
                    self.fallas_decisivas += 1
            if fallas:
                logger.error("Estado: escrituras decisivas sin confirmar: %s", ", ".join(fallas))
                raise EscrituraDecisivaFallida(tuple(fallas))
        return not sin_terminar

    async def esperar(self) -> None:
        """Espera las escrituras en vuelo (tests y cierre)."""
        while self._tareas:
            await asyncio.gather(*list(self._tareas), return_exceptions=True)
            # Ceder una vuelta al loop: si todas ya terminaron, gather (3.12)
            # resuelve sin pasar por el loop y el `discard` del done callback
            # no llega a correr (el while no terminaba nunca).
            await asyncio.sleep(0)

    async def cerrar(self, timeout_s: float = 5.0) -> None:
        """Espera hasta `timeout_s` y cancela lo que quede."""
        if not self._tareas:
            return
        _, pendientes = await asyncio.wait(list(self._tareas), timeout=timeout_s)
        for tarea in pendientes:
            tarea.cancel()
        if pendientes:
            await asyncio.gather(*pendientes, return_exceptions=True)
            self._avisar(f"Estado: {len(pendientes)} escrituras canceladas al cerrar")


def _cerrar_coro(coro: Awaitable[None]) -> None:
    """Una corrutina que no se va a correr se cierra (sin el warning de
    "never awaited")."""
    cerrar = getattr(coro, "close", None)
    if callable(cerrar):
        cerrar()


# -- almacén en memoria ---------------------------------------------------------


class EstadoMemoria:
    """Almacén en memoria con la misma semántica que el de Supabase.

    Guarda y devuelve copias (`copy.deepcopy`): nadie muta el estado por
    referencia, igual que si viniera de la base. Valida los ids igual que
    `EstadoSupabase` y respeta las claves foráneas (`gw_contextos` y
    `gw_esperas` → `gw_compras`), los índices únicos (una espera abierta por
    compra, una aprobación pendiente por contexto, cart y total) y el trigger
    `gw_compras_proteger` de 004, para que un test en memoria vea los mismos
    errores que producción. Cada operación corre sin ceder el loop: es
    atómica, como una fila en la base.

    Con una sola instancia (la demo local) nada de esto cambia lo de hoy: las
    escrituras de una compra van en orden y con `actualizada` creciente.
    """

    nombre = "memoria"

    def __init__(self, *, durable: bool = False, max_compras: int = 10_000) -> None:
        self.durable = durable
        self._max_compras = max_compras
        self._compras: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._contextos: dict[str, dict[str, Any]] = {}
        self._ofertas: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._veredictos: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        self._aprobaciones: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._entregas: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._esperas: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._eventos: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._orden_eventos = 0  # como `generated always as identity`

    def _recortar(self, tabla: OrderedDict[Any, Any], maximo: int) -> None:
        while len(tabla) > maximo:
            tabla.popitem(last=False)

    # -- compras ----------------------------------------------------------------

    async def guardar_compra(self, fila: dict[str, Any]) -> None:
        """Upsert por id. Si ya está, solo cambian las columnas de la fila
        (como PostgREST) y pasa por el trigger de 004: una escritura vieja o
        que saca a la compra de un estado terminal se descarta sin error."""
        fila = _validar_fila_compra(fila)
        actual = self._compras.get(fila["id"])
        if actual is not None:
            nueva = _proteger_compra(actual, {**actual, **copy.deepcopy(fila)})
            if nueva is not None:
                self._compras[fila["id"]] = nueva
            else:
                logger.debug("Estado: escritura descartada de la compra %s", fila["id"])
            return
        self._compras[fila["id"]] = copy.deepcopy(fila)
        while len(self._compras) > self._max_compras:
            vieja, _ = self._compras.popitem(last=False)
            # Como `on delete cascade`: las ataduras y esperas de una compra borrada se van.
            for ctx in [c for c, f in self._contextos.items() if f["compra_id"] == vieja]:
                del self._contextos[ctx]
            for esp in [e for e, f in self._esperas.items() if f["compra_id"] == vieja]:
                del self._esperas[esp]

    async def guardar_compra_condicionada(
        self,
        fila: dict[str, Any],
        *,
        estado_esperado: str | tuple[str, ...] | frozenset[str],
        espera_id_esperado: str | None | _Cualquiera,
        actualizada_esperada: datetime | str | None = None,
    ) -> bool:
        """Escribe la compra solo si la guardada sigue como se la leyó: en
        `estado_esperado` (uno o varios), con `espera_id_esperado` (None = sin
        espera; `CUALQUIERA` = sin condición) y, si se pasa, con esa
        `actualizada`. True si escribió; False si no coincide, si la compra no
        está o si el trigger la descartó. Es la transición condicional (por
        ejemplo, normalizar una espera huérfana sin pisar a quien la cerró)."""
        fila = _validar_fila_compra(fila)
        estados = _estados_esperados(estado_esperado)
        espera = _espera_esperada(espera_id_esperado)
        actualizada = _fecha(actualizada_esperada) if actualizada_esperada is not None else None
        actual = self._compras.get(fila["id"])
        if actual is None or actual["estado"] not in estados:
            return False
        if espera is not CUALQUIERA and actual.get("espera_id") != espera:
            return False
        if actualizada is not None and _fecha(actual["actualizada"]) != actualizada:
            return False
        nueva = _proteger_compra(actual, {**actual, **copy.deepcopy(fila)})
        if nueva is None:
            return False
        self._compras[fila["id"]] = nueva
        return True

    async def cargar_compra(self, compra_id: str) -> dict[str, Any] | None:
        fila = self._compras.get(_id_compra(compra_id))
        return copy.deepcopy(fila) if fila is not None else None

    async def listar_compras(
        self,
        integrador_id: str,
        *,
        limite: int = 50,
        estado: str | tuple[str, ...] | None = None,
        desde: datetime | None = None,
        hasta: datetime | None = None,
        referencia: str | None = None,
        antes_de: tuple[datetime | str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Las compras del integrador, la más nueva primero. Filtros: estado
        (uno o varios), `creada` en [`desde`, `hasta`), la `referencia`
        exacta y, para seguir un listado, `antes_de` = (creada, id) de la
        última que se vio."""
        _id_slug(integrador_id, "integrador_id")
        limite = _limite(limite)
        estados = _estados_filtro(estado)
        desde = _fecha(desde) if desde is not None else None
        hasta = _fecha(hasta) if hasta is not None else None
        referencia = _referencia(referencia)
        cursor = _cursor(antes_de)
        filas = [
            f
            for f in self._compras.values()
            if f["integrador_id"] == integrador_id
            and (estados is None or f["estado"] in estados)
            and (desde is None or _fecha(f["creada"]) >= desde)
            and (hasta is None or _fecha(f["creada"]) < hasta)
            and (referencia is None or f.get("referencia") == referencia)
            and (cursor is None or _antes_que(f, cursor))
        ]
        # La más nueva primero; a igual `creada` (filas de otro proceso), por id.
        filas.sort(key=lambda f: (_fecha(f["creada"]), f["id"]), reverse=True)
        return copy.deepcopy(filas[:limite])

    async def contar_activas(self, integrador_id: str, desde: datetime) -> int:
        """Compras creadas desde `desde` (el TTL) que todavía no resolvieron
        la negociación (`ESTADOS_ACTIVOS`; diseño §1.3)."""
        _id_slug(integrador_id, "integrador_id")
        desde = _fecha(desde)
        return sum(
            1
            for f in self._compras.values()
            if f["integrador_id"] == integrador_id
            and _fecha(f["creada"]) >= desde
            and f["estado"] in ESTADOS_ACTIVOS
        )

    # -- contextos ----------------------------------------------------------------

    async def atar_contexto(self, contexto_id: str, compra_id: str, comercio_id: str) -> str:
        """Ata el contexto si nadie lo tiene; devuelve el dueño (gana el primero)."""
        _id_seguro(contexto_id, "contexto_id")
        _id_compra(compra_id)
        _id_slug(comercio_id, "comercio_id")
        actual = self._contextos.get(contexto_id)
        if actual is not None:
            return actual["compra_id"]
        if compra_id not in self._compras:
            # La clave foránea de gw_contextos: la compra tiene que estar guardada.
            raise ErrorAlmacen("la compra no está en el almacén")
        self._contextos[contexto_id] = {
            "contexto_id": contexto_id,
            "compra_id": compra_id,
            "comercio_id": comercio_id,
            "creado": _iso(datetime.now(timezone.utc)),
        }
        return compra_id

    async def compra_de_contexto(self, contexto_id: str) -> str | None:
        fila = self._contextos.get(_id_seguro(contexto_id, "contexto_id"))
        return fila["compra_id"] if fila is not None else None

    async def contextos_de_compra(self, compra_id: str) -> list[str]:
        """Los `contextId` atados a la compra, en orden de atadura."""
        _id_compra(compra_id)
        return [c for c, f in self._contextos.items() if f["compra_id"] == compra_id][
            :LIMITE_LISTADO_MAXIMO
        ]

    async def contextos_de_compras(self, compra_ids: Any) -> dict[str, list[str]]:
        """Los contextos de varias compras de una vez (el listado): por compra,
        en orden de atadura, como `contextos_de_compra`."""
        ids = _ids_compras(compra_ids)
        return {
            i: [c for c, f in self._contextos.items() if f["compra_id"] == i][:LIMITE_LISTADO_MAXIMO]
            for i in ids
        }

    async def purgar(self, antes_de: datetime) -> dict[str, int]:
        """Borra lo que es más viejo que `antes_de` (la retención,
        `GATEWAY_RETENCION_DIAS`): compras, contextos, ofertas, últimos
        veredictos, aprobaciones, entregas de webhooks, esperas y eventos.
        Devuelve cuántas filas borró de cada tabla (sin contar lo que se va
        en cascada con una compra)."""
        limite = _fecha(antes_de)

        def viejas(tabla: OrderedDict[Any, Any] | dict[Any, Any], columna: str) -> list[Any]:
            return [k for k, f in tabla.items() if f.get(columna) and _fecha(f[columna]) < limite]

        tablas = {
            "gw_eventos": (self._eventos, "ts"),
            "gw_ofertas": (self._ofertas, "registradas"),
            "gw_ultimos_veredictos": (self._veredictos, "ts"),
            "gw_aprobaciones": (self._aprobaciones, "creada"),
            "gw_entregas_webhook": (self._entregas, "creada"),
            "gw_esperas": (self._esperas, "creada"),
            "gw_contextos": (self._contextos, "creado"),
            "gw_compras": (self._compras, "creada"),
        }
        borradas: dict[str, int] = {}
        for nombre in TABLAS_PURGA:
            tabla, columna = tablas[nombre]
            claves = viejas(tabla, columna)
            for clave in claves:
                del tabla[clave]
            borradas[nombre] = len(claves)
        # Como `on delete cascade`: lo que apuntaba a una compra borrada.
        for tabla in (self._contextos, self._esperas):
            for clave in [k for k, f in tabla.items() if f["compra_id"] not in self._compras]:
                del tabla[clave]
        return borradas

    # -- ofertas y último veredicto ---------------------------------------------

    async def guardar_ofertas(self, fila: dict[str, Any]) -> None:
        contexto_id = _id_seguro(fila.get("contexto_id"), "contexto_id")
        self._ofertas[contexto_id] = copy.deepcopy(fila)
        self._ofertas.move_to_end(contexto_id)
        self._recortar(self._ofertas, self._max_compras * 4)

    async def cargar_ofertas(self, contexto_id: str) -> dict[str, Any] | None:
        fila = self._ofertas.get(_id_seguro(contexto_id, "contexto_id"))
        return copy.deepcopy(fila) if fila is not None else None

    async def guardar_ultimo_veredicto(self, fila: dict[str, Any]) -> None:
        clave = (
            _id_seguro(fila.get("contexto_id"), "contexto_id"),
            _id_seguro(fila.get("cart_id"), "cart_id"),
        )
        if fila.get("status") not in STATUS_VEREDICTO_SQL:
            raise ErrorAlmacen("status de veredicto desconocido")
        self._veredictos[clave] = copy.deepcopy(fila)
        self._veredictos.move_to_end(clave)
        self._recortar(self._veredictos, self._max_compras * 4)

    async def cargar_ultimo_veredicto(
        self, contexto_id: str, cart_id: str
    ) -> dict[str, Any] | None:
        clave = (_id_seguro(contexto_id, "contexto_id"), _id_seguro(cart_id, "cart_id"))
        fila = self._veredictos.get(clave)
        return copy.deepcopy(fila) if fila is not None else None

    async def ultimos_veredictos_de_contexto(self, contexto_id: str) -> list[dict[str, Any]]:
        """El último veredicto de cada cart de ese contexto, el más nuevo primero."""
        _id_seguro(contexto_id, "contexto_id")
        filas = [f for (ctx, _), f in self._veredictos.items() if ctx == contexto_id]
        filas.sort(key=lambda f: f["cart_id"])
        filas.sort(key=lambda f: _fecha(f["ts"]), reverse=True)
        return copy.deepcopy(filas[:LIMITE_LISTADO_MAXIMO])

    # -- aprobaciones -------------------------------------------------------------

    def _pendiente_igual(self, fila: dict[str, Any]) -> dict[str, Any] | None:
        for otra in self._aprobaciones.values():
            if otra["estado"] == "pendiente" and _misma_clave_aprobacion(otra, fila):
                return otra
        return None

    async def guardar_aprobacion(self, fila: dict[str, Any]) -> None:
        """Upsert por id. Como el índice único parcial de 004: otra pendiente
        con el mismo contexto, cart y total es un error (409 en la base)."""
        fila = _validar_fila_aprobacion(fila)
        if fila["estado"] == "pendiente":
            otra = self._pendiente_igual(fila)
            if otra is not None and otra["id"] != fila["id"]:
                raise ErrorAlmacen("ya hay una aprobación pendiente para ese contexto, cart y total")
        self._aprobaciones[fila["id"]] = copy.deepcopy(fila)
        self._recortar(self._aprobaciones, self._max_compras * 4)

    async def crear_aprobacion_pendiente(
        self, fila: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        """(fila guardada, creada). Si ya hay una pendiente con el mismo
        contexto, cart, centavos y moneda (la creó otra instancia), devuelve
        esa con `creada=False`: el segundo POST da la misma aprobación."""
        fila = _validar_fila_aprobacion(fila)
        if fila["estado"] != "pendiente":
            raise ErrorAlmacen("una aprobación nueva está pendiente")
        existente = self._pendiente_igual(fila)
        if existente is not None:
            return copy.deepcopy(existente), False
        if fila["id"] in self._aprobaciones:
            raise ErrorAlmacen("aprobacion_id repetido")
        self._aprobaciones[fila["id"]] = copy.deepcopy(fila)
        self._recortar(self._aprobaciones, self._max_compras * 4)
        return copy.deepcopy(fila), True

    async def cargar_aprobacion_pendiente(
        self,
        contexto_id: str,
        cart_id: str,
        *,
        centavos: int | None = None,
        moneda: str | None = None,
    ) -> dict[str, Any] | None:
        """La pendiente más nueva de ese contexto y cart (y, si se pasan, de
        ese total firmado)."""
        _id_seguro(contexto_id, "contexto_id")
        _id_seguro(cart_id, "cart_id")
        if centavos is not None:
            _entero(centavos, "centavos")
        if moneda is not None:
            _id_seguro(moneda, "moneda")
        pendientes = [
            f
            for f in self._aprobaciones.values()
            if f["contexto_id"] == contexto_id
            and f["cart_id"] == cart_id
            and f["estado"] == "pendiente"
            and (centavos is None or f.get("centavos") == centavos)
            and (moneda is None or f.get("moneda") == moneda)
        ]
        if not pendientes:
            return None
        return copy.deepcopy(max(pendientes, key=lambda f: _fecha(f["creada"])))

    async def reclamar_aprobacion(
        self, aprobacion_id: str, *, usada: str, task_id: str | None
    ) -> bool:
        """`pendiente` → `usada` una sola vez: True solo para quien la cambió."""
        fila = self._aprobaciones.get(_id_seguro(aprobacion_id, "aprobacion_id"))
        if task_id is not None:
            _id_seguro(task_id, "task_id")
        if fila is None or fila["estado"] != "pendiente":
            return False
        fila.update({"estado": "usada", "usada": usada, "task_id": task_id})
        return True

    async def aprobaciones_de_compra(self, compra_id: str) -> list[dict[str, Any]]:
        """Las aprobaciones (pendientes y usadas) de la compra, la más vieja primero."""
        _id_compra(compra_id)
        filas = [f for f in self._aprobaciones.values() if f.get("compra_id") == compra_id]
        filas.sort(key=lambda f: (_fecha(f["creada"]), f["id"]))
        return copy.deepcopy(filas[:LIMITE_LISTADO_MAXIMO])

    async def aprobaciones_de_compras(self, compra_ids: Any) -> dict[str, list[dict[str, Any]]]:
        """Las aprobaciones de varias compras de una vez (el listado), como
        `aprobaciones_de_compra` para cada una."""
        ids = _ids_compras(compra_ids)
        return {i: await self.aprobaciones_de_compra(i) for i in ids}

    # -- esperas (004) ------------------------------------------------------------

    def _abierta(self, compra_id: str) -> dict[str, Any] | None:
        for fila in self._esperas.values():
            if fila["compra_id"] == compra_id and fila["estado"] == "abierta":
                return fila
        return None

    async def abrir_espera(self, fila: dict[str, Any]) -> dict[str, Any] | None:
        """Inserta la espera (`fila_espera`). Devuelve la fila guardada, o
        None si la compra ya tiene otra abierta (el índice único): quien
        llama no retiene nada. Si la abierta es esta misma (un reintento),
        la devuelve. Sin la compra guardada, `ErrorAlmacen` (clave foránea)."""
        fila = _validar_fila_espera(fila)
        if fila["compra_id"] not in self._compras:
            raise ErrorAlmacen("la compra no está en el almacén")
        abierta = self._abierta(fila["compra_id"])
        if abierta is not None or fila["id"] in self._esperas:
            if abierta is not None and abierta["id"] == fila["id"]:
                return copy.deepcopy(abierta)
            return None
        self._esperas[fila["id"]] = copy.deepcopy(fila)
        if len(self._esperas) > self._max_compras * 4:
            # Se van las cerradas más viejas; una abierta no (alguien la espera).
            for viejo in [e for e, f in self._esperas.items() if f["estado"] != "abierta"]:
                if len(self._esperas) <= self._max_compras * 4:
                    break
                del self._esperas[viejo]
        return copy.deepcopy(fila)

    async def leer_espera(self, espera_id: str) -> dict[str, Any] | None:
        fila = self._esperas.get(_id_espera(espera_id))
        return copy.deepcopy(fila) if fila is not None else None

    async def espera_abierta_de(self, compra_id: str) -> dict[str, Any] | None:
        """La espera abierta de la compra (aunque ya haya pasado su `vence`:
        cerrarla es de quien la retiene o del que normaliza), o None."""
        fila = self._abierta(_id_compra(compra_id))
        return copy.deepcopy(fila) if fila is not None else None

    async def responder_espera(
        self,
        espera_id: str,
        respuesta: Any,
        ahora: datetime,
        *,
        tipo: str | None = None,
        oportunidad_id: str | None = None,
    ) -> bool:
        """`abierta` → `respondida` solo si sigue abierta y no venció
        (`vence > ahora`), y si se pasan, del tipo y la oportunidad dados.
        True solo para quien la cambió: entre dos instancias gana una."""
        _id_espera(espera_id)
        respuesta = _respuesta_json(respuesta)
        ahora = _fecha(ahora)
        if tipo is not None:
            _tipo_espera(tipo)
        if oportunidad_id is not None:
            _id_seguro(oportunidad_id, "oportunidad_id")
        fila = self._esperas.get(espera_id)
        if fila is None or fila["estado"] != "abierta" or not _fecha(fila["vence"]) > ahora:
            return False
        if tipo is not None and fila["tipo"] != tipo:
            return False
        if oportunidad_id is not None and fila["oportunidad_id"] != oportunidad_id:
            return False
        fila.update({"estado": "respondida", "respuesta": respuesta, "resuelta": _iso(ahora)})
        return True

    async def vencer_espera(self, espera_id: str, ahora: datetime) -> dict[str, Any] | None:
        """`abierta` → `vencida` si sigue abierta (sin mirar `vence`: quien
        llama decide cuándo, también al cancelarse la llamada). Devuelve la
        fila como quedó: `vencida` si la cerró esta llamada (o ya estaba) o
        `respondida` si alguien respondió antes (usar su `respuesta`). None
        si la espera no existe."""
        _id_espera(espera_id)
        ahora = _fecha(ahora)
        fila = self._esperas.get(espera_id)
        if fila is None:
            return None
        if fila["estado"] == "abierta":
            fila.update({"estado": "vencida", "resuelta": _iso(ahora)})
        return copy.deepcopy(fila)

    # -- eventos (004) ------------------------------------------------------------

    async def guardar_evento(self, fila: dict[str, Any]) -> None:
        """Inserta el evento (`fila_evento`) con el `orden` siguiente; el mismo
        `id` dos veces no duplica (ignore-duplicates)."""
        fila = _validar_fila_evento(fila)
        if fila["id"] in self._eventos:
            return
        self._orden_eventos += 1
        self._eventos[fila["id"]] = {"orden": self._orden_eventos, **fila}
        self._recortar(self._eventos, self._max_compras * 20)

    async def listar_eventos(
        self, compra_id: str, desde_orden: int = 0, *, limite: int = 100
    ) -> list[dict[str, Any]]:
        """Los eventos de la compra con `orden > desde_orden`, en orden."""
        _id_compra(compra_id)
        _entero(desde_orden, "desde_orden")
        limite = _limite(limite)
        filas = [
            f for f in self._eventos.values()
            if f["compra_id"] == compra_id and f["orden"] > desde_orden
        ]
        filas.sort(key=lambda f: f["orden"])
        return copy.deepcopy(filas[:limite])

    async def contar_eventos(self, compra_id: str, tipo: str) -> int:
        """Cuántos eventos de ese tipo tiene la compra (el tope del historial)."""
        _id_compra(compra_id)
        _tipo_evento(tipo)
        return sum(1 for f in self._eventos.values() if f["compra_id"] == compra_id and f["tipo"] == tipo)

    async def listar_eventos_recientes(
        self, compra_id: str, limite: int = MAX_EVENTOS_RECIENTES
    ) -> list[dict[str, Any]]:
        """Los `limite` eventos más nuevos de la compra, en orden (la
        explicación: el final de la compra no se pierde)."""
        _id_compra(compra_id)
        limite = _entero(limite, "limite", minimo=1)
        filas = sorted(
            (f for f in self._eventos.values() if f["compra_id"] == compra_id),
            key=lambda f: f["orden"],
        )
        return copy.deepcopy(filas[-limite:])

    # -- entregas de webhooks y su cola (004) ---------------------------------

    async def guardar_entrega(self, entrega: EntregaWebhook) -> None:
        """Upsert del registro de la entrega. Las columnas de la cola
        (`cuerpo`, `proximo_intento`, `tomada_hasta`) quedan como estaban."""
        entrega_id = _id_seguro(entrega.id, "entrega_id")
        self._entregas[entrega_id] = {**self._entregas.get(entrega_id, {}), **entrega.a_json()}
        self._entregas.move_to_end(entrega_id)
        self._recortar(self._entregas, self._max_compras)

    async def listar_entregas(
        self, integrador_id: str, *, limite: int = 50
    ) -> list[dict[str, Any]]:
        """Las entregas del integrador, la más nueva primero (las columnas de
        `EntregaWebhook.a_json()`, sin el cuerpo)."""
        _id_slug(integrador_id, "integrador_id")
        limite = _limite(limite)
        filas = [f for f in self._entregas.values() if f["integrador_id"] == integrador_id]
        filas.sort(key=lambda f: (_fecha(f["creada"]), f["id"]), reverse=True)
        return [_proyectar(f, COLUMNAS_ENTREGA) for f in filas[:limite]]

    async def encolar_entrega(
        self, entrega: EntregaWebhook, cuerpo: bytes, *, proximo_intento: datetime
    ) -> None:
        """Pone la entrega en la cola: el registro, el cuerpo exacto y cuándo
        intentarla, sin tomar. Upsert por id (encolarla de nuevo no duplica)."""
        fila = _fila_cola(entrega, cuerpo, proximo_intento)
        self._entregas[fila["id"]] = {**self._entregas.get(fila["id"], {}), **fila}
        self._entregas.move_to_end(fila["id"])
        self._recortar(self._entregas, self._max_compras)

    async def tomar_entregas(
        self, ahora: datetime, *, hasta: datetime, limite: int = 10
    ) -> list[dict[str, Any]]:
        """Toma hasta `limite` entregas pendientes cuyo `proximo_intento` ya
        llegó y que nadie tiene tomada (o cuya toma venció): les pone
        `tomada_hasta=hasta` y las devuelve (con el cuerpo). Cada una la toma
        una sola instancia; quien la toma la marca con `marcar_entrega`."""
        ahora, hasta = _ventana_toma(ahora, hasta)
        limite = _limite(limite)
        candidatas = [
            f for f in self._entregas.values()
            if f.get("estado") == "pendiente"
            and f.get("proximo_intento") is not None
            and _fecha(f["proximo_intento"]) <= ahora
            and (f.get("tomada_hasta") is None or _fecha(f["tomada_hasta"]) < ahora)
        ]
        candidatas.sort(key=lambda f: (_fecha(f["proximo_intento"]), f["id"]))
        tomadas = []
        for fila in candidatas[:limite]:
            fila["tomada_hasta"] = _iso(hasta)
            tomadas.append(copy.deepcopy(fila))
        return tomadas

    async def marcar_entrega(
        self,
        entrega: EntregaWebhook,
        *,
        tomada_hasta: datetime | str,
        proximo_intento: datetime | None = None,
    ) -> bool:
        """Después de un intento: guarda el estado de la entrega y la suelta
        (`tomada_hasta=null`), con el `proximo_intento` si sigue pendiente.
        Solo si la sigue teniendo esta toma (`tomada_hasta` igual a la que
        devolvió `tomar_entregas`): True si escribió."""
        cambios = _cambios_marca(entrega, proximo_intento)
        toma = _fecha(tomada_hasta)
        fila = self._entregas.get(entrega.id)
        if fila is None or fila.get("tomada_hasta") is None or _fecha(fila["tomada_hasta"]) != toma:
            return False
        fila.update(cambios)
        return True

    async def cerrar(self) -> None:
        return None


# -- almacén en Supabase (PostgREST) --------------------------------------------


class EstadoSupabase:
    """Almacén durable sobre PostgREST (tablas de `proxy/sql/003_gateway.sql`
    y `004_serverless.sql`).

    Cada id que va en un filtro se valida antes de mandar el pedido. Toda
    falla (red, timeout, status ≥ 300, respuesta que no es lo esperado) es
    `ErrorAlmacen` con un mensaje sin la clave ni el cuerpo de la respuesta.
    Las operaciones que deciden algo son atómicas en la base: la atadura de
    un contexto (`ignore-duplicates`, gana el primero), el reclamo de una
    aprobación y la respuesta o el vencimiento de una espera (PATCH
    condicionado a `estado=eq.<el de antes>` con `return=representation`:
    una fila = ganó), abrir una espera y crear una aprobación pendiente
    (índices únicos parciales: ante 409 / 23505 se lee la que ya existe;
    `on_conflict` de PostgREST no sirve con un índice parcial), la escritura
    condicionada de la compra y la toma de una entrega (`tomada_hasta`).
    """

    nombre = "supabase"
    durable = True

    def __init__(
        self,
        url: str,
        clave: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = TIMEOUT_SEGUNDOS,
    ) -> None:
        self._rest = f"{url.rstrip('/')}/rest/v1"
        self._clave = clave
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(timeout_s),
            trust_env=False,
            follow_redirects=False,
        )

    def __repr__(self) -> str:  # sin la clave
        return f"EstadoSupabase({self._rest!r})"

    def _cabeceras(self, prefer: str | None) -> dict[str, str]:
        headers = cabeceras(self._clave, prefer or "")
        if not prefer:
            del headers["Prefer"]
        return headers

    async def _pedir(
        self,
        metodo: str,
        tabla: str,
        *,
        params: dict[str, str],
        prefer: str | None,
        cuerpo: Any = None,
        aceptar: frozenset[int] = frozenset(),
    ) -> httpx.Response:
        """`aceptar`: status ≥ 300 que quien llama interpreta (el 409 de un
        índice único); cualquier otro es `ErrorAlmacen`."""
        try:
            respuesta = await self._client.request(
                metodo,
                f"{self._rest}/{tabla}",
                params=params,
                headers=self._cabeceras(prefer),
                json=cuerpo,
            )
        except httpx.HTTPError as e:
            raise ErrorAlmacen(f"{metodo} {tabla}: {type(e).__name__}") from None
        if respuesta.status_code >= 300 and respuesta.status_code not in aceptar:
            raise ErrorAlmacen(f"{metodo} {tabla}: HTTP {respuesta.status_code}")
        return respuesta

    @staticmethod
    def _lista(respuesta: httpx.Response, que: str) -> list[dict[str, Any]]:
        try:
            filas = respuesta.json()
        except ValueError:
            raise ErrorAlmacen(f"{que}: respuesta no es JSON") from None
        if not isinstance(filas, list) or not all(isinstance(f, dict) for f in filas):
            raise ErrorAlmacen(f"{que}: respuesta inesperada")
        return filas

    @staticmethod
    def _codigo_pg(respuesta: httpx.Response) -> str | None:
        """El código de Postgres del error de PostgREST (`23505`, `23503`…)."""
        try:
            cuerpo = respuesta.json()
        except ValueError:
            return None
        codigo = cuerpo.get("code") if isinstance(cuerpo, dict) else None
        return codigo if isinstance(codigo, str) else None

    async def _filas(self, tabla: str, params: dict[str, str]) -> list[dict[str, Any]]:
        respuesta = await self._pedir("GET", tabla, params=params, prefer=None)
        return self._lista(respuesta, f"GET {tabla}")

    async def _una(self, tabla: str, params: dict[str, str]) -> dict[str, Any] | None:
        filas = await self._filas(tabla, params)
        return filas[0] if filas else None

    async def _upsert(self, tabla: str, conflicto: str, fila: dict[str, Any]) -> None:
        await self._pedir(
            "POST",
            tabla,
            params={"on_conflict": conflicto},
            prefer="resolution=merge-duplicates,return=minimal",
            cuerpo=fila,
        )

    async def _patch(
        self, tabla: str, params: dict[str, str], cuerpo: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """PATCH condicionado con `return=representation`: las filas que
        cambiaron (cero si el filtro no coincidió o el trigger la descartó)."""
        respuesta = await self._pedir(
            "PATCH", tabla, params=params, prefer="return=representation", cuerpo=cuerpo
        )
        return self._lista(respuesta, f"PATCH {tabla}")

    async def _insertar(
        self, tabla: str, fila: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Insert simple (sin `on_conflict`): (fila insertada, None) o, si la
        base lo rechazó con 409, (None, código de Postgres)."""
        respuesta = await self._pedir(
            "POST", tabla, params={}, prefer="return=representation", cuerpo=fila,
            aceptar=frozenset({409}),
        )
        if respuesta.status_code == 409:
            return None, self._codigo_pg(respuesta)
        filas = self._lista(respuesta, f"POST {tabla}")
        if len(filas) != 1:
            raise ErrorAlmacen(f"POST {tabla}: respuesta inesperada")
        return filas[0], None

    # -- compras ----------------------------------------------------------------

    async def guardar_compra(self, fila: dict[str, Any]) -> None:
        """Upsert por id. Con 004 aplicada, el trigger `gw_compras_proteger`
        descarta sin error una escritura vieja o que saca a la compra de un
        estado terminal (la respuesta es la misma)."""
        await self._upsert(TABLA_COMPRAS, "id", _validar_fila_compra(fila))

    async def guardar_compra_condicionada(
        self,
        fila: dict[str, Any],
        *,
        estado_esperado: str | tuple[str, ...] | frozenset[str],
        espera_id_esperado: str | None | _Cualquiera,
        actualizada_esperada: datetime | str | None = None,
    ) -> bool:
        """PATCH de la compra condicionado al estado (uno o varios), a la
        espera (`espera_id=is.null`, `eq.<id>` o sin condición con
        `CUALQUIERA`) y, si se pasa, a `actualizada`. True si cambió una fila."""
        fila = _validar_fila_compra(fila)
        estados = _estados_esperados(estado_esperado)
        espera = _espera_esperada(espera_id_esperado)
        params = {
            "id": f"eq.{fila['id']}",
            "estado": f"eq.{estados[0]}" if len(estados) == 1 else f"in.({','.join(estados)})",
        }
        if espera is None:
            params["espera_id"] = "is.null"
        elif isinstance(espera, str):
            params["espera_id"] = f"eq.{espera}"
        if actualizada_esperada is not None:
            params["actualizada"] = f"eq.{_iso(_fecha(actualizada_esperada))}"
        cuerpo = {k: v for k, v in fila.items() if k != "id"}
        return len(await self._patch(TABLA_COMPRAS, params, cuerpo)) == 1

    async def cargar_compra(self, compra_id: str) -> dict[str, Any] | None:
        return await self._una(
            TABLA_COMPRAS, {"id": f"eq.{_id_compra(compra_id)}", "select": "*"}
        )

    async def listar_compras(
        self,
        integrador_id: str,
        *,
        limite: int = 50,
        estado: str | tuple[str, ...] | None = None,
        desde: datetime | None = None,
        hasta: datetime | None = None,
        referencia: str | None = None,
        antes_de: tuple[datetime | str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Como `EstadoMemoria.listar_compras`. El cursor es un filtro de
        PostgREST (`creada < c OR (creada = c AND id < i)`), con las fechas
        entre comillas dentro del `or`; todo valor se valida antes."""
        params = {
            "integrador_id": f"eq.{_id_slug(integrador_id, 'integrador_id')}",
            "order": "creada.desc,id.desc",  # como EstadoMemoria: a igual fecha, por id
            "limit": str(_limite(limite)),
            "select": "*",
        }
        estados = _estados_filtro(estado)
        referencia = _referencia(referencia)
        cursor = _cursor(antes_de)
        if estados is not None:
            params["estado"] = (
                f"eq.{estados[0]}" if len(estados) == 1 else f"in.({','.join(estados)})"
            )
        if desde is not None:
            params["creada"] = f"gte.{_iso(_fecha(desde))}"
        if hasta is not None:
            params["and"] = f'(creada.lt."{_iso(_fecha(hasta))}")'
        if referencia is not None:
            params["referencia"] = f"eq.{referencia}"
        if cursor is not None:
            momento = _iso(cursor[0])
            params["or"] = f'(creada.lt."{momento}",and(creada.eq."{momento}",id.lt.{cursor[1]}))'
        return await self._filas(TABLA_COMPRAS, params)

    @staticmethod
    def _total(respuesta: httpx.Response, que: str) -> int:
        """El total de `Content-Range` (`0-0/57` o `*/0`) de `count=exact`."""
        rango = respuesta.headers.get("content-range", "")
        _, _, total = rango.rpartition("/")
        if not total.isdigit():
            raise ErrorAlmacen(f"{que}: sin total en Content-Range")
        return int(total)

    # Filas por página al leer varias compras: el máximo que devuelve
    # PostgREST por pedido (`db-max-rows`, 1000 en Supabase) puede ser menos
    # de lo que hay, así que se pagina con el total de `count=exact`.
    FILAS_POR_PAGINA = 1000

    async def _filas_todas(self, tabla: str, params: dict[str, str]) -> list[dict[str, Any]]:
        """Todas las filas del filtro, de a páginas (`offset`), hasta el total
        que informa la base. Más de `MAX_FILAS_LECTURA` es `ErrorAlmacen`."""
        filas: list[dict[str, Any]] = []
        while True:
            respuesta = await self._pedir(
                "GET", tabla,
                params={**params, "limit": str(self.FILAS_POR_PAGINA), "offset": str(len(filas))},
                prefer="count=exact",
            )
            pagina = self._lista(respuesta, f"GET {tabla}")
            total = self._total(respuesta, f"GET {tabla}")
            if total > MAX_FILAS_LECTURA:
                raise ErrorAlmacen(f"GET {tabla}: demasiadas filas")
            filas.extend(pagina)
            if not pagina or len(filas) >= total:
                return filas

    async def contextos_de_compras(self, compra_ids: Any) -> dict[str, list[str]]:
        """Los contextos de varias compras con `compra_id=in.(…)`: una consulta
        por tabla (de a `IDS_POR_CONSULTA` ids), no una por compra."""
        ids = _ids_compras(compra_ids)
        resultado: dict[str, list[str]] = {i: [] for i in ids}
        for inicio in range(0, len(ids), IDS_POR_CONSULTA):
            trozo = ids[inicio: inicio + IDS_POR_CONSULTA]
            filas = await self._filas_todas(TABLA_CONTEXTOS, {
                "compra_id": f"in.({','.join(trozo)})",
                "select": "compra_id,contexto_id",
                "order": "compra_id.asc,creado.asc,contexto_id.asc",
            })
            for fila in filas:
                compra = fila.get("compra_id")
                if compra in resultado and len(resultado[compra]) < LIMITE_LISTADO_MAXIMO:
                    resultado[compra].append(_id_seguro(fila.get("contexto_id"), "contexto_id"))
        return resultado

    async def aprobaciones_de_compras(self, compra_ids: Any) -> dict[str, list[dict[str, Any]]]:
        """Las aprobaciones de varias compras con `compra_id=in.(…)`, la más
        vieja primero en cada una (como `aprobaciones_de_compra`)."""
        ids = _ids_compras(compra_ids)
        resultado: dict[str, list[dict[str, Any]]] = {i: [] for i in ids}
        for inicio in range(0, len(ids), IDS_POR_CONSULTA):
            trozo = ids[inicio: inicio + IDS_POR_CONSULTA]
            filas = await self._filas_todas(TABLA_APROBACIONES, {
                "compra_id": f"in.({','.join(trozo)})",
                "select": "*",
                "order": "compra_id.asc,creada.asc,id.asc",
            })
            for fila in filas:
                compra = fila.get("compra_id")
                if compra in resultado and len(resultado[compra]) < LIMITE_LISTADO_MAXIMO:
                    resultado[compra].append(fila)
        return resultado

    # La columna de fecha de cada tabla que mira la purga y las de su clave
    # (lo único que vuelve de cada fila borrada).
    _PURGA: dict[str, tuple[str, str]] = {
        "gw_eventos": ("ts", "id"),
        "gw_ofertas": ("registradas", "contexto_id"),
        "gw_ultimos_veredictos": ("ts", "contexto_id,cart_id"),
        "gw_aprobaciones": ("creada", "id"),
        "gw_entregas_webhook": ("creada", "id"),
        "gw_esperas": ("creada", "id"),
        "gw_contextos": ("creado", "contexto_id"),
        "gw_compras": ("creada", "id"),
    }

    async def purgar(self, antes_de: datetime) -> dict[str, int]:
        """Como `EstadoMemoria.purgar`: un `DELETE` por tabla con
        `<fecha>=lt.<antes_de>` (los contextos y las esperas de una compra
        borrada se van además en cascada). Una falla en el medio deja lo ya
        borrado (la próxima purga sigue)."""
        limite = _iso(_fecha(antes_de))
        borradas: dict[str, int] = {}
        for tabla in TABLAS_PURGA:
            columna, clave = self._PURGA[tabla]
            respuesta = await self._pedir(
                "DELETE", tabla, params={columna: f"lt.{limite}", "select": clave},
                prefer="return=representation",
            )
            borradas[tabla] = len(self._lista(respuesta, f"DELETE {tabla}"))
        return borradas

    async def contar_activas(self, integrador_id: str, desde: datetime) -> int:
        """Cuenta con `Prefer: count=exact` y lee el total de `Content-Range`
        (`0-0/57` o `*/0`); con `limit=1` no baja todas las filas."""
        respuesta = await self._pedir(
            "GET",
            TABLA_COMPRAS,
            params={
                "integrador_id": f"eq.{_id_slug(integrador_id, 'integrador_id')}",
                "creada": f"gte.{_iso(_fecha(desde))}",
                "estado": f"in.({','.join(ESTADOS_ACTIVOS)})",
                "select": "id",
                "limit": "1",
            },
            prefer="count=exact",
        )
        return self._total(respuesta, "GET gw_compras")

    # -- contextos ----------------------------------------------------------------

    async def atar_contexto(self, contexto_id: str, compra_id: str, comercio_id: str) -> str:
        """Inserta la atadura ignorando duplicados y lee el dueño: si otro la
        ató antes (en este proceso o en otro), devuelve ese."""
        contexto_id = _id_seguro(contexto_id, "contexto_id")
        fila = {
            "contexto_id": contexto_id,
            "compra_id": _id_compra(compra_id),
            "comercio_id": _id_slug(comercio_id, "comercio_id"),
        }
        await self._pedir(
            "POST",
            TABLA_CONTEXTOS,
            params={"on_conflict": "contexto_id"},
            prefer="resolution=ignore-duplicates,return=minimal",
            cuerpo=fila,
        )
        dueno = await self.compra_de_contexto(contexto_id)
        if dueno is None:
            raise ErrorAlmacen("POST gw_contextos: la atadura no quedó guardada")
        return dueno

    async def compra_de_contexto(self, contexto_id: str) -> str | None:
        fila = await self._una(
            TABLA_CONTEXTOS,
            {
                "contexto_id": f"eq.{_id_seguro(contexto_id, 'contexto_id')}",
                "select": "compra_id",
            },
        )
        if fila is None:
            return None
        return _id_compra(fila.get("compra_id"))

    async def contextos_de_compra(self, compra_id: str) -> list[str]:
        """Los `contextId` atados a la compra, en orden de atadura."""
        filas = await self._filas(
            TABLA_CONTEXTOS,
            {
                "compra_id": f"eq.{_id_compra(compra_id)}",
                "select": "contexto_id",
                "order": "creado.asc,contexto_id.asc",
                "limit": str(LIMITE_LISTADO_MAXIMO),
            },
        )
        return [_id_seguro(f.get("contexto_id"), "contexto_id") for f in filas]

    # -- ofertas y último veredicto ---------------------------------------------------

    async def guardar_ofertas(self, fila: dict[str, Any]) -> None:
        _id_seguro(fila.get("contexto_id"), "contexto_id")
        await self._upsert(TABLA_OFERTAS, "contexto_id", fila)

    async def cargar_ofertas(self, contexto_id: str) -> dict[str, Any] | None:
        return await self._una(
            TABLA_OFERTAS,
            {"contexto_id": f"eq.{_id_seguro(contexto_id, 'contexto_id')}", "select": "*"},
        )

    async def guardar_ultimo_veredicto(self, fila: dict[str, Any]) -> None:
        _id_seguro(fila.get("contexto_id"), "contexto_id")
        _id_seguro(fila.get("cart_id"), "cart_id")
        if fila.get("status") not in STATUS_VEREDICTO_SQL:
            raise ErrorAlmacen("status de veredicto desconocido")
        await self._upsert(TABLA_VEREDICTOS, "contexto_id,cart_id", fila)

    async def cargar_ultimo_veredicto(
        self, contexto_id: str, cart_id: str
    ) -> dict[str, Any] | None:
        return await self._una(
            TABLA_VEREDICTOS,
            {
                "contexto_id": f"eq.{_id_seguro(contexto_id, 'contexto_id')}",
                "cart_id": f"eq.{_id_seguro(cart_id, 'cart_id')}",
                "select": "*",
            },
        )

    async def ultimos_veredictos_de_contexto(self, contexto_id: str) -> list[dict[str, Any]]:
        """El último veredicto de cada cart de ese contexto, el más nuevo primero."""
        return await self._filas(
            TABLA_VEREDICTOS,
            {
                "contexto_id": f"eq.{_id_seguro(contexto_id, 'contexto_id')}",
                "order": "ts.desc,cart_id.asc",
                "limit": str(LIMITE_LISTADO_MAXIMO),
                "select": "*",
            },
        )

    # -- aprobaciones -------------------------------------------------------------

    async def guardar_aprobacion(self, fila: dict[str, Any]) -> None:
        """Upsert por id. Con 004, otra pendiente con la misma clave da 409
        (`ErrorAlmacen`): para crear una sin chocar, `crear_aprobacion_pendiente`."""
        await self._upsert(TABLA_APROBACIONES, "id", _validar_fila_aprobacion(fila))

    async def crear_aprobacion_pendiente(
        self, fila: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        """(fila guardada, creada). Insert simple; si choca con el índice de
        pendientes (409 / 23505: otra instancia la creó antes), lee y devuelve
        esa con `creada=False`. Si en el medio la usaron, reintenta (hasta
        `INTENTOS_APROBACION`); otro 409 (u otro código) es `ErrorAlmacen`."""
        fila = _validar_fila_aprobacion(fila)
        if fila["estado"] != "pendiente":
            raise ErrorAlmacen("una aprobación nueva está pendiente")
        for _ in range(INTENTOS_APROBACION):
            creada, codigo = await self._insertar(TABLA_APROBACIONES, fila)
            if creada is not None:
                return creada, True
            if codigo != PG_UNICA:
                raise ErrorAlmacen("POST gw_aprobaciones: HTTP 409")
            existente = await self.cargar_aprobacion_pendiente(
                fila["contexto_id"], fila["cart_id"],
                centavos=fila["centavos"], moneda=fila["moneda"],
            )
            if existente is not None:
                return existente, False
        raise ErrorAlmacen("POST gw_aprobaciones: no se pudo crear ni leer la pendiente")

    async def cargar_aprobacion_pendiente(
        self,
        contexto_id: str,
        cart_id: str,
        *,
        centavos: int | None = None,
        moneda: str | None = None,
    ) -> dict[str, Any] | None:
        params = {
            "contexto_id": f"eq.{_id_seguro(contexto_id, 'contexto_id')}",
            "cart_id": f"eq.{_id_seguro(cart_id, 'cart_id')}",
            "estado": "eq.pendiente",
            "order": "creada.desc",
            "limit": "1",
            "select": "*",
        }
        if centavos is not None:
            params["centavos"] = f"eq.{_entero(centavos, 'centavos')}"
        if moneda is not None:
            params["moneda"] = f"eq.{_id_seguro(moneda, 'moneda')}"
        return await self._una(TABLA_APROBACIONES, params)

    async def reclamar_aprobacion(
        self, aprobacion_id: str, *, usada: str, task_id: str | None
    ) -> bool:
        """PATCH condicionado a `estado=eq.pendiente`: la base hace el
        "un solo uso" atómico. True solo si cambió exactamente una fila."""
        params = {
            "id": f"eq.{_id_seguro(aprobacion_id, 'aprobacion_id')}",
            "estado": "eq.pendiente",
        }
        if task_id is not None:
            _id_seguro(task_id, "task_id")
        respuesta = await self._pedir(
            "PATCH",
            TABLA_APROBACIONES,
            params=params,
            prefer="return=representation",
            cuerpo={"estado": "usada", "usada": usada, "task_id": task_id},
        )
        try:
            filas = respuesta.json()
        except ValueError:
            raise ErrorAlmacen("PATCH gw_aprobaciones: respuesta no es JSON") from None
        if not isinstance(filas, list):
            raise ErrorAlmacen("PATCH gw_aprobaciones: respuesta inesperada")
        return len(filas) == 1

    async def aprobaciones_de_compra(self, compra_id: str) -> list[dict[str, Any]]:
        """Las aprobaciones (pendientes y usadas) de la compra, la más vieja primero."""
        return await self._filas(
            TABLA_APROBACIONES,
            {
                "compra_id": f"eq.{_id_compra(compra_id)}",
                "order": "creada.asc,id.asc",
                "limit": str(LIMITE_LISTADO_MAXIMO),
                "select": "*",
            },
        )

    # -- esperas (004) ------------------------------------------------------------

    async def abrir_espera(self, fila: dict[str, Any]) -> dict[str, Any] | None:
        """Insert simple de la espera. Si choca con el índice de una abierta
        por compra (409 / 23505), devuelve la abierta solo si es esta misma (un
        reintento después de un timeout); si es otra, None. Otro 409 (la
        compra no está: 23503) es `ErrorAlmacen`."""
        fila = _validar_fila_espera(fila)
        creada, codigo = await self._insertar(TABLA_ESPERAS, fila)
        if creada is not None:
            return creada
        if codigo != PG_UNICA:
            raise ErrorAlmacen("POST gw_esperas: HTTP 409")
        abierta = await self.espera_abierta_de(fila["compra_id"])
        return abierta if abierta is not None and abierta.get("id") == fila["id"] else None

    async def leer_espera(self, espera_id: str) -> dict[str, Any] | None:
        return await self._una(
            TABLA_ESPERAS, {"id": f"eq.{_id_espera(espera_id)}", "select": "*"}
        )

    async def espera_abierta_de(self, compra_id: str) -> dict[str, Any] | None:
        return await self._una(
            TABLA_ESPERAS,
            {
                "compra_id": f"eq.{_id_compra(compra_id)}",
                "estado": "eq.abierta",
                "order": "creada.desc",
                "limit": "1",
                "select": "*",
            },
        )

    async def responder_espera(
        self,
        espera_id: str,
        respuesta: Any,
        ahora: datetime,
        *,
        tipo: str | None = None,
        oportunidad_id: str | None = None,
    ) -> bool:
        """PATCH condicionado a `estado=eq.abierta&vence=gt.<ahora>` (y al
        tipo y la oportunidad si se pasan). True si cambió una fila: ganó."""
        params = {
            "id": f"eq.{_id_espera(espera_id)}",
            "estado": "eq.abierta",
            "vence": f"gt.{_iso(_fecha(ahora))}",
        }
        if tipo is not None:
            params["tipo"] = f"eq.{_tipo_espera(tipo)}"
        if oportunidad_id is not None:
            params["oportunidad_id"] = f"eq.{_id_seguro(oportunidad_id, 'oportunidad_id')}"
        cuerpo = {
            "estado": "respondida",
            "respuesta": _respuesta_json(respuesta),
            "resuelta": _iso(_fecha(ahora)),
        }
        return len(await self._patch(TABLA_ESPERAS, params, cuerpo)) == 1

    async def vencer_espera(self, espera_id: str, ahora: datetime) -> dict[str, Any] | None:
        """PATCH condicionado a `estado=eq.abierta`; si no cambió nada (alguien
        respondió o ya venció), lee la fila actual. Devuelve la fila como
        quedó, o None si no existe."""
        params = {"id": f"eq.{_id_espera(espera_id)}", "estado": "eq.abierta"}
        cuerpo = {"estado": "vencida", "resuelta": _iso(_fecha(ahora))}
        filas = await self._patch(TABLA_ESPERAS, params, cuerpo)
        if len(filas) == 1:
            return filas[0]
        return await self.leer_espera(espera_id)

    # -- eventos (004) ------------------------------------------------------------

    async def guardar_evento(self, fila: dict[str, Any]) -> None:
        """Insert ignorando duplicados por `id` (reintentar no duplica)."""
        await self._pedir(
            "POST",
            TABLA_EVENTOS,
            params={"on_conflict": "id"},
            prefer="resolution=ignore-duplicates,return=minimal",
            cuerpo=_validar_fila_evento(fila),
        )

    async def listar_eventos(
        self, compra_id: str, desde_orden: int = 0, *, limite: int = 100
    ) -> list[dict[str, Any]]:
        filas = await self._filas(
            TABLA_EVENTOS,
            {
                "compra_id": f"eq.{_id_compra(compra_id)}",
                "orden": f"gt.{_entero(desde_orden, 'desde_orden')}",
                "order": "orden.asc",
                "limit": str(_limite(limite)),
                "select": "*",
            },
        )
        for fila in filas:
            _entero(fila.get("orden"), "orden", minimo=1)
        return filas

    async def contar_eventos(self, compra_id: str, tipo: str) -> int:
        """Con `count=exact` y `limit=1` (no baja las filas)."""
        respuesta = await self._pedir(
            "GET", TABLA_EVENTOS,
            params={"compra_id": f"eq.{_id_compra(compra_id)}", "tipo": f"eq.{_tipo_evento(tipo)}",
                    "select": "id", "limit": "1"},
            prefer="count=exact",
        )
        return self._total(respuesta, "GET gw_eventos")

    async def listar_eventos_recientes(
        self, compra_id: str, limite: int = MAX_EVENTOS_RECIENTES
    ) -> list[dict[str, Any]]:
        """De a páginas, del más nuevo al más viejo (`orden.desc`, `orden=lt.`),
        hasta `limite`; se devuelven en orden."""
        compra_id = _id_compra(compra_id)
        limite = _entero(limite, "limite", minimo=1)
        filas: list[dict[str, Any]] = []
        antes: int | None = None
        while len(filas) < limite:
            pagina_limite = min(LIMITE_LISTADO_MAXIMO, limite - len(filas))
            params = {"compra_id": f"eq.{compra_id}", "order": "orden.desc",
                      "limit": str(pagina_limite), "select": "*"}
            if antes is not None:
                params["orden"] = f"lt.{antes}"
            pagina = await self._filas(TABLA_EVENTOS, params)
            for fila in pagina:
                _entero(fila.get("orden"), "orden", minimo=1)
            filas.extend(pagina)
            if len(pagina) < pagina_limite:
                break
            antes = pagina[-1]["orden"]
        return list(reversed(filas))

    # -- entregas de webhooks y su cola (004) -----------------------------------

    async def guardar_entrega(self, entrega: EntregaWebhook) -> None:
        _id_seguro(entrega.id, "entrega_id")
        await self._upsert(TABLA_ENTREGAS, "id", entrega.a_json())

    async def listar_entregas(
        self, integrador_id: str, *, limite: int = 50
    ) -> list[dict[str, Any]]:
        return await self._filas(
            TABLA_ENTREGAS,
            {
                "integrador_id": f"eq.{_id_slug(integrador_id, 'integrador_id')}",
                "order": "creada.desc,id.desc",
                "limit": str(_limite(limite)),
                "select": ",".join(COLUMNAS_ENTREGA),
            },
        )

    async def encolar_entrega(
        self, entrega: EntregaWebhook, cuerpo: bytes, *, proximo_intento: datetime
    ) -> None:
        await self._upsert(TABLA_ENTREGAS, "id", _fila_cola(entrega, cuerpo, proximo_intento))

    async def tomar_entregas(
        self, ahora: datetime, *, hasta: datetime, limite: int = 10
    ) -> list[dict[str, Any]]:
        """Lee las candidatas y toma cada una con un PATCH condicionado a que
        siga pendiente y sin tomar (o con la toma vencida): si otra instancia
        la tomó en el medio, el PATCH no cambia nada y no se devuelve."""
        ahora, hasta = _ventana_toma(ahora, hasta)
        # Libre: sin tomar o con la toma vencida (el valor va entre comillas
        # dentro del `or`, como pide PostgREST para los `:` y `.` de la fecha).
        libre = f'(tomada_hasta.is.null,tomada_hasta.lt."{_iso(ahora)}")'
        candidatas = await self._filas(
            TABLA_ENTREGAS,
            {
                "estado": "eq.pendiente",
                "proximo_intento": f"lte.{_iso(ahora)}",
                "or": libre,
                "order": "proximo_intento.asc,id.asc",
                "limit": str(_limite(limite)),
                "select": "id",
            },
        )
        tomadas: list[dict[str, Any]] = []
        for candidata in candidatas:
            filas = await self._patch(
                TABLA_ENTREGAS,
                {
                    "id": f"eq.{_id_seguro(candidata.get('id'), 'entrega_id')}",
                    "estado": "eq.pendiente",
                    "or": libre,
                },
                {"tomada_hasta": _iso(hasta)},
            )
            if len(filas) == 1:
                tomadas.append(filas[0])
        return tomadas

    async def marcar_entrega(
        self,
        entrega: EntregaWebhook,
        *,
        tomada_hasta: datetime | str,
        proximo_intento: datetime | None = None,
    ) -> bool:
        cambios = _cambios_marca(entrega, proximo_intento)
        params = {
            "id": f"eq.{entrega.id}",
            "tomada_hasta": f"eq.{_iso(_fecha(tomada_hasta))}",
        }
        return len(await self._patch(TABLA_ENTREGAS, params, cambios)) == 1

    async def cerrar(self) -> None:
        await self._client.aclose()


# -- desde el entorno ---------------------------------------------------------------


def estado_desde_entorno(env: Mapping[str, str]) -> AlmacenEstado:
    """El almacén según `GATEWAY_ESTADO` (`memoria` por defecto o `supabase`).

    `supabase` se pide explícito y exige credenciales: sin ellas es un error
    de configuración (`ValueError`, el proxy no arranca), no una vuelta
    silenciosa a memoria, porque el operador pidió durabilidad. Un valor
    desconocido también es `ValueError`."""
    valor = (env.get("GATEWAY_ESTADO") or "memoria").strip().lower()
    if valor == "memoria":
        return EstadoMemoria()
    if valor == "supabase":
        datos = credenciales(env)
        if datos is None:
            raise ValueError(
                "GATEWAY_ESTADO=supabase exige SUPABASE_URL y "
                "SUPABASE_SERVICE_ROLE_KEY (o SUPABASE_SECRET_KEY)"
            )
        return EstadoSupabase(*datos)
    raise ValueError("GATEWAY_ESTADO tiene que ser 'memoria' o 'supabase'")
