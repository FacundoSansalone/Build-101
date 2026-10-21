"""Los eventos de las compras de integrador van al historial (`gw_eventos`).

`ProxyGuard._emitir` guarda, en segundo plano y sin decidir nada, la
proyección saneada (`proxy/historial.py`) de cada evento de una compra de
integrador: en memoria y, con varias instancias, en Supabase (004). Con
Supabase y una sola instancia la base puede no tener 004: no se escribe.
Lo que escribe el comercio (con la inyección de los escenarios) no llega a
ninguna fila. Un corte del filtro deja la compra `bloqueada` con su `corte`.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from contracts import ResultadoFiltro
from proxy.estado import EstadoMemoria, EstadoSupabase
from proxy.tests.ayudantes import evaluador_espia
from proxy.tests.fake_world import cargar_fixture
from proxy.tests.gateway_ayudantes import (
    GATEWAY_PUBLIC_URL,
    armar_gateway,
    buscar,
    checkout,
    cliente_publico,
    crear_compra,
)
from proxy.tests.postgrest_falso import BaseFalsa

pytestmark = pytest.mark.anyio

URL_SUPABASE = "https://base.supabase.test"


@pytest.fixture
def config_gw(config):
    return replace(config, gateway_public_url=GATEWAY_PUBLIC_URL)


def _textos_del_comercio(escenario: str) -> list[str]:
    datos = cargar_fixture(escenario)
    textos = [c["item_label"] for c in datos["carts"]]
    textos += [d["description"] for d in datos["offer_details"]]
    textos += [d["title"] for d in datos["offer_details"]]
    return textos


def _sin_texto_del_comercio(filas: list[dict[str, Any]], escenario: str) -> None:
    volcado = json.dumps(filas, ensure_ascii=False)
    for texto in _textos_del_comercio(escenario):
        assert texto not in volcado, texto
    assert "checkout_jwt" not in volcado and "eyJ" not in volcado
    assert "catalog_search" not in volcado
    assert "Note for AI" not in volcado


async def _compra_con_checkout(mundo, escenario: str = "B", cart: str = "cart_1") -> str:
    async with cliente_publico(mundo.app) as c:
        compra_id = (await crear_compra(c))["compra_id"]
        contexto = (await buscar(c, compra_id, escenario=escenario)).json()["result"]["contextId"]
        resp = await checkout(c, compra_id, contexto, cart)
        assert resp.status_code == 200
    await mundo.esperar_fondo()
    return compra_id


async def test_los_eventos_de_la_compra_van_al_historial_saneados(config_gw):
    almacen = EstadoMemoria()
    mundo = armar_gateway(config_gw, evaluador_espia()[0], almacen=almacen)
    compra_id = await _compra_con_checkout(mundo)
    filas = await almacen.listar_eventos(compra_id)
    tipos = [f["tipo"] for f in filas]
    assert "busqueda_enviada" in tipos and "ofertas_recibidas" in tipos
    assert {"checkout_pedido", "checkout_recibido", "veredicto", "reenviado"} <= set(tipos)
    ofertas = next(f for f in filas if f["tipo"] == "ofertas_recibidas")["datos"]
    assert ofertas["ofertas"] == 4
    assert [c["cart_id"] for c in ofertas["carts"]] == ["cart_1", "cart_2", "cart_3", "cart_4"]
    veredicto = next(f for f in filas if f["tipo"] == "veredicto")["datos"]
    assert veredicto["status"] == "aprobado" and veredicto["cart_id"] == "cart_1"
    assert all(f["escenario"] is None and f["integrador_id"] == "acme" for f in filas)
    _sin_texto_del_comercio(filas, "B")


async def test_el_historial_es_de_cada_compra(config_gw):
    almacen = EstadoMemoria()
    mundo = armar_gateway(config_gw, evaluador_espia()[0], almacen=almacen)
    primera = await _compra_con_checkout(mundo)
    segunda = await _compra_con_checkout(mundo, cart="cart_2")
    ids_primera = {f["id"] for f in await almacen.listar_eventos(primera)}
    ids_segunda = {f["id"] for f in await almacen.listar_eventos(segunda)}
    assert ids_primera and ids_segunda and not ids_primera & ids_segunda


async def test_la_demo_no_va_al_historial(armar_proxy):
    """El tráfico anónimo (la demo) no es de una compra de integrador."""
    from proxy.tests.ayudantes import buscar as buscar_demo
    from proxy.tests.ayudantes import cliente_de

    almacen = EstadoMemoria()
    async with cliente_de(armar_proxy(almacen=almacen)) as c:
        await buscar_demo(c, "B")
    assert await almacen.listar_eventos("cmp-0123456789abcdef") == []
    assert almacen._eventos == {}


def _filtro_que_corta(objetivo: Any) -> ResultadoFiltro:
    texto = json.dumps(objetivo, ensure_ascii=False)
    if "Note for AI" in texto:
        return ResultadoFiltro(pasa=False, senales=["heuristica:instruccion_ia", "libre: Note for AI"])
    return ResultadoFiltro(pasa=True)


def _filtro_que_falla(objetivo: Any) -> ResultadoFiltro:
    raise RuntimeError("el modelo no respondió")


def _filtro_sin_revisar(objetivo: Any) -> ResultadoFiltro:
    return ResultadoFiltro(pasa=False, senales=["filtro:sin_respuesta"])


@pytest.mark.parametrize("filtro, corte, causa, senales", [
    (_filtro_que_corta, "manipulacion", "filtro", ["heuristica:instruccion_ia"]),
    (_filtro_sin_revisar, "sin_revisar", "filtro", ["filtro:sin_respuesta"]),
    (_filtro_que_falla, "sin_revisar", "filtro_fallo", []),
])
async def test_el_corte_del_filtro_queda_en_la_compra_y_en_el_historial(
    config_gw, filtro, corte, causa, senales
):
    almacen = EstadoMemoria()
    mundo = armar_gateway(config_gw, evaluador_espia()[0], almacen=almacen, filtro=filtro)
    async with cliente_publico(mundo.app) as c:
        compra_id = (await crear_compra(c))["compra_id"]
        await buscar(c, compra_id, escenario="B")
        assert (await c.get(f"/v1/compras/{compra_id}")).json()["estado"] == "bloqueada"
    await mundo.esperar_fondo()
    assert mundo.proxy.compras.get(compra_id).corte == corte
    fila = await almacen.cargar_compra(compra_id)
    assert fila["datos"]["corte"] == corte
    filas = await almacen.listar_eventos(compra_id)
    (bloqueado,) = [f for f in filas if f["tipo"] == "bloqueado"]
    assert bloqueado["datos"]["etapa"] == "seguridad"
    assert bloqueado["datos"]["causa"] == causa
    assert bloqueado["datos"]["senales"] == senales
    assert bloqueado["datos"]["motivo"]
    _sin_texto_del_comercio(filas, "B")


async def test_con_varias_instancias_el_historial_va_a_supabase(config_gw):
    base = BaseFalsa()
    almacen = EstadoSupabase(URL_SUPABASE, "clave", transport=base.transporte())
    config = replace(config_gw, instancias="varias", sondeo_s=0.02, barrera_s=3.0,
                     gracia_espera_s=0.4)
    mundo = armar_gateway(config, evaluador_espia()[0], almacen=almacen, demo=None)
    compra_id = await _compra_con_checkout(mundo)
    filas = base.filas("gw_eventos")
    assert filas and {f["compra_id"] for f in filas} == {compra_id}
    assert "veredicto" in {f["tipo"] for f in filas}
    _sin_texto_del_comercio(filas, "B")


async def test_con_supabase_y_una_instancia_no_se_escribe_el_historial(config_gw):
    base = BaseFalsa()
    almacen = EstadoSupabase(URL_SUPABASE, "clave", transport=base.transporte())
    mundo = armar_gateway(config_gw, evaluador_espia()[0], almacen=almacen)
    await _compra_con_checkout(mundo)
    assert base.filas("gw_eventos") == []
    assert mundo.proxy.historial_activo is False


async def test_si_el_historial_falla_la_compra_sigue(config_gw):
    class AlmacenSinHistorial(EstadoMemoria):
        async def guardar_evento(self, fila):
            raise RuntimeError("se cayó la tabla")

    mundo = armar_gateway(config_gw, evaluador_espia()[0], almacen=AlmacenSinHistorial())
    async with cliente_publico(mundo.app) as c:
        compra_id = (await crear_compra(c))["compra_id"]
        contexto = (await buscar(c, compra_id)).json()["result"]["contextId"]
        resp = await checkout(c, compra_id, contexto, "cart_1")
        assert resp.json()["result"]["status"]["state"] == "completed"
    await mundo.esperar_fondo()


async def test_un_checkout_que_el_proxy_no_pudo_evaluar_queda_en_la_compra(config_gw):
    """Sin veredicto del Guard (el checkout es de otro cart), la compra guarda
    el bloqueo del proxy: el panel la cuenta como frenada sin leer eventos."""
    from proxy.tests.ayudantes import solo_en_checkout

    def otro_cart(respuesta: dict[str, Any], pedido: dict[str, Any]) -> dict[str, Any]:
        for artefacto in respuesta["result"].get("artifacts") or []:
            for parte in artefacto.get("parts") or []:
                checkout_ap2 = (parte.get("data") or {}).get("ap2.checkout")
                if isinstance(checkout_ap2, dict):
                    checkout_ap2["cart_id"] = "cart_3"
        return respuesta

    almacen = EstadoMemoria()
    mundo = armar_gateway(config_gw, evaluador_espia()[0], almacen=almacen)
    mundo.merchant.manipular = solo_en_checkout(otro_cart)
    compra_id = await _compra_con_checkout(mundo)
    compra = mundo.proxy.compras.get(compra_id)
    assert compra.veredictos == []
    assert compra.bloqueo_proxy["cart_id"] == "cart_1"
    assert compra.bloqueo_proxy["contexto_id"] == compra.contexto_id
    fila = await almacen.cargar_compra(compra_id)
    assert fila["datos"]["bloqueo_proxy"] == compra.bloqueo_proxy
    (bloqueado,) = [f for f in await almacen.listar_eventos(compra_id) if f["tipo"] == "bloqueado"]
    assert bloqueado["datos"]["sin_veredicto"] is True
    assert bloqueado["datos"]["motivo"].startswith("No se pudo evaluar el checkout:")


async def test_el_historial_guarda_los_eventos_en_el_orden_en_que_pasaron(config_gw):
    """Las búsquedas y las ofertas cuentan cuántas hay antes de guardarse (una
    ida más al almacén): igual entran antes que el `reenviado` que se emitió
    después. Con Supabase, `orden` es la identidad: la línea de tiempo la usa."""
    import asyncio

    class AlmacenLento(EstadoMemoria):
        async def contar_eventos(self, compra_id, tipo):
            await asyncio.sleep(0.01)
            return await super().contar_eventos(compra_id, tipo)

    almacen = AlmacenLento()
    mundo = armar_gateway(config_gw, evaluador_espia()[0], almacen=almacen)
    compra_id = await _compra_con_checkout(mundo)
    filas = await almacen.listar_eventos(compra_id)
    filas.sort(key=lambda f: f["orden"])
    tipos = [f["tipo"] for f in filas]
    assert tipos.index("busqueda_enviada") < tipos.index("ofertas_recibidas") < tipos.index("reenviado")
    async with cliente_publico(mundo.app) as c:
        linea = (await c.get(f"/v1/compras/{compra_id}/explicacion")).json()["linea_de_tiempo"]
    textos = [e["texto"] for e in linea]
    assert textos.index("El comercio mandó 4 ofertas.") < textos.index("Le pasamos las ofertas a tu agente.")


async def test_una_busqueda_sin_ofertas_queda_marcada_en_la_compra(config_gw):
    """La marca `sin_ofertas` (el listado la usa sin leer eventos): la prende
    una búsqueda que el comercio terminó sin ofertas y la apaga una posterior
    que trae ofertas."""
    from proxy.tests.test_sin_resultados import sin_resultados

    almacen = EstadoMemoria()
    mundo = armar_gateway(config_gw, evaluador_espia()[0], almacen=almacen)
    mundo.merchant.manipular = sin_resultados
    async with cliente_publico(mundo.app) as c:
        compra_id = (await crear_compra(c))["compra_id"]
        contexto = (await buscar(c, compra_id)).json()["result"]["contextId"]
        await mundo.esperar_fondo()
        compra = mundo.proxy.compras.get(compra_id)
        assert compra.sin_ofertas is True
        assert (await almacen.cargar_compra(compra_id))["datos"]["sin_ofertas"] is True
        explicada = (await c.get(f"/v1/compras/{compra_id}/explicacion")).json()
        listada = (await c.get("/v1/compras", params={"resultado": "sin_ofertas"})).json()
        assert explicada["resultado"] == "sin_ofertas"
        assert [v["compra_id"] for v in listada["compras"]] == [compra_id]

        mundo.merchant.manipular = None
        await buscar(c, compra_id, contexto=contexto)
        await mundo.esperar_fondo()
        assert compra.sin_ofertas is False
        assert (await almacen.cargar_compra(compra_id))["datos"]["sin_ofertas"] is False
        assert (await c.get(f"/v1/compras/{compra_id}/explicacion")).json()["resultado"] == "en_curso"


async def test_las_busquedas_de_una_compra_tienen_tope_en_el_historial(config_gw, monkeypatch):
    """Un agente que busca sin parar no hace crecer el historial sin límite:
    `busqueda_enviada` y `ofertas_recibidas` se guardan hasta
    `MAX_EVENTOS_REPETIDOS` por compra; el resto (como `reenviado`), siempre."""
    from proxy import app as modulo_app

    monkeypatch.setattr(modulo_app, "MAX_EVENTOS_REPETIDOS", 3)
    almacen = EstadoMemoria()
    mundo = armar_gateway(config_gw, evaluador_espia()[0], almacen=almacen)
    async with cliente_publico(mundo.app) as c:
        compra_id = (await crear_compra(c))["compra_id"]
        contexto = None
        for _ in range(6):
            resp = await buscar(c, compra_id, contexto=contexto)
            contexto = resp.json()["result"]["contextId"]
            await mundo.esperar_fondo()
        await checkout(c, compra_id, contexto, "cart_1")
    await mundo.esperar_fondo()
    tipos = [f["tipo"] for f in await almacen.listar_eventos(compra_id)]
    assert tipos.count("busqueda_enviada") == 3 and tipos.count("ofertas_recibidas") == 3
    assert tipos.count("reenviado") >= 6 and "veredicto" in tipos


@pytest.mark.parametrize("primero, segundo, queda", [
    ("detecta", "falla", "manipulacion"),
    ("falla", "detecta", "manipulacion"),
    ("falla", "falla", "sin_revisar"),
])
async def test_el_corte_de_la_compra_solo_sube(config_gw, primero, segundo, queda):
    """Si dos respuestas del comercio cortan la misma compra (dos búsquedas a la
    vez), una detección no se pisa con un "no pude revisar"."""
    from proxy import a2a
    from proxy.app import Llamada

    mundo = armar_gateway(config_gw, evaluador_espia()[0], almacen=EstadoMemoria())
    async with cliente_publico(mundo.app) as c:
        compra_id = (await crear_compra(c))["compra_id"]
    compra = mundo.proxy.compras.get(compra_id)
    integrador = mundo.integradores.get("acme")
    resultados = iter([primero, segundo])

    def filtro(objetivo: Any) -> ResultadoFiltro:
        if next(resultados) == "falla":
            raise RuntimeError("el modelo no respondió")
        return ResultadoFiltro(pasa=False, senales=["heuristica:instruccion_ia"])

    mundo.proxy.filtro = filtro
    for _ in range(2):
        llamada = Llamada(pedido=a2a.Pedido(), guard_activo=True, compra=compra,
                          integrador=integrador)
        assert await mundo.proxy._filtrar({"kind": "task"}, llamada) is not None
    assert compra.estado == "bloqueada" and compra.corte == queda
    await mundo.esperar_fondo()
