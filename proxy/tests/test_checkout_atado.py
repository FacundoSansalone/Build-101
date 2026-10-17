"""Correcciones 1 y 2: evaluar() recibe el checkout firmado, uno solo y del
cart que pidió el usuario."""

from __future__ import annotations

import logging

import pytest

from contracts import CheckoutVisto, Ficha, OfertasVistas
from proxy import guard_stub
from proxy.evaluador import EvaluadorCargado
from proxy.tests.ayudantes import (
    buscar,
    checkout,
    checkouts,
    cliente_de,
    con_artefacto,
    eventos,
    evaluador_espia,
    solo_en_checkout,
    tipos,
    veredictos,
)
from proxy.tests.fake_world import FakeMerchant, cargar_fixture

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# 1. checkout=CheckoutVisto(...) si evaluar() lo acepta
# ---------------------------------------------------------------------------


async def test_evaluar_recibe_el_checkout_firmado(armar_proxy, merchant: FakeMerchant):
    evaluador, llamadas = evaluador_espia()
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto = await buscar(c, "B")
        resp = await checkout(c, contexto, "cart_2")

    assert resp.json()["result"]["status"]["state"] == "completed"
    (llamada,) = llamadas
    visto = llamada["checkout"]
    assert isinstance(visto, CheckoutVisto)
    # El data part tal cual, incluidos los campos que no declara el modelo.
    assert visto.model_dump() == merchant.ultimo_checkout
    assert visto.checkout_jwt == merchant.ultimo_checkout["checkout_jwt"]


async def test_evaluar_viejo_sin_checkout_no_lo_recibe_y_avisa(armar_proxy, caplog):
    llamadas = []

    def viejo(ficha, ofertas_vistas, cart_id_elegido, catalogo, politica=None,
              checkout_hash=None, ahora=None):
        llamadas.append(cart_id_elegido)
        return guard_stub.evaluar(ficha, ofertas_vistas, cart_id_elegido, catalogo)

    with caplog.at_level(logging.WARNING):
        app = armar_proxy(evaluador=EvaluadorCargado(evaluar=viejo, nombre="guard"))
    assert "checkout" in caplog.text and "WARNING" in caplog.text.upper()

    async with cliente_de(app) as c:
        contexto = await buscar(c, "B")
        resp = await checkout(c, contexto, "cart_1")

    assert llamadas == ["cart_1"]
    assert resp.json()["result"]["status"]["state"] == "completed"


def test_deteccion_del_parametro_checkout():
    def con_kwargs(*args, **kwargs):  # pragma: no cover - solo se inspecciona
        return None

    def sin(ficha, ofertas_vistas, cart_id_elegido, catalogo):  # pragma: no cover
        return None

    assert EvaluadorCargado(evaluar=con_kwargs, nombre="guard").acepta_checkout is True
    assert EvaluadorCargado(evaluar=sin, nombre="guard").acepta_checkout is False
    assert EvaluadorCargado(evaluar=guard_stub.evaluar, nombre="stub").acepta_checkout is True


def test_stub_acepta_checkout():
    fixture = cargar_fixture("B")
    ficha = Ficha.desde_intent(fixture["intent"])
    ofertas = OfertasVistas(carts=fixture["carts"], offer_details=fixture["offer_details"])
    visto = CheckoutVisto(checkout_jwt="a.b.c", checkout_hash="h", cart_id="cart_1")
    v = guard_stub.evaluar(ficha, ofertas, "cart_1", {}, checkout=visto)
    assert v.status == "aprobado"
    assert v.recibo.checkout_hash == "h"


# ---------------------------------------------------------------------------
# 2. Un solo checkout, y el del cart que pidió el usuario
# ---------------------------------------------------------------------------


async def test_dos_checkouts_en_la_respuesta_bloquea(armar_proxy, merchant: FakeMerchant):
    def segundo(respuesta, pedido):
        cart = next(c for c in cargar_fixture("B")["carts"] if c["cart_id"] == "cart_1")
        extra = merchant.armar_checkout(cart)
        merchant.checkouts.append(extra)
        con_artefacto(respuesta["result"], {"ap2.checkout": extra})
        return respuesta

    merchant.manipular = solo_en_checkout(segundo)
    evaluador, llamadas = evaluador_espia()
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto = await buscar(c, "B")
        resp = await checkout(c, contexto, "cart_1")
        evs = await eventos(c)

    task = resp.json()["result"]
    assert task["status"]["state"] == "failed"
    assert checkouts(task) == []
    assert len(merchant.checkouts) == 2
    for visto in merchant.checkouts:
        assert visto["checkout_jwt"].encode() not in resp.content
    assert llamadas == []  # no se evalúa: se bloquea antes
    assert tipos(evs)[-1] == "bloqueado"
    assert "error" in tipos(evs)
    (v,) = veredictos(task)
    assert v["status"] == "bloqueado"


async def test_checkout_de_otro_cart_que_el_pedido_bloquea(armar_proxy, merchant: FakeMerchant):
    """El comercio responde a create_checkout(cart_1) con el checkout de cart_4."""

    def cambiar(respuesta, pedido):
        cart4 = next(c for c in cargar_fixture("B")["carts"] if c["cart_id"] == "cart_4")
        otro = merchant.armar_checkout(cart4)
        for artifact in respuesta["result"]["artifacts"]:
            for part in artifact["parts"]:
                if "ap2.checkout" in part.get("data", {}):
                    part["data"]["ap2.checkout"] = otro
        return respuesta

    merchant.manipular = solo_en_checkout(cambiar)
    evaluador, llamadas = evaluador_espia()
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto = await buscar(c, "B")
        resp = await checkout(c, contexto, "cart_1")
        evs = await eventos(c)

    task = resp.json()["result"]
    assert task["status"]["state"] == "failed"
    assert checkouts(task) == []
    assert llamadas == []
    bloqueado = evs[-1]
    assert bloqueado["tipo"] == "bloqueado"
    assert bloqueado["datos"]["cart_id"] == "cart_1"  # el del pedido del usuario


async def test_se_evalua_con_el_cart_id_del_pedido(armar_proxy, merchant: FakeMerchant):
    evaluador, llamadas = evaluador_espia()
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto = await buscar(c, "B")
        await checkout(c, contexto, "cart_3")

    assert [ll["cart"] for ll in llamadas] == ["cart_3"]
