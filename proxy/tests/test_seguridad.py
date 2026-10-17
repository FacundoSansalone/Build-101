"""Correcciones 9, 10 y 13: API de control solo local (Host, Origin,
Content-Type), Agent Card acotada y sin headers del comercio, JSONL privado."""

from __future__ import annotations

import os
from dataclasses import replace

import httpx
import pytest

from proxy.config import Config, verificar_host_local
from proxy.tests.ayudantes import RPC, buscar, checkout, cliente_de
from proxy.tests.fake_world import (
    HEADERS_A2A,
    PROXY_PUBLIC_URL,
    FakeMerchant,
    cuerpo,
    pedido_find_products,
)

pytestmark = pytest.mark.anyio

CONTROL_POST = ["/guard/state", "/guard/aprobaciones"]


# ---------------------------------------------------------------------------
# 9. CSRF y DNS rebinding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ruta", CONTROL_POST)
async def test_post_de_control_exige_json(cliente: httpx.AsyncClient, ruta):
    for tipo in ("text/plain", "application/x-www-form-urlencoded", None):
        headers = {"content-type": tipo} if tipo else {}
        resp = await cliente.post(ruta, content=b'{"guard_activo": false}', headers=headers)
        assert resp.status_code == 415, tipo
    assert (await cliente.get("/guard/state")).json()["guard_activo"] is True


@pytest.mark.parametrize("ruta", CONTROL_POST)
async def test_post_de_control_rechaza_otro_origen(cliente: httpx.AsyncClient, ruta):
    for origen in ("http://evil.test", "null", "http://localhost:8021"):
        resp = await cliente.post(ruta, json={"guard_activo": False},
                                  headers={"Origin": origen})
        assert resp.status_code == 403, origen
    assert (await cliente.get("/guard/state")).json()["guard_activo"] is True


async def test_post_de_control_acepta_el_dashboard_y_sin_origin(cliente: httpx.AsyncClient):
    for origen in ("http://localhost:8020", "http://127.0.0.1:8020", None):
        headers = {"Origin": origen} if origen else {}
        resp = await cliente.post("/guard/state", json={"guard_activo": False},
                                  headers=headers)
        assert resp.status_code == 200, origen


@pytest.mark.parametrize(
    "ruta", ["/guard/state", "/guard/events/history", "/guard/aprobaciones", "/guard/public-key"]
)
async def test_host_que_no_es_local_da_403(proxy_app, ruta):
    for base in ("http://evil.test:8010", "http://localhost:9999", "http://192.168.0.5:8010"):
        async with cliente_de(proxy_app, base_url=base) as c:
            assert (await c.get(ruta)).status_code == 403, base


async def test_host_127_0_0_1_vale(proxy_app):
    async with cliente_de(proxy_app, base_url="http://127.0.0.1:8010") as c:
        assert (await c.get("/guard/state")).status_code == 200


async def test_post_con_host_ajeno_no_cambia_nada(proxy_app):
    async with cliente_de(proxy_app, base_url="http://rebind.evil.test:8010") as c:
        resp = await c.post("/guard/state", json={"guard_activo": False})
    assert resp.status_code == 403
    assert proxy_app.state.proxy.guard_activo is True


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.2"])
def test_host_loopback_arranca(host):
    verificar_host_local(replace(Config(), host=host))


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::", "ejemplo.com"])
def test_host_remoto_no_arranca_sin_permiso(host):
    with pytest.raises(SystemExit):
        verificar_host_local(replace(Config(), host=host))
    verificar_host_local(replace(Config(), host=host, permitir_remoto=True))


def test_permitir_remoto_desde_el_entorno():
    assert Config.desde_entorno({}).permitir_remoto is False
    assert Config.desde_entorno({"PROXY_ALLOW_REMOTE": "1"}).permitir_remoto is True


# ---------------------------------------------------------------------------
# 10. Agent Card
# ---------------------------------------------------------------------------


async def test_solo_se_sirven_las_rutas_de_la_card(cliente: httpx.AsyncClient, merchant):
    for resto in ("otra-cosa", ".well-known/otra.json", "../../guard/state",
                  ".well-known/agent-card.json/x"):
        resp = await cliente.get(f"/a2a/merchant_agent/{resto}")
        assert resp.status_code == 404, resto
    resp = await cliente.get("/a2a/merchant_agent/.well-known/agent.json")
    assert resp.status_code == 200
    assert resp.json()["url"] == PROXY_PUBLIC_URL


async def test_card_sin_headers_del_comercio(cliente: httpx.AsyncClient, merchant: FakeMerchant):
    merchant.card_headers = {"Set-Cookie": "sesion=robada; Path=/", "X-Comercio": "1",
                             "Content-Type": "text/html"}
    resp = await cliente.get("/a2a/merchant_agent/.well-known/agent-card.json")

    assert resp.status_code == 200
    assert "set-cookie" not in resp.headers
    assert "x-comercio" not in resp.headers
    assert resp.headers["content-type"].split(";")[0] == "application/json"


async def test_rpc_sin_set_cookie_del_comercio(cliente: httpx.AsyncClient, merchant: FakeMerchant):
    merchant.headers_extra = {"Set-Cookie": "sesion=robada; Path=/"}
    merchant.content_type_json = "Application/JSON; charset=utf-8"
    resp = await cliente.post(
        RPC, content=cuerpo(pedido_find_products("B")), headers=HEADERS_A2A
    )
    assert "set-cookie" not in resp.headers
    assert resp.headers["content-type"].split(";")[0] == "application/json"
    assert resp.headers["x-a2a-extensions"]


# ---------------------------------------------------------------------------
# 13. JSONL con permisos restrictivos
# ---------------------------------------------------------------------------


async def test_jsonl_se_crea_privado(cliente: httpx.AsyncClient, config):
    contexto = await buscar(cliente, "B")
    await checkout(cliente, contexto, "cart_1")
    assert config.events_jsonl.exists()
    if os.name != "nt":
        assert (config.events_jsonl.stat().st_mode & 0o777) == 0o600
