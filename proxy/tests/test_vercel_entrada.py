"""La entrada serverless del motor (lote D de Vercel).

- `create_app(..., clave_recibo=, rutas_control=, rutas_heredadas=)`: con la
  clave inyectada firma y publica con ella y no toca el archivo; sin la API
  de control no hay `/guard/*`, ni `ControlLocal`, ni CORS, ni archivo de
  token, y todo pedido es del oyente público; sin las rutas heredadas no
  está la ruta vieja del comercio por defecto. Con los defaults, la app de
  siempre.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.middleware.cors import CORSMiddleware

from guard.recibo import verificar_recibo
from proxy.config import Config
from proxy.evaluador import ClaveRecibo
from proxy.seguridad import HEADER_TOKEN, ControlLocal
from proxy.tests.ayudantes import buscar, checkout, cliente_de, eventos
from proxy.tests.gateway_ayudantes import (
    CLAVE_ACME,
    armar_gateway,
    cliente_publico,
    crear_compra,
    jws_valido,
)
from proxy.tests.gateway_ayudantes import buscar as buscar_compra
from proxy.tests.gateway_ayudantes import checkout as checkout_compra

pytestmark = pytest.mark.anyio


def _rutas(app: Any) -> set[str]:
    return {getattr(r, "path", "") for r in app.routes}


def _middlewares(app: Any) -> list[Any]:
    return [m.cls for m in app.user_middleware]


@pytest.fixture
def clave() -> ClaveRecibo:
    return ClaveRecibo(clave=ec.generate_private_key(ec.SECP256R1()), kid="kaiai-prueba-1")


# -- create_app con los defaults: la app de siempre ------------------------------------


def test_los_defaults_dejan_la_app_de_siempre(armar_proxy, config):
    app = armar_proxy()
    rutas = _rutas(app)
    assert {"/guard/state", "/guard/public-key", config.rpc_path, "/a2a/m/{comercio}"} <= rutas
    assert {ControlLocal, CORSMiddleware} <= set(_middlewares(app))
    assert app.state.proxy.clave_recibo is None
    assert config.api_token_path.read_text("utf-8").strip() == app.state.proxy.api_token
    assert config.guard_key_path.exists()  # la del archivo, creada al arrancar


# -- clave_recibo ----------------------------------------------------------------------


async def test_con_clave_inyectada_firma_y_publica_con_ella(armar_proxy, config, clave):
    from proxy.evaluador import cargar_evaluador

    app = armar_proxy(
        clave_recibo=clave, evaluador=cargar_evaluador(False),
        config=replace(config, permitir_stub=False),
    )
    assert not config.guard_key_path.exists()  # no se crea ni se lee
    async with cliente_de(app) as c:
        publica = (await c.get("/guard/public-key")).json()
        contexto = await buscar(c, "B")
        await checkout(c, contexto, "cart_1")
        (veredicto,) = [e for e in await eventos(c) if e["tipo"] == "veredicto"]
    assert publica == {"kid": "kaiai-prueba-1", "jwk": clave.jwk_publica()}
    recibo = veredicto["datos"]["veredicto"]["recibo"]
    assert recibo["kid"] == "kaiai-prueba-1" and veredicto["datos"]["recibo_firmado"] is True
    from contracts import Recibo

    assert verificar_recibo(Recibo.model_validate(recibo), clave.clave.public_key())
    assert not config.guard_key_path.exists()


async def test_v1_public_key_y_evaluar_con_la_clave_inyectada(config, clave):
    """El gateway (`NucleoProxy`) usa la misma: `/v1/public-key` y el recibo
    de las compras de integrador."""
    from proxy.evaluador import cargar_evaluador

    mundo = armar_gateway(
        replace(config, permitir_stub=False), cargar_evaluador(False), clave_recibo=clave,
        demo=None,
    )
    async with cliente_publico(mundo.app) as c:
        publica = (await c.get("/v1/public-key")).json()
        compra = await crear_compra(c)
        busqueda = await buscar_compra(c, compra["compra_id"], escenario="baseline")
        assert busqueda.status_code == 200
        contexto = busqueda.json()["result"]["contextId"]
        await checkout_compra(c, compra["compra_id"], contexto, "cart_1")
        vista = (await c.get(f"/v1/compras/{compra['compra_id']}")).json()
    assert publica["kid"] == "kaiai-prueba-1" and publica["keys"] == [clave.jwk_publica()]
    (veredicto,) = vista["veredictos"]
    assert veredicto["recibo"]["kid"] == "kaiai-prueba-1"
    assert jws_valido(veredicto["recibo"]["jws"], publica["jwk"])
    assert not config.guard_key_path.exists()


# -- rutas_control=False ----------------------------------------------------------------


async def test_sin_rutas_de_control_no_hay_guard_ni_token_y_todo_es_publico(armar_proxy, config):
    app = armar_proxy(rutas_control=False)
    assert not any(r.startswith("/guard") for r in _rutas(app))
    assert not {ControlLocal, CORSMiddleware} & set(_middlewares(app))
    assert not config.api_token_path.exists()  # ningún archivo de token
    token = {HEADER_TOKEN: app.state.proxy.api_token}
    async with cliente_de(app) as c:  # por el "puerto local" 8010: igual es público
        guard = await c.get("/guard/state", headers=token)
        anonimo = await c.post(config.rpc_path, json={})
        v1 = await c.get("/v1/compras")
        abierta = await c.get("/v1/openapi.json")
        preflight = await c.options(
            "/v1/compras",
            headers={"Origin": "http://localhost:8020", "Access-Control-Request-Method": "POST"},
        )
    assert guard.status_code == 404
    assert anonimo.status_code == 401  # el A2A anónimo de la demo no existe
    assert v1.status_code == 401 and abierta.status_code == 200
    assert "access-control-allow-origin" not in preflight.headers


async def test_sin_rutas_de_control_la_clave_sigue_entrando(config):
    from proxy.tests.ayudantes import evaluador_espia

    mundo = armar_gateway(config, evaluador_espia()[0], rutas_control=False, demo=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mundo.app), base_url="https://kaiai.example.com",
        headers={"Authorization": f"Bearer {CLAVE_ACME}"},
    ) as c:
        compra = await crear_compra(c)
    assert compra["estado"]


# -- rutas_heredadas=False -----------------------------------------------------------


async def test_sin_rutas_heredadas_no_esta_la_ruta_vieja(armar_proxy, config):
    app = armar_proxy(rutas_heredadas=False)
    rutas = _rutas(app)
    assert config.rpc_path not in rutas and f"{config.rpc_path}/{{resto:path}}" not in rutas
    assert {"/a2a/m/{comercio}", "/a2a/m/{comercio}/{resto:path}"} <= rutas
    async with cliente_de(app) as c:
        vieja = await c.post(config.rpc_path, content=json.dumps({}).encode())
        card = await c.get(f"{config.rpc_path}/.well-known/agent-card.json")
        nueva = await c.get("/a2a/m/merchant_1/.well-known/agent-card.json")
    assert vieja.status_code == 404 and card.status_code == 404
    assert nueva.status_code == 200
