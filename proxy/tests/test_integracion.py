"""Integración de la etapa 2 en el proxy, contra el comercio falso.

- La ficha de la demo lleva los atributos y la categoría de la taxonomía
  (`guard.ficha.ficha_desde_intent`), también sin compra (etapa 1).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date
from typing import Any

import httpx
import pytest

from contracts import Contexto, Requisito
from intencion import Pregunta
from proxy.tests.ayudantes import RPC, buscar, checkout, cliente_de, evaluador_espia
from proxy.tests.fake_world import HEADERS_A2A, cuerpo, pedido_find_products

pytestmark = pytest.mark.anyio


class AgenteMinimo:
    """Agente de intención que pregunta y no propone nada."""

    nombre = "minimo"

    def __init__(self, requisitos: list[Requisito] | None = None) -> None:
        self.requisitos = requisitos or []

    def preguntar(self, pedido: str, hoy: date, categorias: list[str] | None = None) -> Pregunta:
        return Pregunta(pregunta="¿Para qué la necesitás?", requisitos=list(self.requisitos))

    def armar_contexto(self, pedido: str, pregunta: str, respuesta: str, hoy: date) -> Contexto:
        return Contexto(pedido=pedido, pregunta=pregunta, respuesta=respuesta)

    def detectar_oportunidad(self, contexto, ficha, ofertas):
        return None


@pytest.fixture
def config_rapida(config):
    return replace(config, espera_contexto_s=5.0, espera_decision_s=5.0)


async def _compra(c: httpx.AsyncClient, estado: str, intentos: int = 300) -> dict[str, Any]:
    compras: list[dict[str, Any]] = []
    for _ in range(intentos):
        compras = (await c.get("/guard/compras")).json()["compras"]
        for compra in compras:
            if compra["estado"] == estado:
                return compra
        await asyncio.sleep(0.01)
    raise AssertionError(f"Ninguna compra llegó a {estado}: {compras}")


def _atributos(ficha) -> dict[str, Requisito]:
    return {r.atributo: r for r in ficha.requisitos if r.campo == "atributo"}


# -- 1. ficha con atributos ---------------------------------------------------------


async def test_sin_compra_el_guard_usa_la_ficha_del_escenario_con_atributos(armar_proxy):
    """Etapa 1 (sin agente de intención): la ficha es la del escenario de la
    compra, no el pedido global de zapatillas."""
    evaluador, llamadas = evaluador_espia()
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto = await buscar(c, "notebook_malicioso")
        await checkout(c, contexto, "cart_4")
    (llamada,) = llamadas
    ficha = llamada["ficha"]
    atributos = _atributos(ficha)
    assert atributos["memoria_ram"].operador == "minimo"
    assert atributos["memoria_ram"].valor == 16 and atributos["memoria_ram"].unidad == "GB"
    assert atributos["weight"].obligatorio is False
    assert ficha.categoria_taxonomia == "328"
    assert next(r.valor for r in ficha.requisitos if r.campo == "precio") == 1200.0


async def test_la_ficha_confirmada_de_la_demo_lleva_los_atributos(armar_proxy, config_rapida):
    evaluador, llamadas = evaluador_espia()
    app = armar_proxy(config=config_rapida, agente=AgenteMinimo(), evaluador=evaluador)
    async with cliente_de(app) as c:
        pedido = pedido_find_products("notebook_honesto")
        busqueda = asyncio.create_task(c.post(RPC, content=cuerpo(pedido), headers=HEADERS_A2A))
        compra = await _compra(c, "esperando_contexto")
        await c.post(
            "/guard/compras/contexto", json={"compra_id": compra["compra_id"], "omitir": True}
        )
        contexto = (await busqueda).json()["result"]["contextId"]
        await checkout(c, contexto, "cart_1")
    (llamada,) = llamadas
    assert {"memoria_ram", "almacenamiento", "tamano_pantalla"} <= set(_atributos(llamada["ficha"]))
    assert llamada["ficha"].categoria_taxonomia == "328"
