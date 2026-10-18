"""Proxy A2A del Pre-Sign Guard (Starlette + httpx).

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
import copy
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import httpx
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from contracts import (
    GUARD_VEREDICTO_KEY,
    STATUS_LLEGAN_A_FIRMA,
    CheckoutVisto,
    Evento,
    Ficha,
    OfertasVistas,
    Veredicto,
)
from proxy import a2a
from proxy.aprobaciones import Aprobacion, RegistroAprobaciones, SinEscalar
from proxy.config import Config
from proxy.evaluador import (
    EvaluadorCargado,
    cargar_evaluador,
    clave_publica,
    firmar_recibo,
    preparar_clave,
    validar_evaluador,
)
from proxy.eventos import CAPACIDAD, BusDeEventos
from proxy.fuentes import cargar_catalogo, cargar_intent, violaciones_ap2
from proxy.persistencia import Persistencia
from proxy.seguridad import ControlLocal, escribir_token, generar_token

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
_NO_HACIA_COMERCIO = _HOP_BY_HOP | {"host", "content-length", "accept-encoding", "cookie"}
# Del comercio al cliente solo pasa esto (el Content-Type se normaliza). Nunca
# Set-Cookie: las cookies no distinguen puerto y llegarían al dashboard.
_DEL_COMERCIO_AL_CLIENTE = {"x-a2a-extensions"}

# Únicas rutas de la Agent Card que se piden al comercio.
RUTAS_CARD = (".well-known/agent-card.json", ".well-known/agent.json")
RUTAS_POST_CONTROL = ("/guard/state", "/guard/aprobaciones")

_MAX_CONTEXTOS = 500
_MAX_ID = 200  # largo máximo de contexto_id / cart_id en la API de aprobaciones

# Plantillas fijas del texto para el agente: "Pre-Sign Guard: <status>. <motivo>"
# (el motivo lo arma guard/ sin texto del comercio). Ningún string del
# comercio entra acá.
_SUFIJO_RETENIDO = (
    "El checkout requiere aprobación humana: revisalo en el dashboard del "
    "Guard y, si se aprueba, reintentá create_checkout."
)
_SUFIJO_BLOQUEADO = "No se presenta para firma."

Accion = Literal["pasar", "retener", "bloquear"]


def _filtrar(headers: Any, excluidos: set[str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in excluidos}


def _del_comercio(headers: Any) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() in _DEL_COMERCIO_AL_CLIENTE}


def _recordar(cache: OrderedDict, clave: Any, valor: Any) -> None:
    cache[clave] = valor
    cache.move_to_end(clave)
    while len(cache) > _MAX_CONTEXTOS:
        cache.popitem(last=False)


@dataclass(frozen=True)
class OfertasGuardadas:
    carts: list[dict[str, Any]]
    offer_details: list[dict[str, Any]]
    escenario: str | None


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

    @property
    def retiene(self) -> bool:
        return self.accion != "pasar"

    @property
    def requiere_aprobacion(self) -> bool:
        return self.accion == "retener"

    @property
    def texto(self) -> str:
        base = f"Pre-Sign Guard: {self.status}. {self.motivo}"
        if self.accion == "retener":
            return f"{base} {_SUFIJO_RETENIDO}"
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
    ) -> None:
        # Fail-closed: sin un evaluar() que ate el checkout (o con el stub sin
        # PROXY_ALLOW_STUB=1) el proxy no se arma.
        validar_evaluador(evaluador, config.permitir_stub)
        self.config = config
        self.client = client
        self.evaluador = evaluador
        self.guard_activo = config.guard_activo
        self.bus = BusDeEventos(
            config.events_jsonl, max_suscriptores=config.max_suscriptores_sse
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
        if evaluador.nombre == "stub":
            logger.warning(
                "EVALUADOR STUB (PROXY_ALLOW_STUB=1): los veredictos NO son del "
                "Guard real; /guard/state y cada evento veredicto lo marcan."
            )

    # -- utilidades ----------------------------------------------------------

    def _emitir(
        self,
        tipo: str,
        datos: dict[str, Any],
        *,
        guard_activo: bool,
        contexto_id: str | None = None,
        escenario: str | None = None,
    ) -> Evento:
        evento = self.bus.emitir(
            tipo,
            datos,
            guard_activo=guard_activo,
            contexto_id=contexto_id,
            escenario=escenario,
        )
        if self.persistencia is not None:
            self.persistencia.guardar_evento(evento)  # solo los `veredicto`
        return evento

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
        )

    def ficha(self) -> Ficha:
        if self._ficha is None:
            self._ficha = Ficha.desde_intent(cargar_intent())
        return self._ficha

    def estado(self) -> dict[str, Any]:
        return {
            "guard_activo": self.guard_activo,
            "merchant_url": self.config.merchant_upstream_url,
            "evaluador": self.evaluador.nombre,
        }

    # -- Agent Card ----------------------------------------------------------

    async def card(self, request: Request) -> Response:
        resto = request.path_params["resto"]
        if resto not in RUTAS_CARD:
            return JSONResponse({"error": "no_encontrado"}, status_code=404)
        url = f"{self.config.merchant_upstream_url}/{resto}"
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
        card, _ = a2a.parsear_json(contenido)
        if not isinstance(card, dict):
            return JSONResponse({"error": "card_ilegible"}, status_code=502)
        logger.info("Agent Card: %s → %s", card.get("url"), self.config.proxy_public_url)
        # Solo el JSON reescrito: ningún header del comercio (Set-Cookie, etc.).
        return JSONResponse(a2a.reescribir_card(card, self.config.proxy_public_url))

    # -- JSON-RPC ------------------------------------------------------------

    async def rpc(self, request: Request) -> Response:
        raw = await request.body()
        llamada = Llamada(pedido=a2a.leer_pedido(raw), guard_activo=self.guard_activo)
        pedido = llamada.pedido
        logger.info(
            "→ comercio: %s %s contexto=%s", pedido.metodo_rpc, pedido.tipo, pedido.contexto_id
        )
        self._eventos_del_pedido(llamada)

        upstream = self.client.build_request(
            "POST",
            self.config.merchant_upstream_url,
            content=raw,  # los bytes tal cual: los mandatos no se tocan
            headers=_filtrar(request.headers, _NO_HACIA_COMERCIO),
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
        body, error = a2a.parsear_json(contenido)
        salida = contenido
        if error is None and isinstance(body, dict):
            # guard.veredicto solo lo pone el proxy.
            body, cambiado = a2a.quitar_veredictos_ajenos(body)
            try:
                salida = a2a.dumps(body) if cambiado else contenido
            except ValueError as e:  # defensa (N6): algo que no se puede reescribir
                error = f"{type(e).__name__}: {e}"[:200]
        if error is not None or not isinstance(body, dict):
            detalle = error or f"se esperaba un objeto JSON y llegó {type(body).__name__}"
            return self._respuesta_ilegible(contenido, llamada, status, detalle)

        result = body.get("result")
        n_checkouts = a2a.contar_clave(body, a2a.CHECKOUT_KEY)

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
                self._registrar_ofertas(respuesta, llamada)
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
        except ValueError as e:  # defensa (N6): nunca un 500 sin veredicto ni evento
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
        except ValueError as e:  # defensa (N6): un evento que no se puede reescribir
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
        if not est.resuelto and not est.cortado:
            metodo = "create_checkout" if est.checkouts else None
            self._emitir_reenviado(llamada, est.contexto_id, metodo=metodo)

    async def _sse_evento(
        self, evento: a2a.EventoSSE, llamada: Llamada, est: EstadoStream
    ) -> list[bytes]:
        pedido, activo = llamada.pedido, llamada.guard_activo
        body, error = a2a.parsear_json(evento.data)
        crudo = b""
        if error is None and isinstance(body, dict):
            body, cambiado = a2a.quitar_veredictos_ajenos(body)
            try:
                crudo = a2a.bloque_sse(body) if cambiado else evento.serializar()
            except ValueError as e:  # defensa (N6)
                error = f"{type(e).__name__}: {e}"[:200]
        if error is not None or not isinstance(body, dict):
            detalle = error or "el evento no es un objeto JSON"
            datos = evento.data.encode("utf-8")
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
                    self._registrar_ofertas(est.ofertas, llamada)
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

    def _registrar_ofertas(self, respuesta: a2a.Respuesta, llamada: Llamada) -> None:
        """Solo desde la respuesta a un find_products del cliente."""
        pedido = llamada.pedido
        if pedido.tipo != "find_products" or not respuesta.carts:
            return
        contexto_id = respuesta.contexto_id or pedido.contexto_id
        escenario = respuesta.escenario or pedido.escenario
        guardadas = OfertasGuardadas(
            carts=copy.deepcopy(respuesta.carts),
            offer_details=copy.deepcopy(respuesta.offer_details or []),
            escenario=escenario,
        )
        if contexto_id:
            _recordar(self._ofertas, contexto_id, guardadas)
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
        )

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
        self._emitir_checkout_recibido(checkout, cart_id, contexto_id, True)

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
        datos: dict[str, Any] = {
            "cart_id": cart_id,
            "checkout_jwt": jwt,
            "checkout_hash": checkout.get("checkout_hash"),
            "jwt_header": header,
            "jwt_payload": payload,
            "ap2_violaciones": violaciones_ap2(copy.deepcopy(cart)),
        }
        if checkout.get("cart_id") != cart_id:
            datos["cart_id_checkout"] = checkout.get("cart_id")
        self._emitir(
            "checkout_recibido",
            datos,
            guard_activo=guard_activo,
            contexto_id=contexto_id,
            escenario=guardadas.escenario if guardadas else None,
        )

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
        catalogo, comercio_id, fuente = await cargar_catalogo(
            self.client, self.config.catalogo_url
        )
        try:
            resultado = self.evaluador.evaluar(
                self.ficha(),
                OfertasVistas(
                    carts=copy.deepcopy(guardadas.carts),
                    offer_details=copy.deepcopy(guardadas.offer_details),
                    comercio_id=comercio_id,
                ),
                cart_id,
                catalogo,
                politica=None,
                checkout_hash=checkout.get("checkout_hash"),
                checkout=visto,  # siempre: el Guard ata lo firmado al cart
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
            )
        recibo_firmado = bool(veredicto.recibo is not None and veredicto.recibo.firma)

        logger.info(
            "veredicto %s para %s (%s, catálogo %s): %s",
            veredicto.status, cart_id, self.evaluador.nombre, fuente, veredicto.motivo,
        )
        self._emitir(
            "veredicto",
            {
                "veredicto": veredicto.model_dump(mode="json"),
                "fuente": fuente,
                "recibo_firmado": recibo_firmado,
                "evaluador": self.evaluador.nombre,  # "stub" = no es el Guard real
            },
            guard_activo=True,
            contexto_id=contexto_id,
            escenario=escenario,
        )
        # La aprobación humana se ata solo a lo firmado (N2): sin total entero
        # exacto en el JWT no se puede aprobar.
        centavos, moneda = a2a.monto_firmado(checkout) or (None, None)
        self.aprobaciones.registrar_veredicto(
            contexto_id, cart_id, veredicto.status, centavos, moneda
        )
        if veredicto.status in STATUS_LLEGAN_A_FIRMA:
            accion: Accion = "pasar"
        elif veredicto.status == "escalar":
            accion = "retener"
        else:
            accion = "bloquear"
        por_aprobar = (
            veredicto.status == "escalar"
            and self.aprobaciones.disponible(contexto_id, cart_id, centavos, moneda) is not None
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
        )

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
            aprobacion = self.aprobaciones.consumir(
                decision.contexto_id or "",
                decision.cart_id or "",
                decision.centavos,
                decision.moneda,
                task_id if isinstance(task_id, str) else None,
            )
            final = replace(
                decision,
                accion="pasar" if aprobacion else "retener",
                por_aprobar=False,
                aprobacion=aprobacion,
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
        if not decision.retiene:
            datos: dict[str, Any] = {"metodo": "create_checkout"}
            if decision.aprobacion is not None:
                datos["aprobacion_humana"] = True
                datos["aprobacion_id"] = decision.aprobacion.id
            logger.info("← reenviado: create_checkout contexto=%s", contexto_id)
            self._emitir(
                "reenviado",
                datos,
                guard_activo=llamada.guard_activo,
                contexto_id=contexto_id,
                escenario=self._escenario(contexto_id),
            )
            return
        logger.warning("✗ %s %s: %s", decision.status, decision.cart_id, decision.motivo)
        self._emitir(
            "bloqueado",
            {
                "cart_id": decision.cart_id,
                "motivo": decision.motivo,
                "status": decision.status,
                "requiere_aprobacion": decision.requiere_aprobacion,
            },
            guard_activo=llamada.guard_activo,
            contexto_id=contexto_id,
            escenario=self._escenario(contexto_id),
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
        )

    def _emitir_error_comercio(self, error: Any, llamada: Llamada) -> None:
        detalle = error.get("message") if isinstance(error, dict) else error
        self._error(llamada, f"El comercio respondió un error JSON-RPC: {detalle}")

    # -- API de control (dashboard) -------------------------------------------

    async def get_state(self, request: Request) -> Response:
        return JSONResponse(self.estado())

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
        return JSONResponse(self.estado())

    async def get_aprobaciones(self, request: Request) -> Response:
        return JSONResponse(self.aprobaciones.listar())

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
        try:
            aprobacion, creada = self.aprobaciones.aprobar(body["contexto_id"], body["cart_id"])
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

    async def historial(self, request: Request) -> Response:
        try:
            limit = int(request.query_params.get("limit", CAPACIDAD))
        except ValueError:
            return JSONResponse({"error": "limit tiene que ser un entero"}, status_code=400)
        if limit < 1:
            return JSONResponse({"error": "limit tiene que ser >= 1"}, status_code=400)
        eventos = self.bus.historial(min(limit, CAPACIDAD))
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
                    yield bloque(evento)
                while True:
                    try:
                        evento = await asyncio.wait_for(cola.get(), self.ping_segundos)
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            break
                        yield b": ping\n\n"
                        continue
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


def create_app(
    config: Config | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    evaluador: EvaluadorCargado | None = None,
    ping_segundos: float = 15.0,
    persistencia: Persistencia | None = None,
) -> Starlette:
    """Arma la app. `transport` permite inyectar el comercio en los tests.

    `persistencia` (Supabase) la arma `python -m proxy` desde el entorno; acá
    no se lee el entorno, así los tests nunca escriben en la base real.
    """
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
    proxy = ProxyGuard(config, client, evaluador, ping_segundos, persistencia=persistencia)
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
        if persistencia is not None:
            await persistencia.cerrar()
        await client.aclose()

    rpc_path = config.rpc_path
    app = Starlette(
        routes=[
            Route(rpc_path, proxy.rpc, methods=["POST"]),
            Route(f"{rpc_path}/{{resto:path}}", proxy.card, methods=["GET"]),
            Route("/guard/state", proxy.get_state, methods=["GET"]),
            Route("/guard/state", proxy.post_state, methods=["POST"]),
            Route("/guard/aprobaciones", proxy.get_aprobaciones, methods=["GET"]),
            Route("/guard/aprobaciones", proxy.post_aprobacion, methods=["POST"]),
            Route("/guard/events", proxy.eventos_sse, methods=["GET"]),
            Route("/guard/events/history", proxy.historial, methods=["GET"]),
            Route("/guard/public-key", proxy.public_key, methods=["GET"]),
        ],
        middleware=[
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
    return app
