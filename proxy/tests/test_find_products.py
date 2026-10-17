"""find_products pasa sin cambios y el proxy guarda las ofertas."""

from __future__ import annotations

import json

import httpx
import pytest

from proxy.tests.fake_world import (
    EXTENSION_URI,
    HEADERS_A2A,
    FakeMerchant,
    cargar_fixture,
    cuerpo,
    data_parts_de,
    parsear_sse,
    pedido_find_products,
)

pytestmark = pytest.mark.anyio

RPC = "/a2a/merchant_agent"


async def test_respuesta_y_pedido_byte_identicos(
    cliente: httpx.AsyncClient, merchant: FakeMerchant
):
    enviado = cuerpo(pedido_find_products("B"))

    resp = await cliente.post(RPC, content=enviado, headers=HEADERS_A2A)

    assert resp.status_code == 200
    # Lo que recibió el comercio es exactamente lo que mandó el cliente.
    recibido, headers = merchant.recibidos[-1]
    assert recibido == enviado
    assert headers["x-a2a-extensions"] == EXTENSION_URI
    # Lo que devuelve el proxy es exactamente lo que respondió el comercio.
    assert resp.content == merchant.ultima_respuesta
    assert resp.headers["x-a2a-extensions"] == EXTENSION_URI


async def test_demo_scenario_se_preserva(
    cliente: httpx.AsyncClient, merchant: FakeMerchant
):
    resp = await cliente.post(
        RPC, content=cuerpo(pedido_find_products("B2")), headers=HEADERS_A2A
    )

    recibido = json.loads(merchant.recibidos[-1][0])
    partes = [p["data"] for p in recibido["params"]["message"]["parts"] if p["kind"] == "data"]
    assert {"demo_scenario": "B2"} in partes
    datos = data_parts_de(resp.json()["result"])
    assert any(d.get("demo_scenario") == "B2" for d in datos)


async def test_eventos_de_busqueda_y_ofertas(cliente: httpx.AsyncClient):
    resp = await cliente.post(
        RPC, content=cuerpo(pedido_find_products("B")), headers=HEADERS_A2A
    )
    contexto = resp.json()["result"]["contextId"]

    eventos = (await cliente.get("/guard/events/history")).json()
    tipos = [e["tipo"] for e in eventos]
    assert tipos == ["busqueda_enviada", "ofertas_recibidas", "reenviado"]

    busqueda, ofertas, reenviado = eventos
    assert busqueda["datos"] == {
        "catalog_search": "Nike basketball shoes for outdoor use, size 42"
    }
    assert busqueda["escenario"] == "B"
    # El pedido no traía contextId: el evento sale con el de la respuesta.
    assert busqueda["contexto_id"] == contexto
    fixture = cargar_fixture("B")
    assert ofertas["datos"]["carts"] == fixture["carts"]
    assert ofertas["datos"]["offer_details"] == fixture["offer_details"]
    assert ofertas["contexto_id"] == contexto
    assert ofertas["escenario"] == "B"
    assert reenviado["datos"] == {"metodo": "find_products"}
    assert all(e["guard_activo"] is True for e in eventos)


async def test_find_products_por_sse_pasa_igual(
    cliente: httpx.AsyncClient, merchant: FakeMerchant
):
    resp = await cliente.post(
        RPC,
        content=cuerpo(pedido_find_products("B", metodo="message/stream")),
        headers=HEADERS_A2A,
    )

    assert resp.headers["content-type"].startswith("text/event-stream")
    assert parsear_sse(resp.content) == parsear_sse(merchant.ultima_respuesta)
    eventos = (await cliente.get("/guard/events/history")).json()
    ofertas = [e for e in eventos if e["tipo"] == "ofertas_recibidas"]
    assert len(ofertas) == 1
    assert len(ofertas[0]["datos"]["carts"]) == 4
    assert ofertas[0]["escenario"] == "B"
    contexto = parsear_sse(resp.content)[0]["result"]["contextId"]
    (busqueda,) = [e for e in eventos if e["tipo"] == "busqueda_enviada"]
    assert busqueda["contexto_id"] == contexto
    assert [e["tipo"] for e in eventos] == [
        "busqueda_enviada", "ofertas_recibidas", "reenviado"
    ]


async def test_busqueda_con_context_id_en_el_pedido_sale_con_ese_contexto(
    cliente: httpx.AsyncClient,
):
    pedido = pedido_find_products("B")
    pedido["params"]["message"]["contextId"] = "ctx-del-agente"
    await cliente.post(RPC, content=cuerpo(pedido), headers=HEADERS_A2A)

    eventos = (await cliente.get("/guard/events/history")).json()
    assert eventos[0]["tipo"] == "busqueda_enviada"
    assert eventos[0]["contexto_id"] == "ctx-del-agente"
    assert all(e["contexto_id"] == "ctx-del-agente" for e in eventos)


async def test_comercio_caido_da_error_jsonrpc_y_evento(armar_proxy):
    from proxy.tests.fake_world import RuteoPorHost

    app = armar_proxy(transport=RuteoPorHost({}))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8010"
    ) as c:
        pedido = pedido_find_products("B")
        resp = await c.post(RPC, content=cuerpo(pedido), headers=HEADERS_A2A)
        eventos = (await c.get("/guard/events/history")).json()

    assert resp.status_code == 502
    assert resp.json()["id"] == pedido["id"]
    assert "error" in resp.json()
    assert eventos[-1]["tipo"] == "error"
    assert eventos[-1]["datos"]["mensaje"]
