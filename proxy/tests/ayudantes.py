"""Ayudantes de los tests del proxy (pedidos, lectura de tareas, evaluadores)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx

from contracts import GUARD_VEREDICTO_KEY, Veredicto
from proxy import guard_stub
from proxy.evaluador import EvaluadorCargado
from proxy.tests.fake_world import (
    HEADERS_A2A,
    cuerpo,
    data_parts_de,
    pedido_create_checkout,
    pedido_find_products,
)

RPC = "/a2a/merchant_agent"
BASE = "http://localhost:8010"


@asynccontextmanager
async def cliente_de(app, base_url: str = BASE) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=base_url, timeout=10
    ) as c:
        yield c


async def buscar(c: httpx.AsyncClient, escenario: str = "B", **kwargs: Any) -> str:
    pedido = pedido_find_products(escenario)
    if kwargs.get("context_id"):
        pedido["params"]["message"]["contextId"] = kwargs["context_id"]
    resp = await c.post(RPC, content=cuerpo(pedido), headers=HEADERS_A2A)
    return resp.json()["result"]["contextId"]


async def checkout(
    c: httpx.AsyncClient, contexto: str, cart_id: str, metodo: str = "message/send"
) -> httpx.Response:
    return await c.post(
        RPC,
        content=cuerpo(pedido_create_checkout(contexto, cart_id, metodo)),
        headers=HEADERS_A2A,
    )


async def tasks_get(c: httpx.AsyncClient, task_id: str) -> httpx.Response:
    pedido = {"id": "g-1", "jsonrpc": "2.0", "method": "tasks/get", "params": {"id": task_id}}
    return await c.post(RPC, content=cuerpo(pedido), headers=HEADERS_A2A)


async def eventos(c: httpx.AsyncClient) -> list[dict[str, Any]]:
    return (await c.get("/guard/events/history")).json()


def tipos(evs: list[dict[str, Any]]) -> list[str]:
    return [e["tipo"] for e in evs]


def checkouts(task: dict[str, Any]) -> list[dict[str, Any]]:
    return [d["ap2.checkout"] for d in data_parts_de(task) if "ap2.checkout" in d]


def veredictos(task: dict[str, Any]) -> list[dict[str, Any]]:
    return [d[GUARD_VEREDICTO_KEY] for d in data_parts_de(task) if GUARD_VEREDICTO_KEY in d]


def texto_estado(task: dict[str, Any]) -> str:
    message = (task.get("status") or {}).get("message") or {}
    return " ".join(p.get("text", "") for p in message.get("parts") or [])


def evaluador_espia(
    status: str | None = None, motivo: str = "motivo de test"
) -> tuple[EvaluadorCargado, list[dict[str, Any]]]:
    """Evaluador con la firma nueva (acepta `checkout`) que registra cada llamada.

    Con `status=None` delega en el stub; si no, devuelve ese status fijo.
    """
    llamadas: list[dict[str, Any]] = []

    def evaluar(ficha, ofertas_vistas, cart_id_elegido, catalogo, politica=None,
                checkout_hash=None, ahora=None, checkout=None):
        llamadas.append(
            dict(ficha=ficha, ofertas=ofertas_vistas, cart=cart_id_elegido,
                 catalogo=catalogo, politica=politica, hash=checkout_hash,
                 checkout=checkout)
        )
        if status is None:
            return guard_stub.evaluar(ficha, ofertas_vistas, cart_id_elegido, catalogo,
                                      politica, checkout_hash, ahora, checkout=checkout)
        return Veredicto(status=status, cart_id_elegido=cart_id_elegido,
                         cart_id_recomendado="cart_1", ranking=["cart_1"], chequeos=[],
                         motivo=motivo)

    return EvaluadorCargado(evaluar=evaluar, nombre="guard"), llamadas


def solo_en_checkout(fn: Callable[[dict[str, Any], dict[str, Any]], Any]):
    """Aplica `fn` solo a las respuestas de create_checkout (pedido con cart_id)."""

    def manipular(respuesta: dict[str, Any], pedido: dict[str, Any]) -> Any:
        partes = (pedido.get("params") or {}).get("message", {}).get("parts", [])
        if any("cart_id" in (p.get("data") or {}) for p in partes):
            return fn(respuesta, pedido)
        return respuesta

    return manipular


def con_artefacto(task: dict[str, Any], data: dict[str, Any], artifact_id: str = "art-extra") -> None:
    task.setdefault("artifacts", []).append(
        {"artifactId": artifact_id, "parts": [{"kind": "data", "data": data}]}
    )


def json_de(resp: httpx.Response) -> dict[str, Any]:
    return json.loads(resp.content)
