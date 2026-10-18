"""Integración de la etapa 2 en el proxy, contra el comercio falso.

- La ficha de la demo lleva los atributos y la categoría de la taxonomía
  (`guard.ficha.ficha_desde_intent`), también sin compra (etapa 1).
- Antes de `evaluar()` se consulta al fabricante por GTIN (`fabricante/`) y
  sus datos entran dentro del `catalogo`. Sin dato o con falla, lo
  obligatorio escala. El evento `veredicto` dice qué fuente se usó.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date
from typing import Any

import httpx
import pytest

from contracts import Contexto, Requisito
from fabricante import FixtureFabricante
from intencion import Pregunta
from proxy.evaluador import EvaluadorNoDisponible, cargar_evaluador
from proxy.tests.ayudantes import (
    RPC,
    buscar,
    checkout,
    checkouts,
    cliente_de,
    evaluador_espia,
    eventos,
    texto_estado,
    veredictos,
)
from proxy.tests.fake_world import HEADERS_A2A, cuerpo, pedido_find_products

pytestmark = pytest.mark.anyio


class AgenteMinimo:
    """Agente de intención que pregunta y no propone nada (anota las ofertas
    que le llegan al detector)."""

    nombre = "minimo"

    def __init__(self, requisitos: list[Requisito] | None = None) -> None:
        self.requisitos = requisitos or []
        self.ofertas_vistas: list[list[Any]] = []

    def preguntar(self, pedido: str, hoy: date, categorias: list[str] | None = None) -> Pregunta:
        return Pregunta(pregunta="¿Para qué la necesitás?", requisitos=list(self.requisitos))

    def armar_contexto(self, pedido: str, pregunta: str, respuesta: str, hoy: date) -> Contexto:
        return Contexto(pedido=pedido, pregunta=pregunta, respuesta=respuesta)

    def detectar_oportunidad(self, contexto, ficha, ofertas):
        self.ofertas_vistas.append(list(ofertas))
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


# -- 2. fabricante -----------------------------------------------------------------


@pytest.fixture
def guard_real():
    try:
        return cargar_evaluador()
    except EvaluadorNoDisponible:
        pytest.skip("guard.evaluar() con checkout= no está disponible")


class FabricanteRoto:
    nombre = "rota"

    def consultar(self, gtin):
        raise RuntimeError("se cayó la red")


async def _veredicto(c: httpx.AsyncClient) -> dict[str, Any]:
    (evento,) = [e for e in await eventos(c) if e["tipo"] == "veredicto"]
    return evento["datos"]


async def test_el_guard_bloquea_la_ram_falsa_con_el_fabricante_fixture(armar_proxy, guard_real):
    app = armar_proxy(evaluador=guard_real, fabricante=FixtureFabricante())
    async with cliente_de(app) as c:
        contexto = await buscar(c, "notebook_malicioso")
        task = (await checkout(c, contexto, "cart_4")).json()["result"]
        datos = await _veredicto(c)
    assert not checkouts(task)
    assert veredictos(task)[0]["status"] == "bloqueado"
    ram = {
        q["tipo"]: q for q in datos["veredicto"]["chequeos"] if q.get("campo") == "atributo:memoria_ram"
    }
    assert ram["cumplimiento"]["resultado"] == "bloquea"
    assert ram["cumplimiento"]["origen"] == "fabricante"
    # La evidencia dice que el dato es de un fixture (datos de prueba).
    assert "fixture de prueba" in ram["evidencia"]["detalle"]
    # El evento dice qué fuente del fabricante se usó, rotulada como fixture.
    fabricante = datos["fabricante"]
    assert fabricante["fuente"] == "fixture" and fabricante["fixture"] is True
    consultas = {q["gtin"]: q for q in fabricante["consultas"]}
    assert consultas["02001234000031"]["con_dato"] is True  # la Zento Slim 14


async def test_con_el_fabricante_la_honesta_llega_a_la_firma(armar_proxy, guard_real):
    app = armar_proxy(evaluador=guard_real, fabricante=FixtureFabricante())
    async with cliente_de(app) as c:
        contexto = await buscar(c, "notebook_honesto")
        task = (await checkout(c, contexto, "cart_1")).json()["result"]
    assert checkouts(task), texto_estado(task)
    assert veredictos(task)[0]["status"] in ("aprobado", "con_avisos")


async def test_sin_fabricante_lo_obligatorio_escala(armar_proxy, guard_real):
    async with cliente_de(armar_proxy(evaluador=guard_real)) as c:
        contexto = await buscar(c, "notebook_honesto")
        task = (await checkout(c, contexto, "cart_1")).json()["result"]
        datos = await _veredicto(c)
    assert not checkouts(task)
    assert veredictos(task)[0]["status"] == "escalar"
    assert datos["fabricante"] == {"fuente": "ninguna", "fixture": False, "consultas": []}


async def test_si_el_fabricante_falla_el_guard_escala(armar_proxy, guard_real):
    app = armar_proxy(evaluador=guard_real, fabricante=FabricanteRoto())
    async with cliente_de(app) as c:
        contexto = await buscar(c, "notebook_honesto")
        task = (await checkout(c, contexto, "cart_1")).json()["result"]
        datos = await _veredicto(c)
    assert veredictos(task)[0]["status"] == "escalar"  # nunca aprueba sin la fuente
    assert datos["fabricante"]["fuente"] == "rota"
    assert {q["motivo"] for q in datos["fabricante"]["consultas"]} == {"error"}
    assert not any(q["con_dato"] for q in datos["fabricante"]["consultas"])


async def test_la_oportunidad_ve_las_ofertas_verificadas_con_el_fabricante(
    armar_proxy, config_rapida
):
    agente = AgenteMinimo()
    app = armar_proxy(config=config_rapida, agente=agente, fabricante=FixtureFabricante())
    async with cliente_de(app) as c:
        pedido = pedido_find_products("notebook_malicioso")
        busqueda = asyncio.create_task(c.post(RPC, content=cuerpo(pedido), headers=HEADERS_A2A))
        compra = await _compra(c, "esperando_contexto")
        await c.post(
            "/guard/compras/contexto",
            json={"compra_id": compra["compra_id"], "respuesta": "Para programar."},
        )
        await busqueda
    (ofertas,) = agente.ofertas_vistas
    zento = next(o for o in ofertas if o.cart_id == "cart_4")
    evidencia = zento.evidencia["atributo:memoria_ram"]
    assert evidencia.estado == "contradicho" and evidencia.origen_usado == "fabricante"
    # El dato del fabricante, no el declarado (16 GB).
    assert float(zento.atributos["memoria_ram"].valor) == 8.0


# -- 3. AP2 con el tope de cada compra ------------------------------------------------


async def _ap2(c: httpx.AsyncClient) -> list[Any] | None:
    (evento,) = [e for e in await eventos(c) if e["tipo"] == "checkout_recibido"]
    return evento["datos"]["ap2_violaciones"]


async def test_ap2_usa_el_tope_de_la_compra_y_no_el_global(armar_proxy):
    """El ataque de notebooks está dentro del tope de USD 1.200 de ese pedido:
    AP2 no lo frena (con el tope global de zapatillas, USD 150, parecía que sí)."""
    evaluador, _ = evaluador_espia()
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto = await buscar(c, "notebook_malicioso")
        await checkout(c, contexto, "cart_4")  # USD 899
        assert await _ap2(c) == []


async def test_ap2_frena_lo_que_supera_el_tope_de_la_compra(armar_proxy):
    evaluador, _ = evaluador_espia()
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto = await buscar(c, "notebook_malicioso")
        await checkout(c, contexto, "cart_3")  # USD 1.599 > 1.200
        violaciones = await _ap2(c)
    assert violaciones and any("159900" in v or "1599" in v for v in violaciones)


def test_intent_ap2_sale_de_la_ficha_de_la_compra():
    from contracts import Ficha
    from proxy.fuentes import intent_ap2

    ficha = Ficha(
        pedido_original="x",
        requisitos=[
            Requisito(campo="precio", operador="maximo", valor=1200.0, obligatorio=True),
            Requisito(campo="moneda", operador="igual", valor="USD", obligatorio=True),
        ],
        confirmada=True,
    )
    assert intent_ap2(ficha) == {"hard": {"max_price": 1200.0, "currency": "USD"}}
    # Sin tope o sin moneda obligatorios no hay mandato que chequear.
    sin_tope = ficha.model_copy(update={"requisitos": ficha.requisitos[1:]})
    assert intent_ap2(sin_tope) is None
    preferido = ficha.model_copy(
        update={"requisitos": [ficha.requisitos[0].model_copy(update={"obligatorio": False}),
                               ficha.requisitos[1]]}
    )
    assert intent_ap2(preferido) is None


def test_sin_intent_no_se_corre_ap2():
    from proxy.fuentes import violaciones_ap2

    assert violaciones_ap2({"cart_id": "c", "amount": 10.0, "currency": "USD"}, None) is None


def test_python_m_proxy_arma_la_fuente_del_fabricante():
    from proxy.__main__ import cargar_fabricante

    assert isinstance(cargar_fabricante({}), FixtureFabricante)  # por defecto, el fixture
    assert cargar_fabricante({"FABRICANTE_FUENTE": "ninguna"}) is None
    with pytest.raises(SystemExit):
        cargar_fabricante({"FABRICANTE_FUENTE": "otra"})
    with pytest.raises(SystemExit):
        cargar_fabricante({"FABRICANTE_FUENTE": "icecat"})  # sin ICECAT_USUARIO
