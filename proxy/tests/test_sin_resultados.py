"""Una búsqueda sin resultados también termina (etapa 3, comercio general).

El comercio general (comportamiento `sin_resultados`, o un pedido que no está
en su dataset) responde una tarea `completed` sin ningún `ap2.cart` (el
`find_items_workflow` del sample no agrega carts y llama a `complete()`). El
proxy no emitía `ofertas_recibidas` y el relato de /demo quedaba "en curso"
para siempre. Ahora emite `ofertas_recibidas` con `carts: []` y
`offer_details: []`, solo si el comercio dio la búsqueda por terminada: una
tarea fallida, un stream cortado o una respuesta que cortó el filtro no
cuentan como "no tiene nada".
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from contracts import ResultadoFiltro
from proxy.tests.ayudantes import RPC, checkout, cliente_de, eventos, tipos
from proxy.tests.fake_world import (
    HEADERS_A2A,
    FakeMerchant,
    cuerpo,
    parsear_sse,
    pedido_find_products,
)

pytestmark = pytest.mark.anyio


def _es_busqueda(pedido: dict[str, Any]) -> bool:
    partes = (pedido.get("params") or {}).get("message", {}).get("parts", [])
    return any("catalog_search" in (p.get("data") or {}) for p in partes)


def _sin_carts_en(artefactos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Como responde el comercio general sin resultados: sin `ap2.cart` y con
    `demo.offer_details: []` (el `demo_scenario` y el `risk_data` siguen)."""
    quedan = []
    for artefacto in artefactos:
        datos = [p.get("data") or {} for p in artefacto.get("parts", [])]
        if any("ap2.cart" in d for d in datos):
            continue
        for d in datos:
            if "demo.offer_details" in d:
                d["demo.offer_details"] = []
        quedan.append(artefacto)
    return quedan


def sin_resultados(respuesta: dict[str, Any], pedido: dict[str, Any]) -> dict[str, Any]:
    if _es_busqueda(pedido):
        task = respuesta["result"]
        task["artifacts"] = _sin_carts_en(task.get("artifacts") or [])
    return respuesta


def sin_resultados_sse(eventos_sse: list[dict[str, Any]], pedido: dict[str, Any]) -> list[dict[str, Any]]:
    salida = []
    for evento in eventos_sse:
        if evento.get("kind") == "artifact-update":
            quedan = _sin_carts_en([evento["artifact"]])
            if not quedan:
                continue
        salida.append(evento)
    return salida


def _ofertas(evs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in evs if e["tipo"] == "ofertas_recibidas"]


async def test_sin_carts_emite_ofertas_recibidas_vacio(armar_proxy, merchant: FakeMerchant):
    merchant.manipular = sin_resultados
    async with cliente_de(armar_proxy()) as c:
        resp = await c.post(RPC, content=cuerpo(pedido_find_products("B")), headers=HEADERS_A2A)
        evs = await eventos(c)

    # La respuesta pasa tal cual: el agente de compras le dice al usuario que no hay nada.
    assert resp.content == merchant.ultima_respuesta
    assert tipos(evs) == ["busqueda_enviada", "ofertas_recibidas", "reenviado"]
    (ofertas,) = _ofertas(evs)
    assert ofertas["datos"] == {"carts": [], "offer_details": []}
    assert ofertas["contexto_id"] == resp.json()["result"]["contextId"]
    assert ofertas["escenario"] == "B"
    assert ofertas["guard_activo"] is True


async def test_sin_carts_por_sse_tambien(armar_proxy, merchant: FakeMerchant):
    merchant.manipular_sse = sin_resultados_sse
    async with cliente_de(armar_proxy()) as c:
        resp = await c.post(
            RPC, content=cuerpo(pedido_find_products("B", metodo="message/stream")),
            headers=HEADERS_A2A,
        )
        evs = await eventos(c)

    assert parsear_sse(resp.content) == parsear_sse(merchant.ultima_respuesta)
    assert tipos(evs) == ["busqueda_enviada", "ofertas_recibidas", "reenviado"]
    assert _ofertas(evs)[0]["datos"] == {"carts": [], "offer_details": []}


async def test_con_el_intermediario_apagado_tambien(armar_proxy, merchant: FakeMerchant):
    merchant.manipular = sin_resultados
    async with cliente_de(armar_proxy()) as c:
        await c.post("/guard/state", json={"guard_activo": False})
        await c.post(RPC, content=cuerpo(pedido_find_products("B")), headers=HEADERS_A2A)
        evs = await eventos(c)
    (ofertas,) = _ofertas(evs)
    assert ofertas["guard_activo"] is False and ofertas["datos"]["carts"] == []


async def test_una_tarea_fallida_sin_carts_no_cuenta_como_sin_resultados(
    armar_proxy, merchant: FakeMerchant
):
    def fallida(respuesta: dict[str, Any], pedido: dict[str, Any]) -> dict[str, Any]:
        respuesta = sin_resultados(respuesta, pedido)
        respuesta["result"]["status"]["state"] = "failed"
        return respuesta

    merchant.manipular = fallida
    async with cliente_de(armar_proxy()) as c:
        await c.post(RPC, content=cuerpo(pedido_find_products("B")), headers=HEADERS_A2A)
        evs = await eventos(c)
    assert _ofertas(evs) == []


async def test_un_stream_sin_evento_final_no_cuenta(armar_proxy, merchant: FakeMerchant):
    merchant.manipular_sse = lambda evs, pedido: [
        e for e in sin_resultados_sse(evs, pedido) if e.get("kind") != "status-update"
    ]
    async with cliente_de(armar_proxy()) as c:
        await c.post(
            RPC, content=cuerpo(pedido_find_products("B", metodo="message/stream")),
            headers=HEADERS_A2A,
        )
        evs = await eventos(c)
    assert _ofertas(evs) == []


@pytest.mark.parametrize("metodo", ["message/send", "message/stream"])
async def test_lo_que_corta_el_filtro_no_termina_como_sin_resultados(
    armar_proxy, merchant: FakeMerchant, metodo: str
):
    merchant.manipular = sin_resultados
    merchant.manipular_sse = sin_resultados_sse

    def filtro(respuesta: dict[str, Any]) -> ResultadoFiltro:
        return ResultadoFiltro(pasa=False, motivo="instrucciones al agente",
                               senales=["instrucciones al agente"])

    async with cliente_de(armar_proxy(filtro=filtro)) as c:
        await c.post(
            RPC, content=cuerpo(pedido_find_products("B", metodo=metodo)), headers=HEADERS_A2A
        )
        evs = await eventos(c)
    assert "bloqueado" in tipos(evs)
    assert _ofertas(evs) == []


async def test_con_nuestro_agente_termina_sin_consultar_nada(armar_proxy, config, merchant: FakeMerchant):
    """Con la intención activa: el porqué se pregunta igual, la respuesta
    vacía pasa y no hay oportunidad que consultar."""
    from proxy.tests.test_etapa2 import RESPUESTA_USUARIO, AgenteFalso, _compra

    merchant.manipular = sin_resultados
    app = armar_proxy(config=replace(config, espera_contexto_s=5.0, espera_decision_s=5.0),
                      agente=AgenteFalso())
    async with cliente_de(app) as c:
        busqueda = asyncio.create_task(c.post(
            RPC, content=cuerpo(pedido_find_products("computadoras")), headers=HEADERS_A2A
        ))
        compra = await _compra(c, "esperando_contexto")
        await c.post("/guard/compras/contexto",
                     json={"compra_id": compra["compra_id"], "respuesta": RESPUESTA_USUARIO})
        task = (await busqueda).json()["result"]
        evs = await eventos(c)

    assert task["status"]["state"] == "completed"
    assert [e["datos"]["carts"] for e in _ofertas(evs)] == [[]]
    assert "oportunidad" not in tipos(evs)
    assert tipos(evs)[-2:] == ["ofertas_recibidas", "reenviado"]


async def test_otra_busqueda_sin_resultados_no_borra_las_ofertas_de_antes(
    armar_proxy, merchant: FakeMerchant
):
    """El agente busca otra cosa en la misma compra y no hay nada: lo que
    ofreció antes el comercio se puede seguir comprando (y verificando)."""
    busquedas = {"n": 0}

    def segunda_vacia(respuesta: dict[str, Any], pedido: dict[str, Any]) -> dict[str, Any]:
        if _es_busqueda(pedido):
            busquedas["n"] += 1
            if busquedas["n"] == 2:
                return sin_resultados(respuesta, pedido)
        return respuesta

    merchant.manipular = segunda_vacia
    async with cliente_de(armar_proxy()) as c:
        primera = pedido_find_products("B")
        primera["params"]["message"]["contextId"] = "ctx-misma-compra"
        await c.post(RPC, content=cuerpo(primera), headers=HEADERS_A2A)
        segunda = pedido_find_products("B")
        segunda["params"]["message"]["contextId"] = "ctx-misma-compra"
        await c.post(RPC, content=cuerpo(segunda), headers=HEADERS_A2A)
        resp = await checkout(c, "ctx-misma-compra", "cart_1")
        evs = await eventos(c)

    assert [len(e["datos"]["carts"]) for e in _ofertas(evs)] == [4, 0]
    # El checkout se evaluó con las ofertas de la primera búsqueda.
    assert "veredicto" in tipos(evs)
    assert resp.json()["result"]["status"]["state"] == "completed"
