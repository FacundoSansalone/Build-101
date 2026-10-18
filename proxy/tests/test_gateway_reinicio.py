"""Reinicios del gateway con almacén durable (diseño del gateway, §1.7).

Dos `create_app` con el mismo `EstadoMemoria(durable=True)` simulan un
reinicio: lo de memoria (esperas, cachés) se pierde y lo del almacén vuelve.
Las esperas se cierran del lado seguro, las ofertas persistidas sirven para
el checkout, una aprobación se usa una sola vez y, si el almacén falla al
hidratar, no se reenvía nada.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from contracts import Ficha
from proxy.estado import EstadoMemoria
from proxy.tests.ayudantes import checkouts, evaluador_espia, veredictos
from proxy.tests.fake_world import FakeMerchant
from proxy.tests.gateway_ayudantes import (
    GATEWAY_PUBLIC_URL,
    armar_gateway,
    buscar,
    checkout,
    cliente_publico,
    crear_compra,
)
from proxy.tests.test_etapa2 import DATOS, RESPUESTA_USUARIO, AgenteFalso
from proxy.tipos_gateway import ErrorAlmacen

pytestmark = pytest.mark.anyio


@pytest.fixture
def config_gw(config):
    return replace(
        config, gateway_public_url=GATEWAY_PUBLIC_URL,
        espera_contexto_s=5.0, espera_decision_s=5.0,
    )


async def _compra_en(c, compra_id: str, estado: str, intentos: int = 300) -> dict[str, Any]:
    vista: dict[str, Any] = {}
    for _ in range(intentos):
        vista = (await c.get(f"/v1/compras/{compra_id}")).json()
        if vista.get("estado") == estado:
            return vista
        await asyncio.sleep(0.01)
    raise AssertionError(f"La compra no llegó a {estado}: {vista}")


def _ficha_computadoras() -> dict[str, Any]:
    ficha = Ficha.desde_intent(DATOS["intent"])
    return {"requisitos": [r.model_dump(mode="json") for r in ficha.requisitos]}


async def _cancelar(tarea: asyncio.Task) -> None:
    tarea.cancel()
    try:
        await tarea
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 - la llamada del "proceso viejo"
        pass


async def test_esperando_decision_queda_vencida_y_corta_lo_que_sigue(config_gw):
    almacen = EstadoMemoria(durable=True)
    merchant = FakeMerchant()
    viejo = armar_gateway(
        config_gw, evaluador_espia()[0], merchant=merchant, almacen=almacen, agente=AgenteFalso()
    )
    async with cliente_publico(viejo.app) as c:
        compra_id = (
            await crear_compra(
                c, pedido="20 computadoras", ficha=_ficha_computadoras(),
                intencion={"porque": RESPUESTA_USUARIO},
            )
        )["compra_id"]
        busqueda = asyncio.create_task(buscar(c, compra_id, escenario="computadoras"))
        await _compra_en(c, compra_id, "esperando_decision")
        await viejo.esperar_fondo()

        nuevo = armar_gateway(config_gw, evaluador_espia()[0], merchant=merchant, almacen=almacen)
        async with cliente_publico(nuevo.app) as c2:
            vista = (await c2.get(f"/v1/compras/{compra_id}")).json()
            assert vista["estado"] == "vencida"  # el silencio no es autorización
            assert vista["pendiente"] is None
            recibidos = len(merchant.recibidos)
            resp = await buscar(c2, compra_id, escenario="computadoras")
            assert resp.status_code == 409
            assert resp.json()["error"]["data"]["codigo"] == "compra_vencida"
            assert len(merchant.recibidos) == recibidos
            decision = await c2.post(
                f"/v1/compras/{compra_id}/decision",
                json={"oportunidad_id": "op-x", "decision": "aceptar"},
            )
            assert decision.status_code == 409
            assert decision.json()["error"]["codigo"] == "estado"
        await _cancelar(busqueda)


async def test_esperando_contexto_vuelve_negociando_y_sigue_literal(config_gw):
    almacen = EstadoMemoria(durable=True)
    merchant = FakeMerchant()
    viejo = armar_gateway(
        config_gw, evaluador_espia()[0], merchant=merchant, almacen=almacen, agente=AgenteFalso()
    )
    async with cliente_publico(viejo.app) as c:
        compra_id = (
            await crear_compra(
                c, pedido="20 computadoras", ficha=_ficha_computadoras(),
                intencion={"preguntar": True},
            )
        )["compra_id"]
        busqueda = asyncio.create_task(buscar(c, compra_id, escenario="computadoras"))
        await _compra_en(c, compra_id, "esperando_contexto")
        await viejo.esperar_fondo()

        nuevo = armar_gateway(
            config_gw, evaluador_espia()[0], merchant=merchant, almacen=almacen,
            agente=AgenteFalso(),
        )
        async with cliente_publico(nuevo.app) as c2:
            vista = (await c2.get(f"/v1/compras/{compra_id}")).json()
            assert vista["estado"] == "negociando" and vista["pendiente"] is None
            tarde = await c2.post(f"/v1/compras/{compra_id}/respuesta", json={"omitir": True})
            assert tarde.status_code == 409
            task = (await buscar(c2, compra_id, escenario="computadoras")).json()["result"]
            assert task["status"]["state"] == "completed"  # el pedido literal
            assert nuevo.proxy.agente.preguntas == []  # no vuelve a preguntar
            assert (await c2.get(f"/v1/compras/{compra_id}")).json()["estado"] == "sin_oportunidad"
        await _cancelar(busqueda)


async def test_el_checkout_despues_del_reinicio_usa_las_ofertas_persistidas(config_gw):
    almacen = EstadoMemoria(durable=True)
    merchant = FakeMerchant()
    viejo = armar_gateway(config_gw, evaluador_espia()[0], merchant=merchant, almacen=almacen)
    async with cliente_publico(viejo.app) as c:
        compra_id = (await crear_compra(c))["compra_id"]
        contexto = (await buscar(c, compra_id)).json()["result"]["contextId"]
    await viejo.esperar_fondo()

    evaluador, llamadas = evaluador_espia()
    nuevo = armar_gateway(config_gw, evaluador, merchant=merchant, almacen=almacen)
    assert nuevo.proxy._ofertas == {}
    async with cliente_publico(nuevo.app) as c:
        task = (await checkout(c, compra_id, contexto, "cart_1")).json()["result"]
        assert checkouts(task) == [merchant.ultimo_checkout]
        assert [x["cart_id"] for x in llamadas[0]["ofertas"].carts] == [
            "cart_1", "cart_2", "cart_3", "cart_4",
        ]
        vista = (await c.get(f"/v1/compras/{compra_id}")).json()
        assert vista["contextos"] == [contexto]
        assert vista["veredictos"][-1]["status"] == "aprobado"


async def test_sin_ofertas_persistidas_el_checkout_se_bloquea(config_gw):
    almacen = EstadoMemoria(durable=True)
    merchant = FakeMerchant()
    viejo = armar_gateway(config_gw, evaluador_espia()[0], merchant=merchant, almacen=almacen)
    async with cliente_publico(viejo.app) as c:
        compra_id = (await crear_compra(c))["compra_id"]
        contexto = (await buscar(c, compra_id)).json()["result"]["contextId"]
    await viejo.esperar_fondo()
    almacen._ofertas.clear()  # la escritura en segundo plano se perdió

    nuevo = armar_gateway(config_gw, evaluador_espia()[0], merchant=merchant, almacen=almacen)
    async with cliente_publico(nuevo.app) as c:
        task = (await checkout(c, compra_id, contexto, "cart_1")).json()["result"]
    assert not checkouts(task)
    assert veredictos(task)[0]["status"] == "bloqueado"


async def test_una_aprobacion_reclamada_no_se_reusa_despues_del_reinicio(config_gw):
    almacen = EstadoMemoria(durable=True)
    merchant = FakeMerchant()
    viejo = armar_gateway(
        config_gw, evaluador_espia(status="escalar")[0], merchant=merchant, almacen=almacen
    )
    async with cliente_publico(viejo.app) as c:
        compra_id = (await crear_compra(c))["compra_id"]
        contexto = (await buscar(c, compra_id)).json()["result"]["contextId"]
        await checkout(c, compra_id, contexto, "cart_1")
        aprobada = await c.post(f"/v1/compras/{compra_id}/aprobaciones", json={"cart_id": "cart_1"})
        assert aprobada.status_code == 201
        pasa = (await checkout(c, compra_id, contexto, "cart_1")).json()["result"]
        assert checkouts(pasa)  # reclamada en el almacén y usada
    await viejo.esperar_fondo()
    (fila,) = almacen._aprobaciones.values()
    assert fila["estado"] == "usada" and fila["integrador_id"] == "acme"

    nuevo = armar_gateway(
        config_gw, evaluador_espia(status="escalar")[0], merchant=merchant, almacen=almacen
    )
    async with cliente_publico(nuevo.app) as c:
        otra = (await checkout(c, compra_id, contexto, "cart_1")).json()["result"]
    assert not checkouts(otra)


async def test_una_aprobacion_pendiente_sobrevive_al_reinicio_y_se_usa_una_vez(config_gw):
    almacen = EstadoMemoria(durable=True)
    merchant = FakeMerchant()
    viejo = armar_gateway(
        config_gw, evaluador_espia(status="escalar")[0], merchant=merchant, almacen=almacen
    )
    async with cliente_publico(viejo.app) as c:
        compra_id = (await crear_compra(c))["compra_id"]
        contexto = (await buscar(c, compra_id)).json()["result"]["contextId"]
        await checkout(c, compra_id, contexto, "cart_1")
        await viejo.esperar_fondo()

        # Se aprueba ya en el proceso nuevo: el último veredicto sale del almacén.
        nuevo = armar_gateway(
            config_gw, evaluador_espia(status="escalar")[0], merchant=merchant, almacen=almacen
        )
        async with cliente_publico(nuevo.app) as c2:
            assert (await c2.get(f"/v1/compras/{compra_id}")).json()["pendiente"]["tipo"] == (
                "aprobacion"
            )
            aprobada = await c2.post(
                f"/v1/compras/{compra_id}/aprobaciones", json={"cart_id": "cart_1"}
            )
            assert aprobada.status_code == 201, aprobada.text
            await nuevo.esperar_fondo()

    otro = armar_gateway(
        config_gw, evaluador_espia(status="escalar")[0], merchant=merchant, almacen=almacen
    )
    async with cliente_publico(otro.app) as c3:
        pasa = (await checkout(c3, compra_id, contexto, "cart_1")).json()["result"]
        assert checkouts(pasa)  # hidratada del almacén y reclamada
        de_nuevo = (await checkout(c3, compra_id, contexto, "cart_1")).json()["result"]
        assert not checkouts(de_nuevo)


class AlmacenQueFalla(EstadoMemoria):
    """Un almacén caído al hidratar: guarda bien, pero no puede leer."""

    async def cargar_compra(self, compra_id: str) -> dict[str, Any] | None:
        raise ErrorAlmacen("caído")


async def test_si_el_almacen_falla_al_hidratar_da_503_y_no_reenvia(config_gw):
    almacen = AlmacenQueFalla(durable=True)
    mundo = armar_gateway(config_gw, evaluador_espia()[0], almacen=almacen)
    async with cliente_publico(mundo.app) as c:
        compra_id = (await crear_compra(c))["compra_id"]
        mundo.proxy.compras.quitar(compra_id)  # como después de un reinicio
        resp = await buscar(c, compra_id)
        assert resp.status_code == 503
        assert resp.json()["error"]["data"]["codigo"] == "estado_no_disponible"
        api = await c.get(f"/v1/compras/{compra_id}")
        assert api.status_code == 503
        assert api.json()["error"]["codigo"] == "estado_no_disponible"
    assert mundo.merchant.recibidos == []


class AlmacenSinEscritura(EstadoMemoria):
    async def guardar_compra(self, fila: dict[str, Any]) -> None:
        raise ErrorAlmacen("sin espacio")


async def test_si_no_se_puede_guardar_la_compra_no_existe(config_gw):
    mundo = armar_gateway(config_gw, evaluador_espia()[0], almacen=AlmacenSinEscritura())
    async with cliente_publico(mundo.app) as c:
        resp = await c.post("/v1/compras", json={
            "pedido": "x",
            "ficha": {"requisitos": [
                {"campo": "precio", "operador": "maximo", "valor": 150, "obligatorio": True},
                {"campo": "moneda", "operador": "igual", "valor": "USD", "obligatorio": True},
            ]},
        })
    assert resp.status_code == 503
    assert mundo.proxy.compras.listar() == []


class AlmacenSinAprobaciones(EstadoMemoria):
    async def guardar_aprobacion(self, fila: dict[str, Any]) -> None:
        raise ErrorAlmacen("caído")


async def test_si_no_se_puede_guardar_la_aprobacion_no_queda_aprobada(config_gw):
    mundo = armar_gateway(
        config_gw, evaluador_espia(status="escalar")[0],
        almacen=AlmacenSinAprobaciones(durable=True),
    )
    async with cliente_publico(mundo.app) as c:
        compra_id = (await crear_compra(c))["compra_id"]
        contexto = (await buscar(c, compra_id)).json()["result"]["contextId"]
        await checkout(c, compra_id, contexto, "cart_1")
        resp = await c.post(f"/v1/compras/{compra_id}/aprobaciones", json={"cart_id": "cart_1"})
        assert resp.status_code == 503
        assert mundo.proxy.aprobaciones.listar()["pendientes"] == []
        sigue = (await checkout(c, compra_id, contexto, "cart_1")).json()["result"]
        assert not checkouts(sigue)
