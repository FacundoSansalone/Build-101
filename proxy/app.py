"""Proxy A2A de KaiAI (Starlette + httpx).

Se mete entre el Shopping Agent y el Merchant Agent sin tocar AP2:

- Sirve la Agent Card del comercio con su propia URL, así el cliente A2A no
  lo saltea.
- Reenvía el JSON-RPC tal cual (los bytes del pedido no se re-arman, así los
  mandatos llegan intactos) y soporta respuesta JSON y SSE.
- `find_products`: guarda carts, offer_details y escenario por el context_id
  de la respuesta. Solo de ahí salen las ofertas.
- `create_checkout`: llama a `evaluar()` con el cart y el contexto del
  pedido y aplica el veredicto. Nunca modifica el `ap2.checkout`: si pasa, lo
  deja igual y agrega el veredicto en un artefacto aparte; si se bloquea o se
  retiene (`escalar`), la tarea vuelve `failed` sin el checkout.
- Lo que no puede leer o no cuadra (más de un checkout, otro cart, otro
  contexto, respuesta ilegible, red caída) no llega a la firma.
- Emite `Evento`s para el dashboard (SSE + JSONL) y expone el interruptor y
  las aprobaciones humanas.
- Si hay credenciales de Supabase, registra ahí los veredictos y las
  aprobaciones (`proxy.persistencia`). Es solo registro: si falla, sigue.

El proxy no decide: decide `guard/`. El proxy solo transporta, registra,
aplica el veredicto y bloquea lo que no pudo evaluar (fail-closed).
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import functools
import hmac
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Any, Literal

import httpx
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from contracts import (
    GUARD_PARAMETROS_KEY,
    GUARD_VEREDICTO_KEY,
    STATUS_LLEGAN_A_FIRMA,
    CheckoutVisto,
    Evento,
    Ficha,
    Filtro,
    OfertasVistas,
    Requisito,
    ResultadoFiltro,
    Turno,
    Veredicto,
)
from proxy import a2a, vistas
from proxy.api_v1 import rutas_v1
from proxy.aprobaciones import (
    Aprobacion,
    DeIntegrador,
    RegistroAprobaciones,
    SinEscalar,
    aprobacion_desde_fila,
)
from proxy.claves import AutenticacionGateway, RegistroIntegradores
from proxy.comercios import (
    RegistroComercios,
    comercio_por_defecto,
    merchant_id_coincide,
    politica_de,
    url_publica_comercio,
)
from proxy.compras import (
    ESTADOS_TERMINALES_API,
    LARGO_MAXIMO_RESPUESTA,
    MAX_CONTEXTOS_POR_COMPRA,
    Compra,
    CompraDesconocida,
    EstadoInvalido,
    RegistroCompras,
)
from proxy.config import Config
from proxy.estado import (
    EstadoMemoria,
    TareasEnFondo,
    fila_ofertas,
    fila_ultimo_veredicto,
)
from proxy.evaluador import (
    EvaluadorCargado,
    cargar_evaluador,
    clave_publica,
    firmar_recibo,
    preparar_clave,
    validar_evaluador,
)
from proxy.eventos import CAPACIDAD, BusDeEventos
from proxy.fuentes import (
    cargar_catalogo,
    cargar_intent,
    ficha_del_intent,
    intent_ap2,
    intent_del_escenario,
    violaciones_ap2,
)
from proxy.pedido_usuario import LARGO_MAXIMO as LARGO_MAXIMO_PEDIDO
from proxy.pedido_usuario import PedidoDelUsuario, pedido_para_intake
from proxy.persistencia import Persistencia
from proxy.seguridad import HEADER_TOKEN, ControlLocal, escribir_token, generar_token
from proxy.tipos_gateway import (
    HEADER_COMPRA,
    ID_COMPRA_RE,
    ID_SEGURO_RE,
    AlmacenEstado,
    Comercio,
    ErrorAlmacen,
    FotoCompra,
    Integrador,
    es_id,
    vista_veredicto,
)
from proxy.webhooks import Despachador, tipo_webhook

logger = logging.getLogger("proxy")

# Headers que no se copian del cliente al comercio.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
# Tampoco los headers de control propios: el token de /guard/* (lo manda el
# reenvío del dashboard) y la compra del gateway nunca llegan al comercio.
_NO_HACIA_COMERCIO = _HOP_BY_HOP | {
    "host", "content-length", "accept-encoding", "cookie",
    HEADER_TOKEN.lower(), HEADER_COMPRA.lower(),
}
# Del comercio al cliente solo pasa esto (el Content-Type se normaliza). Nunca
# Set-Cookie: las cookies no distinguen puerto y llegarían al dashboard.
_DEL_COMERCIO_AL_CLIENTE = {"x-a2a-extensions"}

# Únicas rutas de la Agent Card que se piden al comercio.
RUTAS_CARD = (".well-known/agent-card.json", ".well-known/agent.json")
RUTAS_POST_CONTROL = (
    "/guard/state",
    "/guard/aprobaciones",
    "/guard/compras/contexto",
    "/guard/compras/decision",
    "/guard/compras/pedido",
)

_MAX_CONTEXTOS = 500
# Marcas de lo que es de compras de integrador (`ProxyGuard._visible`): más
# que el historial del bus (200) y que la cola de un suscriptor SSE (1000),
# para que un evento no pierda la marca mientras todavía se puede mostrar.
_MAX_MARCAS_PRIVADAS = 5000
_MAX_ID = 200  # largo máximo de contexto_id / cart_id en la API de aprobaciones

# Plantillas fijas del texto para el agente: "KaiAI: <status>. <motivo>"
# (el motivo lo arma guard/ sin texto del comercio). Ningún string del
# comercio entra acá.
_SUFIJO_RETENIDO = (
    "El checkout requiere aprobación humana: revisalo en el dashboard del "
    "Guard y, si se aprueba, reintentá create_checkout."
)
_SUFIJO_BLOQUEADO = "No se presenta para firma."
# Compras de integrador: el dashboard no autoriza, autoriza el dueño por /v1.
_SUFIJO_RETENIDO_API = (
    "El checkout requiere aprobación humana: el usuario tiene que aprobarlo por "
    "la API del Guard y después reintentá create_checkout."
)
# Errores del tráfico A2A de integrador antes de reenviar (diseño §1.2): un
# HTTP con status y un error JSON-RPC con código fijo. Mensajes fijos. El
# tráfico anónimo usa `contexto_ajeno` y `estado_no_disponible` (un contexto
# de una compra de integrador).
_ERRORES_A2A: dict[str, tuple[int, str]] = {
    "falta_compra": (400, f"falta {HEADER_COMPRA} o no tiene el formato de una compra."),
    "comercio_no_permitido": (403, "el comercio no está permitido para esta clave."),
    "comercio_desconocido": (404, "el comercio no existe."),
    "compra_desconocida": (404, "la compra no existe."),
    "comercio_distinto": (409, "la compra es de otro comercio."),
    "compra_bloqueada": (409, "la compra se bloqueó por seguridad."),
    "compra_vencida": (409, "la compra está vencida."),
    "contexto_ajeno": (409, "el contextId del pedido es de otra compra."),
    "cuerpo_demasiado_grande": (413, "el pedido supera el tamaño máximo."),
    "pedido_ilegible": (400, "el pedido no es un JSON-RPC legible."),
    "estado_no_disponible": (503, "el estado del gateway no está disponible; reintentá."),
}
_CODIGO_ERROR_A2A = -32001

# Etapa 2 (docs/FLUJO.md). Textos fijos: nada del comercio ni del LLM.
_PREGUNTA_POR_DEFECTO = (
    "¿Para qué necesitás esto? ¿Hay algo que pueda variar, como la cantidad, "
    "las fechas o la marca?"
)
_MOTIVO_FILTRO = (
    "El filtro de seguridad detectó contenido peligroso en la respuesta del "
    "proveedor: la conversación se bloquea y no se reenvía."
)
_MOTIVO_FILTRO_FALLO = (
    "El filtro de seguridad no pudo revisar la respuesta del proveedor: "
    "no se reenvía."
)
_TEXTO_PAUSA = (
    "KaiAI: en pausa. Hay una alternativa esperando la decisión del "
    "usuario y no respondió: sin su autorización no se avanza ni se compra."
)
_TEXTO_PARAMETROS = (
    "KaiAI: el usuario autorizó nuevos parámetros para esta compra "
    "({requisitos}). La oferta {cart_id} los cumple: seguí la compra con esos "
    "parámetros."
)
_MOTIVO_FILTRO_ILEGIBLE = (
    "La respuesta del proveedor no se pudo leer y el filtro de seguridad no "
    "puede revisarla: no se reenvía."
)
_MOTIVO_YA_BLOQUEADA = (
    "Esta conversación ya se bloqueó por seguridad: no se reenvía nada más "
    "del proveedor."
)
_MOTIVO_CONTEXTO_AJENO = (
    "La respuesta del proveedor usa el contexto de otra compra: no se reenvía."
)
# El agente del cliente espera hasta 600 s por llamada (DEFAULT_TIMEOUT del
# sample) y las dos esperas caen en la misma: presupuesto con margen.
_PRESUPUESTO_LLAMADA_S = 570.0
_MARGEN_S = 20.0  # para el LLM y la respuesta después de la última espera
# Una repregunta del intake solo si queda al menos esto de la llamada (si
# no, sigue con lo que tiene: la consulta de una oportunidad también espera).
_MINIMO_REPREGUNTA_S = 30.0
# La taxonomía (unas 3 llamadas cortas) solo si queda al menos esto.
_MINIMO_TAXONOMIA_S = 10.0
_TIMEOUT_LLM_S = 45.0
# Otra búsqueda de la misma compra espera a la que está preguntando lo que le
# queda a su espera más esto (el LLM de después y la respuesta), no el
# presupuesto entero de la llamada.
_MARGEN_ESPERA_SECUNDARIA_S = _TIMEOUT_LLM_S + _MARGEN_S
_TIMEOUT_FILTRO_S = 30.0
_TIMEOUT_FABRICANTE_S = 15.0  # todas las consultas de una compra (Icecat: 3 s cada una)
# Hilos propios: el filtro en uno y el LLM y el fabricante en otro. `wait_for`
# corta la espera, no el hilo: con el pool compartido de asyncio, unos LLM
# colgados dejaban al filtro (lo primero de cada respuesta) sin hilos.
HILOS_FILTRO = 16
HILOS_LLM = 16
MAX_INTAKES = 200  # estados de intake en memoria (como las compras)

AccionEtapa2 = Literal["pasar", "con_parametros", "vencida", "bloqueada", "contexto_ajeno"]

Accion = Literal["pasar", "retener", "bloquear"]


def _filtrar(headers: Any, excluidos: set[str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in excluidos}


def _ficha_verificable(
    texto: str, requisitos: list[Requisito], categoria_taxonomia: str | None = None
) -> Ficha | None:
    """La ficha del agente con lo que el Guard sabe chequear (None si nada)."""
    from intencion.agente_decisiones import requisitos_verificables

    verificables, _ = requisitos_verificables(list(requisitos))
    if not verificables:
        return None
    return Ficha(
        pedido_original=texto,
        requisitos=verificables,
        confirmada=True,
        categoria_taxonomia=categoria_taxonomia,
    )


def _con_titulo_publico(oportunidad: Any, ofertas: list[Any]) -> Any:
    """La oportunidad con el título público de su oferta, o sin título."""
    from intencion.agente import titulo_publico

    elegida = next((o for o in ofertas if o.cart_id == oportunidad.cart_id), None)
    titulo = titulo_publico(elegida) if elegida is not None else ""
    return oportunidad.model_copy(update={"titulo": titulo})


@dataclass
class EstadoIntake:
    """El intake de una compra: qué turno, qué falta y cómo terminó."""

    turno: int = 0
    confirmar: bool = False  # el turno actual pide confirmar la ficha
    resultado: Any = None  # intencion.intake.ResultadoIntake (None sin intake)
    final: bool = False
    confirmada: bool | None = None
    ficha_autorizada: list[Requisito] | None = None
    sin_verificar_guard: list[Requisito] | None = None
    categoria_taxonomia: str | None = None  # taxonomía de Google (si hay cliente)

    def intake_json(self) -> dict[str, Any]:
        r = self.resultado
        if r is None:
            return {}
        return {
            "faltan": list(r.faltan),
            "a_confirmar": list(r.a_confirmar),
            "ficha_parcial": [p.a_json() for p in r.parametros],
            "cortado": r.cortado,
            "mensaje": r.mensaje,
            "fuera_de_catalogo": r.fuera_de_catalogo,
        }

    def cierre_json(self) -> dict[str, Any]:
        return {
            "final": self.final,
            "confirmada": self.confirmada,
            "ficha_autorizada": [q.model_dump(mode="json") for q in self.ficha_autorizada or []],
            "sin_verificar_guard": [
                q.model_dump(mode="json") for q in self.sin_verificar_guard or []
            ],
            "categoria_taxonomia": self.categoria_taxonomia,
        }

    def a_json(self) -> dict[str, Any]:
        datos: dict[str, Any] = {"turno": self.turno, "confirmar": self.confirmar}
        datos.update(self.intake_json())
        if self.final:
            datos.update(self.cierre_json())
        return datos


def _del_comercio(headers: Any) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() in _DEL_COMERCIO_AL_CLIENTE}


def _recordar(cache: OrderedDict, clave: Any, valor: Any, maximo: int = _MAX_CONTEXTOS) -> None:
    cache[clave] = valor
    cache.move_to_end(clave)
    while len(cache) > maximo:
        cache.popitem(last=False)


def comercio_firmado(checkout_jwt: Any) -> str | None:
    """`merchant.id` del checkout que firmó el comercio (None si no se lee).

    Es la identidad del comercio que usa el chequeo de política: viene firmada,
    a diferencia del catálogo público, que puede no existir. Solo se lee.
    """
    _, payload = a2a.decodificar_jwt(checkout_jwt)
    merchant = payload.get("merchant") if isinstance(payload, dict) else None
    comercio = merchant.get("id") if isinstance(merchant, dict) else None
    return comercio if isinstance(comercio, str) and comercio else None


@dataclass(frozen=True)
class OfertasGuardadas:
    carts: list[dict[str, Any]]
    offer_details: list[dict[str, Any]]
    escenario: str | None
    # El comercio (id de ruta) que las respondió: un comercio no puede pisar
    # ni usar las ofertas de otro devolviendo su contextId.
    comercio_id: str | None = None


@dataclass(frozen=True)
class Decision:
    """Qué hacer con un checkout.

    `veredicto` es None cuando el proxy no pudo evaluar (fail-closed): ahí el
    status es `bloqueado` y el motivo es un texto fijo del proxy.
    """

    accion: Accion
    status: str
    motivo: str
    cart_id: str | None
    contexto_id: str | None
    veredicto: Veredicto | None = None
    # (task, huella del data part ap2.checkout completo, cart): ver a2a.huella_checkout
    clave: tuple[str | None, str, str | None] | None = None
    centavos: int | None = None
    moneda: str | None = None
    por_aprobar: bool = False  # escalar con una aprobación humana disponible
    aprobacion: Aprobacion | None = None  # la que se usó para dejarlo pasar
    via_api: bool = False  # compra de integrador: la aprobación va por /v1
    # Compra de integrador: solo vale una aprobación creada por /v1.
    solo_api: bool = False
    # La aprobación ya reclamada en el almacén durable: se usa esa, aunque
    # otro checkout concurrente la haya sacado de memoria mientras tanto.
    reclamada: Aprobacion | None = None

    @property
    def retiene(self) -> bool:
        return self.accion != "pasar"

    @property
    def requiere_aprobacion(self) -> bool:
        return self.accion == "retener"

    @property
    def texto(self) -> str:
        base = f"KaiAI: {self.status}. {self.motivo}"
        if self.accion == "retener":
            return f"{base} {_SUFIJO_RETENIDO_API if self.via_api else _SUFIJO_RETENIDO}"
        if self.accion == "bloquear":
            return f"{base} {_SUFIJO_BLOQUEADO}"
        return base

    def veredicto_json(self) -> dict[str, Any]:
        if self.veredicto is not None:
            return self.veredicto.model_dump(mode="json")
        # Sin veredicto del Guard: el agente igual necesita el motivo.
        return Veredicto(
            status="bloqueado",
            cart_id_elegido=self.cart_id or "",
            cart_id_recomendado=None,
            ranking=[],
            chequeos=[],
            motivo=self.motivo,
        ).model_dump(mode="json")

    def artefacto(self) -> dict[str, Any]:
        """El artefacto para el agente: versión reducida, sin texto del
        comercio (N5). El completo va al dashboard en el evento `veredicto`."""
        return a2a.artefacto_veredicto(a2a.veredicto_para_agente(self.veredicto_json()))


@dataclass
class Llamada:
    """Un pedido JSON-RPC en curso."""

    pedido: a2a.Pedido
    guard_activo: bool  # una sola lectura del interruptor por pedido
    busqueda_emitida: bool = False
    # Etapa 2: solo find_products con el Guard prendido. En el tráfico de
    # integrador, la compra del X-Compra-Id en todo pedido.
    compra: Compra | None = None
    inicio: float = field(default_factory=time.monotonic)  # presupuesto de las esperas
    # Gateway: a qué comercio va (en `rpc` siempre se completa), quién llama
    # (None = anónimo, la demo) y por qué oyente entró.
    comercio: Comercio | None = None
    integrador: Integrador | None = None
    oyente: str = "local"

    @property
    def de_integrador(self) -> bool:
        return self.integrador is not None


@dataclass
class EstadoStream:
    ofertas: a2a.Respuesta = field(default_factory=a2a.Respuesta)
    ofertas_registradas: bool = False
    task_id: str | None = None
    contexto_id: str | None = None
    task_vista: bool = False
    checkouts: int = 0
    decision: Decision | None = None
    pendiente: list[bytes] | None = None  # eventos retenidos hasta el final
    cortado: bool = False
    resuelto: bool = False


class ProxyGuard:
    def __init__(
        self,
        config: Config,
        client: httpx.AsyncClient,
        evaluador: EvaluadorCargado,
        ping_segundos: float = 15.0,
        api_token: str | None = None,
        persistencia: Persistencia | None = None,
        agente: Any = None,
        filtro: Filtro | None = None,
        fabricante: Any = None,
        integradores: RegistroIntegradores | None = None,
        comercios: RegistroComercios | None = None,
        almacen: AlmacenEstado | None = None,
        despachador: Despachador | None = None,
        filtro_modo: str | None = None,
    ) -> None:
        # Fail-closed: sin un evaluar() que ate el checkout (o con el stub sin
        # PROXY_ALLOW_STUB=1) el proxy no se arma.
        validar_evaluador(evaluador, config.permitir_stub)
        self.config = config
        self.client = client
        self.evaluador = evaluador
        self.guard_activo = config.guard_activo
        self.bus = BusDeEventos(
            config.events_jsonl,
            max_suscriptores=config.max_suscriptores_sse,
            # Los de compras de integrador, aparte: el principal lo sirve el dashboard.
            jsonl_privado=config.jsonl_privado,
        )
        self.ping_segundos = ping_segundos
        # Secreto de los POST de /guard/* (N4): uno por arranque.
        self.api_token = api_token or generar_token()
        self.aprobaciones = RegistroAprobaciones()
        self.persistencia = persistencia
        if persistencia is not None and persistencia.al_fallar is None:
            persistencia.al_fallar = lambda mensaje: self._emitir(
                "error", {"mensaje": mensaje}, guard_activo=self.guard_activo
            )
        self._ofertas: OrderedDict[str, OfertasGuardadas] = OrderedDict()
        self._decisiones: OrderedDict[str, Decision] = OrderedDict()
        self._ficha: Ficha | None = None
        # Etapa 2: agente de intención (intencion.Agente) y filtro de seguridad
        # (contracts.Filtro). Los arma `python -m proxy` desde el entorno; sin
        # agente no hay pregunta ni oportunidades; sin filtro no hay corte.
        self.agente = agente
        self.filtro = filtro
        # Lo que dice `describir()` del módulo del filtro (solo informa).
        self.filtro_modo = filtro_modo if filtro is not None else None
        # Los cierra el lifespan de `create_app`. Públicos: `/v1` sin estado
        # (`sin_estado`) también corre el filtro y el fabricante en estos.
        self.ejecutor_filtro = ThreadPoolExecutor(HILOS_FILTRO, thread_name_prefix="kaiai-filtro")
        self.ejecutor_llm = ThreadPoolExecutor(HILOS_LLM, thread_name_prefix="kaiai-llm")
        # Fuente del fabricante por GTIN (fabricante.FuenteFabricante): la arma
        # `python -m proxy` (FABRICANTE_FUENTE, por defecto el fixture). Sin
        # fuente, lo obligatorio que solo declara el comercio escala.
        self.fabricante = fabricante
        # Con el TTL: una compra de integrador viva no sale de memoria.
        self.compras = RegistroCompras(ttl_s=config.compra_ttl_s)
        # Intake (agentes con `evaluar_intake`): estado por compra y las
        # compras cuya ficha confirmó el usuario (la API resuelve la espera).
        self._intakes: OrderedDict[str, EstadoIntake] = OrderedDict()
        self._confirmaciones: set[str] = set()
        # Lo que escribió el usuario en /demo, para el intake de la próxima
        # compra (proxy.pedido_usuario; solo por la API de control).
        self.pedido_usuario = PedidoDelUsuario()
        # Gateway de producción (diseño del gateway). Sin configuración nueva:
        # sin integradores (sin oyente público), un solo comercio (el de
        # siempre), almacén en memoria y sin webhooks: todo como antes.
        self.integradores = integradores or RegistroIntegradores.vacio()
        self.comercios = comercios or RegistroComercios.solo(
            comercio_por_defecto(
                id=config.comercio_por_defecto_id,
                a2a_url=config.merchant_upstream_url,
                catalogo_url=config.catalogo_url,
                respaldo_fixture=config.catalogo_respaldo_fixture,
            )
        )
        # Ojo: `estado()` es el resumen de /guard/state; el almacén es otra cosa.
        self.almacen: AlmacenEstado = almacen if almacen is not None else EstadoMemoria()
        self.despachador = despachador
        # Candado y escrituras en vuelo por compra (`_escribir_compra`).
        self._escrituras_compra: dict[str, list[Any]] = {}
        # Lo que es de compras de integrador, para no mostrarlo en los GET de
        # /guard/* sin el token (`_visible`): los ids de sus eventos y los
        # contextos que se ataron a una (aunque la compra salga de memoria).
        self._eventos_privados: OrderedDict[str, bool] = OrderedDict()
        self._contextos_api: OrderedDict[str, bool] = OrderedDict()
        self.en_fondo = TareasEnFondo(
            al_fallar=lambda mensaje: self._emitir(
                "error", {"mensaje": mensaje}, guard_activo=self.guard_activo
            )
        )
        if evaluador.nombre == "stub":
            logger.warning(
                "EVALUADOR STUB (PROXY_ALLOW_STUB=1): los veredictos NO son del "
                "Guard real; /guard/state y cada evento veredicto lo marcan."
            )
        if config.demo_hoy is not None:
            logger.warning(
                "DEMO_HOY=%s: el agente de intención toma esa fecha como hoy "
                "(el Guard sigue con el reloj de verdad).",
                config.demo_hoy.isoformat(),
            )

    # -- utilidades ----------------------------------------------------------

    def hoy(self) -> date:
        """El "hoy" del agente de intención: `DEMO_HOY` si está fijado (las
        ofertas de la demo tienen fechas fijas) y si no, el del reloj."""
        return self.config.demo_hoy or date.today()

    async def _en_hilo(
        self, ejecutor: ThreadPoolExecutor, funcion: Callable[..., Any], *args: Any
    ) -> Any:
        """Como `asyncio.to_thread` (con el contexto), pero en un pool propio.
        Si la espera se cancela antes de que tome un hilo, no corre."""
        contexto = contextvars.copy_context()
        return await asyncio.get_running_loop().run_in_executor(
            ejecutor, functools.partial(contexto.run, funcion, *args)
        )

    def cerrar_hilos(self) -> None:
        """Al apagar: sin esperar a los que siguen corriendo (un LLM colgado)
        y descartando los que no empezaron."""
        for ejecutor in (self.ejecutor_filtro, self.ejecutor_llm):
            ejecutor.shutdown(wait=False, cancel_futures=True)

    def _emitir(
        self,
        tipo: str,
        datos: dict[str, Any],
        *,
        guard_activo: bool,
        contexto_id: str | None = None,
        escenario: str | None = None,
        llamada: Llamada | None = None,
    ) -> Evento:
        privado = self._es_de_integrador_evento(datos, contexto_id, llamada)
        evento = self.bus.emitir(
            tipo,
            datos,
            guard_activo=guard_activo,
            contexto_id=contexto_id,
            escenario=escenario,
            privado=privado,  # al JSONL privado, no al que sirve el dashboard
        )
        if privado:
            _recordar(self._eventos_privados, evento.id, True, _MAX_MARCAS_PRIVADAS)
        if self.persistencia is not None:
            self.persistencia.guardar_evento(evento)  # solo los `veredicto`
        self._notificar_webhook(evento)
        return evento

    def _es_de_integrador_evento(
        self, datos: Any, contexto_id: str | None, llamada: Llamada | None
    ) -> bool:
        """¿El evento es de una compra de integrador? Lo es si sale de su
        tráfico, si trae su `compra_id` o si su contexto se ató alguna vez a
        una (aunque la compra ya no esté en memoria)."""
        if llamada is not None and llamada.de_integrador:
            return True
        if contexto_id and contexto_id in self._contextos_api:
            return True
        compra_id = datos.get("compra_id") if isinstance(datos, dict) else None
        compra = self.compras.get(compra_id) if isinstance(compra_id, str) else None
        compra = compra or self.compras.de_contexto(contexto_id)
        return compra is not None and compra.origen == "api"

    def _con_token(self, request: Request) -> bool:
        """El pedido trae el token de la API de control (`X-Guard-Token`)."""
        recibido = request.headers.get(HEADER_TOKEN, "").encode("utf-8", "replace")
        return hmac.compare_digest(recibido, self.api_token.encode("utf-8"))

    def _visible(self, evento: Evento, request: Request) -> bool:
        """Los eventos de compras de integrador solo salen con el token: el
        dashboard puede estar publicado (deploy/tenki.sh) y sus GET se
        reenvían sin autenticar. /v1 aísla a cada integrador; esto también."""
        return evento.id not in self._eventos_privados or self._con_token(request)

    def _notificar_webhook(self, evento: Evento) -> None:
        """El webhook del integrador dueño de la compra del evento (§1.6).

        Solo compras de API con despachador. El cuerpo es la `VistaCompra` del
        momento (la misma que da el GET): nada del vendedor que el GET no
        muestre. Nunca lanza: un webhook que no sale no corta la compra.
        """
        if self.despachador is None:
            return
        try:
            tipo = tipo_webhook(evento)
            if tipo is None:
                return
            compra_id = evento.datos.get("compra_id") if isinstance(evento.datos, dict) else None
            compra = (
                self.compras.get(compra_id) if isinstance(compra_id, str) else None
            ) or self.compras.de_contexto(evento.contexto_id)
            if compra is None or compra.origen != "api" or not compra.integrador_id:
                return
            self.despachador.notificar(
                compra.integrador_id,
                tipo,
                vistas.vista_compra(self.foto(compra), a2a_url=self.url_a2a(compra.comercio_id)),
            )
        except Exception:  # noqa: BLE001 - el webhook nunca rompe el flujo
            logger.exception("No se pudo programar el webhook del evento %s", evento.tipo)

    # -- gateway: compras de integrador -----------------------------------------

    def url_a2a(self, comercio_id: str | None) -> str | None:
        """La URL A2A pública de un comercio (la que usa el integrador)."""
        if not comercio_id:
            return None
        try:
            return url_publica_comercio(self.config.gateway_public_url, comercio_id)
        except ValueError:
            return None

    def foto(self, compra: Compra) -> FotoCompra:
        """La foto de una compra para /v1 y los webhooks: sus contextos y las
        aprobaciones (pendientes y usadas) de esos contextos."""
        contextos = self.compras.contextos_de(compra.id)
        listado = self.aprobaciones.listar()
        aprobaciones = [
            a
            for a in [*listado["pendientes"], *listado["usadas"]]
            if a.get("contexto_id") in contextos
        ]
        return compra.foto(contextos=contextos, aprobaciones=aprobaciones)

    def guardar_compra(self, compra: Compra, *, tocar: bool = True) -> None:
        """Solo compras de API: marca el cambio y la guarda en segundo plano
        (si se pierde y hay un reinicio, falta el dato y se bloquea).

        El paso a un estado terminal (`bloqueada`, `vencida`) no se descarta
        por el tope de escrituras en vuelo: perderlo haría revivir la compra
        después de un reinicio. `tocar=False` guarda sin mover `actualizada`
        (una normalización al leer, no un cambio de la compra)."""
        if compra.origen != "api":
            return
        if tocar:
            compra.tocar()
        self.en_fondo.programar(
            self._escribir_compra(compra),
            "compra",
            obligatoria=compra.estado in ESTADOS_TERMINALES_API,
        )

    async def _escribir_compra(self, compra: Compra) -> None:
        """Las escrituras de una misma compra, de a una y en orden (el
        `asyncio.Lock` es FIFO), cada una con la fila **del momento en que
        escribe**: en PostgREST gana el upsert que llega último, así que dos
        en vuelo podían pisar `bloqueada` con un `negociando` más viejo. Así
        la última escritura es siempre el estado más nuevo."""
        par = self._escrituras_compra.get(compra.id)
        if par is None:
            par = self._escrituras_compra[compra.id] = [asyncio.Lock(), 0]
        par[1] += 1
        try:
            async with par[0]:
                await self.almacen.guardar_compra(compra.a_fila())
        finally:
            par[1] -= 1
            if par[1] == 0:
                self._escrituras_compra.pop(compra.id, None)

    def _compra_api(self, llamada: Llamada | None, contexto_id: str | None) -> Compra | None:
        """La compra de integrador de esa llamada o de ese contexto (o None)."""
        compra = llamada.compra if llamada is not None else None
        if compra is None or compra.origen != "api":
            compra = self.compras.de_contexto(contexto_id)
        return compra if compra is not None and compra.origen == "api" else None

    async def hidratar_compra(self, compra_id: str) -> Compra | None:
        """La compra de memoria o, si no está, la del almacén (restaurada en
        memoria con las esperas cerradas del lado seguro). `ErrorAlmacen` si
        el almacén falla: quien llama responde 503 y no sigue."""
        compra = self.compras.get(compra_id)
        if compra is not None:
            return compra
        fila = await self.almacen.cargar_compra(compra_id)
        if fila is None:
            return None
        compra = Compra.desde_fila(fila)
        restaurada = self.compras.restaurar(compra)
        if restaurada is compra and compra.estado != fila.get("estado"):
            # `desde_fila` cerró una espera (reinicio): la fila también, así
            # el listado por estado y el conteo de activas la ven igual. Sin
            # tocar `actualizada`: leer la compra no es un cambio.
            self.guardar_compra(compra, tocar=False)
        return restaurada

    async def atar_contexto_api(
        self, compra: Compra, contexto_id: Any, comercio_id: str
    ) -> Literal["ok", "ajeno", "error"]:
        """Ata un contextId a una compra de integrador (§1.3): en memoria y en
        el almacén (gana el primero). "ajeno" si es de otra compra, si no
        tiene un formato seguro o si la compra ya tiene el máximo de
        contextos; "error" si el almacén falló."""
        if not es_id(ID_SEGURO_RE, contexto_id):
            return "ajeno"
        dueno = self.compras.de_contexto(contexto_id)
        if dueno is not None:
            return "ok" if dueno.id == compra.id else "ajeno"
        if len(self.compras.contextos_de(compra.id)) >= MAX_CONTEXTOS_POR_COMPRA:
            return "ajeno"
        try:
            dueno_id = await self.almacen.atar_contexto(contexto_id, compra.id, comercio_id)
        except ErrorAlmacen as e:
            logger.error("No se pudo atar el contexto de la compra %s: %s", compra.id, e)
            return "error"
        if dueno_id != compra.id or not self.compras.atar(compra, contexto_id):
            return "ajeno"
        _recordar(self._contextos_api, contexto_id, True, _MAX_MARCAS_PRIVADAS)
        return "ok"

    def _escenario(self, contexto_id: str | None) -> str | None:
        guardadas = self._ofertas.get(contexto_id) if contexto_id else None
        return guardadas.escenario if guardadas else None

    def _error(self, llamada: Llamada, mensaje: str, contexto_id: str | None = None) -> None:
        logger.error(mensaje)
        contexto_id = contexto_id or llamada.pedido.contexto_id
        self._emitir(
            "error",
            {"mensaje": mensaje},
            guard_activo=llamada.guard_activo,
            contexto_id=contexto_id,
            escenario=llamada.pedido.escenario or self._escenario(contexto_id),
            llamada=llamada,
        )

    def ficha(self) -> Ficha:
        """La ficha global de la demo (el pedido de zapatillas de scenarios.json).

        Con `PROXY_FICHA_DEMO=0` (como un comercio real) no hay ficha de la
        demo: una vacía y sin confirmar, sin parámetros autorizados, con la que
        el Guard escala (nunca aprueba sin saber qué autorizó el usuario).
        """
        if self._ficha is None:
            self._ficha = (
                ficha_del_intent(cargar_intent())
                if self.config.ficha_demo
                else Ficha(pedido_original="", requisitos=[], confirmada=False)
            )
        return self._ficha

    @property
    def gateway_configurado(self) -> bool:
        """¿Hay algo del gateway de producción configurado? (claves, varios
        comercios o un almacén que no es el de memoria por defecto)."""
        return (
            self.integradores.hay_claves()
            or len(self.comercios.todos()) > 1
            or self.almacen.nombre != "memoria"
            or self.despachador is not None
        )

    def estado(self, *, con_token: bool) -> dict[str, Any]:
        datos = {
            "guard_activo": self.guard_activo,
            "merchant_url": self.config.merchant_upstream_url,
            "evaluador": self.evaluador.nombre,
            "intencion": getattr(self.agente, "nombre", "agente") if self.agente else None,
            "filtro": self.filtro is not None,
            "filtro_modo": self.filtro_modo,
        }
        if self.gateway_configurado:
            # Solo con el gateway configurado: sin configuración nueva, el
            # /guard/state es el mismo de siempre (lo leen el dashboard y sus tests).
            gateway: dict[str, Any] = {
                "almacen": self.almacen.nombre,
                "publico": self.integradores.hay_claves(),
            }
            if con_token:
                # Sin token (el GET del dashboard publicado se reenvía sin
                # autenticar) no se enumeran los comercios ni los integradores.
                gateway = {
                    "integradores": len(self.integradores.todos()),
                    "comercios": [c.id for c in self.comercios.todos()],
                    **gateway,
                }
            datos["gateway"] = gateway
        return datos

    def ficha_de(self, contexto_id: str | None) -> Ficha:
        """Los parámetros autorizados de esa compra (etapa 2) o, si no hay
        compra con parámetros, la ficha de la demo: la del escenario de esa
        compra (con sus atributos) y, si no se conoce, la global."""
        escenario = self._escenario(contexto_id)
        compra = self.compras.de_contexto(contexto_id)
        if compra is not None:
            ficha = compra.resolver_ficha(self._ficha_confirmada(escenario))
            if ficha is not None:
                return ficha
        return self._ficha_confirmada(escenario) or self.ficha()

    def _ficha_autorizada(self, llamada: Llamada | None, contexto_id: str | None) -> Ficha | None:
        """Los parámetros con los que se evalúa un checkout.

        Con una compra de integrador, solo los que autorizó el integrador:
        nunca la ficha de la demo, que elige el `demo_scenario` que declara
        el comercio. None si el tráfico es de integrador y no hay compra o
        ficha: quien llama bloquea (fail-closed)."""
        compra = self._compra_api(llamada, contexto_id)
        if compra is not None:
            return compra.ficha if compra.ficha_resuelta else None
        if llamada is not None and llamada.de_integrador:
            return None
        return self.ficha_de(contexto_id)

    # -- Agent Card ----------------------------------------------------------

    def _comercio_de(self, request: Request) -> Comercio | None:
        """El comercio de la ruta: `/a2a/m/{comercio}` o, en la ruta vieja, el
        por defecto. Un id que no está en el registro → None (404): el
        upstream sale siempre del registro, nunca de una URL libre."""
        return self.comercios.get(request.path_params.get("comercio"))

    def _permitido(self, integrador: Integrador | None, comercio: Comercio) -> bool:
        if integrador is None:
            return True  # tráfico anónimo del oyente local (la demo)
        return comercio in self.comercios.permitidos(integrador)

    def _url_publica_card(self, request: Request, comercio: Comercio) -> str:
        """La URL que la Agent Card reescrita le da al cliente: la del oyente
        por el que entró (si no, el cliente A2A saltearía el proxy)."""
        if _oyente(request) == "publico":
            ruta = request.url.path
            sufijo = "/" + request.path_params["resto"]
            if ruta.endswith(sufijo):
                ruta = ruta[: -len(sufijo)]
            return f"{self.config.gateway_public_url}{ruta}"
        if "comercio" not in request.path_params:
            return self.config.proxy_public_url  # ruta vieja: igual que siempre
        return url_publica_comercio(self.config.base_publica_local, comercio.id)

    async def card(self, request: Request) -> Response:
        comercio = self._comercio_de(request)
        integrador = _integrador(request)
        if comercio is None and integrador is None:
            return JSONResponse({"error": "comercio_desconocido"}, status_code=404)
        # Con integrador, el mismo 403 si no existe o no está permitido (como
        # /v1): no se dice qué comercios tiene registrados el operador.
        if comercio is None or not self._permitido(integrador, comercio):
            return JSONResponse({"error": "comercio_no_permitido"}, status_code=403)
        resto = request.path_params["resto"]
        if resto not in RUTAS_CARD:
            return JSONResponse({"error": "no_encontrado"}, status_code=404)
        url = f"{comercio.a2a_url}/{resto}"
        try:
            async with self.client.stream(
                "GET", url, headers={"Accept": "application/json"}
            ) as resp:
                status = resp.status_code
                contenido = await a2a.leer_acotado(
                    resp.aiter_bytes(), self.config.max_respuesta_bytes
                )
        except (httpx.HTTPError, a2a.LimiteExcedido) as e:
            mensaje = f"El comercio no entrega la Agent Card ({type(e).__name__}: {e})"
            logger.error(mensaje)
            self._emitir("error", {"mensaje": mensaje}, guard_activo=self.guard_activo)
            return JSONResponse({"error": "comercio_no_responde"}, status_code=502)
        if status != 200:
            return JSONResponse(
                {"error": "card_no_disponible", "status_comercio": status}, status_code=502
            )
        card, _ = _leer_del_comercio(contenido)
        if not isinstance(card, dict):
            return JSONResponse({"error": "card_ilegible"}, status_code=502)
        # Con un integrador el Guard está siempre prendido (como en `rpc`).
        activo = True if integrador is not None else self.guard_activo
        if activo and self.filtro is not None and not await self._card_pasa(
            card, comercio, integrador
        ):
            return JSONResponse({"error": "card_bloqueada"}, status_code=502)
        publica = self._url_publica_card(request, comercio)
        logger.info("Agent Card: %s → %s", card.get("url"), publica)
        # Solo el JSON reescrito: ningún header del comercio (Set-Cookie, etc.).
        return JSONResponse(a2a.reescribir_card(card, publica))

    async def _card_pasa(
        self, card: dict[str, Any], comercio: Comercio, integrador: Integrador | None
    ) -> bool:
        """La Agent Card también es texto del comercio que lee el agente
        (nombre, descripciones, skills): pasa por el filtro antes de llegarle.
        Si el filtro la corta, falla o tarda de más, no se entrega (y queda un
        evento `error` con un motivo fijo, nada de la card)."""
        llamada = Llamada(
            pedido=a2a.Pedido(), guard_activo=True, comercio=comercio, integrador=integrador
        )
        try:
            if (await self._correr_filtro(card)).pasa:
                return True
            motivo = "La Agent Card del comercio no pasó el filtro de seguridad: no se entrega."
        except Exception as e:  # noqa: BLE001 - fail-closed
            logger.exception("El filtro de seguridad falló con la Agent Card")
            motivo = (
                f"El filtro de seguridad no pudo revisar la Agent Card ({type(e).__name__}): "
                "no se entrega."
            )
        self._error(llamada, motivo)
        return False

    # -- JSON-RPC ------------------------------------------------------------

    async def rpc(self, request: Request) -> Response:
        comercio = self._comercio_de(request)
        integrador = _integrador(request)
        if comercio is None and integrador is None:
            return _error_a2a(None, "comercio_desconocido")
        # Con integrador, el mismo 403 si no existe o no está permitido (como
        # /v1): no se dice qué comercios tiene registrados el operador.
        if comercio is None or not self._permitido(integrador, comercio):
            return _error_a2a(None, "comercio_no_permitido")
        if integrador is not None:
            # Tráfico de integrador: el cuerpo se lee con tope (413).
            try:
                raw = await a2a.leer_acotado(request.stream(), self.config.max_cuerpo_a2a)
            except a2a.LimiteExcedido:
                return _error_a2a(None, "cuerpo_demasiado_grande")
            if not _pedido_legible(raw):
                # Lo que el proxy no lee en estricto no se reenvía: el comercio
                # (`json.loads`) sí lo leería, y el `contextId` quedaría sin
                # chequear contra la compra (§1.2).
                return _error_a2a(None, "pedido_ilegible")
        else:
            raw = await request.body()
        llamada = Llamada(
            pedido=a2a.leer_pedido(raw),
            # El interruptor de /guard/state es de la demo: con un integrador
            # el Guard está siempre prendido.
            guard_activo=True if integrador is not None else self.guard_activo,
            comercio=comercio,
            integrador=integrador,
            oyente=_oyente(request),
        )
        pedido = llamada.pedido
        if integrador is None:
            # Tráfico anónimo (la demo): nunca en el contexto de una compra de
            # integrador. Lo adoptaría (su ficha, sus ofertas) y podría
            # bloquearla. No se reenvía ni se emite nada.
            ajeno = await self._contexto_es_de_integrador(pedido.contexto_id)
            if ajeno is None:
                return _error_a2a(pedido.rpc_id, "estado_no_disponible")
            if ajeno:
                return _error_a2a(pedido.rpc_id, "contexto_ajeno")
        if integrador is not None:
            error = await self._preparar_integrador(request, llamada)
            if error is not None:
                return error
        logger.info(
            "→ comercio %s: %s %s contexto=%s",
            comercio.id, pedido.metodo_rpc, pedido.tipo, pedido.contexto_id,
        )
        self._eventos_del_pedido(llamada)
        if llamada.de_integrador and pedido.tipo == "find_products":
            # La ficha la autorizó el integrador: sin intake, solo el porqué.
            await self._etapa_contexto_api(llamada)
        elif pedido.tipo == "find_products" and llamada.guard_activo and self.agente is not None:
            # Etapa 2, paso 1: antes de negociar, el porqué (retiene el pedido).
            await self._etapa_contexto(llamada)

        excluidos = _NO_HACIA_COMERCIO
        if llamada.de_integrador:
            # La clave del integrador y su compra nunca llegan al comercio.
            excluidos = _NO_HACIA_COMERCIO | {"authorization", HEADER_COMPRA.lower()}
        upstream = self.client.build_request(
            "POST",
            comercio.a2a_url,
            content=raw,  # los bytes tal cual: los mandatos no se tocan
            headers=_filtrar(request.headers, excluidos),
        )
        try:
            resp = await self.client.send(upstream, stream=True)
        except httpx.HTTPError as e:
            return self._sin_respuesta(
                llamada, "el comercio no respondió", f"{type(e).__name__}: {e}"
            )

        headers = _del_comercio(resp.headers)
        if "text/event-stream" in resp.headers.get("content-type", "").lower():
            return StreamingResponse(
                self._sse(resp, llamada),
                status_code=resp.status_code,
                headers={**headers, "Cache-Control": "no-cache"},
                media_type="text/event-stream",
                # Si el cliente se va a mitad del stream, Starlette cancela el
                # envío y el `finally` de `_sse` recién corre cuando el GC junta
                # el generador: la respuesta del comercio se cierra igual acá.
                background=BackgroundTask(resp.aclose),
            )
        try:
            contenido = await a2a.leer_acotado(
                resp.aiter_bytes(), self.config.max_respuesta_bytes
            )
        except a2a.LimiteExcedido as e:
            return self._respuesta_excedida(llamada, e)
        except httpx.HTTPError as e:
            return self._sin_respuesta(
                llamada, "la respuesta del comercio se cortó", f"{type(e).__name__}: {e}"
            )
        finally:
            await resp.aclose()
        cuerpo, status = await self._procesar_json(contenido, llamada, resp.status_code)
        return Response(cuerpo, status_code=status, headers=headers, media_type="application/json")

    async def _preparar_integrador(self, request: Request, llamada: Llamada) -> Response | None:
        """Antes de reenviar un pedido de integrador (§1.2 y §1.3).

        Valida el `X-Compra-Id`, hidrata la compra (memoria o almacén),
        chequea dueño, comercio, estado y vida, ata el `contextId` del pedido
        y, para un `create_checkout`, trae las ofertas del almacén si no están
        en memoria. Devuelve la respuesta de error (y no se reenvía nada) o
        None. Si el almacén falla, 503: fail-closed, nunca se sigue a ciegas.
        """
        pedido = llamada.pedido
        integrador = llamada.integrador
        comercio = llamada.comercio
        assert integrador is not None and comercio is not None
        compra_id = request.headers.get(HEADER_COMPRA)
        if not es_id(ID_COMPRA_RE, compra_id):
            return _error_a2a(pedido.rpc_id, "falta_compra")
        try:
            compra = await self.hidratar_compra(compra_id)
        except ErrorAlmacen as e:
            logger.error("Estado no disponible para la compra %s: %s", compra_id, e)
            return _error_a2a(pedido.rpc_id, "estado_no_disponible")
        if compra is None or compra.origen != "api" or compra.integrador_id != integrador.id:
            return _error_a2a(pedido.rpc_id, "compra_desconocida")  # sin filtrar que existe
        if compra.comercio_id != comercio.id:
            return _error_a2a(pedido.rpc_id, "comercio_distinto")
        if compra.estado in ESTADOS_TERMINALES_API:
            return _error_a2a(pedido.rpc_id, f"compra_{compra.estado}")
        if (datetime.now(timezone.utc) - compra.creada).total_seconds() > self.config.compra_ttl_s:
            return _error_a2a(pedido.rpc_id, "compra_vencida")
        if pedido.contexto_id is not None:
            atadura = await self.atar_contexto_api(compra, pedido.contexto_id, comercio.id)
            if atadura == "error":
                return _error_a2a(pedido.rpc_id, "estado_no_disponible")
            if atadura == "ajeno":
                return _error_a2a(pedido.rpc_id, "contexto_ajeno")
        contexto_id = pedido.contexto_id
        if pedido.tipo == "create_checkout" and contexto_id and contexto_id not in self._ofertas:
            # Después de un reinicio: las ofertas que vio el proxy en find_products.
            try:
                fila = await self.almacen.cargar_ofertas(contexto_id)
            except ErrorAlmacen as e:
                logger.error("No se pudieron leer las ofertas de %s: %s", contexto_id, e)
                return _error_a2a(pedido.rpc_id, "estado_no_disponible")
            guardadas = _ofertas_de_fila(fila)
            if guardadas is not None:
                _recordar(self._ofertas, contexto_id, guardadas)
        llamada.compra = compra
        return None

    async def _etapa_contexto_api(self, llamada: Llamada) -> None:
        """El porqué de una compra de integrador (§1.3), sin intake: la ficha
        ya la autorizó el integrador y no cambia.

        - con `intencion.porque`: se arma el contexto sin preguntar;
        - con `intencion.preguntar`: una pregunta (webhook `compra.pregunta`)
          y se retiene el pedido hasta la respuesta por /v1 o el plazo;
        - si no, o sin agente de intención: el pedido literal, sin
          oportunidades.

        Corre una sola vez por compra. Otra búsqueda que llega mientras tanto
        espera lo mismo (`contexto_listo`). Si el agente falla, sigue literal.
        """
        compra = llamada.compra
        assert compra is not None
        if compra.estado != "creada":
            # Si vence, sigue literal, como siempre (el Guard igual verifica).
            await self._esperar_a_la_otra(llamada, compra, compra.contexto_listo)
            return
        # Antes de cualquier await: otra búsqueda de la misma compra ya no
        # entra acá (espera `contexto_listo`).
        compra.estado = "negociando"
        pedido = llamada.pedido
        hoy = self.hoy()
        contexto = None
        try:
            if self.agente is None or not compra.intencion_activa:
                return
            if compra.porque:
                turnos = [Turno(pregunta=_PREGUNTA_POR_DEFECTO, respuesta=compra.porque)]
                contexto = await self._contexto_turno(llamada, compra.pedido, turnos, hoy, False)
                datos: dict[str, Any] = {
                    "compra_id": compra.id, "turno": 0, "respuesta": compra.porque,
                    "omitido": False, "vencido": False, "origen": "api",
                    "contexto": contexto.model_dump(mode="json") if contexto else None,
                }
            elif compra.preguntar_porque:
                try:
                    categorias = await self._categorias(llamada.comercio)
                    pregunta = await self._llm(
                        self.agente.preguntar, compra.pedido, hoy, categorias
                    )
                    texto = pregunta.pregunta if isinstance(pregunta.pregunta, str) else ""
                    texto = texto.strip() or _PREGUNTA_POR_DEFECTO
                except Exception as e:  # noqa: BLE001 - sin agente, la pregunta por defecto
                    logger.exception("El agente de intención no pudo armar la pregunta")
                    self._error(
                        llamada,
                        f"El agente de intención no pudo armar la pregunta ({type(e).__name__})",
                    )
                    texto = _PREGUNTA_POR_DEFECTO
                compra.nueva_espera(texto)
                self._emitir_pregunta(llamada, compra, EstadoIntake())
                self.guardar_compra(compra)
                respuesta, vencido = await compra.esperar_respuesta()
                if respuesta:
                    turnos = [Turno(pregunta=compra.pregunta, respuesta=respuesta)]
                    contexto = await self._contexto_turno(
                        llamada, compra.pedido, turnos, hoy, False
                    )
                datos = {
                    "compra_id": compra.id, "turno": 0, "respuesta": respuesta,
                    "omitido": respuesta is None and not vencido, "vencido": vencido,
                    "origen": "api",
                    "contexto": contexto.model_dump(mode="json") if contexto else None,
                }
            else:
                return
            self._emitir(
                "contexto_registrado",
                datos,
                guard_activo=True,
                contexto_id=pedido.contexto_id,
                escenario=pedido.escenario,
                llamada=llamada,
            )
        finally:
            # También si se cancela la llamada: ninguna compra queda colgada.
            if compra.estado == "esperando_contexto":
                compra.estado = "negociando"
            compra.contexto = contexto
            compra.contexto_listo.set()
            self.guardar_compra(compra)

    def _eventos_del_pedido(self, llamada: Llamada) -> None:
        pedido = llamada.pedido
        if pedido.tipo == "find_products" and pedido.contexto_id:
            self._emitir_busqueda(llamada, pedido.contexto_id)
        elif pedido.tipo == "create_checkout":
            self._emitir(
                "checkout_pedido",
                {"cart_id": pedido.cart_id},
                guard_activo=llamada.guard_activo,
                contexto_id=pedido.contexto_id,
                escenario=self._escenario(pedido.contexto_id),
                llamada=llamada,
            )

    def _emitir_busqueda(self, llamada: Llamada, contexto_id: str | None) -> None:
        """`busqueda_enviada` sale una vez, con el contextId del pedido o, si
        no lo traía, con el de la respuesta del comercio."""
        pedido = llamada.pedido
        if pedido.tipo != "find_products" or llamada.busqueda_emitida:
            return
        llamada.busqueda_emitida = True
        self._emitir(
            "busqueda_enviada",
            {"catalog_search": pedido.catalog_search},
            guard_activo=llamada.guard_activo,
            contexto_id=contexto_id,
            escenario=pedido.escenario,
            llamada=llamada,
        )

    def _es_de_checkout(self, llamada: Llamada) -> bool:
        return llamada.pedido.tipo == "create_checkout"

    def _sin_respuesta(self, llamada: Llamada, motivo: str, detalle: str) -> Response:
        """Red caída, timeout o stream cortado antes de tener una respuesta."""
        self._emitir_busqueda(llamada, None)
        if llamada.guard_activo and self._es_de_checkout(llamada):
            cuerpo = self._bloqueo_sintetico(llamada, f"{motivo}: no hay checkout que evaluar", detalle)
            return Response(cuerpo, status_code=200, media_type="application/json")
        mensaje = f"Proxy del Guard: {motivo} ({detalle})"
        self._error(llamada, mensaje)
        return JSONResponse(a2a.error_jsonrpc(llamada.pedido.rpc_id, mensaje), status_code=502)

    def _respuesta_excedida(self, llamada: Llamada, e: a2a.LimiteExcedido) -> Response:
        self._emitir_busqueda(llamada, None)
        motivo = f"la respuesta del comercio supera el tope de {e.limite} bytes"
        if llamada.guard_activo and (
            self._es_de_checkout(llamada)
            or llamada.pedido.es_de_tarea
            or a2a.CHECKOUT_KEY.encode() in e.parcial
        ):
            cuerpo = self._bloqueo_sintetico(llamada, motivo)
            return Response(cuerpo, status_code=200, media_type="application/json")
        mensaje = f"Proxy del Guard: {motivo}"
        self._error(llamada, mensaje)
        return JSONResponse(a2a.error_jsonrpc(llamada.pedido.rpc_id, mensaje), status_code=502)

    async def _procesar_json(
        self, contenido: bytes, llamada: Llamada, status: int
    ) -> tuple[bytes, int]:
        pedido, activo = llamada.pedido, llamada.guard_activo
        body, error = _leer_del_comercio(contenido)
        salida = contenido
        if error is None and isinstance(body, dict):
            try:
                # guard.veredicto solo lo pone el proxy.
                body, cambiado = a2a.quitar_veredictos_ajenos(body)
                salida = a2a.dumps(body) if cambiado else contenido
            except (ValueError, RecursionError) as e:  # defensa (N6): no se puede reescribir
                error = f"{type(e).__name__}: {e}"[:200]
        if error is not None or not isinstance(body, dict):
            detalle = error or f"se esperaba un objeto JSON y llegó {type(body).__name__}"
            return self._respuesta_ilegible(contenido, llamada, status, detalle)

        result = body.get("result")
        n_checkouts = a2a.contar_clave(body, a2a.CHECKOUT_KEY)

        if activo and self.filtro is not None:
            # Etapa 2, paso 2: la seguridad va primero, antes de toda otra etapa
            # (también sobre un error del comercio o un result que no es objeto).
            objetivo = result if isinstance(result, dict) else body
            corte = await self._filtrar(objetivo, llamada)
            if corte is not None:
                return self._cortar(
                    llamada, result if isinstance(result, dict) else {}, corte[0],
                    etapa="seguridad", senales=corte[1],
                ), 200

        if not await self._contexto_respuesta_ok(llamada, result):
            # El comercio respondió con el contexto de otra compra (o uno
            # ilegible): no se reenvía nada de esa respuesta.
            return self._cortar(
                llamada, {}, _MOTIVO_CONTEXTO_AJENO, etapa="seguridad"
            ), 200

        if "error" in body and "result" not in body:
            self._emitir_busqueda(llamada, None)
            self._emitir_error_comercio(body["error"], llamada)
            return salida, status

        kind = result.get("kind") if isinstance(result, dict) else None
        if kind != "task":
            contexto = result.get("contextId") if isinstance(result, dict) else None
            self._emitir_busqueda(llamada, contexto if isinstance(contexto, str) else None)
            if activo and (self._es_de_checkout(llamada) or n_checkouts):
                return self._bloqueo_sintetico(
                    llamada,
                    f"respuesta de tipo {kind or type(result).__name__} donde se "
                    "esperaba una tarea",
                ), 200
            self._emitir_reenviado(llamada, contexto if isinstance(contexto, str) else None)
            return salida, status

        contexto = result.get("contextId") if isinstance(result.get("contextId"), str) else None
        self._emitir_busqueda(llamada, contexto)
        if n_checkouts == 0:
            if pedido.tipo == "find_products":
                respuesta = a2a.Respuesta()
                respuesta.ingerir(result)
                if not self._registrar_ofertas(respuesta, llamada):
                    return self._cortar(
                        llamada, result, _MOTIVO_CONTEXTO_AJENO, etapa="seguridad"
                    ), 200
                if llamada.compra is not None:
                    # Etapa 2, pasos 3 a 6: ¿hay una oportunidad? (puede retener)
                    accion, nuevo = await self._etapa_oportunidad(llamada, respuesta, result)
                    if accion in ("vencida", "bloqueada", "contexto_ajeno"):
                        return self._cortar_etapa2(accion, llamada, result), 200
                    if accion == "con_parametros" and nuevo is not None:
                        self._emitir_reenviado(llamada, contexto)
                        return a2a.dumps({**body, "result": nuevo}), status
            self._emitir_reenviado(llamada, contexto)
            return salida, status

        decision = await self._decidir_tarea(result, llamada, n_checkouts)
        if decision is None:  # Guard apagado: passthrough
            self._emitir_reenviado(llamada, contexto, metodo="create_checkout")
            return salida, status
        decision = self._resolver(decision, result.get("id"))
        if decision.retiene:
            nuevo = a2a.tarea_bloqueada(result, decision.texto, decision.artefacto())
        else:
            nuevo = a2a.tarea_con_artefacto(result, decision.artefacto())
        try:
            cuerpo = a2a.dumps({**body, "result": nuevo})
        except (ValueError, RecursionError) as e:  # defensa (N6): nunca un 500 sin veredicto
            return self._bloqueo_sintetico(
                llamada, "la respuesta del comercio no se pudo reenviar",
                f"{type(e).__name__}: {e}"[:200],
            ), 200
        self._emitir_resultado(decision, llamada)
        return cuerpo, status

    def _respuesta_ilegible(
        self, contenido: bytes, llamada: Llamada, status: int, detalle: str
    ) -> tuple[bytes, int]:
        """JSON inválido, con NaN/Infinity o con números que desbordan (1e999).

        Si puede ser un checkout: bloqueo limpio (`error` + `bloqueado`). Si
        no: evento `error` y la respuesta tal cual, salvo que traiga un
        `guard.veredicto` (no se puede sacar de algo ilegible): ahí 502.
        """
        self._emitir_busqueda(llamada, None)
        if llamada.guard_activo and self.filtro is not None:
            # Etapa 2: lo que no se puede leer no se puede filtrar.
            self._error(llamada, f"Respuesta ilegible del comercio: {detalle}")
            return self._cortar(llamada, {}, _MOTIVO_FILTRO_ILEGIBLE, etapa="seguridad"), 200
        if llamada.guard_activo and (
            self._es_de_checkout(llamada) or a2a.menciona_checkout(contenido)
        ):
            return self._bloqueo_sintetico(
                llamada, "la respuesta del comercio no se pudo leer", detalle
            ), 200
        self._error(llamada, f"Respuesta ilegible del comercio: {detalle}")
        if a2a.menciona_clave(contenido, GUARD_VEREDICTO_KEY):
            error = a2a.error_jsonrpc(
                llamada.pedido.rpc_id, "Proxy del Guard: respuesta ilegible del comercio"
            )
            return a2a.dumps(error), 502
        return contenido, status

    # -- SSE -----------------------------------------------------------------

    def _bloque(self, llamada: Llamada, result: dict[str, Any]) -> bytes:
        return a2a.bloque_sse(a2a.respuesta_jsonrpc(llamada.pedido.rpc_id, result))

    async def _sse(self, resp: httpx.Response, llamada: Llamada) -> AsyncIterator[bytes]:
        est = EstadoStream()
        falla: str | None = None
        try:
            lineas = a2a.lineas_acotadas(resp.aiter_bytes(), self.config.max_respuesta_bytes)
            async for evento in a2a.leer_sse(lineas):
                for bloque in await self._sse_evento(evento, llamada, est):
                    yield bloque
                if est.cortado:
                    break
        except a2a.LimiteExcedido as e:
            falla = f"el stream del comercio supera el tope de {e.limite} bytes"
        except httpx.HTTPError as e:
            falla = f"el stream del comercio se cortó ({type(e).__name__})"
        except (ValueError, RecursionError) as e:  # defensa (N6): no se puede reescribir
            falla = f"un evento del comercio no se pudo reenviar ({type(e).__name__})"
        finally:
            await resp.aclose()

        if falla is not None and not est.cortado:
            for bloque in self._sse_falla(llamada, est, falla):
                yield bloque
        elif est.pendiente is not None and not est.cortado:
            for bloque in self._sse_cortar(
                llamada, est, "el stream del comercio terminó sin evento final después del checkout"
            ):
                yield bloque

        self._emitir_busqueda(llamada, est.contexto_id)
        if llamada.pedido.tipo == "find_products" and not est.ofertas_registradas:
            self._registrar_ofertas(est.ofertas, llamada)
        compra = llamada.compra
        # Solo la búsqueda decide la oportunidad: en el tráfico de integrador
        # la compra viaja también en el checkout y no tiene que cambiar ahí.
        if (
            compra is not None
            and compra.estado == "negociando"
            and llamada.pedido.tipo == "find_products"
        ):
            # Etapa 2: en un stream no se retiene la respuesta (ya salió evento
            # por evento): no hay oportunidad, sigue el pedido original. El
            # Guard igual evalúa el checkout con los parámetros de la compra.
            contexto = llamada.pedido.contexto_id or est.ofertas.contexto_id or est.contexto_id
            if contexto:
                self.compras.atar(compra, contexto)
            compra.resolver_ficha(
                self._ficha_confirmada(est.ofertas.escenario or llamada.pedido.escenario)
            )
            compra.estado = "sin_oportunidad"
            self.guardar_compra(compra)
        if not est.resuelto and not est.cortado:
            metodo = "create_checkout" if est.checkouts else None
            self._emitir_reenviado(llamada, est.contexto_id, metodo=metodo)

    async def _sse_evento(
        self, evento: a2a.EventoSSE, llamada: Llamada, est: EstadoStream
    ) -> list[bytes]:
        pedido, activo = llamada.pedido, llamada.guard_activo
        body, error = _leer_del_comercio(evento.data)
        crudo = b""
        if error is None and isinstance(body, dict):
            try:
                body, cambiado = a2a.quitar_veredictos_ajenos(body)
                crudo = a2a.bloque_sse(body) if cambiado else evento.serializar()
            except (ValueError, RecursionError) as e:  # defensa (N6)
                error = f"{type(e).__name__}: {e}"[:200]
        if error is not None or not isinstance(body, dict):
            detalle = error or "el evento no es un objeto JSON"
            datos = evento.data.encode("utf-8")
            if activo and self.filtro is not None:
                # Etapa 2: lo que no se puede leer no se puede filtrar.
                self._error(llamada, f"Evento SSE ilegible del comercio: {detalle}")
                return self._sse_cortar_etapa2(llamada, est, _MOTIVO_FILTRO_ILEGIBLE)
            if activo and (
                self._es_de_checkout(llamada)
                or est.pendiente is not None
                or a2a.menciona_checkout(datos)
            ):
                return self._sse_cortar(
                    llamada, est, "un evento SSE del comercio no se pudo leer", detalle
                )
            self._error(llamada, f"Evento SSE ilegible del comercio: {detalle}")
            if a2a.menciona_clave(datos, GUARD_VEREDICTO_KEY):
                return []  # un guard.veredicto que no se puede sacar no se reenvía
            return self._sse_encolar(est, evento.serializar(), final=False)

        result = body.get("result")
        n = a2a.contar_clave(body, a2a.CHECKOUT_KEY)
        if activo and self.filtro is not None:
            # Etapa 2, paso 2: cada evento del proveedor pasa por el filtro.
            corte = await self._filtrar(result if isinstance(result, dict) else body, llamada)
            if corte is not None:
                return self._sse_cortar_etapa2(llamada, est, corte[0], corte[1])
        if not await self._contexto_respuesta_ok(llamada, result):
            return self._sse_cortar_etapa2(llamada, est, _MOTIVO_CONTEXTO_AJENO)
        if not isinstance(result, dict):
            if "error" in body:
                self._emitir_error_comercio(body["error"], llamada)
            if activo and n:
                return self._sse_cortar(llamada, est, "un ap2.checkout fuera de un evento de tarea")
            return self._sse_encolar(est, crudo, final=False)

        kind = result.get("kind")
        if kind == "task":
            est.task_vista = True
            est.task_id = result.get("id") if isinstance(result.get("id"), str) else est.task_id
        elif kind in ("artifact-update", "status-update"):
            tid = result.get("taskId")
            est.task_id = tid if isinstance(tid, str) else est.task_id
        ctx = result.get("contextId")
        est.contexto_id = ctx if isinstance(ctx, str) else est.contexto_id
        final = a2a.es_final(result)

        if n == 0:
            if pedido.tipo == "find_products":
                est.ofertas.ingerir(result)
                self._emitir_busqueda(llamada, est.ofertas.contexto_id or est.contexto_id)
                if final and not est.ofertas_registradas:
                    est.ofertas_registradas = True
                    if not self._registrar_ofertas(est.ofertas, llamada):
                        return self._sse_cortar_etapa2(llamada, est, _MOTIVO_CONTEXTO_AJENO)
            return self._sse_encolar(est, crudo, final=final, llamada=llamada)

        # -- un evento con ap2.checkout ------------------------------------
        if not activo:
            est.checkouts += n
            previa = self._decisiones.get(est.task_id) if est.task_id else None
            if previa is not None and previa.retiene:
                return self._sse_aplicar_retencion(llamada, est, previa, result)
            checkouts = a2a.checkouts_del_result(result)
            if checkouts and isinstance(checkouts[0], dict):
                cart_id = pedido.cart_id or checkouts[0].get("cart_id")
                self._emitir_checkout_recibido(
                    checkouts[0], str(cart_id) if cart_id else None,
                    pedido.contexto_id or est.contexto_id, False,
                )
            return [crudo]
        if est.checkouts + n > 1:
            return self._sse_cortar(llamada, est, "más de un ap2.checkout en el mismo stream")
        est.checkouts += n
        if kind not in ("task", "artifact-update"):
            return self._sse_cortar(
                llamada, est, f"un ap2.checkout dentro de un evento {kind}"
            )
        checkouts = a2a.checkouts_del_result(result)
        if len(checkouts) != n:
            return self._sse_cortar(llamada, est, "el ap2.checkout vino fuera de los artefactos")
        if not isinstance(checkouts[0], dict):
            return self._sse_cortar(llamada, est, "el ap2.checkout no tiene el formato esperado")
        decision = await self._decidir_checkout(
            checkouts[0], llamada, est.task_id, ctx if isinstance(ctx, str) else None
        )
        est.decision = decision
        if decision.retiene:
            return self._sse_aplicar_retencion(llamada, est, decision, result)
        if kind == "task" and final:
            decision = self._resolver(decision, est.task_id)
            if decision.retiene:
                return self._sse_aplicar_retencion(llamada, est, decision, result)
            # Primero se escribe (puede fallar: N6) y recién después se da por resuelto.
            bloque = self._bloque(llamada, a2a.tarea_con_artefacto(result, decision.artefacto()))
            est.resuelto = True
            self._emitir_resultado(decision, llamada)
            return [bloque]
        # Pasa: se retiene hasta el evento final, para que un segundo
        # checkout en el mismo stream no llegue nunca al cliente.
        est.pendiente = [crudo]
        return []

    def _sse_encolar(
        self, est: EstadoStream, crudo: bytes, *, final: bool, llamada: Llamada | None = None
    ) -> list[bytes]:
        if est.pendiente is None:
            return [crudo]
        est.pendiente.append(crudo)
        if final and llamada is not None:
            return self._sse_liberar(llamada, est)
        return []

    def _sse_liberar(self, llamada: Llamada, est: EstadoStream) -> list[bytes]:
        pendiente = est.pendiente or []
        est.pendiente = None
        decision = self._resolver(est.decision, est.task_id)
        if decision.retiene:  # p. ej. la aprobación ya se usó en otro intento
            return self._sse_aplicar_retencion(llamada, est, decision, None)
        final = pendiente.pop()
        artefacto = a2a.evento_artefacto(est.task_id, est.contexto_id, decision.artefacto())
        bloque = self._bloque(llamada, artefacto)
        est.resuelto = True
        self._emitir_resultado(decision, llamada)
        return [*pendiente, bloque, final]

    def _sse_aplicar_retencion(
        self,
        llamada: Llamada,
        est: EstadoStream,
        decision: Decision,
        result: dict[str, Any] | None,
    ) -> list[bytes]:
        decision = self._resolver(decision, est.task_id)
        est.cortado = True
        est.resuelto = True
        est.pendiente = None
        self._emitir_resultado(decision, llamada)
        if result is not None and result.get("kind") == "task":
            return [self._bloque(llamada, a2a.tarea_bloqueada(result, decision.texto, decision.artefacto()))]
        contexto = est.contexto_id or llamada.pedido.contexto_id
        if not est.task_vista and est.task_id is None:
            return [self._bloque(
                llamada,
                a2a.tarea_sintetica_bloqueada(None, contexto, decision.texto, decision.artefacto()),
            )]
        return [
            self._bloque(llamada, a2a.evento_artefacto(est.task_id, contexto, decision.artefacto())),
            self._bloque(llamada, a2a.evento_fallido(est.task_id, contexto, decision.texto)),
        ]

    def _sse_cortar(
        self, llamada: Llamada, est: EstadoStream, motivo: str, detalle: str | None = None
    ) -> list[bytes]:
        pedido = llamada.pedido
        previa = est.decision
        cart_id = previa.cart_id if previa else pedido.cart_id
        contexto = (previa.contexto_id if previa else None) or pedido.contexto_id or est.contexto_id
        decision = self._bloqueo(llamada, cart_id, contexto, motivo, detalle)
        return self._sse_aplicar_retencion(llamada, est, decision, None)

    def _sse_falla(self, llamada: Llamada, est: EstadoStream, motivo: str) -> list[bytes]:
        if est.resuelto:  # el evento final ya salió: no hay nada que retener
            self._error(llamada, f"Proxy del Guard: {motivo}", est.contexto_id)
            return []
        if llamada.guard_activo and (
            self._es_de_checkout(llamada) or est.checkouts or est.pendiente is not None
        ):
            return self._sse_cortar(llamada, est, motivo)
        self._error(llamada, f"Proxy del Guard: {motivo}", est.contexto_id)
        return []

    # -- ofertas -------------------------------------------------------------

    def _registrar_ofertas(self, respuesta: a2a.Respuesta, llamada: Llamada) -> bool:
        """Solo desde la respuesta a un find_products del cliente.

        False si ese contexto ya tiene ofertas de **otro** comercio: no se
        pisan (un comercio malicioso no envenena las de otro devolviendo su
        contextId) y el llamador corta la respuesta.
        """
        pedido = llamada.pedido
        if pedido.tipo != "find_products" or not respuesta.carts:
            return True
        contexto_id = respuesta.contexto_id or pedido.contexto_id
        escenario = respuesta.escenario or pedido.escenario
        comercio_id = llamada.comercio.id if llamada.comercio is not None else None
        previas = self._ofertas.get(contexto_id) if contexto_id else None
        if previas is not None and previas.comercio_id not in (None, comercio_id):
            self._error(
                llamada,
                f"El comercio {comercio_id} respondió ofertas en un contexto que ya tiene "
                f"ofertas de {previas.comercio_id}: no se registran",
                contexto_id,
            )
            return False
        guardadas = OfertasGuardadas(
            carts=copy.deepcopy(respuesta.carts),
            offer_details=copy.deepcopy(respuesta.offer_details or []),
            escenario=escenario,
            comercio_id=comercio_id,
        )
        if contexto_id:
            _recordar(self._ofertas, contexto_id, guardadas)
            if llamada.de_integrador:
                # Para evaluar el create_checkout aunque haya un reinicio.
                self.en_fondo.programar(
                    self.almacen.guardar_ofertas(
                        fila_ofertas(
                            contexto_id, comercio_id, guardadas.carts, guardadas.offer_details,
                            escenario, datetime.now(timezone.utc),
                        )
                    ),
                    "ofertas",
                )
        logger.info(
            "← ofertas: contexto=%s escenario=%s carts=%s",
            contexto_id,
            escenario,
            [c.get("cart_id") for c in guardadas.carts],
        )
        self._emitir(
            "ofertas_recibidas",
            {
                "carts": copy.deepcopy(guardadas.carts),
                "offer_details": copy.deepcopy(guardadas.offer_details),
            },
            guard_activo=llamada.guard_activo,
            contexto_id=contexto_id,
            escenario=escenario,
            llamada=llamada,
        )
        return True

    async def _contexto_respuesta_ok(self, llamada: Llamada, result: Any) -> bool:
        """El `contextId` que trae la respuesta (JSON o cada evento SSE).

        - Tráfico de integrador: tiene que ser de la compra del pedido. Si no
          estaba atado, se ata.
        - Tráfico anónimo (la demo): no puede ser de una compra de integrador
          (el comercio no la puede "mudar" ahí, ni con un `tasks/get` de una
          tarea suya).

        Uno de otra compra, ilegible o una falla del almacén → False (el
        llamador corta)."""
        if not isinstance(result, dict) or result.get("contextId") is None:
            return True
        if not llamada.de_integrador:
            return await self._contexto_es_de_integrador(result["contextId"]) is False
        compra, comercio = llamada.compra, llamada.comercio
        if compra is None or comercio is None:
            return False
        return await self.atar_contexto_api(compra, result["contextId"], comercio.id) == "ok"

    # -- etapa 2: contexto, filtro y oportunidad (docs/FLUJO.md) --------------

    def _restante(self, llamada: Llamada) -> float:
        """Lo que queda del presupuesto de la llamada: el agente del cliente
        espera hasta 600 s y las dos esperas caen en la misma llamada."""
        return max(0.0, _PRESUPUESTO_LLAMADA_S - (time.monotonic() - llamada.inicio))

    async def _esperar_a_la_otra(
        self, llamada: Llamada, compra: Compra, evento: asyncio.Event
    ) -> bool:
        """Otra búsqueda de la misma compra espera a la que está preguntando
        (`evento`: `contexto_listo` o `resuelta`). True si terminó.

        Cada tramo dura lo que le queda a la espera de la compra
        (`compra.restante()`) más un margen para el LLM, nunca más que la
        llamada. Si vence y la compra abrió otra espera mientras tanto (otro
        plazo), vuelve a calcular; si no avanzó, False y quien llama cierra
        del lado seguro (antes esperaba el presupuesto entero, 570 s).

        Sin plazo (`vence` None: la primera todavía arma la pregunta con el
        LLM, o ya pasó la espera y sigue con el LLM de después) no hay nada
        que acote salvo la llamada: el tramo es lo que le queda a la llamada."""
        while not evento.is_set():
            tramo = self._restante(llamada)
            if compra.vence is not None:
                tramo = min(tramo, compra.restante() + _MARGEN_ESPERA_SECUNDARIA_S)
            if tramo <= 0:
                return False
            plazo = compra.vence
            try:
                await asyncio.wait_for(evento.wait(), tramo)
            except asyncio.TimeoutError:
                if compra.vence == plazo:
                    return False
        return True

    async def _llm(self, funcion: Any, *args: Any) -> Any:
        """Una llamada al agente de intención, en un hilo (el pool del LLM) y
        con tope de tiempo."""
        return await asyncio.wait_for(
            self._en_hilo(self.ejecutor_llm, funcion, *args), _TIMEOUT_LLM_S
        )

    def _ficha_confirmada(self, escenario: str | None) -> Ficha | None:
        """La ficha confirmada de la demo para ese escenario (si hay), con los
        atributos y la categoría de la taxonomía (`ficha_desde_intent`)."""
        if not self.config.ficha_demo:
            return None  # PROXY_FICHA_DEMO=0: como con un comercio real
        intent = intent_del_escenario(escenario)
        if intent is None:
            return None
        try:
            return ficha_del_intent(intent)
        except (ValueError, ValidationError):
            logger.exception("La ficha del escenario %s no es válida", escenario)
            return None

    async def _categorias(self, comercio: Comercio | None = None) -> list[str]:
        """Categorías del catálogo público del comercio de la llamada: el
        agente solo puede elegir una de estas (así el Guard compara contra un
        valor que el catálogo usa)."""
        comercio = comercio or self.comercios.por_defecto
        catalogo, _, _ = await cargar_catalogo(self.client, comercio.catalogo_url)
        return sorted(
            {p["category"] for p in catalogo.values() if isinstance(p.get("category"), str)}
        )

    async def _etapa_contexto(self, llamada: Llamada) -> None:
        """Paso 1: antes de negociar, nuestro agente pregunta el porqué y, con
        un agente de intake (`evaluar_intake`), lo que falte para comprar.

        Retiene el find_products hasta que el usuario contesta, omite o vence
        el plazo. Con intake, si después de una respuesta falta algo necesario
        (categoría, cantidad, tope, moneda, plazo) vuelve a preguntar, y al
        final pide confirmar la ficha: todo dentro del presupuesto de la
        llamada (570 s). Lo inferido solo se autoriza si el usuario confirma.

        Otra búsqueda de la misma compra no vuelve a preguntar: si el porqué
        está pendiente, espera lo mismo. Si el agente falla, la compra sigue
        con lo que tiene (o el pedido literal): la seguridad no depende de esto.
        """
        pedido = llamada.pedido
        existente = self.compras.de_contexto(pedido.contexto_id)
        if existente is not None:
            llamada.compra = existente
            if not existente.contexto_listo.is_set():
                try:
                    await asyncio.wait_for(
                        existente.contexto_listo.wait(), self._restante(llamada)
                    )
                except asyncio.TimeoutError:
                    pass
            return
        # Una compra nueva: si el usuario registró su pedido en /demo, el
        # intake lee sus palabras además de la búsqueda del agente (una vez).
        texto = pedido_para_intake(pedido.catalog_search or "", self.pedido_usuario.tomar())
        hoy = self.hoy()
        categorias: list[str] | None = None
        resultado: Any = None  # intencion.intake.ResultadoIntake del último turno
        try:
            categorias = await self._categorias(llamada.comercio)
            pregunta = await self._llm(self.agente.preguntar, texto, hoy, categorias)
            pregunta_texto = pregunta.pregunta
            resultado = getattr(pregunta, "intake", None)
            ficha_llm = _ficha_verificable(texto, pregunta.requisitos)
        except Exception as e:  # noqa: BLE001 - sin agente, el pedido sigue literal
            logger.exception("El agente de intención no pudo armar la pregunta")
            self._error(
                llamada, f"El agente de intención no pudo armar la pregunta ({type(e).__name__})"
            )
            pregunta_texto, ficha_llm, resultado = _PREGUNTA_POR_DEFECTO, None, None
        con_intake = resultado is not None and callable(
            getattr(self.agente, "evaluar_intake", None)
        )
        compra = self.compras.crear(texto, pregunta_texto, ficha_llm, pedido.contexto_id)
        llamada.compra = compra
        estado = self._estado_intake(compra.id)
        estado.resultado = resultado if con_intake else None
        self._emitir_pregunta(llamada, compra, estado)
        turnos: list[Turno] = []
        contexto = None
        try:
            while True:
                respuesta, vencido = await compra.esperar_respuesta()
                confirmo = compra.id in self._confirmaciones
                self._confirmaciones.discard(compra.id)
                nuevo = None
                if respuesta:
                    turnos.append(Turno(pregunta=compra.pregunta, respuesta=respuesta))
                    if con_intake:
                        nuevo = await self._intake_turno(llamada, texto, turnos, hoy, categorias)
                    contexto = (
                        await self._contexto_turno(llamada, texto, turnos, hoy, con_intake)
                        or contexto
                    )
                    if nuevo is not None:
                        estado.resultado = nuevo
                        if not compra.ficha_resuelta:
                            compra.ficha_llm = _ficha_verificable(
                                texto, nuevo.requisitos_autorizables(confirmada=False)
                            )
                # ¿Otra pregunta? Solo si el intake la pide y queda tiempo.
                otra, sin_tiempo = False, False
                if nuevo is not None and nuevo.siguiente in ("repreguntar", "confirmar"):
                    otra = self._restante(llamada) - _MARGEN_S >= _MINIMO_REPREGUNTA_S
                    sin_tiempo = not otra
                datos: dict[str, Any] = {
                    "compra_id": compra.id,
                    "turno": estado.turno,
                    "respuesta": respuesta,
                    "omitido": respuesta is None and not vencido and not confirmo,
                    "vencido": vencido,
                    "contexto": contexto.model_dump(mode="json") if contexto else None,
                }
                if con_intake:
                    datos["intake"] = estado.resultado.a_json()
                    if not otra:
                        categoria = await self._clasificar(llamada, texto, turnos)
                        self._cerrar_intake(
                            compra, texto, estado, confirmada=confirmo, categoria_taxonomia=categoria
                        )
                        datos.update(estado.cierre_json(), sin_tiempo=sin_tiempo)
                self._emitir(
                    "contexto_registrado",
                    datos,
                    guard_activo=True,
                    contexto_id=pedido.contexto_id,
                    escenario=pedido.escenario,
                    llamada=llamada,
                )
                if not otra:
                    break
                estado.turno += 1
                compra.nueva_espera(nuevo.pregunta)
                self._emitir_pregunta(llamada, compra, estado)
        finally:
            # También si se cancela la llamada: ninguna compra queda colgada.
            if compra.estado == "esperando_contexto":
                compra.estado = "negociando"
            compra.contexto = contexto
            if con_intake and not estado.final:
                self._cerrar_intake(compra, texto, estado, confirmada=False)
            compra.contexto_listo.set()

    # -- etapa 2: intake (repreguntas y confirmación de la ficha) ---------------

    def _estado_intake(self, compra_id: str) -> EstadoIntake:
        estado = self._intakes[compra_id] = EstadoIntake()
        while len(self._intakes) > MAX_INTAKES:
            self._intakes.popitem(last=False)
        return estado

    def _emitir_pregunta(self, llamada: Llamada, compra: Compra, estado: EstadoIntake) -> None:
        """`pedido_detectado` de un turno (el primero es el porqué)."""
        vence = compra.fijar_plazo(
            min(self.config.espera_contexto_s, self._restante(llamada) - _MARGEN_S)
        )
        r = estado.resultado
        estado.confirmar = r is not None and r.siguiente == "confirmar"
        datos: dict[str, Any] = {
            "compra_id": compra.id,
            "pedido": compra.pedido,
            "pregunta": compra.pregunta,
            "requisitos": [q.model_dump(mode="json") for q in compra.requisitos],
            "vence": vence.isoformat(),
            "turno": estado.turno,
            "confirmar": estado.confirmar,
        }
        if r is not None:
            datos.update(estado.intake_json())
        self._emitir(
            "pedido_detectado",
            datos,
            guard_activo=True,
            contexto_id=llamada.pedido.contexto_id,
            escenario=llamada.pedido.escenario,
            llamada=llamada,
        )

    async def _intake_turno(
        self, llamada: Llamada, texto: str, turnos: list[Turno], hoy: date, categorias: Any
    ) -> Any:
        """El intake con todas las respuestas; None si el agente falla."""
        try:
            return await self._llm(self.agente.evaluar_intake, texto, list(turnos), hoy, categorias)
        except Exception as e:  # noqa: BLE001 - sin intake, sigue con lo que hay
            logger.exception("El agente de intención no pudo revisar la respuesta")
            self._error(
                llamada,
                f"El agente de intención no pudo revisar la respuesta ({type(e).__name__})",
            )
            return None

    async def _contexto_turno(
        self, llamada: Llamada, texto: str, turnos: list[Turno], hoy: date, con_intake: bool
    ) -> Any:
        """El contexto con la primera respuesta y, con intake, el historial."""
        primero, *resto = turnos
        funcion = self.agente.armar_contexto
        if con_intake and resto:
            funcion = functools.partial(funcion, historial=list(resto))
        try:
            return await self._llm(funcion, texto, primero.pregunta, primero.respuesta, hoy)
        except Exception as e:  # noqa: BLE001 - sin contexto, sin oportunidades
            logger.exception("El agente de intención no pudo armar el contexto")
            self._error(
                llamada,
                f"El agente de intención no pudo armar el contexto ({type(e).__name__})",
            )
            return None

    async def _clasificar(self, llamada: Llamada, texto: str, turnos: list[Turno]) -> str | None:
        """La categoría de la taxonomía de Google para la ficha del intake.

        Solo con un agente que sabe clasificar (`clasificar_taxonomia`, el de
        decisiones) y si queda tiempo en la llamada. Es una propuesta que se
        registra en la ficha: el Guard no depende de ella. Si falla, vacía.
        """
        funcion = getattr(self.agente, "clasificar_taxonomia", None)
        if not callable(funcion) or self._restante(llamada) - _MARGEN_S < _MINIMO_TAXONOMIA_S:
            return None
        descripcion = " / ".join([texto, *(t.respuesta for t in turnos if t.respuesta)])
        try:
            categoria = await self._llm(funcion, descripcion)
        except Exception as e:  # noqa: BLE001 - sin taxonomía, la ficha sigue igual
            logger.exception("El agente de intención no pudo clasificar el pedido")
            self._error(
                llamada, f"El agente de intención no pudo clasificar el pedido ({type(e).__name__})"
            )
            return None
        return categoria if isinstance(categoria, str) and categoria.isdigit() else None

    def _cerrar_intake(
        self,
        compra: Compra,
        texto: str,
        estado: EstadoIntake,
        confirmada: bool,
        categoria_taxonomia: str | None = None,
    ) -> None:
        """Los parámetros autorizados que salen del intake.

        Lo que el usuario dijo con confianza alta va siempre; lo inferido o
        dudoso, solo si confirmó la ficha. Lo que el Guard no supiera chequear
        (`guard.chequeos.CAMPOS_SOPORTADOS` y `OPERADORES_SOPORTADOS`; hoy
        todo, atributos y `minimo` incluidos) se informaría aparte. Si la demo
        tiene una ficha confirmada, esa sigue mandando (`Compra.resolver_ficha`;
        `PROXY_FICHA_DEMO=0` la apaga).
        """
        from intencion.agente_decisiones import requisitos_verificables

        r = estado.resultado
        requisitos = r.requisitos_autorizables(confirmada) if r is not None else []
        verificables, fuera = requisitos_verificables(requisitos)
        estado.final, estado.confirmar, estado.confirmada = True, False, confirmada
        estado.ficha_autorizada, estado.sin_verificar_guard = verificables, fuera
        estado.categoria_taxonomia = categoria_taxonomia
        if not compra.ficha_resuelta:
            compra.ficha_llm = _ficha_verificable(texto, verificables, categoria_taxonomia)

    def _compra_json(self, datos: dict[str, Any]) -> dict[str, Any]:
        """La compra del registro + el estado del intake (turno, faltan…)."""
        estado = self._intakes.get(datos.get("compra_id"))
        if estado is not None:
            datos["intake"] = estado.a_json()
        return datos

    def _confirmar(self, compra_id: str) -> Response:
        """El usuario confirma la ficha (solo en el turno de confirmación)."""
        compra = self.compras.get(compra_id)
        if compra is None:
            return JSONResponse({"error": "compra_desconocida"}, status_code=404)
        estado = self._intakes.get(compra_id)
        if estado is None or not estado.confirmar or compra.estado != "esperando_contexto":
            return JSONResponse({"error": "estado", "estado": compra.estado}, status_code=409)
        self._confirmaciones.add(compra_id)
        try:
            compra = self.compras.responder(compra_id, None)
        except EstadoInvalido as e:
            self._confirmaciones.discard(compra_id)
            return JSONResponse({"error": "estado", "estado": e.estado}, status_code=409)
        return JSONResponse(self._compra_json(compra.a_json()))

    async def _correr_filtro(self, objetivo: Any) -> ResultadoFiltro:
        """El filtro sobre una copia de `objetivo`, en su pool y con tope de
        tiempo. Lanza si falla, si tarda de más o si no devuelve un
        `ResultadoFiltro`: quien llama falla cerrado."""
        salida = await asyncio.wait_for(
            self._en_hilo(self.ejecutor_filtro, self.filtro, copy.deepcopy(objetivo)),
            _TIMEOUT_FILTRO_S,
        )
        if isinstance(salida, ResultadoFiltro):
            return salida
        return ResultadoFiltro.model_validate(salida)

    async def _filtrar(
        self, objetivo: dict[str, Any], llamada: Llamada
    ) -> tuple[str, list[str]] | None:
        """Paso 2: el filtro sobre un mensaje del proveedor, antes que nada.

        None = pasa. Si no, (motivo fijo del proxy, señales del filtro). Si
        el filtro falla o tarda de más, tampoco pasa (fail-closed). Corre en
        un hilo: un filtro lento no frena al resto del proxy.
        """
        # El eco exacto de nuestro propio pedido en el history no es texto del
        # proveedor: no se le pasa al filtro (la respuesta reenviada no cambia).
        revisar = a2a.sin_eco_del_pedido(objetivo, llamada.pedido.mensaje)
        try:
            veredicto = await self._correr_filtro(revisar)
            corte = None if veredicto.pasa else (_MOTIVO_FILTRO, list(veredicto.senales))
        except Exception as e:  # noqa: BLE001 - fail-closed
            logger.exception("El filtro de seguridad falló")
            self._error(llamada, f"El filtro de seguridad falló ({type(e).__name__})")
            corte = (_MOTIVO_FILTRO_FALLO, [])
        if corte is not None:
            contexto = objetivo.get("contextId")
            compra = llamada.compra or self.compras.de_contexto(
                contexto if isinstance(contexto, str) else llamada.pedido.contexto_id
            )
            # El tráfico anónimo no bloquea una compra de integrador (el
            # comercio puede contestar con el contexto de una ajena).
            if compra is not None and (compra.origen != "api" or llamada.de_integrador):
                compra.estado = "bloqueada"
                compra.resuelta.set()
                self.guardar_compra(compra)
        return corte

    async def _etapa_oportunidad(
        self, llamada: Llamada, respuesta: a2a.Respuesta, result: dict[str, Any]
    ) -> tuple[AccionEtapa2, dict[str, Any] | None]:
        """Pasos 3 a 6: si ninguna oferta cumple los parámetros autorizados
        pero una cubre la necesidad, retiene la respuesta y consulta al usuario.

        - acepta: la respuesta pasa intacta + `guard.parametros_autorizados`
          (y la compra queda con esos parámetros para el Guard);
        - rechaza: pasa intacta y siguen los parámetros originales;
        - vence: no se entrega nada (el silencio no es autorización).

        Otra búsqueda de la misma compra recibe el mismo resultado.
        """
        compra = llamada.compra
        assert compra is not None
        # El contexto del pedido manda; el del comercio solo si el pedido no traía.
        contexto_id = llamada.pedido.contexto_id or respuesta.contexto_id
        if contexto_id and not self.compras.atar(compra, contexto_id):
            return "contexto_ajeno", None
        escenario = respuesta.escenario or llamada.pedido.escenario
        compra.resolver_ficha(self._ficha_confirmada(escenario))

        if compra.estado == "esperando_decision":
            listo = await self._esperar_a_la_otra(llamada, compra, compra.resuelta)
            if not listo or compra.estado == "esperando_decision":
                return "vencida", None  # sin decisión: el silencio no autoriza
        if compra.estado == "aceptada" and compra.oportunidad and compra.requisitos_autorizados:
            artefacto = self._artefacto_parametros(
                compra.oportunidad.cart_id, compra.oportunidad.id, compra.requisitos_autorizados
            )
            return "con_parametros", a2a.tarea_con_artefacto(result, artefacto)
        if compra.estado in ("vencida", "bloqueada"):
            return compra.estado, None
        if compra.estado != "negociando":
            return "pasar", None  # sin_oportunidad o rechazada: sigue el pedido original
        if compra.contexto is None or compra.ficha is None or self.agente is None:
            compra.estado = "sin_oportunidad"
            self.guardar_compra(compra)
            return "pasar", None

        ofertas: list[Any] = []
        try:
            from guard.extractor import extraer
            from guard.verificador import verificar_todas

            comercio = llamada.comercio or self.comercios.por_defecto
            catalogo, comercio_id, _ = await cargar_catalogo(self.client, comercio.catalogo_url)
            detalles = copy.deepcopy(respuesta.offer_details or [])
            catalogo, _ = await self._con_fabricante(catalogo, detalles)
            vistas = OfertasVistas(
                carts=copy.deepcopy(respuesta.carts), offer_details=detalles, comercio_id=comercio_id
            )
            # Verificadas como las verifica el Guard: con el fabricante y los
            # atributos que pide la ficha de la compra.
            atributos = [
                r.atributo for r in compra.ficha.requisitos if r.campo == "atributo" and r.atributo
            ]
            ofertas = verificar_todas(extraer(vistas).ofertas, catalogo, atributos)
            oportunidad = await self._llm(
                self.agente.detectar_oportunidad, compra.contexto, compra.ficha, ofertas
            )
        except Exception as e:  # noqa: BLE001 - sin análisis, sigue el pedido original
            logger.exception("El agente de intención no pudo analizar las ofertas")
            self._error(
                llamada,
                f"El agente de intención no pudo analizar las ofertas ({type(e).__name__})",
                contexto_id,
            )
            oportunidad = None
        if oportunidad is None:
            compra.estado = "sin_oportunidad"
            self.guardar_compra(compra)
            return "pasar", None
        # El usuario ve el título público (catálogo o fabricante) o ninguno
        # (el dashboard muestra el SKU), nunca el que declara el vendedor:
        # lo asegura el proxy, cualquiera sea el agente.
        oportunidad = _con_titulo_publico(oportunidad, ofertas)

        compra.oportunidad = oportunidad
        # Las otras búsquedas esperan esta decisión (`resuelta`). Puede venir
        # puesta: `Compra.desde_fila` la deja así al hidratar después de un
        # reinicio, y otra búsqueda pasaba sin la decisión del usuario.
        compra.resuelta.clear()
        compra.estado = "esperando_decision"
        vence = compra.fijar_plazo(
            min(self.config.espera_decision_s, self._restante(llamada) - _MARGEN_S)
        )
        self.guardar_compra(compra)
        self._emitir(
            "oportunidad",
            {
                "compra_id": compra.id,
                "oportunidad": oportunidad.model_dump(mode="json"),
                "vence": vence.isoformat(),
            },
            guard_activo=True,
            contexto_id=contexto_id,
            escenario=escenario,
            llamada=llamada,
        )
        decision = None
        try:
            decision = await compra.esperar_decision()
        finally:
            # También si se cancela la llamada: sin decisión, vencida.
            if decision == "aceptar":
                # Los que cambian salen de la oferta del comercio: van marcados
                # `aceptado` para que la vista de /v1 y los webhooks los filtren
                # (`vistas._ficha_publica`). Los que no cambian siguen siendo
                # del pedido.
                originales = list(compra.ficha.requisitos) if compra.ficha else []
                compra.requisitos_autorizados = [
                    r if r in originales else r.model_copy(update={"origen": "aceptado"})
                    for r in oportunidad.requisitos_propuestos
                ]
                compra.ficha = Ficha(
                    pedido_original=compra.pedido,
                    requisitos=compra.requisitos_autorizados,
                    confirmada=True,
                    categoria_taxonomia=compra.ficha.categoria_taxonomia if compra.ficha else None,
                )
                compra.estado = compra.decision = "aceptada"
            elif decision == "rechazar":
                compra.estado = compra.decision = "rechazada"
            else:
                compra.estado = compra.decision = "vencida"
            compra.resuelta.set()
            self.guardar_compra(compra)
        logger.warning("Oportunidad %s: %s", oportunidad.id, compra.decision)
        self._emitir(
            "oportunidad_decidida",
            {
                "compra_id": compra.id,
                "oportunidad_id": oportunidad.id,
                "decision": compra.decision,
                "requisitos_autorizados": [
                    r.model_dump(mode="json") for r in compra.requisitos_autorizados
                ]
                if compra.requisitos_autorizados is not None
                else None,
            },
            guard_activo=True,
            contexto_id=contexto_id,
            escenario=escenario,
            llamada=llamada,
        )
        if compra.decision == "aceptada":
            artefacto = self._artefacto_parametros(
                oportunidad.cart_id, oportunidad.id, compra.requisitos_autorizados or []
            )
            return "con_parametros", a2a.tarea_con_artefacto(result, artefacto)
        if compra.decision == "rechazada":
            return "pasar", None
        return "vencida", None

    def _cortar_etapa2(
        self, accion: AccionEtapa2, llamada: Llamada, result: dict[str, Any]
    ) -> bytes:
        """La respuesta cortada para cada acción de la etapa 2 que no pasa."""
        if accion == "vencida":
            return self._cortar(
                llamada, result,
                "El usuario no respondió la consulta: la alternativa no se autorizó.",
                etapa="intencion", texto=_TEXTO_PAUSA,
            )
        if accion == "bloqueada":
            return self._cortar(llamada, result, _MOTIVO_YA_BLOQUEADA, etapa="seguridad")
        return self._cortar(llamada, result, _MOTIVO_CONTEXTO_AJENO, etapa="seguridad")

    def _artefacto_parametros(
        self, cart_id: str, oportunidad_id: str, requisitos: list[Any]
    ) -> dict[str, Any]:
        """`guard.parametros_autorizados` para el agente del cliente. El texto
        es una plantilla fija con los valores de los requisitos (que salen de
        los datos tipados de la oferta, no de texto libre del comercio)."""
        from guard.chequeos import formatear, nombre_campo

        seguro = a2a.cart_id_seguro(cart_id)
        texto = _TEXTO_PARAMETROS.format(
            requisitos="; ".join(
                f"{nombre_campo(r.campo)}: {formatear(r.valor)}" for r in requisitos
            ),
            cart_id=seguro or "elegida",
        )
        datos = {
            "cart_id": seguro,
            "oportunidad_id": oportunidad_id,
            "requisitos": [r.model_dump(mode="json") for r in requisitos],
            "texto": texto,
        }
        return {
            "artifactId": f"guard-parametros-{uuid.uuid4().hex}",
            "name": GUARD_PARAMETROS_KEY,
            "parts": [{"kind": "data", "data": {GUARD_PARAMETROS_KEY: datos}}],
        }

    def _emitir_corte(
        self,
        llamada: Llamada,
        contexto_id: str | None,
        motivo: str,
        etapa: str,
        senales: list[str] | None = None,
    ) -> Decision:
        """Evento `bloqueado` de un corte de la etapa 2 y su `Decision`."""
        pedido = llamada.pedido
        datos: dict[str, Any] = {
            "cart_id": pedido.cart_id,
            "motivo": motivo,
            "status": "bloqueado",
            "requiere_aprobacion": False,
            "etapa": etapa,
        }
        if llamada.compra is not None:
            datos["compra_id"] = llamada.compra.id
        if senales:
            datos["senales"] = [str(s)[:200] for s in senales[:20]]
        logger.warning("✗ corte en %s: %s", etapa, motivo)
        self._emitir(
            "bloqueado",
            datos,
            guard_activo=llamada.guard_activo,
            contexto_id=contexto_id,
            escenario=pedido.escenario or self._escenario(contexto_id),
            llamada=llamada,
        )
        return Decision(
            accion="bloquear",
            status="bloqueado",
            motivo=motivo,
            cart_id=pedido.cart_id,
            contexto_id=contexto_id,
            via_api=llamada.de_integrador,
        )

    def _cortar(
        self,
        llamada: Llamada,
        result: dict[str, Any],
        motivo: str,
        *,
        etapa: str,
        texto: str | None = None,
        senales: list[str] | None = None,
    ) -> bytes:
        """Una tarea `failed` sin nada del proveedor, solo con el veredicto
        `bloqueado` del proxy. El motivo y el texto son fijos del proxy."""
        task_id = result.get("id") if isinstance(result.get("id"), str) else None
        contexto_id = (
            result.get("contextId") if isinstance(result.get("contextId"), str) else None
        ) or llamada.pedido.contexto_id
        decision = self._emitir_corte(llamada, contexto_id, motivo, etapa, senales)
        tarea = a2a.tarea_sintetica_bloqueada(
            task_id, contexto_id, texto or decision.texto, decision.artefacto()
        )
        return a2a.dumps(a2a.respuesta_jsonrpc(llamada.pedido.rpc_id, tarea))

    def _sse_cortar_etapa2(
        self,
        llamada: Llamada,
        est: EstadoStream,
        motivo: str,
        senales: list[str] | None = None,
    ) -> list[bytes]:
        """Corte del filtro en un stream: lo que sigue del proveedor no pasa."""
        contexto = est.contexto_id or llamada.pedido.contexto_id
        decision = self._emitir_corte(llamada, contexto, motivo, "seguridad", senales)
        est.cortado = True
        est.resuelto = True
        est.pendiente = None
        est.ofertas_registradas = True  # sin ofertas: un checkout después falla cerrado
        if not est.task_vista and est.task_id is None:
            return [self._bloque(
                llamada,
                a2a.tarea_sintetica_bloqueada(None, contexto, decision.texto, decision.artefacto()),
            )]
        return [
            self._bloque(llamada, a2a.evento_artefacto(est.task_id, contexto, decision.artefacto())),
            self._bloque(llamada, a2a.evento_fallido(est.task_id, contexto, decision.texto)),
        ]

    # -- checkout ------------------------------------------------------------

    async def _decidir_tarea(
        self, result: dict[str, Any], llamada: Llamada, n_checkouts: int
    ) -> Decision | None:
        """Una tarea (respuesta JSON) que trae ap2.checkout. None = passthrough."""
        task_id = result.get("id") if isinstance(result.get("id"), str) else None
        contexto = result.get("contextId") if isinstance(result.get("contextId"), str) else None
        checkouts = a2a.checkouts_del_result(result)
        if not llamada.guard_activo:
            previa = self._decisiones.get(task_id) if task_id else None
            if previa is not None and previa.retiene:
                # Una tarea retenida no se libera apagando el Guard.
                return previa
            if checkouts and isinstance(checkouts[0], dict):
                cart_id = llamada.pedido.cart_id or checkouts[0].get("cart_id")
                self._emitir_checkout_recibido(
                    checkouts[0], str(cart_id) if cart_id else None,
                    llamada.pedido.contexto_id or contexto, False,
                )
            return None
        pedido = llamada.pedido
        if len(checkouts) != n_checkouts:
            return self._bloqueo(
                llamada, pedido.cart_id, pedido.contexto_id or contexto,
                "el ap2.checkout vino fuera de los artefactos de la tarea",
            )
        if len(checkouts) > 1:
            return self._bloqueo(
                llamada, pedido.cart_id, pedido.contexto_id or contexto,
                f"la respuesta trae {len(checkouts)} ap2.checkout: se espera uno solo",
            )
        if not isinstance(checkouts[0], dict):
            return self._bloqueo(
                llamada, pedido.cart_id, pedido.contexto_id or contexto,
                "el ap2.checkout no tiene el formato esperado",
            )
        return await self._decidir_checkout(checkouts[0], llamada, task_id, contexto)

    async def _decidir_checkout(
        self,
        checkout: dict[str, Any],
        llamada: Llamada,
        task_id: str | None,
        contexto_respuesta: str | None,
    ) -> Decision:
        """Con el Guard prendido: valida que el checkout sea el del pedido y
        pide el veredicto (o reusa el de la caché, para tasks/get)."""
        pedido = llamada.pedido
        if pedido.tipo == "create_checkout":
            cart_id, contexto_id = pedido.cart_id, pedido.contexto_id
        elif pedido.es_de_tarea:
            previa = self._decisiones.get(task_id) if task_id else None
            if previa is None:
                return self._bloqueo(
                    llamada, None, contexto_respuesta,
                    "un ap2.checkout de una tarea que el proxy no evaluó",
                )
            if previa.retiene:
                return previa  # bloqueada o retenida: nunca devuelve el checkout
            # Cualquier cambio en el data part ap2.checkout (no solo el JWT) reevalúa.
            if previa.clave == (task_id, a2a.huella_checkout(checkout), previa.cart_id):
                return previa
            cart_id, contexto_id = previa.cart_id, previa.contexto_id  # cambió: se reevalúa
        else:
            return self._bloqueo(
                llamada, pedido.cart_id, contexto_respuesta or pedido.contexto_id,
                "un ap2.checkout en una respuesta que no es a create_checkout",
            )

        clave = (task_id, a2a.huella_checkout(checkout), cart_id)
        self._emitir_checkout_recibido(checkout, cart_id, contexto_id, True, llamada)

        def bloqueo(motivo: str, detalle: str | None = None) -> Decision:
            return self._bloqueo(llamada, cart_id, contexto_id, motivo, detalle, clave=clave)

        if not contexto_id:
            return bloqueo("el pedido de checkout no trae contextId")
        if contexto_respuesta != contexto_id:
            return bloqueo("la respuesta del comercio es de otro contexto que el pedido")
        if checkout.get("cart_id") != cart_id:
            return bloqueo("el ap2.checkout es de otro carrito que el que pidió el agente")
        guardadas = self._ofertas.get(contexto_id)
        if guardadas is None:
            return bloqueo(
                "no hay ofertas registradas para esta compra: el proxy no vio su find_products"
            )
        comercio = llamada.comercio or self.comercios.por_defecto
        if guardadas.comercio_id not in (None, comercio.id):
            return bloqueo("las ofertas de este contexto son de otro comercio")
        try:
            visto = CheckoutVisto.model_validate(checkout)
        except ValidationError as e:
            return bloqueo("el ap2.checkout no tiene el formato esperado", str(e))
        return await self._evaluar(llamada, checkout, visto, cart_id, contexto_id, guardadas, clave)

    def _emitir_checkout_recibido(
        self,
        checkout: dict[str, Any],
        cart_id: str | None,
        contexto_id: str | None,
        guard_activo: bool,
        llamada: Llamada | None = None,
    ) -> None:
        jwt = checkout.get("checkout_jwt")
        header, payload = a2a.decodificar_jwt(jwt)
        guardadas = self._ofertas.get(contexto_id) if contexto_id else None
        cart = None
        if guardadas:
            cart = next((c for c in guardadas.carts if c.get("cart_id") == cart_id), None)
        if cart is None:
            cart = {
                "cart_id": cart_id,
                "amount": checkout.get("amount"),
                "currency": checkout.get("currency"),
            }
        # Con el tope de esta compra (su ficha), no el pedido global. Sin
        # ficha (compra de integrador que no está), una vacía: el checkout se
        # bloquea igual en `_evaluar` y esto es solo para el dashboard.
        ficha = self._ficha_autorizada(llamada, contexto_id) or Ficha(
            pedido_original="", requisitos=[], confirmada=False
        )
        datos: dict[str, Any] = {
            "cart_id": cart_id,
            "checkout_jwt": jwt,
            "checkout_hash": checkout.get("checkout_hash"),
            "jwt_header": header,
            "jwt_payload": payload,
            "ap2_violaciones": violaciones_ap2(copy.deepcopy(cart), intent_ap2(ficha)),
        }
        if checkout.get("cart_id") != cart_id:
            datos["cart_id_checkout"] = checkout.get("cart_id")
        self._emitir(
            "checkout_recibido",
            datos,
            guard_activo=guard_activo,
            contexto_id=contexto_id,
            escenario=guardadas.escenario if guardadas else None,
            llamada=llamada,
        )

    async def _con_fabricante(
        self, catalogo: dict[str, dict], offer_details: list[dict[str, Any]]
    ) -> tuple[dict[str, dict], dict[str, Any]]:
        """El catálogo más la ficha del fabricante de cada GTIN de las ofertas
        (`fabricante.consultas`), y el resumen para el evento `veredicto`.

        El I/O vive acá, afuera de `evaluar()`: los datos entran en el mismo
        `catalogo` como `DatoExterno` (`guard.fuentes.con_fuentes`). Sin
        fuente, sin dato, con falla o si tarda de más, el Guard no tiene
        fuente independiente y escala lo obligatorio: nunca aprueba por eso.
        """
        from fabricante import datos_externos
        from fabricante.consultas import consultar_gtins, gtins_de_ofertas, resumen
        from guard.fuentes import con_fuentes

        if self.fabricante is None:
            return catalogo, resumen(None, [])
        gtins = gtins_de_ofertas(offer_details, catalogo)
        try:
            resultados = await asyncio.wait_for(
                self._en_hilo(self.ejecutor_llm, consultar_gtins, self.fabricante, gtins),
                _TIMEOUT_FABRICANTE_S,
            )
        except Exception as e:  # noqa: BLE001 - sin fabricante, lo obligatorio escala
            logger.warning("La fuente del fabricante no respondió: %s", type(e).__name__)
            datos = resumen(self.fabricante, [])
            datos["error"] = type(e).__name__
            return catalogo, datos
        return con_fuentes(catalogo, datos_externos(resultados)), resumen(self.fabricante, resultados)

    async def _evaluar(
        self,
        llamada: Llamada,
        checkout: dict[str, Any],
        visto: CheckoutVisto,
        cart_id: str,
        contexto_id: str,
        guardadas: OfertasGuardadas,
        clave: tuple[str | None, str, str | None],
    ) -> Decision:
        escenario = guardadas.escenario
        comercio = llamada.comercio or self.comercios.por_defecto
        catalogo, comercio_catalogo, fuente = await cargar_catalogo(
            self.client, comercio.catalogo_url, comercio.respaldo_fixture
        )
        # El comercio es el que FIRMÓ el checkout, no el que dice el catálogo
        # (que puede no existir). Un catálogo de otro comercio no se usa.
        comercio_id = comercio_firmado(visto.checkout_jwt)
        if not merchant_id_coincide(comercio, comercio_id):
            # Coherencia (no autenticidad: la firma la verifica AP2 después):
            # lo firmado tiene que ser del comercio de la ruta.
            return self._bloqueo(
                llamada, cart_id, contexto_id,
                "el checkout lo firmó un comercio distinto del de la ruta", clave=clave,
            )
        catalogo_descartado = None
        if catalogo and comercio_catalogo and comercio_id and comercio_catalogo != comercio_id:
            catalogo_descartado = (
                f"el catálogo es del comercio {comercio_catalogo!r} y el checkout "
                f"lo firmó {comercio_id!r}: se evalúa sin catálogo"
            )
            logger.warning("Catálogo descartado: %s", catalogo_descartado)
            catalogo, fuente = {}, "ninguno"
        ficha = self._ficha_autorizada(llamada, contexto_id)
        if ficha is None:
            return self._bloqueo(
                llamada, cart_id, contexto_id,
                "no se encontraron los parámetros que autorizó el integrador", clave=clave,
            )
        catalogo, fabricante = await self._con_fabricante(catalogo, guardadas.offer_details)
        # Un solo instante por evaluación: la vigencia del JWT y la fecha del
        # recibo (en el Guard) y la vista y la fila del veredicto. Es el reloj
        # de verdad, nunca DEMO_HOY.
        ahora = datetime.now(timezone.utc)
        con_ahora = {"ahora": ahora} if self.evaluador.acepta_ahora else {}
        try:
            resultado = self.evaluador.evaluar(
                ficha,  # parámetros autorizados de la compra
                OfertasVistas(
                    carts=copy.deepcopy(guardadas.carts),
                    offer_details=copy.deepcopy(guardadas.offer_details),
                    comercio_id=comercio_id,
                ),
                cart_id,
                catalogo,
                # Con merchant_ids, el chequeo de política del Guard también
                # ata lo firmado a la ruta; sin ellos, None (como siempre).
                politica=politica_de([comercio]),
                checkout_hash=checkout.get("checkout_hash"),
                checkout=visto,  # siempre: el Guard ata lo firmado al cart
                **con_ahora,
            )
            veredicto = (
                resultado
                if isinstance(resultado, Veredicto)
                else Veredicto.model_validate(
                    resultado.model_dump() if hasattr(resultado, "model_dump") else resultado
                )
            )
        except Exception as e:  # noqa: BLE001 - nunca aprobar si evaluar() falla
            logger.exception("evaluar() falló")
            return self._bloqueo(
                llamada, cart_id, contexto_id, "el Guard no pudo evaluar el checkout",
                f"evaluar() falló: {type(e).__name__}: {e}", clave=clave,
            )

        try:
            veredicto = firmar_recibo(veredicto, self.config.guard_key_path)
        except Exception as e:  # noqa: BLE001 - el recibo sin firma igual sirve
            logger.exception("No se pudo firmar el recibo")
            self._emitir(
                "error",
                {"mensaje": f"No se pudo firmar el recibo: {type(e).__name__}: {e}"},
                guard_activo=True,
                contexto_id=contexto_id,
                escenario=escenario,
                llamada=llamada,
            )
        recibo_firmado = bool(veredicto.recibo is not None and veredicto.recibo.firma)

        logger.info(
            "veredicto %s para %s (%s, catálogo %s): %s",
            veredicto.status, cart_id, self.evaluador.nombre, fuente, veredicto.motivo,
        )
        datos_veredicto: dict[str, Any] = {
            "veredicto": veredicto.model_dump(mode="json"),
            "fuente": fuente,  # "http", "fixture" (demo) o "ninguno"
            # Ficha del fabricante por GTIN: qué fuente (un fixture va rotulado)
            # y qué GTIN tuvieron dato.
            "fabricante": fabricante,
            "comercio_id": comercio_id,  # del checkout firmado
            "recibo_firmado": recibo_firmado,
            "evaluador": self.evaluador.nombre,  # "stub" = no es el Guard real
        }
        if catalogo_descartado:
            datos_veredicto["catalogo_descartado"] = catalogo_descartado
        # La aprobación humana se ata solo a lo firmado (N2): sin total entero
        # exacto en el JWT no se puede aprobar.
        centavos, moneda = a2a.monto_firmado(checkout) or (None, None)
        compra_api = self._compra_api(llamada, contexto_id)
        if compra_api is not None:
            # Antes del evento: el webhook `compra.veredicto` lleva la foto
            # con este veredicto (vista reducida, sin texto del comercio).
            compra_api.registrar_veredicto(
                vista_veredicto(
                    veredicto, contexto_id=contexto_id, ts=ahora, evaluador=self.evaluador.nombre
                )
            )
            self.en_fondo.programar(
                self.almacen.guardar_ultimo_veredicto(
                    fila_ultimo_veredicto(
                        contexto_id, cart_id, veredicto.status, centavos, moneda, ahora
                    )
                ),
                "último veredicto",
            )
            self.guardar_compra(compra_api)
        self._emitir(
            "veredicto",
            datos_veredicto,
            guard_activo=True,
            contexto_id=contexto_id,
            escenario=escenario,
            llamada=llamada,
        )
        self.aprobaciones.registrar_veredicto(
            contexto_id, cart_id, veredicto.status, centavos, moneda,
            de_api=compra_api is not None,
        )
        if veredicto.status in STATUS_LLEGAN_A_FIRMA:
            accion: Accion = "pasar"
        elif veredicto.status == "escalar":
            accion = "retener"
        else:
            accion = "bloquear"
        por_aprobar = False
        reclamada = None
        if veredicto.status == "escalar":
            if compra_api is not None and self.almacen.durable:
                reclamada = await self._reclamar_aprobacion(
                    contexto_id, cart_id, centavos, moneda
                )
                por_aprobar = reclamada is not None
            else:
                # Compra de integrador: solo la aprobación de su dueño (/v1).
                por_aprobar = (
                    self.aprobaciones.disponible(
                        contexto_id, cart_id, centavos, moneda, solo_api=compra_api is not None
                    )
                    is not None
                )
        return Decision(
            accion="pasar" if por_aprobar else accion,
            status=veredicto.status,
            motivo=veredicto.motivo,
            cart_id=cart_id,
            contexto_id=contexto_id,
            veredicto=veredicto,
            clave=clave,
            centavos=centavos,
            moneda=moneda,
            por_aprobar=por_aprobar,
            via_api=llamada.de_integrador,
            solo_api=compra_api is not None,
            reclamada=reclamada,
        )

    async def _reclamar_aprobacion(
        self, contexto_id: str, cart_id: str, centavos: int | None, moneda: str | None
    ) -> Aprobacion | None:
        """Gate durable de una compra de integrador (§1.7): la aprobación se
        hidrata del almacén si no está en memoria y se reclama ahí antes de
        usarla (`pendiente` → `usada`, atómico en la base). Si el reclamo no
        cambia la fila o el almacén falla, no se usa: se retiene.

        Devuelve la aprobación reclamada (o None): `_resolver` usa esa misma,
        así un checkout concurrente que perdió el reclamo y la descarta de
        memoria no la quema sin que pase ninguno.

        Si después el stream se corta, la aprobación queda gastada y hay que
        aprobar de nuevo (fail-closed)."""
        if self.aprobaciones.pendiente(contexto_id, cart_id) is None:
            try:
                fila = await self.almacen.cargar_aprobacion_pendiente(contexto_id, cart_id)
                if fila is not None:
                    self.aprobaciones.restaurar_pendiente(aprobacion_desde_fila(fila))
            except ErrorAlmacen as e:
                logger.error("No se pudo hidratar la aprobación de %s: %s", contexto_id, e)
                return None
        aprobacion = self.aprobaciones.disponible(
            contexto_id, cart_id, centavos, moneda, solo_api=True
        )
        if aprobacion is None:
            return None
        try:
            reclamada = await self.almacen.reclamar_aprobacion(
                aprobacion.id, usada=datetime.now(timezone.utc).isoformat(), task_id=None
            )
        except ErrorAlmacen as e:
            logger.error("No se pudo reclamar la aprobación %s: %s", aprobacion.id, e)
            return None
        if not reclamada:
            # Ya la usó otro (otro proceso o antes de un reinicio): fuera de memoria.
            self.aprobaciones.descartar(aprobacion.id)
            logger.warning("La aprobación %s ya estaba usada: se retiene", aprobacion.id)
            return None
        return aprobacion

    def _bloqueo(
        self,
        llamada: Llamada,
        cart_id: str | None,
        contexto_id: str | None,
        motivo: str,
        detalle: str | None = None,
        *,
        clave: tuple[str | None, str, str | None] | None = None,
    ) -> Decision:
        """Fail-closed: lo que el proxy no pudo evaluar no llega a la firma.

        El motivo es un texto fijo del proxy (va al agente); el detalle (que
        puede traer texto del comercio) solo va al evento `error`.
        """
        mensaje = f"No se pudo evaluar el checkout: {motivo}"
        if detalle:
            mensaje += f" ({detalle})"
        self._error(llamada, mensaje, contexto_id)
        return Decision(
            accion="bloquear",
            status="bloqueado",
            motivo=f"No se pudo evaluar el checkout: {motivo}.",
            cart_id=cart_id,
            contexto_id=contexto_id,
            clave=clave,
            via_api=llamada.de_integrador,
        )

    def _bloqueo_sintetico(
        self, llamada: Llamada, motivo: str, detalle: str | None = None
    ) -> bytes:
        """Respuesta JSON-RPC con una tarea `failed` armada por el proxy, para
        cuando no hay una tarea legible del comercio."""
        pedido = llamada.pedido
        decision = self._resolver(
            self._bloqueo(llamada, pedido.cart_id, pedido.contexto_id, motivo, detalle), None
        )
        self._emitir_resultado(decision, llamada)
        task = a2a.tarea_sintetica_bloqueada(
            None, pedido.contexto_id, decision.texto, decision.artefacto()
        )
        return a2a.dumps(a2a.respuesta_jsonrpc(pedido.rpc_id, task))

    def _resolver(self, decision: Decision, task_id: Any) -> Decision:
        """Última palabra antes de responder: si el Guard dio `escalar` y hay
        una aprobación humana para ese contexto, cart y monto, se usa (un solo
        uso). Guarda la decisión final en la caché de la tarea."""
        final = decision
        if decision.por_aprobar:
            tarea = task_id if isinstance(task_id, str) else None
            if decision.reclamada is not None:
                # Ya ganó el reclamo en el almacén: es suya.
                aprobacion: Aprobacion | None = self.aprobaciones.usar(decision.reclamada, tarea)
            else:
                aprobacion = self.aprobaciones.consumir(
                    decision.contexto_id or "",
                    decision.cart_id or "",
                    decision.centavos,
                    decision.moneda,
                    tarea,
                    solo_api=decision.solo_api,
                )
            final = replace(
                decision,
                accion="pasar" if aprobacion else "retener",
                por_aprobar=False,
                aprobacion=aprobacion,
                reclamada=None,
            )
            if aprobacion:
                if self.persistencia is not None:
                    self.persistencia.guardar_aprobacion(aprobacion)
                logger.warning(
                    "Aprobación humana %s usada: pasa el checkout de %s (contexto %s)",
                    aprobacion.id, decision.cart_id, decision.contexto_id,
                )
        if isinstance(task_id, str) and task_id:
            _recordar(self._decisiones, task_id, final)
        return final

    def _emitir_resultado(self, decision: Decision, llamada: Llamada) -> None:
        contexto_id = decision.contexto_id or llamada.pedido.contexto_id
        compra = self._compra_api(llamada, contexto_id)
        if not decision.retiene:
            datos: dict[str, Any] = {"metodo": "create_checkout"}
            if decision.aprobacion is not None:
                datos["aprobacion_humana"] = True
                datos["aprobacion_id"] = decision.aprobacion.id
                if compra is not None:
                    compra.aprobacion_pendiente = None  # ya se usó
                    self.guardar_compra(compra)
            logger.info("← reenviado: create_checkout contexto=%s", contexto_id)
            self._emitir(
                "reenviado",
                datos,
                guard_activo=llamada.guard_activo,
                contexto_id=contexto_id,
                escenario=self._escenario(contexto_id),
                llamada=llamada,
            )
            return
        logger.warning("✗ %s %s: %s", decision.status, decision.cart_id, decision.motivo)
        datos = {
            "cart_id": decision.cart_id,
            "motivo": decision.motivo,
            "status": decision.status,
            "requiere_aprobacion": decision.requiere_aprobacion,
        }
        if compra is not None:
            datos["compra_id"] = compra.id
            if decision.veredicto is None:
                # Bloqueo del proxy (no del Guard): sale como `compra.bloqueada`.
                # Uno del Guard ya salió como `compra.veredicto`.
                datos["sin_veredicto"] = True
            if decision.requiere_aprobacion and decision.contexto_id and decision.cart_id:
                # Antes del evento: el webhook `compra.requiere_aprobacion`
                # lleva la foto con lo que hay que aprobar (solo lo firmado).
                compra.aprobacion_pendiente = {
                    "contexto_id": decision.contexto_id,
                    "cart_id": decision.cart_id,
                    "centavos": decision.centavos,
                    "moneda": decision.moneda,
                    "motivo": decision.motivo,
                    "chequeos": a2a.veredicto_para_agente(decision.veredicto_json())["chequeos"],
                }
                self.guardar_compra(compra)
        self._emitir(
            "bloqueado",
            datos,
            guard_activo=llamada.guard_activo,
            contexto_id=contexto_id,
            escenario=self._escenario(contexto_id),
            llamada=llamada,
        )

    def _emitir_reenviado(
        self, llamada: Llamada, contexto_id: str | None, metodo: str | None = None
    ) -> None:
        pedido = llamada.pedido
        datos: dict[str, Any] = {"metodo": metodo or pedido.tipo}
        if pedido.mandatos:
            datos["mandatos"] = dict(pedido.mandatos)
        contexto_id = contexto_id or pedido.contexto_id
        logger.info("← reenviado: %s contexto=%s", datos["metodo"], contexto_id)
        self._emitir(
            "reenviado",
            datos,
            guard_activo=llamada.guard_activo,
            contexto_id=contexto_id,
            escenario=pedido.escenario or self._escenario(contexto_id),
            llamada=llamada,
        )

    def _emitir_error_comercio(self, error: Any, llamada: Llamada) -> None:
        detalle = error.get("message") if isinstance(error, dict) else error
        self._error(llamada, f"El comercio respondió un error JSON-RPC: {detalle}")

    # -- API de control (dashboard) -------------------------------------------

    async def get_state(self, request: Request) -> Response:
        return JSONResponse(self.estado(con_token=self._con_token(request)))

    async def post_state(self, request: Request) -> Response:
        body, error = a2a.parsear_json(await request.body())
        if error is not None:
            return JSONResponse({"error": "El cuerpo no es JSON"}, status_code=400)
        if not isinstance(body, dict) or not isinstance(body.get("guard_activo"), bool):
            return JSONResponse(
                {"error": 'Se espera {"guard_activo": true|false}'}, status_code=400
            )
        self.guard_activo = body["guard_activo"]
        logger.warning("Interruptor del Guard: %s", "sí" if self.guard_activo else "no")
        self._emitir(
            "guard_cambiado",
            {"guard_activo": self.guard_activo},
            guard_activo=self.guard_activo,
        )
        # El POST ya exige el token (ControlLocal).
        return JSONResponse(self.estado(con_token=self._con_token(request)))

    async def get_aprobaciones(self, request: Request) -> Response:
        listado = self.aprobaciones.listar()
        if not self._con_token(request):
            # Sin token, sin las de compras de integrador (ver `_visible`).
            listado = {
                clave: [a for a in lista if not self._contexto_de_integrador(a.get("contexto_id"))]
                for clave, lista in listado.items()
            }
        return JSONResponse(listado)

    def _contexto_de_integrador(self, contexto_id: Any) -> bool:
        if not isinstance(contexto_id, str):
            return False
        if contexto_id in self._contextos_api:
            return True
        return self._compra_api(None, contexto_id) is not None

    async def _contexto_es_de_integrador(self, contexto_id: Any) -> bool | None:
        """¿El contexto es de una compra de integrador? Primero la memoria y,
        si puede haber compras que no están en memoria (claves activas o un
        almacén durable, después de un reinicio), el almacén. None si el
        almacén falla: quien llama corta (fail-closed)."""
        if not isinstance(contexto_id, str) or not contexto_id:
            return False
        if self._contexto_de_integrador(contexto_id):
            return True
        if not (self.integradores.hay_claves() or self.almacen.durable):
            return False  # la demo sola: no hay compras de integrador
        if not es_id(ID_SEGURO_RE, contexto_id):
            return False  # uno así nunca se ata a una compra (`atar_contexto_api`)
        try:
            return await self.almacen.compra_de_contexto(contexto_id) is not None
        except ErrorAlmacen as e:
            logger.error("No se pudo saber de quién es el contexto %s: %s", contexto_id, e)
            return None

    async def post_aprobacion(self, request: Request) -> Response:
        body, error = a2a.parsear_json(await request.body())
        valido = (
            error is None
            and isinstance(body, dict)
            and set(body) == {"contexto_id", "cart_id"}
            and all(
                isinstance(body[k], str) and 0 < len(body[k]) <= _MAX_ID
                for k in ("contexto_id", "cart_id")
            )
        )
        if not valido:
            return JSONResponse(
                {"error": "se_espera", "formato": {"contexto_id": "str", "cart_id": "str"}},
                status_code=400,
            )
        # Las compras de integrador las aprueba su dueño por /v1 (§1.3). Se
        # mira la memoria, el almacén (la compra puede no estar en memoria) y
        # la marca del registro de aprobaciones: ninguna sola alcanza.
        contexto_id = body["contexto_id"]
        if self._compra_api(None, contexto_id) is not None:
            return _de_integrador()
        if es_id(ID_SEGURO_RE, contexto_id):
            try:
                dueno = await self.almacen.compra_de_contexto(contexto_id)
            except ErrorAlmacen as e:
                logger.error("No se pudo saber de quién es el contexto %s: %s", contexto_id, e)
                return JSONResponse({"error": "estado_no_disponible"}, status_code=503)
            if dueno is not None:
                return _de_integrador()
        try:
            aprobacion, creada = self.aprobaciones.aprobar(contexto_id, body["cart_id"])
        except DeIntegrador:
            return _de_integrador()
        except SinEscalar as e:
            return JSONResponse({"error": "sin_escalar", "detalle": str(e)}, status_code=409)
        if creada:
            if self.persistencia is not None:
                self.persistencia.guardar_aprobacion(aprobacion)
            logger.warning(
                "Aprobación humana %s registrada: contexto %s, %s, %s centavos",
                aprobacion.id, aprobacion.contexto_id, aprobacion.cart_id, aprobacion.centavos,
            )
        return JSONResponse(aprobacion.a_json(), status_code=201 if creada else 200)

    # -- etapa 2: compras (lo que contesta y decide el usuario) --------------

    async def get_compras(self, request: Request) -> Response:
        compras = self.compras.listar()
        if not self._con_token(request):
            # Sin token, sin las compras de integrador (ver `_visible`).
            compras = [c for c in compras if c.get("origen") != "api"]
        return JSONResponse({"compras": [self._compra_json(c) for c in compras]})

    async def post_contexto(self, request: Request) -> Response:
        """La respuesta del usuario a la pregunta del turno: `{compra_id,
        respuesta}`, `{compra_id, omitir: true}` o, en el turno de
        confirmación del intake, `{compra_id, confirmar: true}`."""
        body, error = a2a.parsear_json(await request.body())
        valido = error is None and isinstance(body, dict) and _compra_id_valido(body)
        if valido and set(body) == {"compra_id", "respuesta"}:
            respuesta = body["respuesta"]
            valido = isinstance(respuesta, str) and 0 < len(respuesta.strip()) <= LARGO_MAXIMO_RESPUESTA
        elif valido and set(body) == {"compra_id", "confirmar"}:
            valido = body["confirmar"] is True
        elif valido:
            valido = set(body) == {"compra_id", "omitir"} and body["omitir"] is True
        if not valido:
            return JSONResponse(
                {
                    "error": "se_espera",
                    "formato": [
                        {"compra_id": "str", "respuesta": f"str (1 a {LARGO_MAXIMO_RESPUESTA})"},
                        {"compra_id": "str", "omitir": True},
                        {"compra_id": "str", "confirmar": True},
                    ],
                },
                status_code=400,
            )
        if self._es_de_integrador(body["compra_id"]):
            return _de_integrador()
        if "confirmar" in body:
            return self._confirmar(body["compra_id"])
        texto = body["respuesta"].strip() if "respuesta" in body else None
        return self._resultado_compra(lambda: self.compras.responder(body["compra_id"], texto))

    async def post_pedido_usuario(self, request: Request) -> Response:
        """Lo que escribió el usuario en /demo al empezar una compra:
        `{texto}` (1 a 1000 caracteres). Lo usa el intake de la próxima
        compra nueva, una vez y antes de que venza (`proxy.pedido_usuario`).
        No devuelve el texto. Con el intermediario apagado o sin agente de
        intención no se guarda: ninguna compra lo usaría y quedaría
        esperando a otra."""
        body, error = a2a.parsear_json(await request.body())
        texto = body.get("texto") if isinstance(body, dict) else None
        valido = (
            error is None and isinstance(body, dict) and set(body) == {"texto"}
            and isinstance(texto, str) and 0 < len(texto.strip()) <= LARGO_MAXIMO_PEDIDO
        )
        if not valido:
            return JSONResponse(
                {"error": "se_espera", "formato": {"texto": f"str (1 a {LARGO_MAXIMO_PEDIDO})"}},
                status_code=400,
            )
        if not self.guard_activo or self.agente is None:
            motivo = "intermediario_apagado" if not self.guard_activo else "sin_agente_de_intencion"
            return JSONResponse({"registrado": False, "motivo": motivo})
        try:
            vence = self.pedido_usuario.registrar(texto)
        except ValueError:  # solo caracteres de control
            return JSONResponse(
                {"error": "se_espera", "formato": {"texto": f"str (1 a {LARGO_MAXIMO_PEDIDO})"}},
                status_code=400,
            )
        logger.info("Pedido del usuario registrado para la próxima compra")
        return JSONResponse({"registrado": True, "vence": vence.isoformat()})

    async def post_decision(self, request: Request) -> Response:
        """La decisión sobre la oportunidad: `{compra_id, decision}`."""
        body, error = a2a.parsear_json(await request.body())
        valido = (
            error is None
            and isinstance(body, dict)
            and set(body) == {"compra_id", "decision"}
            and _compra_id_valido(body)
            and body["decision"] in ("aceptar", "rechazar")
        )
        if not valido:
            return JSONResponse(
                {
                    "error": "se_espera",
                    "formato": {"compra_id": "str", "decision": "aceptar | rechazar"},
                },
                status_code=400,
            )
        if self._es_de_integrador(body["compra_id"]):
            return _de_integrador()
        return self._resultado_compra(
            lambda: self.compras.decidir(body["compra_id"], body["decision"])
        )

    def _es_de_integrador(self, compra_id: str) -> bool:
        """Las compras de integrador las autoriza su dueño por /v1, nunca el
        dashboard (que igual las ve: es la consola del operador)."""
        compra = self.compras.get(compra_id)
        return compra is not None and compra.origen == "api"

    def _resultado_compra(self, accion: Any) -> Response:
        try:
            compra = accion()
        except CompraDesconocida:
            return JSONResponse({"error": "compra_desconocida"}, status_code=404)
        except EstadoInvalido as e:
            return JSONResponse({"error": "estado", "estado": e.estado}, status_code=409)
        return JSONResponse(self._compra_json(compra.a_json()))

    async def historial(self, request: Request) -> Response:
        try:
            limit = int(request.query_params.get("limit", CAPACIDAD))
        except ValueError:
            return JSONResponse({"error": "limit tiene que ser un entero"}, status_code=400)
        if limit < 1:
            return JSONResponse({"error": "limit tiene que ser >= 1"}, status_code=400)
        eventos = [e for e in self.bus.historial() if self._visible(e, request)]
        eventos = eventos[-min(limit, CAPACIDAD):]
        return JSONResponse([e.model_dump(mode="json") for e in eventos])

    async def eventos_sse(self, request: Request) -> Response:
        suscripcion = self.bus.suscribir()
        if suscripcion is None:
            return JSONResponse({"error": "demasiados_suscriptores"}, status_code=503)
        backlog, cola = suscripcion

        def bloque(evento: Evento) -> bytes:
            # Los datos traen texto del comercio: mismo saneo que el SSE A2A (N7).
            return a2a.EventoSSE(evento.model_dump_json(), event="evento").serializar()

        async def generar() -> AsyncIterator[bytes]:
            try:
                for evento in backlog:
                    if self._visible(evento, request):
                        yield bloque(evento)
                while True:
                    try:
                        evento = await asyncio.wait_for(cola.get(), self.ping_segundos)
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            break
                        yield b": ping\n\n"
                        continue
                    if self._visible(evento, request):
                        yield bloque(evento)
            finally:
                self.bus.desuscribir(cola)

        return StreamingResponse(
            generar(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def public_key(self, request: Request) -> Response:
        if self.evaluador.nombre != "guard":
            return JSONResponse(
                {"error": "Sin Guard real: no hay clave del recibo"}, status_code=404
            )
        try:
            datos = clave_publica(self.config.guard_key_path)
        except Exception:  # noqa: BLE001
            logger.exception("No se pudo leer la clave del Guard")
            return JSONResponse({"error": "No se pudo leer la clave"}, status_code=500)
        if datos is None:
            return JSONResponse(
                {"error": "guard.recibo no expone la clave"}, status_code=404
            )
        return JSONResponse(datos)


def _compra_id_valido(body: dict[str, Any]) -> bool:
    compra_id = body.get("compra_id")
    return isinstance(compra_id, str) and 0 < len(compra_id) <= _MAX_ID


def _de_integrador() -> Response:
    return JSONResponse({"error": "compra_de_integrador"}, status_code=409)


def _integrador(request: Request) -> Integrador | None:
    """El integrador que dejó `AutenticacionGateway` (None = anónimo)."""
    integrador = getattr(request.state, "integrador", None)
    return integrador if isinstance(integrador, Integrador) else None


def _oyente(request: Request) -> str:
    return "publico" if getattr(request.state, "oyente", "local") == "publico" else "local"


def _leer_del_comercio(contenido: bytes | str) -> tuple[Any, str | None]:
    """JSON estricto de una respuesta (o de un evento SSE) del comercio, que
    además no esté anidado de más (`a2a.PROFUNDIDAD_MAXIMA`): una así no se
    puede copiar ni registrar en un evento, y se trata como ilegible."""
    body, error = a2a.parsear_json(contenido)
    if error is None and a2a.anidado_de_mas(body, a2a.PROFUNDIDAD_MAXIMA):
        return None, f"la respuesta tiene más de {a2a.PROFUNDIDAD_MAXIMA} niveles de anidamiento"
    return body, error


def _pedido_legible(raw: bytes) -> bool:
    """El pedido de un integrador: un objeto JSON estricto (sin NaN ni
    Infinity) con un `contextId`, si viene, de texto. Un `contextId` de otro
    tipo no se convierte: se rechaza."""
    body, error = a2a.parsear_json(raw)
    if error is not None or not isinstance(body, dict):
        return False
    params = body.get("params")
    mensaje = params.get("message") if isinstance(params, dict) else None
    if isinstance(mensaje, dict) and "contextId" in mensaje:
        contexto = mensaje["contextId"]
        return contexto is None or isinstance(contexto, str)
    return True


def _error_a2a(rpc_id: Any, codigo: str) -> Response:
    """Un error del tráfico A2A de integrador antes de reenviar (§1.2): HTTP
    con status y un error JSON-RPC con mensaje fijo y `data.codigo`."""
    status, mensaje = _ERRORES_A2A[codigo]
    cuerpo = a2a.error_jsonrpc(rpc_id, f"KaiAI: {mensaje}", code=_CODIGO_ERROR_A2A)
    cuerpo["error"]["data"] = {"codigo": codigo}
    return JSONResponse(cuerpo, status_code=status)


def _ofertas_de_fila(fila: Any) -> OfertasGuardadas | None:
    """Las ofertas de una fila de `gw_ofertas` (None si no hay o no sirve:
    sin ofertas, el checkout se bloquea)."""
    if not isinstance(fila, dict):
        return None
    carts, detalles = fila.get("carts"), fila.get("offer_details")
    escenario, comercio_id = fila.get("escenario"), fila.get("comercio_id")
    if not (
        isinstance(carts, list)
        and all(isinstance(c, dict) for c in carts)
        and isinstance(detalles, list)
        and all(isinstance(d, dict) for d in detalles)
        and (escenario is None or isinstance(escenario, str))
        and (comercio_id is None or isinstance(comercio_id, str))
    ):
        logger.error("Ofertas guardadas inválidas: se ignoran (el checkout se bloquea)")
        return None
    return OfertasGuardadas(
        carts=carts, offer_details=detalles, escenario=escenario, comercio_id=comercio_id
    )


def create_app(
    config: Config | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    evaluador: EvaluadorCargado | None = None,
    ping_segundos: float = 15.0,
    persistencia: Persistencia | None = None,
    agente: Any = None,
    filtro: Filtro | None = None,
    fabricante: Any = None,
    integradores: RegistroIntegradores | None = None,
    comercios: RegistroComercios | None = None,
    almacen: AlmacenEstado | None = None,
    despachador: Despachador | None = None,
    filtro_modo: str | None = None,
) -> Starlette:
    """Arma la app. `transport` permite inyectar el comercio en los tests.

    `persistencia` (Supabase), `agente` (intencion.Agente), `filtro`
    (contracts.Filtro, de Dev 1, y `filtro_modo`, lo que dice su
    `describir()`) y `fabricante` (fabricante.FuenteFabricante) los arma
    `python -m proxy` desde el entorno; acá no se lee el entorno, así
    los tests nunca escriben en la base real ni llaman a un LLM.

    Gateway de producción (todo opcional; sin esto, la app es la de siempre):
    `integradores` (claves de API; con al menos una activa se reconoce el
    oyente público por `config.gateway_port`), `comercios` (registro de
    comercios; sin él, el comercio de siempre), `almacen` (estado de las
    compras de integrador; sin él, en memoria) y `despachador` (webhooks;
    sin él, se arma uno si algún integrador tiene webhook).
    """
    from proxy.gateway import NucleoProxy  # gateway importa este módulo

    config = config or Config.desde_entorno()
    # Fail-closed antes de abrir nada: sin Guard real (o stub sin permiso) no
    # hay app (EvaluadorNoDisponible).
    evaluador = evaluador or cargar_evaluador(config.permitir_stub)
    validar_evaluador(evaluador, config.permitir_stub)
    client = httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(600.0, connect=5.0),  # create_checkout usa un LLM
        trust_env=False,
    )
    integradores = integradores or RegistroIntegradores.vacio()
    almacen = almacen if almacen is not None else EstadoMemoria()
    if despachador is None and any(i.webhook is not None for i in integradores.todos()):
        despachador = Despachador(
            integradores.get, almacen=almacen, permitir_inseguro=config.webhooks_inseguros
        )
    proxy = ProxyGuard(
        config, client, evaluador, ping_segundos,
        persistencia=persistencia, agente=agente, filtro=filtro, fabricante=fabricante,
        integradores=integradores, comercios=comercios, almacen=almacen,
        despachador=despachador, filtro_modo=filtro_modo,
    )
    nucleo = NucleoProxy(proxy)
    # El oyente público solo existe con claves: sin ellas, todo pedido es local.
    puertos_publicos = {config.gateway_port} if integradores.hay_claves() else set()
    if config.api_token_path is not None:
        # Para el servidor del dashboard (lo lee y lo agrega en su reenvío).
        escribir_token(config.api_token_path, proxy.api_token)
        logger.info("Token de la API de control en %s", config.api_token_path)
    try:
        # La clave del recibo se crea al arrancar, no en la primera decisión.
        if preparar_clave(config.guard_key_path):
            logger.info("Clave del recibo lista en %s", config.guard_key_path)
    except Exception:  # noqa: BLE001 - sin clave el recibo va sin firma
        logger.exception("No se pudo preparar la clave del recibo")

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        yield
        proxy.cerrar_hilos()
        if despachador is not None:
            await despachador.cerrar()
        await proxy.en_fondo.cerrar()
        await almacen.cerrar()
        if persistencia is not None:
            await persistencia.cerrar()
        await client.aclose()

    rpc_path = config.rpc_path
    app = Starlette(
        routes=[
            # Antes que las viejas: `/a2a/m/{id}` es un comercio del registro.
            Route("/a2a/m/{comercio}", proxy.rpc, methods=["POST"]),
            Route("/a2a/m/{comercio}/{resto:path}", proxy.card, methods=["GET"]),
            Route(rpc_path, proxy.rpc, methods=["POST"]),
            Route(f"{rpc_path}/{{resto:path}}", proxy.card, methods=["GET"]),
            Route("/guard/state", proxy.get_state, methods=["GET"]),
            Route("/guard/state", proxy.post_state, methods=["POST"]),
            Route("/guard/aprobaciones", proxy.get_aprobaciones, methods=["GET"]),
            Route("/guard/aprobaciones", proxy.post_aprobacion, methods=["POST"]),
            Route("/guard/compras", proxy.get_compras, methods=["GET"]),
            Route("/guard/compras/contexto", proxy.post_contexto, methods=["POST"]),
            Route("/guard/compras/decision", proxy.post_decision, methods=["POST"]),
            Route("/guard/compras/pedido", proxy.post_pedido_usuario, methods=["POST"]),
            Route("/guard/events", proxy.eventos_sse, methods=["GET"]),
            Route("/guard/events/history", proxy.historial, methods=["GET"]),
            Route("/guard/public-key", proxy.public_key, methods=["GET"]),
            *rutas_v1(
                nucleo, max_cuerpo_bytes=config.max_cuerpo_v1,
                url_publica=config.gateway_public_url,
            ),
        ],
        middleware=[
            # El más externo: decide el oyente, autentica y cierra el público
            # a /a2a y /v1 (en el local, /guard/* sigue con ControlLocal).
            Middleware(
                AutenticacionGateway, registro=integradores, puertos_publicos=puertos_publicos
            ),
            Middleware(
                ControlLocal,
                hosts=config.hosts_permitidos,
                origenes=config.dashboard_origins,
                rutas_post_json=RUTAS_POST_CONTROL,
                token=proxy.api_token,
            ),
            Middleware(
                CORSMiddleware,
                allow_origins=list(config.dashboard_origins),
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["content-type"],
            ),
        ],
        lifespan=lifespan,
    )
    app.state.proxy = proxy
    app.state.nucleo = nucleo
    return app
