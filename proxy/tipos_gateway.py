"""Tipos compartidos del gateway de producción (docs: diseño del gateway).

Solo tipos, constantes, protocolos y `vista_veredicto` (la usan la API, el modo
sin estado y los webhooks). Sin I/O.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from contracts import STATUS_LLEGAN_A_FIRMA, Ficha, OfertasVistas, Oportunidad, Veredicto
from proxy import a2a, alertas

# -- headers y prefijos ------------------------------------------------------
HEADER_COMPRA = "X-Compra-Id"
HEADER_FIRMA_WEBHOOK = "X-Guard-Firma"
HEADER_EVENTO_WEBHOOK = "X-Guard-Evento"
HEADER_ENTREGA_WEBHOOK = "X-Guard-Entrega"
PREFIJO_CLAVE = "psg_"

# -- identificadores ---------------------------------------------------------
ID_COMPRA_RE = re.compile(r"cmp-[0-9a-f]{16}")
ID_ESPERA_RE = re.compile(r"esp-[0-9a-f]{16}")                  # esperas (004_serverless.sql)
ID_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")             # integradores y comercios
ID_SEGURO_RE = a2a.ID_SEGURO_RE                                  # contexto, cart, task, aprobación, merchant.id
KID_RE = re.compile(r"[0-9a-f]{12}")

RUTAS_V1_PUBLICAS: frozenset[str] = frozenset(
    {"/v1/public-key", "/v1/openapi.json", "/v1/docs"}
)
PREFIJO_A2A_COMERCIO = "/a2a/m/"

Oyente = Literal["local", "publico"]
OrigenContenido = Literal["web_search", "pagina", "herramienta", "otro"]
TipoWebhook = Literal[
    "compra.pregunta", "compra.oportunidad", "compra.requiere_aprobacion",
    "compra.veredicto", "compra.bloqueada", "ping",
]
EstadoEntrega = Literal["pendiente", "entregada", "fallida", "descartada"]
TipoEspera = Literal["porque", "decision"]
EstadoEspera = Literal["abierta", "respondida", "vencida"]


def es_id(regex: re.Pattern[str], valor: Any) -> bool:
    return isinstance(valor, str) and regex.fullmatch(valor) is not None


# -- configuración -----------------------------------------------------------
class ConfigInvalida(ValueError):
    """Archivo de claves o de comercios inválido: el proxy no arranca."""


@dataclass(frozen=True)
class ConfigWebhook:
    url: str
    secreto: bytes = field(repr=False)


@dataclass(frozen=True)
class Integrador:
    id: str
    nombre: str
    comercios: tuple[str, ...] = ()        # vacío = solo el comercio por defecto
    webhook: ConfigWebhook | None = None
    max_compras_activas: int = 1000


@dataclass(frozen=True)
class Comercio:
    id: str                                # id de ruta: /a2a/m/{id}
    nombre: str
    a2a_url: str                           # upstream JSON-RPC del Merchant Agent
    catalogo_url: str | None               # catálogo público (None = no publica)
    merchant_ids: tuple[str, ...] = ()     # merchant.id firmados válidos; () = sin chequeo
    respaldo_fixture: bool = False         # solo demo offline


# -- errores -----------------------------------------------------------------
class ErrorApi(Exception):
    """Error que la API /v1 convierte en {"error": {codigo, mensaje, detalle}}."""

    def __init__(
        self, status: int, codigo: str, mensaje: str, detalle: dict[str, Any] | None = None
    ) -> None:
        super().__init__(f"{status} {codigo}: {mensaje}")
        self.status = status
        self.codigo = codigo
        self.mensaje = mensaje
        self.detalle = detalle

    def a_json(self) -> dict[str, Any]:
        cuerpo: dict[str, Any] = {"codigo": self.codigo, "mensaje": self.mensaje}
        if self.detalle:
            cuerpo["detalle"] = self.detalle
        return {"error": cuerpo}


class ErrorAlmacen(RuntimeError):
    """El almacén de estado no respondió o rechazó la operación (fail-closed)."""


class EscrituraDecisivaFallida(ErrorAlmacen):
    """Una escritura decisiva en segundo plano (`TareasEnFondo.programar(...,
    decisiva=True)`) falló, se descartó o no terminó antes de la barrera.

    Es un `ErrorAlmacen`: quien ya traduce esos errores a 503
    `estado_no_disponible` no necesita nada más. `fallas` trae las
    descripciones (fijas, sin datos de la compra)."""

    def __init__(self, fallas: tuple[str, ...]) -> None:
        super().__init__("escrituras decisivas sin confirmar: " + ", ".join(fallas))
        self.fallas = fallas


# -- solicitudes ya validadas (las arma api_v1) ------------------------------
@dataclass(frozen=True)
class SolicitudCompra:
    pedido: str
    ficha: Ficha                    # confirmada=True, pedido_original=pedido
    comercio_id: str | None         # None = el comercio por defecto
    porque: str | None
    preguntar_porque: bool
    referencia: str | None


@dataclass(frozen=True)
class SolicitudEvaluar:
    ficha: Ficha
    checkout: dict[str, Any]        # data part ap2.checkout tal cual
    ofertas: OfertasVistas | None   # comercio_id lo completa el núcleo
    comercio_id: str | None


@dataclass(frozen=True)
class SolicitudFiltrar:
    contenido: Any
    origen: OrigenContenido


@dataclass(frozen=True)
class FiltrosListado:
    """`GET /v1/compras` ya validado: el límite, los filtros y el cursor
    (`(creada, id)` de la última compra de la página anterior)."""

    limite: int = 50
    estado: str | None = None
    desde: datetime | None = None
    hasta: datetime | None = None   # excluido
    referencia: str | None = None
    alerta: str | None = None
    cursor: tuple[datetime, str] | None = None
    # El resultado de la compra (`explicacion.RESULTADOS`) y si tiene alguna
    # alerta de estafa. `ahora` es el momento del pedido: con él se decide
    # `en_curso` o `sin_compra`, igual para filtrar y para mostrar.
    resultado: str | None = None
    intento_de_estafa: bool | None = None
    ahora: datetime | None = None


# -- foto de una compra (de compras.Compra a la API y los webhooks) ----------
@dataclass(frozen=True)
class FotoCompra:
    compra_id: str
    integrador_id: str | None
    comercio_id: str | None
    referencia: str | None
    estado: str
    creada: datetime
    actualizada: datetime
    pedido: str
    contextos: tuple[str, ...]
    ficha: Ficha | None
    intencion_activa: bool
    pregunta: str | None                      # solo si estado == esperando_contexto
    vence: datetime | None
    oportunidad: Oportunidad | None
    decision: str | None
    aprobacion_pendiente: dict[str, Any] | None
    veredictos: tuple[dict[str, Any], ...]    # vista_veredicto(...) en orden
    aprobaciones: tuple[dict[str, Any], ...]  # Aprobacion.a_json()
    # Con estado `bloqueada`: "manipulacion" (el filtro la detectó) o
    # "sin_revisar" (no pudo revisar y cortó); None en una fila de antes.
    corte: str | None = None
    # El último checkout que el proxy frenó sin veredicto (`Compra.bloqueo_proxy`).
    bloqueo_proxy: dict[str, Any] | None = None


@dataclass(frozen=True)
class PaginaCompras:
    """Una página del listado y, si puede haber más, dónde sigue."""

    fotos: tuple[FotoCompra, ...]
    siguiente: tuple[datetime, str] | None = None


# -- entregas de webhooks ----------------------------------------------------
@dataclass
class EntregaWebhook:
    id: str                         # whe-<32 hex>, igual en todos los intentos
    integrador_id: str
    tipo: str
    compra_id: str | None
    creada: datetime
    intentos: int = 0
    estado: EstadoEntrega = "pendiente"
    status_http: int | None = None
    error: str | None = None        # código fijo (ver diseño §1.6)
    destino_host: str | None = None
    actualizada: datetime | None = None

    def a_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "integrador_id": self.integrador_id, "tipo": self.tipo,
            "compra_id": self.compra_id, "creada": self.creada.isoformat(),
            "intentos": self.intentos, "estado": self.estado,
            "status_http": self.status_http, "error": self.error,
            "destino_host": self.destino_host,
            "actualizada": (self.actualizada or self.creada).isoformat(),
        }


# -- almacén de estado (proxy/estado.py) -------------------------------------
class AlmacenEstado(Protocol):
    nombre: str        # "memoria" | "supabase"
    durable: bool      # True: sobrevive a un reinicio; gate de aprobaciones en el almacén

    async def guardar_compra(self, fila: dict[str, Any]) -> None: ...
    async def cargar_compra(self, compra_id: str) -> dict[str, Any] | None: ...
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
    ) -> list[dict[str, Any]]: ...
    async def contar_activas(self, integrador_id: str, desde: datetime) -> int: ...
    async def atar_contexto(self, contexto_id: str, compra_id: str, comercio_id: str) -> str: ...
    async def compra_de_contexto(self, contexto_id: str) -> str | None: ...
    async def guardar_ofertas(self, fila: dict[str, Any]) -> None: ...
    async def cargar_ofertas(self, contexto_id: str) -> dict[str, Any] | None: ...
    async def guardar_ultimo_veredicto(self, fila: dict[str, Any]) -> None: ...
    async def cargar_ultimo_veredicto(
        self, contexto_id: str, cart_id: str
    ) -> dict[str, Any] | None: ...
    async def guardar_aprobacion(self, fila: dict[str, Any]) -> None: ...
    async def cargar_aprobacion_pendiente(
        self, contexto_id: str, cart_id: str, *,
        centavos: int | None = None, moneda: str | None = None,
    ) -> dict[str, Any] | None: ...
    async def reclamar_aprobacion(
        self, aprobacion_id: str, *, usada: str, task_id: str | None
    ) -> bool: ...
    async def guardar_entrega(self, entrega: EntregaWebhook) -> None: ...
    async def cerrar(self) -> None: ...

    # -- varias instancias (proxy/sql/004_serverless.sql) ----------------------
    # Semántica y retornos: ver `proxy/estado.py` (EstadoMemoria y
    # EstadoSupabase la cumplen igual). Toda falla es `ErrorAlmacen`.
    async def guardar_compra_condicionada(
        self,
        fila: dict[str, Any],
        *,
        estado_esperado: str | tuple[str, ...] | frozenset[str],
        espera_id_esperado: Any,     # str | None | estado.CUALQUIERA
        actualizada_esperada: datetime | str | None = None,
    ) -> bool: ...
    async def contextos_de_compra(self, compra_id: str) -> list[str]: ...
    async def ultimos_veredictos_de_contexto(self, contexto_id: str) -> list[dict[str, Any]]: ...
    async def abrir_espera(self, fila: dict[str, Any]) -> dict[str, Any] | None: ...
    async def leer_espera(self, espera_id: str) -> dict[str, Any] | None: ...
    async def espera_abierta_de(self, compra_id: str) -> dict[str, Any] | None: ...
    async def responder_espera(
        self, espera_id: str, respuesta: Any, ahora: datetime, *,
        tipo: str | None = None, oportunidad_id: str | None = None,
    ) -> bool: ...
    async def vencer_espera(self, espera_id: str, ahora: datetime) -> dict[str, Any] | None: ...
    async def crear_aprobacion_pendiente(
        self, fila: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]: ...
    async def aprobaciones_de_compra(self, compra_id: str) -> list[dict[str, Any]]: ...
    # -- historial (panel del cliente) ----------------------------------------
    async def contextos_de_compras(self, compra_ids: Any) -> dict[str, list[str]]: ...
    async def aprobaciones_de_compras(
        self, compra_ids: Any
    ) -> dict[str, list[dict[str, Any]]]: ...
    async def purgar(self, antes_de: datetime) -> dict[str, int]: ...
    async def guardar_evento(self, fila: dict[str, Any]) -> None: ...
    async def listar_eventos(
        self, compra_id: str, desde_orden: int = 0, *, limite: int = 100
    ) -> list[dict[str, Any]]: ...
    async def contar_eventos(self, compra_id: str, tipo: str) -> int: ...
    async def listar_eventos_recientes(
        self, compra_id: str, limite: int = 1000
    ) -> list[dict[str, Any]]: ...
    async def listar_entregas(
        self, integrador_id: str, *, limite: int = 50
    ) -> list[dict[str, Any]]: ...
    async def encolar_entrega(
        self, entrega: EntregaWebhook, cuerpo: bytes, *, proximo_intento: datetime
    ) -> None: ...
    async def tomar_entregas(
        self, ahora: datetime, *, hasta: datetime, limite: int = 10
    ) -> list[dict[str, Any]]: ...
    async def marcar_entrega(
        self, entrega: EntregaWebhook, *, tomada_hasta: datetime | str,
        proximo_intento: datetime | None = None,
    ) -> bool: ...


# -- núcleo que consume la API /v1 (lo implementa la integración) -----------
class NucleoGateway(Protocol):
    # La vida de una compra (`GATEWAY_COMPRA_TTL_S`): pasada sin checkout, `sin_compra`.
    ttl_compra_s: float

    def comercios_de(self, integrador: Integrador) -> list[Comercio]: ...
    def url_a2a(self, comercio_id: str) -> str: ...
    async def crear_compra(self, integrador: Integrador, solicitud: SolicitudCompra) -> FotoCompra: ...
    async def obtener_compra(self, integrador: Integrador, compra_id: str) -> FotoCompra: ...
    async def listar_compras(
        self, integrador: Integrador, filtros: FiltrosListado
    ) -> PaginaCompras: ...
    # Historial (panel del cliente): la compra con sus eventos saneados, y las
    # compras de un período (con False si no entraron todas).
    async def explicacion_compra(
        self, integrador: Integrador, compra_id: str
    ) -> tuple[FotoCompra, list[dict[str, Any]]]: ...
    async def resumen_compras(
        self, integrador: Integrador, desde: datetime, hasta: datetime
    ) -> tuple[list[FotoCompra], bool]: ...
    async def responder_porque(
        self, integrador: Integrador, compra_id: str, respuesta: str | None
    ) -> FotoCompra: ...
    async def decidir_oportunidad(
        self, integrador: Integrador, compra_id: str, oportunidad_id: str,
        decision: Literal["aceptar", "rechazar"],
    ) -> FotoCompra: ...
    async def aprobar(
        self, integrador: Integrador, compra_id: str, cart_id: str, contexto_id: str | None
    ) -> tuple[dict[str, Any], bool]: ...  # (Aprobacion.a_json(), creada)
    async def evaluar(self, integrador: Integrador, solicitud: SolicitudEvaluar) -> dict[str, Any]: ...
    async def filtrar(self, integrador: Integrador, solicitud: SolicitudFiltrar) -> dict[str, Any]: ...
    def clave_publica(self) -> dict[str, Any] | None: ...
    # Una lista o, si las lee del almacén (varias instancias), un awaitable.
    def entregas_webhook(
        self, integrador: Integrador, limite: int
    ) -> list[dict[str, Any]] | Awaitable[list[dict[str, Any]]]: ...
    async def probar_webhook(self, integrador: Integrador) -> dict[str, Any]: ...


# -- vista del veredicto (sin texto del comercio) ----------------------------
def vista_veredicto(
    veredicto: Veredicto, *, contexto_id: str | None, ts: datetime, evaluador: str
) -> dict[str, Any]:
    """La vista reducida del veredicto que guarda la compra (y que devuelven
    `/v1/compras/{id}`, `/v1/evaluar` y los webhooks).

    Además de `chequeos` (solo `tipo` y `resultado`), lleva lo explicable
    (`proxy/alertas.py`, aditivo): `chequeos_detalle` saneados, las `alertas`
    y el `monto`/`moneda` del cart evaluado."""
    base = a2a.veredicto_para_agente(veredicto.model_dump(mode="json"))
    recibo = veredicto.recibo
    monto, moneda = alertas.monto_evaluado(veredicto.chequeos)
    return {
        "status": base["status"],
        "motivo": base["motivo"],
        "cart_id": base["cart_id_elegido"],
        "llega_a_firma": veredicto.status in STATUS_LLEGAN_A_FIRMA,
        "chequeos": base["chequeos"],
        "recibo": {"id": recibo.id, "jws": recibo.firma, "kid": recibo.kid}
        if recibo is not None and recibo.firma
        else None,
        "contexto_id": contexto_id if es_id(ID_SEGURO_RE, contexto_id) else None,
        "ts": ts.isoformat(),
        "evaluador": evaluador,
        "chequeos_detalle": alertas.chequeos_detalle(veredicto.chequeos),
        "alertas": alertas.alertas_de(veredicto.chequeos),
        "monto": monto,
        "moneda": moneda,
    }
