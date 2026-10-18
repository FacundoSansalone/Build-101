"""Revisión de código del 2026-10-18 (segunda ronda): esperas de otra
búsqueda, consultas al almacén por evento, marcas de tiempo de filas ajenas,
caché del filtro sobre la Agent Card y duplicados.

Cada test fija un hallazgo: primero falló con el código de antes."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Any

import pytest

import proxy.app as app_mod
from contracts import Ficha
from proxy import a2a
from proxy.tests.gateway_ayudantes import (
    GATEWAY_PUBLIC_URL,
    armar_gateway,
    buscar,
    cliente_publico,
    crear_compra,
)
from proxy.tests.ayudantes import evaluador_espia
from proxy.tests.test_etapa2 import DATOS, RESPUESTA_USUARIO, AgenteFalso

pytestmark = pytest.mark.anyio


def _llamada_de(compra):
    return app_mod.Llamada(pedido=a2a.Pedido(), guard_activo=True, compra=compra)


# ---------------------------------------------------------------------------
# 1. Otra búsqueda de la misma compra: sin plazo, el presupuesto de la llamada
# ---------------------------------------------------------------------------


async def test_sin_plazo_la_otra_busqueda_espera_lo_que_queda_de_la_llamada(
    armar_proxy, monkeypatch
):
    """La primera búsqueda todavía arma la pregunta (`_categorias` y el LLM):
    `compra.vence` es None. La otra no corta a los 65 s (acá 0,05): espera
    lo que le queda a la llamada, como antes."""
    monkeypatch.setattr(app_mod, "_MARGEN_ESPERA_SECUNDARIA_S", 0.05)
    proxy = armar_proxy().state.proxy
    compra = proxy.compras.crear("20 computadoras", "¿para qué?", None, None)
    compra.estado = "negociando"  # `_etapa_contexto_api`, antes del primer await

    async def la_primera():
        await asyncio.sleep(0.2)  # el LLM tarda más que el margen
        compra.contexto_listo.set()

    primera = asyncio.create_task(la_primera())
    listo = await asyncio.wait_for(
        proxy._esperar_a_la_otra(_llamada_de(compra), compra, compra.contexto_listo), 2
    )
    await primera
    assert listo is True


async def test_despues_de_la_respuesta_la_otra_busqueda_espera_al_llm(armar_proxy, monkeypatch):
    """`esperar_respuesta` pone `vence=None` al terminar y la primera sigue con
    el LLM (`_contexto_turno`). Si ese LLM tarda más que el margen, la otra
    búsqueda no corta: sin plazo, el tope es la llamada."""
    monkeypatch.setattr(app_mod, "_MARGEN_ESPERA_SECUNDARIA_S", 0.05)
    proxy = armar_proxy().state.proxy
    compra = proxy.compras.crear("20 computadoras", "¿para qué?", None, None)
    compra.estado = "esperando_contexto"
    compra.fijar_plazo(0.05)

    async def la_primera():
        await asyncio.sleep(0.03)
        compra.vence = None  # el usuario contestó: el `finally` de esperar_respuesta
        await asyncio.sleep(0.3)  # el LLM arma el contexto
        compra.estado = "negociando"
        compra.contexto_listo.set()

    primera = asyncio.create_task(la_primera())
    listo = await asyncio.wait_for(
        proxy._esperar_a_la_otra(_llamada_de(compra), compra, compra.contexto_listo), 2
    )
    await primera
    assert listo is True


async def test_con_plazo_sigue_acotada_al_plazo_mas_el_margen(armar_proxy, monkeypatch):
    """Con `vence`, el tope sigue siendo lo que le queda más el margen: si la
    primera se colgó, la otra cierra del lado seguro (False) sin esperar la
    llamada entera."""
    monkeypatch.setattr(app_mod, "_MARGEN_ESPERA_SECUNDARIA_S", 0.05)
    proxy = armar_proxy().state.proxy
    compra = proxy.compras.crear("20 computadoras", "¿para qué?", None, None)
    compra.estado = "esperando_contexto"
    compra.fijar_plazo(0.05)
    inicio = time.monotonic()
    listo = await asyncio.wait_for(
        proxy._esperar_a_la_otra(_llamada_de(compra), compra, compra.contexto_listo), 2
    )
    assert listo is False and time.monotonic() - inicio < 1


class AgenteLento(AgenteFalso):
    """El LLM de la pregunta tarda (como uno cerca de su tope de 45 s)."""

    def preguntar(self, *args: Any, **kwargs: Any):
        time.sleep(0.3)
        return super().preguntar(*args, **kwargs)


async def test_integrador_con_preguntar_la_segunda_busqueda_espera_el_porque(
    config, monkeypatch
):
    """Compra de integrador con `intencion.preguntar`: la primera búsqueda está
    en `preguntar` (el LLM) y llega otra de la misma compra. Antes esperaba
    65 s (`vence` todavía None), vencía y seguía literal sin contexto; ahora
    espera la respuesta del usuario, como la primera."""
    monkeypatch.setattr(app_mod, "_MARGEN_ESPERA_SECUNDARIA_S", 0.05)
    config_gw = replace(config, gateway_public_url=GATEWAY_PUBLIC_URL, espera_contexto_s=5.0)
    agente = AgenteLento(cart_id=None)
    mundo = armar_gateway(config_gw, evaluador_espia()[0], agente=agente)
    ficha = Ficha.desde_intent(DATOS["intent"])
    async with cliente_publico(mundo.app) as c:
        compra_id = (
            await crear_compra(
                c, pedido="20 computadoras para el lunes",
                ficha={"requisitos": [r.model_dump(mode="json") for r in ficha.requisitos]},
                intencion={"preguntar": True},
            )
        )["compra_id"]
        compra = mundo.proxy.compras.get(compra_id)
        primera = asyncio.create_task(buscar(c, compra_id, escenario="computadoras"))
        for _ in range(300):
            if compra.estado != "creada":
                break
            await asyncio.sleep(0.01)
        assert compra.estado == "negociando"  # la primera, en preguntar
        segunda = asyncio.create_task(buscar(c, compra_id, escenario="computadoras"))
        for _ in range(300):
            if compra.estado == "esperando_contexto":
                break
            await asyncio.sleep(0.01)
        assert compra.estado == "esperando_contexto"
        await asyncio.sleep(0.2)  # más que el margen
        assert not segunda.done()
        assert mundo.merchant.recibidos == []  # nada siguió literal
        resp = await c.post(
            f"/v1/compras/{compra_id}/respuesta", json={"respuesta": RESPUESTA_USUARIO}
        )
        assert resp.status_code == 200, resp.text
        respuestas = await asyncio.gather(primera, segunda)
    assert [r.status_code for r in respuestas] == [200, 200]
    assert agente.respuestas == [RESPUESTA_USUARIO]
    assert len(mundo.merchant.recibidos) == 2
