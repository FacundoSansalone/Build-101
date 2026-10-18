"""API de control que usa el dashboard (:8020): interruptor, eventos, clave."""

from __future__ import annotations

import json

import httpx
import pytest

from contracts import Evento
from proxy.tests.fake_world import (
    HEADERS_A2A,
    MERCHANT_URL,
    FakeMerchant,
    cuerpo,
    data_parts_de,
    pedido_create_checkout,
    pedido_find_products,
)

pytestmark = pytest.mark.anyio

RPC = "/a2a/merchant_agent"


async def test_get_state(cliente: httpx.AsyncClient):
    resp = await cliente.get("/guard/state")

    assert resp.status_code == 200
    assert resp.json() == {
        "guard_activo": True,
        "merchant_url": MERCHANT_URL,
        "evaluador": "stub",
        "intencion": None,  # etapa 2: sin agente de intención
        "filtro": False,  # etapa 2: sin filtro de seguridad
        "filtro_modo": None,  # sin filtro, sin modo
    }


async def test_post_state_cambia_y_emite_evento(cliente: httpx.AsyncClient):
    resp = await cliente.post("/guard/state", json={"guard_activo": False})

    assert resp.status_code == 200
    assert resp.json() == {
        "guard_activo": False,
        "merchant_url": MERCHANT_URL,
        "evaluador": "stub",
        "intencion": None,  # etapa 2: sin agente de intención
        "filtro": False,  # etapa 2: sin filtro de seguridad
        "filtro_modo": None,  # sin filtro, sin modo
    }
    assert (await cliente.get("/guard/state")).json()["guard_activo"] is False
    (evento,) = (await cliente.get("/guard/events/history")).json()
    assert evento["tipo"] == "guard_cambiado"
    assert evento["datos"] == {"guard_activo": False}
    assert evento["guard_activo"] is False


@pytest.mark.parametrize(
    "body", [{}, {"guard_activo": "si"}, {"otra": True}, [], "true"]
)
async def test_post_state_invalido_da_400(cliente: httpx.AsyncClient, body):
    resp = await cliente.post("/guard/state", content=json.dumps(body),
                              headers={"content-type": "application/json"})
    assert resp.status_code == 400
    assert (await cliente.get("/guard/state")).json()["guard_activo"] is True


async def test_post_state_json_roto_da_400(cliente: httpx.AsyncClient):
    resp = await cliente.post("/guard/state", content=b"{no es json",
                              headers={"content-type": "application/json"})
    assert resp.status_code == 400


async def test_guard_apagado_es_passthrough_puro(
    cliente: httpx.AsyncClient, merchant: FakeMerchant
):
    await cliente.post("/guard/state", json={"guard_activo": False})
    busqueda = await cliente.post(
        RPC, content=cuerpo(pedido_find_products("B")), headers=HEADERS_A2A
    )
    contexto = busqueda.json()["result"]["contextId"]

    resp = await cliente.post(
        RPC,
        content=cuerpo(pedido_create_checkout(contexto, "cart_4")),
        headers=HEADERS_A2A,
    )

    # Sin Guard el ataque pasa: la respuesta es idéntica a la del comercio.
    assert resp.content == merchant.ultima_respuesta
    assert resp.json()["result"]["status"]["state"] == "completed"
    eventos = (await cliente.get("/guard/events/history")).json()
    tipos = [e["tipo"] for e in eventos]
    assert "veredicto" not in tipos and "bloqueado" not in tipos
    assert tipos[-3:] == ["checkout_pedido", "checkout_recibido", "reenviado"]
    assert all(e["guard_activo"] is False for e in eventos)


async def test_interruptor_sin_reiniciar(cliente: httpx.AsyncClient):
    """B con y sin Guard, en el mismo proceso."""
    async def comprar_cart_4() -> str:
        b = await cliente.post(
            RPC, content=cuerpo(pedido_find_products("B")), headers=HEADERS_A2A
        )
        ctx = b.json()["result"]["contextId"]
        r = await cliente.post(
            RPC, content=cuerpo(pedido_create_checkout(ctx, "cart_4")),
            headers=HEADERS_A2A,
        )
        return r.json()["result"]["status"]["state"]

    assert await comprar_cart_4() == "failed"
    await cliente.post("/guard/state", json={"guard_activo": False})
    assert await comprar_cart_4() == "completed"
    await cliente.post("/guard/state", json={"guard_activo": True})
    assert await comprar_cart_4() == "failed"


async def test_guard_activo_desde_config(armar_proxy, config):
    from dataclasses import replace

    app = armar_proxy(config=replace(config, guard_activo=False))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8010"
    ) as c:
        assert (await c.get("/guard/state")).json()["guard_activo"] is False


async def test_cors_para_el_dashboard(cliente: httpx.AsyncClient):
    for origen in ("http://localhost:8020", "http://127.0.0.1:8020"):
        resp = await cliente.options(
            "/guard/state",
            headers={
                "Origin": origen,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == origen
    resp = await cliente.get("/guard/state", headers={"Origin": "http://evil.test"})
    assert "access-control-allow-origin" not in resp.headers


# ---------------------------------------------------------------------------
# Historial y JSONL
# ---------------------------------------------------------------------------


async def test_historial_valida_eventos_y_limit(cliente: httpx.AsyncClient):
    for _ in range(3):
        await cliente.post(
            RPC, content=cuerpo(pedido_find_products("B")), headers=HEADERS_A2A
        )

    todos = (await cliente.get("/guard/events/history")).json()
    assert len(todos) == 9
    for e in todos:
        Evento.model_validate(e)
    ultimos = (await cliente.get("/guard/events/history?limit=2")).json()
    assert ultimos == todos[-2:]
    assert (await cliente.get("/guard/events/history?limit=abc")).status_code == 400
    assert (await cliente.get("/guard/events/history?limit=0")).status_code == 400


async def test_historial_guarda_hasta_200(cliente: httpx.AsyncClient):
    for i in range(205):
        await cliente.post("/guard/state", json={"guard_activo": i % 2 == 0})
    todos = (await cliente.get("/guard/events/history?limit=1000")).json()
    assert len(todos) == 200


async def test_eventos_se_escriben_en_jsonl(cliente: httpx.AsyncClient, config):
    await cliente.post(
        RPC, content=cuerpo(pedido_find_products("B")), headers=HEADERS_A2A
    )

    lineas = config.events_jsonl.read_text("utf-8").splitlines()
    eventos = [Evento.model_validate_json(linea) for linea in lineas]
    assert [e.tipo for e in eventos] == [
        "busqueda_enviada",
        "ofertas_recibidas",
        "reenviado",
    ]


# ---------------------------------------------------------------------------
# Mandatos: se reenvían sin tocar y se muestran en el dashboard
# ---------------------------------------------------------------------------


async def test_mandatos_intactos_y_evento(
    cliente: httpx.AsyncClient, merchant: FakeMerchant
):
    from proxy.tests.fake_world import (
        CHECKOUT_MANDATE_KEY,
        PAYMENT_MANDATE_KEY,
        mensaje_rpc,
    )

    checkout_mandate = "eyJhbGciOiJFUzI1NiJ9.eyJjaGVja291dF9oYXNoIjoieCJ9.c2ln~WyJzYWx0Il0~"
    payment_mandate = "eyJhbGciOiJFUzI1NiJ9.eyJwYXltZW50In0.c2ln~WyJkaXNjIl0~kb.jwt.sig"
    pedido = mensaje_rpc(
        [
            {PAYMENT_MANDATE_KEY: payment_mandate},
            {"risk_data": "fake"},
            {"shopping_agent_id": "trusted_shopping_agent"},
            {CHECKOUT_MANDATE_KEY: checkout_mandate},
        ],
        texto="Initiate a payment",
        context_id="ctx-pago",
    )
    enviado = cuerpo(pedido)

    resp = await cliente.post(RPC, content=enviado, headers=HEADERS_A2A)

    assert merchant.recibidos[-1][0] == enviado  # byte a byte
    assert resp.content == merchant.ultima_respuesta
    assert data_parts_de(resp.json()["result"]) == [{"payment_status": "SUCCESS"}]
    evento = (await cliente.get("/guard/events/history")).json()[-1]
    assert evento["tipo"] == "reenviado"
    assert evento["contexto_id"] == "ctx-pago"
    assert evento["datos"] == {
        "metodo": "otro",
        "mandatos": {
            PAYMENT_MANDATE_KEY: payment_mandate,
            CHECKOUT_MANDATE_KEY: checkout_mandate,
        },
    }


# ---------------------------------------------------------------------------
# Clave pública del recibo
# ---------------------------------------------------------------------------


async def test_public_key_404_con_stub(cliente: httpx.AsyncClient):
    resp = await cliente.get("/guard/public-key")
    assert resp.status_code == 404
