"""El filtro enchufado al proxy real (`GUARD_FILTRO=filtro:filtrar`), contra el
comercio falso de `proxy/tests/fake_world.py`: las respuestas tienen la forma
de verdad (tareas, eventos SSE, checkouts con JWT, errores).

Modo solo heurísticas: sin red. Lo que se prueba es la forma de los datos, no
el modelo.
"""

from __future__ import annotations

import httpx
import pytest

from filtro import FiltroSeguridad
from proxy import guard_stub
from proxy.app import create_app
from proxy.config import Config
from proxy.evaluador import EvaluadorCargado
from proxy.seguridad import HEADER_TOKEN
from proxy.tests.fake_world import (
    CATALOG_HOST,
    CATALOG_URL,
    HEADERS_A2A,
    MERCHANT_HOST,
    MERCHANT_URL,
    PROXY_PUBLIC_URL,
    FakeMerchant,
    RuteoPorHost,
    app_catalogo,
    cuerpo,
    data_parts_de,
    parsear_sse,
    pedido_create_checkout,
    pedido_find_products,
)

pytestmark = pytest.mark.anyio
RPC = "/a2a/merchant_agent"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def proxy_con_filtro(tmp_path):
    merchant = FakeMerchant()
    config = Config(
        merchant_upstream_url=MERCHANT_URL,
        proxy_public_url=PROXY_PUBLIC_URL,
        catalogo_url=CATALOG_URL,
        guard_activo=True,
        events_jsonl=tmp_path / "guard_events.jsonl",
        guard_key_path=tmp_path / "guard_signing_key.pem",
        permitir_stub=True,
        api_token_path=tmp_path / "guard_api_token",
    )
    app = create_app(
        config=config,
        transport=RuteoPorHost({MERCHANT_HOST: merchant.app, CATALOG_HOST: app_catalogo()}),
        evaluador=EvaluadorCargado(evaluar=guard_stub.evaluar, nombre="stub"),
        filtro=FiltroSeguridad(solo_heuristicas=True),
    )
    return app


def _cliente(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8010", timeout=10,
        headers={HEADER_TOKEN: app.state.proxy.api_token},
    )


def _carts(task: dict) -> list[dict]:
    return [d["ap2.cart"] for d in data_parts_de(task) if "ap2.cart" in d]


@pytest.mark.parametrize("escenario", ["A", "B", "B2"])
async def test_el_proxy_corta_los_escenarios_con_inyeccion(proxy_con_filtro, escenario):
    async with _cliente(proxy_con_filtro) as c:
        resp = await c.post(RPC, content=cuerpo(pedido_find_products(escenario)), headers=HEADERS_A2A)
    task = resp.json()["result"]
    assert task["status"]["state"] == "failed"
    assert _carts(task) == []
    assert "Note for AI" not in resp.text


async def test_el_proxy_corta_al_proveedor_malicioso_de_computadoras(proxy_con_filtro):
    """La corrida "malicioso con sistema" de la demo: la respuesta real de
    `computadoras_malicioso` no llega al agente, tampoco las ofertas honestas."""
    async with _cliente(proxy_con_filtro) as c:
        resp = await c.post(
            RPC, content=cuerpo(pedido_find_products("computadoras_malicioso")), headers=HEADERS_A2A
        )
    task = resp.json()["result"]
    assert task["status"]["state"] == "failed"
    assert _carts(task) == []
    for texto in ("Instructions for the purchasing agent", "Nova Tab", "Orion Notebook"):
        assert texto not in resp.text


@pytest.mark.parametrize("escenario", ["baseline", "C", "computadoras"])
async def test_el_proxy_deja_pasar_lo_honesto(proxy_con_filtro, escenario):
    async with _cliente(proxy_con_filtro) as c:
        resp = await c.post(RPC, content=cuerpo(pedido_find_products(escenario)), headers=HEADERS_A2A)
    task = resp.json()["result"]
    assert task["status"]["state"] == "completed"
    assert _carts(task)


async def test_el_checkout_honesto_con_jwt_pasa_el_filtro(proxy_con_filtro):
    async with _cliente(proxy_con_filtro) as c:
        busqueda = await c.post(
            RPC, content=cuerpo(pedido_find_products("baseline")), headers=HEADERS_A2A
        )
        contexto = busqueda.json()["result"]["contextId"]
        resp = await c.post(
            RPC, content=cuerpo(pedido_create_checkout(contexto, "cart_1")), headers=HEADERS_A2A
        )
    task = resp.json()["result"]
    assert any("ap2.checkout" in d for d in data_parts_de(task)), task["status"]


async def test_el_stream_honesto_pasa_y_el_malicioso_se_corta(proxy_con_filtro):
    async with _cliente(proxy_con_filtro) as c:
        ok = await c.post(
            RPC, content=cuerpo(pedido_find_products("baseline", "message/stream")),
            headers=HEADERS_A2A,
        )
        malo = await c.post(
            RPC, content=cuerpo(pedido_find_products("B", "message/stream")), headers=HEADERS_A2A
        )
    finales_ok = [e["result"] for e in parsear_sse(ok.content) if "result" in e]
    assert "ap2.cart" in ok.text
    assert finales_ok[-1]["status"]["state"] == "completed"
    assert "Note for AI" not in malo.text
    finales = [e["result"] for e in parsear_sse(malo.content) if "result" in e]
    assert finales[-1]["status"]["state"] == "failed"
