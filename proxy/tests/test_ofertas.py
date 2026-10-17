"""Corrección 6: las ofertas salen solo de las respuestas a find_products, por
el context_id de esa respuesta, y el checkout usa el contexto del pedido."""

from __future__ import annotations

import pytest

from proxy.tests.ayudantes import (
    RPC,
    buscar,
    checkout,
    checkouts,
    cliente_de,
    con_artefacto,
    eventos,
    evaluador_espia,
    solo_en_checkout,
    tipos,
)
from proxy.tests.fake_world import (
    HEADERS_A2A,
    FakeMerchant,
    cargar_fixture,
    cuerpo,
    mensaje_rpc,
)

pytestmark = pytest.mark.anyio


async def test_carts_en_la_respuesta_del_checkout_se_ignoran(armar_proxy, merchant: FakeMerchant):
    def colar(respuesta, pedido):
        falso = {"cart_id": "cart_9", "item_label": "x", "amount": 1.0, "currency": "USD"}
        con_artefacto(respuesta["result"], {"ap2.cart": falso}, "a-cart")
        con_artefacto(respuesta["result"], {"demo.offer_details": [{"cart_id": "cart_9"}]}, "a-det")
        return respuesta

    merchant.manipular = solo_en_checkout(colar)
    evaluador, llamadas = evaluador_espia()
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto = await buscar(c, "B")
        await checkout(c, contexto, "cart_1")
        await checkout(c, contexto, "cart_2")
        evs = await eventos(c)

    fixture = cargar_fixture("B")
    for llamada in llamadas:
        assert llamada["ofertas"].carts == fixture["carts"]
        assert llamada["ofertas"].offer_details == fixture["offer_details"]
    assert tipos(evs).count("ofertas_recibidas") == 1


async def test_respuesta_que_no_es_find_products_no_registra_ofertas(cliente, merchant):
    """Un pedido sin catalog_search (p. ej. el pago) con ap2.cart en la respuesta."""

    def colar(respuesta, pedido):
        con_artefacto(respuesta["result"], {"ap2.cart": {"cart_id": "cart_9", "amount": 1.0}})
        return respuesta

    merchant.manipular = colar
    pedido = mensaje_rpc([{"checkout_mandate": "x"}], texto="pago", context_id="ctx-pago")
    await cliente.post(RPC, content=cuerpo(pedido), headers=HEADERS_A2A)
    assert "ofertas_recibidas" not in tipos(await eventos(cliente))


async def test_otro_context_id_en_la_respuesta_del_checkout_bloquea(armar_proxy, merchant):
    def mover(respuesta, pedido):
        respuesta["result"]["contextId"] = "ctx-de-otra-compra"
        return respuesta

    merchant.manipular = solo_en_checkout(mover)
    evaluador, llamadas = evaluador_espia()
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto = await buscar(c, "B")
        resp = await checkout(c, contexto, "cart_1")

    task = resp.json()["result"]
    assert task["status"]["state"] == "failed"
    assert checkouts(task) == []
    assert llamadas == []


async def test_el_checkout_usa_las_ofertas_del_contexto_del_pedido(armar_proxy):
    evaluador, llamadas = evaluador_espia()
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto_b = await buscar(c, "B")
        contexto_c = await buscar(c, "C")
        await checkout(c, contexto_b, "cart_1")
        await checkout(c, contexto_c, "cart_1")

    assert llamadas[0]["ofertas"].carts == cargar_fixture("B")["carts"]
    assert llamadas[1]["ofertas"].carts == cargar_fixture("C")["carts"]


async def test_una_nueva_busqueda_en_el_mismo_contexto_reemplaza(armar_proxy, merchant):
    evaluador, llamadas = evaluador_espia()
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto = await buscar(c, "B")
        mismo = await buscar(c, "C", context_id=contexto)
        await checkout(c, contexto, "cart_1")

    assert mismo == contexto
    assert llamadas[0]["ofertas"].carts == cargar_fixture("C")["carts"]


async def test_las_ofertas_guardadas_no_cambian_si_se_mutan_afuera(armar_proxy):
    """Lo que se le pasa a evaluar() es una copia: no puede tocar lo guardado."""
    vistos = []

    def mutador(ficha, ofertas_vistas, cart_id_elegido, catalogo, politica=None,
                checkout_hash=None, ahora=None, checkout=None):
        from proxy import guard_stub

        vistos.append([c["amount"] for c in ofertas_vistas.carts])
        for cart in ofertas_vistas.carts:
            cart["amount"] = 0.0
        return guard_stub.evaluar(ficha, ofertas_vistas, cart_id_elegido, catalogo)

    from proxy.evaluador import EvaluadorCargado

    app = armar_proxy(evaluador=EvaluadorCargado(evaluar=mutador, nombre="guard"))
    async with cliente_de(app) as c:
        contexto = await buscar(c, "B")
        await checkout(c, contexto, "cart_1")
        await checkout(c, contexto, "cart_2")

    assert vistos[0] == vistos[1] == [c["amount"] for c in cargar_fixture("B")["carts"]]
