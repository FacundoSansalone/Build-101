"""Revisión de código del 2026-10-18 (segunda ronda): esperas de otra
búsqueda, consultas al almacén por evento, marcas de tiempo de filas ajenas,
caché del filtro sobre la Agent Card y duplicados.

Cada test fija un hallazgo: primero falló con el código de antes."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

import proxy.app as app_mod
from contracts import Ficha
from proxy import a2a
from proxy import compras as compras_mod
from proxy.compras import RegistroCompras
from proxy.estado import EstadoMemoria
from proxy.tests.ayudantes import RPC, cliente_de, evaluador_espia
from proxy.tests.fake_world import (
    HEADERS_A2A,
    cuerpo,
    data_parts_de,
    parsear_sse,
    pedido_create_checkout,
    pedido_find_products,
)
from proxy.tests.gateway_ayudantes import (
    GATEWAY_PUBLIC_URL,
    armar_gateway,
    buscar,
    cliente_local,
    cliente_publico,
    crear_compra,
)
from proxy.tests.test_compras_api import crear as crear_api
from proxy.tests.test_etapa2 import DATOS, RESPUESTA_USUARIO, AgenteFalso
from proxy.tipos_gateway import ErrorAlmacen

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


# ---------------------------------------------------------------------------
# 2. Tráfico anónimo: una consulta al almacén por contexto y por llamada
# ---------------------------------------------------------------------------


class AlmacenEspia(EstadoMemoria):
    """Cuenta las consultas de dueño de un contexto (con Supabase, cada una
    es un ida y vuelta HTTP) y puede fallar como un almacén caído."""

    def __init__(self, *, falla: bool = False, **kw: Any) -> None:
        super().__init__(**kw)
        self.consultas: list[str] = []
        self.falla = falla

    async def compra_de_contexto(self, contexto_id: str) -> str | None:
        self.consultas.append(contexto_id)
        if self.falla:
            raise ErrorAlmacen("almacén caído")
        return await super().compra_de_contexto(contexto_id)


async def test_un_stream_anonimo_consulta_el_almacen_una_vez_por_contexto(config):
    """Con claves activas, cada evento SSE del comercio preguntaba de nuevo
    de quién es su `contextId`. Ahora la respuesta queda para la llamada."""
    almacen = AlmacenEspia()
    mundo = armar_gateway(
        replace(config, gateway_public_url=GATEWAY_PUBLIC_URL), evaluador_espia()[0],
        almacen=almacen,
    )
    async with cliente_local(mundo.app) as c:
        resp = await c.post(
            RPC, content=cuerpo(pedido_find_products("B", metodo="message/stream")),
            headers=HEADERS_A2A,
        )
    eventos = parsear_sse(resp.content)
    assert resp.status_code == 200 and len(eventos) > 3
    contexto = eventos[0]["result"]["contextId"]
    assert almacen.consultas == [contexto]


async def test_el_contexto_del_pedido_ya_cuenta_para_la_respuesta(config):
    """El pedido trae el `contextId` y la respuesta el mismo: una consulta."""
    almacen = AlmacenEspia()
    mundo = armar_gateway(
        replace(config, gateway_public_url=GATEWAY_PUBLIC_URL), evaluador_espia()[0],
        almacen=almacen,
    )
    async with cliente_local(mundo.app) as c:
        busqueda = await c.post(RPC, content=cuerpo(pedido_find_products("B")), headers=HEADERS_A2A)
        contexto = busqueda.json()["result"]["contextId"]
        for metodo in ("message/send", "message/stream"):
            almacen.consultas.clear()
            resp = await c.post(
                RPC, content=cuerpo(pedido_create_checkout(contexto, "cart_1", metodo)),
                headers=HEADERS_A2A,
            )
            assert resp.status_code == 200
            assert almacen.consultas == [contexto], metodo


async def test_con_claves_y_el_almacen_caido_el_trafico_anonimo_se_corta(config):
    """El fail-closed no cambia: con claves activas, si no se puede saber de
    quién es el contexto, no se reenvía (503 en el pedido)."""
    almacen = AlmacenEspia(falla=True)
    mundo = armar_gateway(
        replace(config, gateway_public_url=GATEWAY_PUBLIC_URL), evaluador_espia()[0],
        almacen=almacen,
    )
    async with cliente_local(mundo.app) as c:
        resp = await c.post(
            RPC, content=cuerpo(pedido_create_checkout("ctx-cualquiera", "cart_1")),
            headers=HEADERS_A2A,
        )
        assert resp.status_code == 503
        assert resp.json()["error"]["data"]["codigo"] == "estado_no_disponible"
        # Sin contextId en el pedido, el de la respuesta: tampoco pasa.
        resp = await c.post(RPC, content=cuerpo(pedido_find_products("B")), headers=HEADERS_A2A)
        task = resp.json()["result"]
        assert task["status"]["state"] == "failed" and not data_parts_de(task)[1:]
    assert len(mundo.merchant.recibidos) == 1  # la búsqueda; el checkout no llegó


async def test_sin_claves_ni_almacen_durable_no_se_consulta_nada(armar_proxy):
    """La demo sola (sin claves y con el almacén en memoria): no puede haber
    compras de integrador y no se pregunta por ningún contexto."""
    almacen = AlmacenEspia()
    app = armar_proxy(almacen=almacen)
    async with cliente_de(app) as c:
        busqueda = await c.post(RPC, content=cuerpo(pedido_find_products("B")), headers=HEADERS_A2A)
        contexto = busqueda.json()["result"]["contextId"]
        await c.post(
            RPC, content=cuerpo(pedido_create_checkout(contexto, "cart_1", "message/stream")),
            headers=HEADERS_A2A,
        )
    assert almacen.consultas == []


# ---------------------------------------------------------------------------
# 4. Una fila de otro reloj adelanta solo su compra, no las marcas del proceso
# ---------------------------------------------------------------------------

_INSTANTE = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
_UN_MICRO = timedelta(microseconds=1)


@pytest.fixture
def reloj_quieto(monkeypatch):
    monkeypatch.setattr(compras_mod, "_ahora", lambda: _INSTANTE)
    monkeypatch.setattr(compras_mod, "_marcas", compras_mod.MarcasMonotonas())


async def test_una_fila_adelantada_no_adelanta_las_otras_compras(reloj_quieto):
    """`tocar()` le pasaba la `actualizada` de una fila hidratada (otro proceso
    con el reloj una hora adelante) como piso a las marcas del proceso: desde
    ahí `creada` y `actualizada` de todas las compras quedaban en el futuro."""
    registro = RegistroCompras()
    ajena = crear_api(registro)
    ajena.actualizada = _INSTANTE + timedelta(hours=1)  # como la deja `desde_fila`
    ajena.tocar()
    assert ajena.actualizada == _INSTANTE + timedelta(hours=1) + _UN_MICRO

    nueva = crear_api(registro)
    nueva.tocar()
    assert nueva.creada < _INSTANTE + timedelta(seconds=1)
    assert nueva.creada < nueva.actualizada < _INSTANTE + timedelta(seconds=1)


async def test_la_compra_ajena_sigue_creciendo_y_el_proceso_tambien(reloj_quieto):
    """La monotonía no se pierde: la compra de la fila sigue creciendo desde su
    marca y las del proceso, desde las suyas (estrictamente, a reloj quieto)."""
    registro = RegistroCompras()
    ajena = crear_api(registro)
    propia = crear_api(registro)
    ajena.actualizada = _INSTANTE + timedelta(hours=1)
    marcas_ajena, marcas_propia = [], []
    for _ in range(3):
        ajena.tocar()
        marcas_ajena.append(ajena.actualizada)
        propia.tocar()
        marcas_propia.append(propia.actualizada)
    assert marcas_ajena == sorted(set(marcas_ajena)) and marcas_ajena[0] > _INSTANTE + timedelta(hours=1)
    assert marcas_propia == sorted(set(marcas_propia))
    assert marcas_propia[0] > propia.creada and marcas_propia[-1] < _INSTANTE + timedelta(seconds=1)
