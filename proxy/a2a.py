"""Helpers sobre el JSON de A2A (JSON-RPC 0.3). Funciones puras, sin I/O.

El proxy trabaja con el JSON crudo y no con los modelos del SDK: así lo que
no entiende pasa igual, y lo firmado (checkout_jwt, mandatos) nunca se
re-arma desde un modelo.
"""

from __future__ import annotations

import base64
import json
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

TipoPedido = Literal["find_products", "create_checkout", "otro"]


def dumps(obj: Any) -> bytes:
    """Mismo formato que JSONResponse de Starlette (lo que usa el SDK)."""
    return json.dumps(
        obj, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


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


def leer_pedido(raw: bytes) -> Pedido:
    """Clasifica el JSON-RPC entrante sin modificarlo."""
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return Pedido()
    if not isinstance(body, dict):
        return Pedido()
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    message = params.get("message") if isinstance(params.get("message"), dict) else {}
    datos = data_parts(message.get("parts"))

    catalog_search = buscar("catalog_search", datos)
    cart_id = buscar("cart_id", datos)
    tipo: TipoPedido = "otro"
    if catalog_search:
        tipo = "find_products"
    elif cart_id:
        tipo = "create_checkout"
    escenario = buscar(SCENARIO_KEY, datos)
    return Pedido(
        rpc_id=body.get("id"),
        metodo_rpc=body.get("method"),
        tipo=tipo,
        contexto_id=message.get("contextId"),
        catalog_search=str(catalog_search) if catalog_search else None,
        cart_id=str(cart_id) if cart_id else None,
        escenario=str(escenario) if escenario else None,
        mandatos={k: v for k in MANDATO_KEYS if (v := buscar(k, datos)) is not None},
    )


# ---------------------------------------------------------------------------
# Respuesta (lo que devuelve el comercio, en uno o varios eventos)
# ---------------------------------------------------------------------------


@dataclass
class Respuesta:
    task_id: str | None = None
    contexto_id: str | None = None
    carts: list[dict[str, Any]] = field(default_factory=list)
    offer_details: list[dict[str, Any]] | None = None
    escenario: str | None = None
    checkout: dict[str, Any] | None = None

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
            if CHECKOUT_KEY in datos and isinstance(datos[CHECKOUT_KEY], dict):
                self.checkout = datos[CHECKOUT_KEY]


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
    except (ValueError, UnicodeDecodeError):
        return None, None
    if not isinstance(h, dict) or not isinstance(p, dict):
        return None, None
    return h, p


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------


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
