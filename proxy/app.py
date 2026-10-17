"""Proxy A2A del Pre-Sign Guard (Starlette + httpx).

Se mete entre el Shopping Agent y el Merchant Agent sin tocar AP2:

- Sirve la Agent Card del comercio con su propia URL, así el cliente A2A no
  lo saltea.
- Reenvía el JSON-RPC tal cual (los bytes del pedido no se re-arman, así los
  mandatos llegan intactos) y soporta respuesta JSON y SSE.
- `find_products`: guarda carts, offer_details y escenario por context_id.
- `create_checkout`: llama a `evaluar()` y aplica el veredicto. Nunca
  modifica el `ap2.checkout`: si pasa, lo deja igual y agrega el veredicto en
  un artefacto aparte; si se bloquea, la tarea vuelve `failed` sin el
  checkout.
- Emite `Evento`s para el dashboard (SSE + JSONL) y expone el interruptor.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from contracts import Evento, Ficha, OfertasVistas, Veredicto
from proxy import a2a
from proxy.config import Config
from proxy.evaluador import (
    EvaluadorCargado,
    cargar_evaluador,
    clave_publica,
    firmar_recibo,
)
from proxy.eventos import CAPACIDAD, BusDeEventos
from proxy.fuentes import cargar_catalogo, cargar_intent, violaciones_ap2

logger = logging.getLogger("proxy")

# Headers que no se copian de un salto al otro.
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
_NO_HACIA_COMERCIO = _HOP_BY_HOP | {"host", "content-length", "accept-encoding"}
_NO_HACIA_CLIENTE = _HOP_BY_HOP | {"content-length", "content-encoding", "date", "server"}

_MAX_CONTEXTOS = 500


def _filtrar(headers: Any, excluidos: set[str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in excluidos}


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
    """Qué hacer con un checkout. `veredicto` es None si no se pudo evaluar."""

    bloquear: bool
    texto: str
    veredicto: Veredicto | None

    @property
    def artefacto(self) -> dict[str, Any] | None:
        if self.veredicto is None:
            return None
        return a2a.artefacto_veredicto(self.veredicto.model_dump(mode="json"))


class ProxyGuard:
    def __init__(
        self,
        config: Config,
        client: httpx.AsyncClient,
        evaluador: EvaluadorCargado,
        ping_segundos: float = 15.0,
    ) -> None:
        self.config = config
        self.client = client
        self.evaluador = evaluador
        self.guard_activo = config.guard_activo
        self.bus = BusDeEventos(config.events_jsonl)
        self.ping_segundos = ping_segundos
        self._ofertas: OrderedDict[str, OfertasGuardadas] = OrderedDict()
        self._decisiones: OrderedDict[str, Decision] = OrderedDict()
        self._ficha: Ficha | None = None

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
        return self.bus.emitir(
            tipo,
            datos,
            guard_activo=guard_activo,
            contexto_id=contexto_id,
            escenario=escenario,
        )

    def _escenario(self, contexto_id: str | None) -> str | None:
        guardadas = self._ofertas.get(contexto_id) if contexto_id else None
        return guardadas.escenario if guardadas else None

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
        url = f"{self.config.merchant_upstream_url}/{resto}"
        try:
            resp = await self.client.get(
                url, headers=_filtrar(request.headers, _NO_HACIA_COMERCIO)
            )
        except httpx.HTTPError as e:
            mensaje = f"El comercio no responde en {url}: {type(e).__name__}: {e}"
            logger.error(mensaje)
            self._emitir("error", {"mensaje": mensaje}, guard_activo=self.guard_activo)
            return JSONResponse({"error": mensaje}, status_code=502)
        headers = _filtrar(resp.headers, _NO_HACIA_CLIENTE)
        es_card = resto.endswith(("agent-card.json", "agent.json"))
        if resp.status_code != 200 or not es_card:
            return Response(resp.content, status_code=resp.status_code, headers=headers)
        try:
            card = resp.json()
        except ValueError:
            return JSONResponse({"error": "La Agent Card del comercio no es JSON"}, 502)
        if not isinstance(card, dict):
            return JSONResponse({"error": "La Agent Card del comercio no es un objeto"}, 502)
        logger.info("Agent Card: %s → %s", card.get("url"), self.config.proxy_public_url)
        return JSONResponse(a2a.reescribir_card(card, self.config.proxy_public_url))

    # -- JSON-RPC ------------------------------------------------------------

    async def rpc(self, request: Request) -> Response:
        raw = await request.body()
        pedido = a2a.leer_pedido(raw)
        guard_activo = self.guard_activo  # una sola lectura por pedido
        logger.info(
            "→ comercio: %s %s contexto=%s",
            pedido.metodo_rpc,
            pedido.tipo,
            pedido.contexto_id,
        )
        self._eventos_del_pedido(pedido, guard_activo)

        upstream = self.client.build_request(
            "POST",
            self.config.merchant_upstream_url,
            content=raw,  # los bytes tal cual: los mandatos no se tocan
            headers=_filtrar(request.headers, _NO_HACIA_COMERCIO),
        )
        try:
            resp = await self.client.send(upstream, stream=True)
        except httpx.HTTPError as e:
            mensaje = (
                f"Proxy del Guard: el comercio no responde en "
                f"{self.config.merchant_upstream_url} ({type(e).__name__}: {e})"
            )
            logger.error(mensaje)
            self._emitir(
                "error",
                {"mensaje": mensaje},
                guard_activo=guard_activo,
                contexto_id=pedido.contexto_id,
                escenario=pedido.escenario,
            )
            return JSONResponse(a2a.error_jsonrpc(pedido.rpc_id, mensaje), status_code=502)

        headers = _filtrar(resp.headers, _NO_HACIA_CLIENTE)
        if resp.headers.get("content-type", "").startswith("text/event-stream"):
            return StreamingResponse(
                self._sse(resp, pedido, guard_activo),
                status_code=resp.status_code,
                headers=headers,
            )
        try:
            contenido = await resp.aread()
        finally:
            await resp.aclose()
        nuevo = await self._procesar_json(contenido, pedido, guard_activo)
        return Response(nuevo, status_code=resp.status_code, headers=headers)

    def _eventos_del_pedido(self, pedido: a2a.Pedido, guard_activo: bool) -> None:
        if pedido.tipo == "find_products":
            self._emitir(
                "busqueda_enviada",
                {"catalog_search": pedido.catalog_search},
                guard_activo=guard_activo,
                contexto_id=pedido.contexto_id,
                escenario=pedido.escenario,
            )
        elif pedido.tipo == "create_checkout":
            self._emitir(
                "checkout_pedido",
                {"cart_id": pedido.cart_id},
                guard_activo=guard_activo,
                contexto_id=pedido.contexto_id,
                escenario=self._escenario(pedido.contexto_id),
            )

    async def _procesar_json(
        self, contenido: bytes, pedido: a2a.Pedido, guard_activo: bool
    ) -> bytes:
        try:
            body = json.loads(contenido)
        except (ValueError, UnicodeDecodeError):
            body = None
        if not isinstance(body, dict):
            self._emitir_reenviado(pedido, a2a.Respuesta(), guard_activo)
            return contenido
        if "error" in body:
            self._emitir_error_comercio(body["error"], pedido, guard_activo)
            return contenido
        result = body.get("result")
        respuesta = a2a.Respuesta()
        if isinstance(result, dict):
            respuesta.ingerir(result)
        self._registrar_ofertas(respuesta, pedido, guard_activo)

        if respuesta.checkout is None or result.get("kind") != "task":
            self._emitir_reenviado(pedido, respuesta, guard_activo)
            return contenido

        decision = await self._decidir(respuesta, pedido, guard_activo)
        if decision is None:  # Guard apagado: passthrough puro
            self._emitir_reenviado(pedido, respuesta, guard_activo)
            return contenido
        if decision.bloquear:
            nuevo = a2a.tarea_bloqueada(result, decision.texto, decision.artefacto)
        else:
            nuevo = a2a.tarea_con_artefacto(result, decision.artefacto)
        self._emitir_resultado(decision, respuesta, pedido, guard_activo)
        return a2a.dumps({**body, "result": nuevo})

    async def _sse(
        self, resp: httpx.Response, pedido: a2a.Pedido, guard_activo: bool
    ) -> AsyncIterator[bytes]:
        respuesta = a2a.Respuesta()
        ofertas_registradas = False
        checkout_visto = False
        cortado = False
        try:
            async for evento in a2a.leer_sse(resp.aiter_lines()):
                if cortado:
                    continue  # se drena el resto del comercio sin reenviarlo
                try:
                    body = json.loads(evento.data)
                except ValueError:
                    yield evento.serializar()
                    continue
                result = body.get("result") if isinstance(body, dict) else None
                if not isinstance(result, dict):
                    if isinstance(body, dict) and "error" in body:
                        self._emitir_error_comercio(body["error"], pedido, guard_activo)
                    yield evento.serializar()
                    continue

                respuesta.ingerir(result)
                if respuesta.checkout is not None and not checkout_visto:
                    checkout_visto = True
                    decision = await self._decidir(respuesta, pedido, guard_activo)
                    if decision is None:
                        yield evento.serializar()
                        continue
                    for bloque in self._sse_con_decision(body, result, respuesta, decision):
                        yield bloque
                    cortado = decision.bloquear
                    self._emitir_resultado(decision, respuesta, pedido, guard_activo)
                    continue

                if result.get("final") and not ofertas_registradas:
                    ofertas_registradas = True
                    self._registrar_ofertas(respuesta, pedido, guard_activo)
                yield evento.serializar()
        finally:
            await resp.aclose()
        if not ofertas_registradas:
            self._registrar_ofertas(respuesta, pedido, guard_activo)
        if not checkout_visto or not guard_activo:
            # Con Guard y checkout, el evento final ya salió en _emitir_resultado.
            self._emitir_reenviado(pedido, respuesta, guard_activo)

    def _sse_con_decision(
        self,
        body: dict[str, Any],
        result: dict[str, Any],
        respuesta: a2a.Respuesta,
        decision: Decision,
    ) -> list[bytes]:
        def bloque(nuevo_result: dict[str, Any]) -> bytes:
            return a2a.EventoSSE(a2a.dumps({**body, "result": nuevo_result}).decode()).serializar()

        task_id, contexto_id = respuesta.task_id, respuesta.contexto_id
        artefacto = decision.artefacto
        if result.get("kind") == "task":
            if decision.bloquear:
                return [bloque(a2a.tarea_bloqueada(result, decision.texto, artefacto))]
            return [bloque(a2a.tarea_con_artefacto(result, artefacto))]
        bloques = []
        if not decision.bloquear:
            bloques.append(bloque(result))  # el artefacto del checkout, intacto
        if artefacto is not None:
            bloques.append(bloque(a2a.evento_artefacto(task_id, contexto_id, artefacto)))
        if decision.bloquear:
            bloques.append(bloque(a2a.evento_fallido(task_id, contexto_id, decision.texto)))
        return bloques

    # -- ofertas y checkout ----------------------------------------------------

    def _registrar_ofertas(
        self, respuesta: a2a.Respuesta, pedido: a2a.Pedido, guard_activo: bool
    ) -> None:
        if not respuesta.carts:
            return
        contexto_id = respuesta.contexto_id or pedido.contexto_id
        escenario = respuesta.escenario or pedido.escenario
        guardadas = OfertasGuardadas(
            carts=list(respuesta.carts),
            offer_details=list(respuesta.offer_details or []),
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
            {"carts": guardadas.carts, "offer_details": guardadas.offer_details},
            guard_activo=guard_activo,
            contexto_id=contexto_id,
            escenario=escenario,
        )

    async def _decidir(
        self, respuesta: a2a.Respuesta, pedido: a2a.Pedido, guard_activo: bool
    ) -> Decision | None:
        """Registra el checkout y, si el Guard está activo, pide el veredicto."""
        checkout = respuesta.checkout or {}
        contexto_id = respuesta.contexto_id or pedido.contexto_id
        cart_id = str(checkout.get("cart_id") or pedido.cart_id or "")
        guardadas = self._ofertas.get(contexto_id) if contexto_id else None
        escenario = guardadas.escenario if guardadas else None

        previa = self._decisiones.get(respuesta.task_id) if respuesta.task_id else None
        if previa is not None and guard_activo:
            return previa  # p. ej. tasks/get de una tarea ya evaluada

        self._emitir_checkout_recibido(checkout, cart_id, guardadas, contexto_id, guard_activo)
        if not guard_activo:
            return None

        decision = await self._evaluar(checkout, cart_id, guardadas, contexto_id, escenario)
        if respuesta.task_id:
            _recordar(self._decisiones, respuesta.task_id, decision)
        return decision

    def _emitir_checkout_recibido(
        self,
        checkout: dict[str, Any],
        cart_id: str,
        guardadas: OfertasGuardadas | None,
        contexto_id: str | None,
        guard_activo: bool,
    ) -> None:
        jwt = checkout.get("checkout_jwt")
        header, payload = a2a.decodificar_jwt(jwt)
        cart = None
        if guardadas:
            cart = next((c for c in guardadas.carts if c.get("cart_id") == cart_id), None)
        if cart is None:
            cart = {
                "cart_id": cart_id,
                "amount": checkout.get("amount"),
                "currency": checkout.get("currency"),
            }
        self._emitir(
            "checkout_recibido",
            {
                "cart_id": cart_id,
                "checkout_jwt": jwt,
                "checkout_hash": checkout.get("checkout_hash"),
                "jwt_header": header,
                "jwt_payload": payload,
                "ap2_violaciones": violaciones_ap2(cart),
            },
            guard_activo=guard_activo,
            contexto_id=contexto_id,
            escenario=guardadas.escenario if guardadas else None,
        )

    async def _evaluar(
        self,
        checkout: dict[str, Any],
        cart_id: str,
        guardadas: OfertasGuardadas | None,
        contexto_id: str | None,
        escenario: str | None,
    ) -> Decision:
        def no_evaluable(motivo: str) -> Decision:
            logger.error(motivo)
            self._emitir(
                "error",
                {"mensaje": motivo},
                guard_activo=True,
                contexto_id=contexto_id,
                escenario=escenario,
            )
            texto = (
                f"Pre-Sign Guard: no se pudo evaluar el checkout de {cart_id} "
                f"({motivo}). No se presenta para firma."
            )
            return Decision(bloquear=True, texto=texto, veredicto=None)

        if guardadas is None:
            return no_evaluable(
                f"no hay ofertas registradas para el contexto {contexto_id}: "
                "el proxy no vio el find_products de esta compra"
            )
        catalogo, comercio_id = await cargar_catalogo(self.client, self.config.catalogo_url)
        try:
            resultado = self.evaluador.evaluar(
                self.ficha(),
                OfertasVistas(
                    carts=guardadas.carts,
                    offer_details=guardadas.offer_details,
                    comercio_id=comercio_id,
                ),
                cart_id,
                catalogo,
                politica=None,
                checkout_hash=checkout.get("checkout_hash"),
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
            return no_evaluable(f"evaluar() falló: {type(e).__name__}: {e}")

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

        logger.info(
            "veredicto %s para %s (%s): %s",
            veredicto.status,
            cart_id,
            self.evaluador.nombre,
            veredicto.motivo,
        )
        self._emitir(
            "veredicto",
            {"veredicto": veredicto.model_dump(mode="json")},
            guard_activo=True,
            contexto_id=contexto_id,
            escenario=escenario,
        )
        bloquear = veredicto.status == "bloqueado"
        texto = (
            f"Pre-Sign Guard bloqueó el checkout de {cart_id}: {veredicto.motivo} "
            "No se presenta para firma."
            if bloquear
            else veredicto.motivo
        )
        return Decision(bloquear=bloquear, texto=texto, veredicto=veredicto)

    def _emitir_resultado(
        self,
        decision: Decision,
        respuesta: a2a.Respuesta,
        pedido: a2a.Pedido,
        guard_activo: bool,
    ) -> None:
        contexto_id = respuesta.contexto_id or pedido.contexto_id
        if not decision.bloquear:
            self._emitir_reenviado(pedido, respuesta, guard_activo)
            return
        cart_id = (respuesta.checkout or {}).get("cart_id") or pedido.cart_id
        motivo = decision.veredicto.motivo if decision.veredicto else decision.texto
        logger.warning("✗ bloqueado %s: %s", cart_id, motivo)
        self._emitir(
            "bloqueado",
            {"cart_id": cart_id, "motivo": motivo},
            guard_activo=guard_activo,
            contexto_id=contexto_id,
            escenario=self._escenario(contexto_id),
        )

    def _emitir_reenviado(
        self, pedido: a2a.Pedido, respuesta: a2a.Respuesta, guard_activo: bool
    ) -> None:
        metodo = pedido.tipo
        if respuesta.checkout is not None:
            metodo = "create_checkout"
        datos: dict[str, Any] = {"metodo": metodo}
        if pedido.mandatos:
            datos["mandatos"] = dict(pedido.mandatos)
        contexto_id = respuesta.contexto_id or pedido.contexto_id
        logger.info("← reenviado: %s contexto=%s", metodo, contexto_id)
        self._emitir(
            "reenviado",
            datos,
            guard_activo=guard_activo,
            contexto_id=contexto_id,
            escenario=respuesta.escenario or pedido.escenario or self._escenario(contexto_id),
        )

    def _emitir_error_comercio(
        self, error: Any, pedido: a2a.Pedido, guard_activo: bool
    ) -> None:
        detalle = error.get("message") if isinstance(error, dict) else error
        self._emitir(
            "error",
            {"mensaje": f"El comercio respondió un error JSON-RPC: {detalle}"},
            guard_activo=guard_activo,
            contexto_id=pedido.contexto_id,
            escenario=pedido.escenario,
        )

    # -- API de control (dashboard) -------------------------------------------

    async def get_state(self, request: Request) -> Response:
        return JSONResponse(self.estado())

    async def post_state(self, request: Request) -> Response:
        try:
            body = json.loads(await request.body())
        except (ValueError, UnicodeDecodeError):
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
        backlog, cola = self.bus.suscribir()

        def bloque(evento: Evento) -> bytes:
            return f"event: evento\ndata: {evento.model_dump_json()}\n\n".encode("utf-8")

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
        except Exception as e:  # noqa: BLE001
            logger.exception("No se pudo leer la clave del Guard")
            return JSONResponse({"error": f"No se pudo leer la clave: {e}"}, status_code=500)
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
) -> Starlette:
    """Arma la app. `transport` permite inyectar el comercio en los tests."""
    config = config or Config.desde_entorno()
    client = httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(600.0, connect=5.0),  # create_checkout usa un LLM
        trust_env=False,
    )
    proxy = ProxyGuard(config, client, evaluador or cargar_evaluador(), ping_segundos)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        yield
        await client.aclose()

    rpc_path = config.rpc_path
    app = Starlette(
        routes=[
            Route(rpc_path, proxy.rpc, methods=["POST"]),
            Route(f"{rpc_path}/{{resto:path}}", proxy.card, methods=["GET"]),
            Route("/guard/state", proxy.get_state, methods=["GET"]),
            Route("/guard/state", proxy.post_state, methods=["POST"]),
            Route("/guard/events", proxy.eventos_sse, methods=["GET"]),
            Route("/guard/events/history", proxy.historial, methods=["GET"]),
            Route("/guard/public-key", proxy.public_key, methods=["GET"]),
        ],
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=list(config.dashboard_origins),
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["*"],
            )
        ],
        lifespan=lifespan,
    )
    app.state.proxy = proxy
    return app
