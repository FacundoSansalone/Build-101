"""N4: los POST de la API de control exigen un token por arranque.

Antes, un proceso local (o el agente con una herramienta HTTP) podía hacer
`POST /guard/aprobaciones` o `POST /guard/state` con `Host` local, JSON y sin
`Origin`, y aprobar un checkout retenido o apagar el Guard. Ahora el proxy
genera un token aleatorio en cada arranque, lo escribe en
`.temp-db/guard_api_token` (modo 0600 donde aplica) y los POST de `/guard/*`
exigen `X-Guard-Token`. Los GET de solo lectura siguen con Host/Origin.
"""

from __future__ import annotations

import os
from dataclasses import replace

import httpx
import pytest

from proxy.config import REPO_ROOT, Config
from proxy.tests.ayudantes import buscar, checkout, evaluador_espia

pytestmark = pytest.mark.anyio

HEADER_TOKEN = "X-Guard-Token"  # la interfaz: el dashboard lo manda con este nombre


def _sin_token(app) -> httpx.AsyncClient:
    """Como curl o una herramienta HTTP del agente: Host local, JSON, sin Origin."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8010", timeout=10
    )


async def test_post_state_sin_token_da_403_y_no_apaga_el_guard(proxy_app):
    async with _sin_token(proxy_app) as c:
        for headers in ({}, {HEADER_TOKEN: ""}, {HEADER_TOKEN: "otro-token"}):
            resp = await c.post("/guard/state", json={"guard_activo": False}, headers=headers)
            assert resp.status_code == 403, headers
            assert resp.json()["error"] == "token_invalido"
        assert (await c.get("/guard/state")).json()["guard_activo"] is True
    assert proxy_app.state.proxy.guard_activo is True


async def test_aprobar_sin_token_da_403_y_no_crea_la_aprobacion(armar_proxy):
    evaluador, _ = evaluador_espia("escalar")
    app = armar_proxy(evaluador=evaluador)
    async with _sin_token(app) as c:
        contexto = await buscar(c, "C")
        await checkout(c, contexto, "cart_1")
        resp = await c.post("/guard/aprobaciones", json={"contexto_id": contexto, "cart_id": "cart_1"})
        lista = (await c.get("/guard/aprobaciones")).json()

    assert resp.status_code == 403
    assert lista == {"pendientes": [], "usadas": []}


async def test_post_con_el_token_del_arranque_pasa(proxy_app):
    token = proxy_app.state.proxy.api_token
    async with _sin_token(proxy_app) as c:
        resp = await c.post("/guard/state", json={"guard_activo": False},
                            headers={HEADER_TOKEN: token})
    assert resp.status_code == 200 and resp.json()["guard_activo"] is False


@pytest.mark.parametrize(
    "ruta", ["/guard/state", "/guard/aprobaciones", "/guard/events/history"]
)
async def test_los_get_no_piden_token_ni_lo_muestran(proxy_app, ruta):
    token = proxy_app.state.proxy.api_token
    async with _sin_token(proxy_app) as c:
        resp = await c.get(ruta)
    assert resp.status_code == 200
    assert token not in resp.text


async def test_los_controles_previos_siguen_antes_que_el_token(proxy_app):
    """Origin ajeno (403) y Content-Type (415) se chequean igual."""
    token = proxy_app.state.proxy.api_token
    async with _sin_token(proxy_app) as c:
        r = await c.post("/guard/state", json={"guard_activo": False},
                         headers={HEADER_TOKEN: token, "Origin": "http://evil.test"})
        assert r.status_code == 403 and r.json()["error"] == "origen_no_permitido"
        r = await c.post("/guard/state", content=b'{"guard_activo": false}',
                         headers={HEADER_TOKEN: token, "content-type": "text/plain"})
        assert r.status_code == 415


def test_el_token_se_escribe_al_arrancar_y_cambia_en_cada_arranque(armar_proxy, config):
    assert not config.api_token_path.exists()
    a = armar_proxy()
    escrito = config.api_token_path.read_text(encoding="utf-8").strip()
    assert escrito == a.state.proxy.api_token
    assert len(escrito) >= 32
    if os.name != "nt":
        assert (config.api_token_path.stat().st_mode & 0o777) == 0o600
    b = armar_proxy()
    assert b.state.proxy.api_token != a.state.proxy.api_token
    assert config.api_token_path.read_text(encoding="utf-8").strip() == b.state.proxy.api_token


def test_sin_ruta_el_token_queda_solo_en_memoria(armar_proxy, config, tmp_path):
    app = armar_proxy(config=replace(config, api_token_path=None))
    assert app.state.proxy.api_token
    assert not config.api_token_path.exists()


def test_ruta_del_token_desde_el_entorno(tmp_path):
    assert Config.desde_entorno({}).api_token_path == REPO_ROOT / ".temp-db" / "guard_api_token"
    c = Config.desde_entorno({"GUARD_API_TOKEN_PATH": str(tmp_path / "t")})
    assert c.api_token_path == tmp_path / "t"
