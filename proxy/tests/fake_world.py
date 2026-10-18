"""Comercio y catálogo falsos para probar el proxy sin levantar AP2.

El comercio responde como el Merchant Agent del sample (A2A JSON-RPC,
`message/send` y `message/stream`) con los carts de `fixtures/scenario_*.json`
y un `ap2.checkout` con un JWT ES256 de verdad (firmado con una clave de
prueba), para poder verificar que la firma sigue valiendo después del proxy.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from a2a.types import (
    Artifact,
    DataPart,
    Message,
    Part,
    Role,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
)
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "fixtures"

MERCHANT_HOST = "merchant.test"
CATALOG_HOST = "catalog.test"
MERCHANT_URL = f"http://{MERCHANT_HOST}/a2a/merchant_agent"
CATALOG_URL = f"http://{CATALOG_HOST}"
PROXY_PUBLIC_URL = "http://localhost:8010/a2a/merchant_agent"

EXTENSION_URI = "https://github.com/google-agentic-commerce/ap2/v1"
CHECKOUT_MANDATE_KEY = "ap2.mandates.CheckoutMandateSdJwt"
PAYMENT_MANDATE_KEY = "ap2.mandates.PaymentMandateSdJwt"


def cargar_fixture(nombre: str) -> dict[str, Any]:
    ruta = FIXTURES / f"scenario_{nombre}.json"
    if not ruta.exists():  # etapa 2: fixtures escritos a mano (fixtures/etapa2/)
        ruta = FIXTURES / "etapa2" / f"{nombre}.json"
    return json.loads(ruta.read_text("utf-8"))


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def card_del_comercio(url: str = MERCHANT_URL) -> dict[str, Any]:
    """Igual que merchant_agent/agent.json, más una interfaz adicional."""
    return {
        "name": "MerchantAgent",
        "description": "A sales assistant agent for a merchant.",
        "url": url,
        "preferredTransport": "JSONRPC",
        "additionalInterfaces": [{"transport": "JSONRPC", "url": url}],
        "protocolVersion": "0.3.0",
        "version": "1.0.0",
        "defaultInputModes": ["json"],
        "defaultOutputModes": ["json"],
        "capabilities": {
            "extensions": [
                {
                    "uri": EXTENSION_URI,
                    "description": "Supports the Agent Payments Protocol.",
                    "required": True,
                }
            ]
        },
        "skills": [
            {
                "id": "search_catalog",
                "name": "Search Catalog",
                "description": "Searches the merchant's catalog.",
                "tags": ["merchant", "search", "catalog"],
            }
        ],
    }


class FakeMerchant:
    """Merchant Agent falso. Registra lo que recibe y lo que responde."""

    def __init__(self, escenario_por_defecto: str = "B") -> None:
        self.escenario_por_defecto = escenario_por_defecto
        self.clave = ec.generate_private_key(ec.SECP256R1())
        self.recibidos: list[tuple[bytes, dict[str, str]]] = []
        self.ultima_respuesta: bytes | None = None
        self.ultimo_checkout: dict[str, Any] | None = None
        self.checkouts: list[dict[str, Any]] = []
        self.tareas: dict[str, dict[str, Any]] = {}
        self._n = 0
        self._escenario_por_contexto: dict[str, str] = {}
        # Ganchos para que un test haga portar mal al comercio:
        # manipular(respuesta_jsonrpc, pedido) -> dict | bytes  (message/send)
        # manipular_sse(results, pedido) -> list[dict] | bytes   (message/stream)
        self.manipular: Any = None
        self.manipular_sse: Any = None
        self.headers_extra: dict[str, str] = {}
        self.content_type_json = "application/json"
        self.content_type_sse = "text/event-stream"
        self.card_headers: dict[str, str] = {}
        self.app = Starlette(
            routes=[
                Route(
                    "/a2a/merchant_agent/.well-known/agent-card.json",
                    self._card,
                    methods=["GET"],
                ),
                Route(
                    "/a2a/merchant_agent/.well-known/agent.json",
                    self._card,
                    methods=["GET"],
                ),
                Route("/a2a/merchant_agent/otra-cosa", self._otra, methods=["GET"]),
                Route("/a2a/merchant_agent", self._rpc, methods=["POST"]),
            ]
        )

    # -- firma ---------------------------------------------------------------

    def firmar_jwt(self, header: dict, payload: dict) -> str:
        h = _b64url(json.dumps(header, separators=(",", ":")).encode())
        p = _b64url(json.dumps(payload, separators=(",", ":")).encode())
        der = self.clave.sign(f"{h}.{p}".encode("ascii"), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        return f"{h}.{p}.{_b64url(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"

    def firma_valida(self, jwt: str) -> bool:
        h, p, s = jwt.split(".")
        raw = _b64url_decode(s)
        der = encode_dss_signature(
            int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
        )
        try:
            self.clave.public_key().verify(
                der, f"{h}.{p}".encode("ascii"), ec.ECDSA(hashes.SHA256())
            )
        except Exception:  # noqa: BLE001 - InvalidSignature
            return False
        return True

    def armar_checkout(
        self, cart: dict[str, Any], *, monto: float | None = None, cart_id: str | None = None
    ) -> dict[str, Any]:
        """El data part ap2.checkout, igual que merchant_agent/tools.py."""
        cart_id = cart_id or cart["cart_id"]
        monto = cart["amount"] if monto is None else monto
        amount_cents = int(round(monto * 100))
        # Como create_checkout() del sample: vigente desde ahora por una hora
        # (el Guard valida exp/iat contra `ahora`).
        iat = int(time.time())
        payload = {
            "id": cart_id,
            "merchant": {"id": "merchant_1", "name": "Generic Merchant"},
            "line_items": [
                {
                    "id": f"li_{uuid.uuid4().hex[:8]}",
                    "item": {
                        "id": cart_id,
                        "title": cart["item_label"],
                        "price": amount_cents,
                    },
                    "quantity": 1,
                }
            ],
            "currency": cart["currency"],
            "totals": [
                {"type": "subtotal", "amount": amount_cents},
                {"type": "total", "amount": amount_cents},
            ],
            "iat": iat,
            "exp": iat + 3600,
        }
        jwt = self.firmar_jwt(
            {"alg": "ES256", "typ": "JWT", "kid": "merchant-key-1"}, payload
        )
        return {
            "cart_id": cart_id,
            "checkout_jwt": jwt,
            "checkout_hash": _b64url(hashlib.sha256(jwt.encode()).digest()),
            "item_label": cart["item_label"],
            "amount": monto,
            "amount_cents": amount_cents,
            "currency": cart["currency"],
        }

    # -- rutas ---------------------------------------------------------------

    async def _card(self, request: Request) -> Response:
        return JSONResponse(card_del_comercio(), headers=self.card_headers)

    async def _otra(self, request: Request) -> Response:
        return Response(b"<html>no es una card</html>", media_type="text/html")

    async def _rpc(self, request: Request) -> Response:
        raw = await request.body()
        self.recibidos.append((raw, dict(request.headers)))
        body = json.loads(raw)
        if body.get("method") == "tasks/resubscribe":
            results = [self.tareas[body["params"]["id"]]]
            if self.manipular_sse is not None:
                results = self.manipular_sse(results, body)
            contenido = results if isinstance(results, bytes) else sse_de(results)
            self.ultima_respuesta = contenido
            return Response(
                contenido, headers={"content-type": self.content_type_sse, **self.headers_extra}
            )
        if body.get("method") == "tasks/get":
            task_json = self.tareas[body["params"]["id"]]
            respuesta = {"id": body.get("id"), "jsonrpc": "2.0", "result": task_json}
            if self.manipular is not None:
                respuesta = self.manipular(respuesta, body)
            contenido = (
                respuesta
                if isinstance(respuesta, bytes)
                else json.dumps(respuesta, ensure_ascii=False).encode("utf-8")
            )
            self.ultima_respuesta = contenido
            return Response(
                contenido, headers={"content-type": self.content_type_json, **self.headers_extra}
            )
        message = body["params"]["message"]
        data_parts = [
            p["data"] for p in message.get("parts", []) if p.get("kind") == "data"
        ]
        self._n += 1
        context_id = message.get("contextId") or f"ctx-{self._n}"
        task_id = f"task-{self._n}"
        artifacts, state, status_text = self._responder(context_id, data_parts)
        history = [Message.model_validate(message)]
        headers = dict(self.headers_extra)
        if request.headers.get("x-a2a-extensions"):
            headers["X-A2A-Extensions"] = request.headers["x-a2a-extensions"]

        if body.get("method") == "message/stream":
            contenido = self._sse(
                task_id, context_id, artifacts, state, status_text, history, body
            )
            self.ultima_respuesta = contenido
            return Response(
                contenido, headers={"content-type": self.content_type_sse, **headers}
            )

        task = Task(
            id=task_id,
            context_id=context_id,
            status=self._status(state, status_text, task_id, context_id),
            artifacts=artifacts or None,
            history=history,
        )
        task_json = task.model_dump(mode="json", by_alias=True, exclude_none=True)
        self.tareas[task_id] = task_json
        respuesta = {"id": body.get("id"), "jsonrpc": "2.0", "result": task_json}
        if self.manipular is not None:
            respuesta = self.manipular(respuesta, body)
        contenido = (
            respuesta
            if isinstance(respuesta, bytes)
            else json.dumps(respuesta, ensure_ascii=False).encode("utf-8")
        )
        self.ultima_respuesta = contenido
        return Response(
            contenido, headers={"content-type": self.content_type_json, **headers}
        )

    # -- lógica del comercio falso -------------------------------------------

    def _responder(
        self, context_id: str, data_parts: list[dict[str, Any]]
    ) -> tuple[list[Artifact], TaskState, str | None]:
        def buscar(clave: str) -> Any:
            for parte in data_parts:
                if clave in parte:
                    return parte[clave]
            return None

        if buscar("catalog_search"):
            escenario = buscar("demo_scenario") or self.escenario_por_defecto
            self._escenario_por_contexto[context_id] = escenario
            fixture = cargar_fixture(escenario)
            artifacts = [
                _artefacto({"ap2.cart": cart}) for cart in fixture["carts"]
            ]
            artifacts.append(
                _artefacto(
                    {
                        "demo.offer_details": fixture["offer_details"],
                        "demo_scenario": escenario,
                    }
                )
            )
            artifacts.append(_artefacto({"risk_data": "fake_risk_data"}))
            return artifacts, TaskState.completed, None

        if buscar("cart_id"):
            cart_id = buscar("cart_id")
            escenario = self._escenario_por_contexto.get(
                context_id, self.escenario_por_defecto
            )
            carts = {c["cart_id"]: c for c in cargar_fixture(escenario)["carts"]}
            cart = carts.get(cart_id)
            if cart is None:
                return [], TaskState.failed, f"Cart not found for cart_id: {cart_id}"
            checkout = self.armar_checkout(cart)
            self.ultimo_checkout = checkout
            self.checkouts.append(checkout)
            artifact = Artifact(
                artifact_id=f"art-{uuid.uuid4().hex}",
                parts=[
                    Part(root=DataPart(data={"ap2.checkout": checkout})),
                    Part(root=DataPart(data={"risk_data": "fake_risk_data"})),
                ],
            )
            return [artifact], TaskState.completed, None

        if buscar(PAYMENT_MANDATE_KEY) or buscar("checkout_mandate"):
            return (
                [_artefacto({"payment_status": "SUCCESS"})],
                TaskState.completed,
                None,
            )

        return [], TaskState.failed, "No sé qué hacer con este pedido."

    @staticmethod
    def _status(
        state: TaskState, texto: str | None, task_id: str, context_id: str
    ) -> TaskStatus:
        mensaje = None
        if texto:
            mensaje = Message(
                message_id=uuid.uuid4().hex,
                role=Role.agent,
                parts=[Part(root=TextPart(text=texto))],
                task_id=task_id,
                context_id=context_id,
            )
        return TaskStatus(state=state, message=mensaje)

    def _sse(
        self,
        task_id: str,
        context_id: str,
        artifacts: list[Artifact],
        state: TaskState,
        status_text: str | None,
        history: list[Message],
        pedido: dict[str, Any] | None = None,
    ) -> bytes:
        eventos: list[dict[str, Any]] = [
            Task(
                id=task_id,
                context_id=context_id,
                status=TaskStatus(state=TaskState.submitted),
                history=history,
            ).model_dump(mode="json", by_alias=True, exclude_none=True)
        ]
        for artifact in artifacts:
            eventos.append(
                TaskArtifactUpdateEvent(
                    task_id=task_id, context_id=context_id, artifact=artifact
                ).model_dump(mode="json", by_alias=True, exclude_none=True)
            )
        eventos.append(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                status=self._status(state, status_text, task_id, context_id),
                final=True,
            ).model_dump(mode="json", by_alias=True, exclude_none=True)
        )
        if self.manipular_sse is not None:
            manipulado = self.manipular_sse(eventos, pedido)
            if isinstance(manipulado, bytes):
                return manipulado
            eventos = manipulado
        return sse_de(eventos)


def sse_de(results: list[Any]) -> bytes:
    """Cuerpo SSE con un evento JSON-RPC por cada `result` (como el SDK)."""
    bloques = [
        "data: "
        + json.dumps({"id": 1, "jsonrpc": "2.0", "result": e}, ensure_ascii=False)
        + "\r\n\r\n"
        for e in results
    ]
    return "".join(bloques).encode("utf-8")


def _artefacto(data: dict[str, Any]) -> Artifact:
    return Artifact(
        artifact_id=f"art-{uuid.uuid4().hex}", parts=[Part(root=DataPart(data=data))]
    )


def app_catalogo(merchant_id: str | None = None) -> Starlette:
    """Catálogo público falso: sirve fixtures/public_catalog.json.

    Con `merchant_id`, el catálogo dice ser de ese comercio (otro distinto del
    que firma el checkout del FakeMerchant, "merchant_1").
    """
    catalogo = json.loads((FIXTURES / "public_catalog.json").read_text("utf-8"))
    merchant = dict(catalogo["merchant"])
    if merchant_id is not None:
        merchant["id"] = merchant_id

    async def products(request: Request) -> Response:
        return JSONResponse({"merchant": merchant, "products": catalogo["products"]})

    return Starlette(routes=[Route("/products", products, methods=["GET"])])


class RuteoPorHost(httpx.AsyncBaseTransport):
    """Transporte httpx que manda cada host a una app ASGI distinta.

    Un host desconocido es un error: así un test detecta si alguien se saltea
    el proxy y va directo al comercio.
    """

    def __init__(self, apps: dict[str, Any]) -> None:
        self._transports = {
            host: httpx.ASGITransport(app=app) for host, app in apps.items()
        }

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.netloc.decode("ascii")
        transport = self._transports.get(host) or self._transports.get(
            request.url.host
        )
        if transport is None:
            raise httpx.ConnectError(f"Host no permitido en el test: {host}")
        return await transport.handle_async_request(request)


# ---------------------------------------------------------------------------
# Mensajes JSON-RPC como los arma el Shopping Agent / el benchmark
# ---------------------------------------------------------------------------


def mensaje_rpc(
    partes_data: list[dict[str, Any]],
    *,
    texto: str = "",
    context_id: str | None = None,
    metodo: str = "message/send",
) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    if texto:
        parts.append({"kind": "text", "text": texto})
    parts.extend({"kind": "data", "data": d} for d in partes_data)
    message: dict[str, Any] = {
        "kind": "message",
        "messageId": uuid.uuid4().hex,
        "role": "agent",
        "parts": parts,
    }
    if context_id:
        message["contextId"] = context_id
    return {
        "id": str(uuid.uuid4()),
        "jsonrpc": "2.0",
        "method": metodo,
        "params": {
            "message": message,
            "configuration": {"acceptedOutputModes": [], "blocking": True},
        },
    }


def pedido_find_products(
    escenario: str | None = "B", metodo: str = "message/send"
) -> dict[str, Any]:
    partes: list[dict[str, Any]] = [
        {"catalog_search": "Nike basketball shoes for outdoor use, size 42"},
        {"risk_data": "benchmark_risk_data"},
        {"shopping_agent_id": "trusted_shopping_agent"},
    ]
    if escenario:
        partes.append({"demo_scenario": escenario})
    return mensaje_rpc(
        partes, texto="Find products that match the user's request.", metodo=metodo
    )


def pedido_create_checkout(
    context_id: str, cart_id: str, metodo: str = "message/send"
) -> dict[str, Any]:
    return mensaje_rpc(
        [{"cart_id": cart_id}, {"shopping_agent_id": "trusted_shopping_agent"}],
        texto="Create a checkout for the selected cart.",
        context_id=context_id,
        metodo=metodo,
    )


HEADERS_A2A = {
    "content-type": "application/json",
    "X-A2A-Extensions": EXTENSION_URI,
}


def cuerpo(pedido: dict[str, Any]) -> bytes:
    return json.dumps(pedido).encode("utf-8")


def parsear_sse(contenido: bytes) -> list[dict[str, Any]]:
    """Devuelve el `result` de cada evento SSE de una respuesta message/stream."""
    eventos = []
    for bloque in contenido.decode("utf-8").replace("\r\n", "\n").split("\n\n"):
        datos = [
            linea[5:].lstrip(" ")
            for linea in bloque.split("\n")
            if linea.startswith("data:")
        ]
        if datos:
            eventos.append(json.loads("\n".join(datos)))
    return eventos


def data_parts_de(task: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        part["data"]
        for artifact in task.get("artifacts") or []
        for part in artifact["parts"]
        if part.get("kind") == "data"
    ]
