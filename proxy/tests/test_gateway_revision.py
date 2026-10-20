"""Regresiones de la revisión adversarial del gateway (2026-10-18).

Cada test reproduce un hallazgo confirmado y fija el arreglo:

- **Desalojo de memoria:** el tope de `RegistroCompras` sacaba compras de
  integrador vivas. Con ellas se iban las ataduras contexto → compra (el
  dashboard podía aprobar su `escalar`, el Guard evaluaba con la ficha de
  la demo) y sus esperas (la respuesta o la decisión del usuario daban 409).
- **Aprobaciones:** solo el dueño de una compra de integrador la aprueba, por
  `/v1`, aunque la compra no esté en memoria; dos reintentos concurrentes no
  queman la aprobación sin que pase ninguno.
- **Panel sin token:** los GET de `/guard/*` (que el dashboard publicado
  reenvía sin autenticar) no muestran compras ni eventos de integradores.
- **Estado persistente:** las escrituras de una compra no se pisan en
  desorden, el listado por estado ve las compras con la espera cerrada y el
  límite de compras activas aguanta pedidos concurrentes.
- **Oportunidad aceptada:** la ficha de `/v1` no lleva texto del vendedor.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
from starlette.requests import Request

from contracts import Evento, ResultadoFiltro
from proxy.claves import RegistroIntegradores
from proxy.compras import MAX_COMPRAS
from proxy.estado import EstadoMemoria
from proxy.seguridad import HEADER_TOKEN
from proxy.tests.ayudantes import checkouts, evaluador_espia, solo_en_checkout
from proxy.tests.fake_world import FakeMerchant
from proxy.tests.gateway_ayudantes import (
    CLAVE_OTRO,
    GATEWAY_PUBLIC_URL,
    FICHA,
    armar_gateway,
    buscar,
    checkout,
    cliente_local,
    cliente_publico,
    crear_compra,
    datos_integradores,
)
from proxy.tests.test_etapa2 import RESPUESTA_USUARIO, AgenteFalso
from proxy.tests.test_gateway_integracion import _compra_en, _ficha_computadoras

pytestmark = pytest.mark.anyio

FICHA_ESTRICTA = {
    "requisitos": [
        {"campo": "precio", "operador": "maximo", "valor": 1, "obligatorio": True},
        {"campo": "moneda", "operador": "igual", "valor": "USD", "obligatorio": True},
    ]
}


@pytest.fixture
def config_gw(config):
    return replace(
        config, gateway_public_url=GATEWAY_PUBLIC_URL,
        espera_contexto_s=5.0, espera_decision_s=5.0,
    )


async def _escalar_retenido(c: httpx.AsyncClient) -> tuple[str, str]:
    """Una compra con su checkout de cart_1 retenido por `escalar`."""
    compra_id = (await crear_compra(c))["compra_id"]
    contexto = (await buscar(c, compra_id)).json()["result"]["contextId"]
    retenida = (await checkout(c, compra_id, contexto, "cart_1")).json()["result"]
    assert not checkouts(retenida)
    return compra_id, contexto


async def _cancelar(tarea: asyncio.Task) -> None:
    tarea.cancel()
    try:
        await tarea
    except BaseException:  # noqa: BLE001 - solo limpieza
        pass


# -- desalojo de memoria y aprobaciones ---------------------------------------------


async def test_otras_compras_no_desalojan_ni_dejan_aprobar_al_dashboard(config_gw):
    mundo = armar_gateway(config_gw, evaluador_espia(status="escalar")[0])
    async with cliente_publico(mundo.app) as c, cliente_publico(
        mundo.app, CLAVE_OTRO
    ) as otro, cliente_local(mundo.app) as panel:
        compra_id, contexto = await _escalar_retenido(c)
        aprobar = {"contexto_id": contexto, "cart_id": "cart_1"}
        assert (await panel.post("/guard/aprobaciones", json=aprobar)).status_code == 409
        await mundo.esperar_fondo()
        for _ in range(MAX_COMPRAS):
            await crear_compra(otro)
        # Resuelta (sin oportunidad, con su checkout retenido) puede salir de
        # memoria: el almacén sigue diciendo de quién es y qué se autorizó.
        r = await panel.post("/guard/aprobaciones", json=aprobar)
        assert r.status_code == 409 and r.json()["error"] == "compra_de_integrador"
        reintento = (await checkout(c, compra_id, contexto, "cart_1")).json()["result"]
        assert not checkouts(reintento)


async def test_sin_la_compra_en_memoria_el_dashboard_tampoco_aprueba(config_gw):
    """Aunque la compra salga de memoria (terminada, o en otro proceso), el
    panel la reconoce por el almacén y por la marca del registro de
    aprobaciones; el reintento sigue retenido."""
    mundo = armar_gateway(config_gw, evaluador_espia(status="escalar")[0])
    async with cliente_publico(mundo.app) as c, cliente_local(mundo.app) as panel:
        compra_id, contexto = await _escalar_retenido(c)
        await mundo.esperar_fondo()
        mundo.proxy.compras.quitar(compra_id)
        r = await panel.post("/guard/aprobaciones", json={"contexto_id": contexto, "cart_id": "cart_1"})
        assert r.status_code == 409 and r.json()["error"] == "compra_de_integrador"
        assert mundo.proxy.aprobaciones.listar()["pendientes"] == []
        reintento = (await checkout(c, compra_id, contexto, "cart_1")).json()["result"]
        assert not checkouts(reintento)
        # El dueño sí, por /v1: y entonces pasa una vez.
        r = await c.post(f"/v1/compras/{compra_id}/aprobaciones", json={"cart_id": "cart_1"})
        assert r.status_code == 201, r.text
        pasa = (await checkout(c, compra_id, contexto, "cart_1")).json()["result"]
        assert checkouts(pasa)


async def test_solo_la_marca_del_registro_alcanza_para_frenar_al_dashboard(config_gw):
    """Ni memoria ni almacén: queda la marca de `registrar_veredicto`."""
    mundo = armar_gateway(config_gw, evaluador_espia(status="escalar")[0])
    async with cliente_publico(mundo.app) as c, cliente_local(mundo.app) as panel:
        compra_id, contexto = await _escalar_retenido(c)
        await mundo.esperar_fondo()
        mundo.proxy.compras.quitar(compra_id)
        mundo.proxy.almacen._contextos.clear()  # noqa: SLF001
        r = await panel.post("/guard/aprobaciones", json={"contexto_id": contexto, "cart_id": "cart_1"})
    assert r.status_code == 409 and r.json()["error"] == "compra_de_integrador"


async def test_el_guard_evalua_con_la_ficha_del_integrador_aunque_no_este_en_memoria(config_gw):
    evaluador, llamadas = evaluador_espia()
    mundo = armar_gateway(config_gw, evaluador)

    async with cliente_publico(mundo.app) as c:
        compra_id = (await crear_compra(c, ficha=FICHA_ESTRICTA))["compra_id"]
        contexto = (await buscar(c, compra_id)).json()["result"]["contextId"]

        def sacar_de_memoria(respuesta: Any, pedido: Any) -> Any:
            mundo.proxy.compras.quitar(compra_id)  # mientras el comercio responde
            return respuesta

        mundo.merchant.manipular = solo_en_checkout(sacar_de_memoria)
        await checkout(c, compra_id, contexto, "cart_1")
    topes = [r.valor for r in llamadas[-1]["ficha"].requisitos if r.campo == "precio"]
    assert topes == [1]  # nunca el tope de la ficha de la demo (150)


async def test_trafico_de_integrador_sin_ficha_autorizada_se_bloquea(config_gw):
    evaluador, llamadas = evaluador_espia()
    mundo = armar_gateway(config_gw, evaluador)
    async with cliente_publico(mundo.app) as c:
        compra_id = (await crear_compra(c))["compra_id"]
        contexto = (await buscar(c, compra_id)).json()["result"]["contextId"]
        mundo.proxy.compras.get(compra_id).ficha_resuelta = False  # sin parámetros
        task = (await checkout(c, compra_id, contexto, "cart_1")).json()["result"]
    assert not checkouts(task) and llamadas == []
    assert "parámetros que autorizó el integrador" in json.dumps(task, ensure_ascii=False)


async def test_la_espera_del_porque_sobrevive_a_las_compras_de_otro_integrador(config_gw):
    mundo = armar_gateway(config_gw, evaluador_espia()[0], agente=AgenteFalso())
    async with cliente_publico(mundo.app) as c, cliente_publico(mundo.app, CLAVE_OTRO) as otro:
        compra_id = (await crear_compra(
            c, pedido="20 computadoras para el lunes", ficha=_ficha_computadoras(),
            intencion={"preguntar": True},
        ))["compra_id"]
        busqueda = asyncio.create_task(buscar(c, compra_id, escenario="computadoras"))
        await _compra_en(c, compra_id, "esperando_contexto")
        for _ in range(MAX_COMPRAS):
            await crear_compra(otro)
        resp = await c.post(
            f"/v1/compras/{compra_id}/respuesta", json={"respuesta": RESPUESTA_USUARIO}
        )
        await _cancelar(busqueda)
    assert resp.status_code == 200, resp.text


async def test_la_decision_sobrevive_a_las_compras_de_otro_integrador(config_gw):
    mundo = armar_gateway(config_gw, evaluador_espia()[0], agente=AgenteFalso())
    async with cliente_publico(mundo.app) as c, cliente_publico(mundo.app, CLAVE_OTRO) as otro:
        compra_id = (await crear_compra(
            c, pedido="20 computadoras", ficha=_ficha_computadoras(),
            intencion={"porque": RESPUESTA_USUARIO},
        ))["compra_id"]
        busqueda = asyncio.create_task(buscar(c, compra_id, escenario="computadoras"))
        vista = await _compra_en(c, compra_id, "esperando_decision")
        for _ in range(MAX_COMPRAS):
            await crear_compra(otro)
        resp = await c.post(
            f"/v1/compras/{compra_id}/decision",
            json={"oportunidad_id": vista["pendiente"]["oportunidad"]["id"], "decision": "aceptar"},
        )
        assert resp.status_code == 200, resp.text
        await busqueda
        vista = (await c.get(f"/v1/compras/{compra_id}")).json()
    assert vista["estado"] == "aceptada"


# -- oportunidad aceptada -----------------------------------------------------------


async def test_los_requisitos_aceptados_quedan_marcados(config_gw):
    mundo = armar_gateway(config_gw, evaluador_espia()[0], agente=AgenteFalso())
    async with cliente_publico(mundo.app) as c:
        compra_id = (await crear_compra(
            c, pedido="20 computadoras", ficha=_ficha_computadoras(),
            intencion={"porque": RESPUESTA_USUARIO},
        ))["compra_id"]
        busqueda = asyncio.create_task(buscar(c, compra_id, escenario="computadoras"))
        vista = await _compra_en(c, compra_id, "esperando_decision")
        await c.post(
            f"/v1/compras/{compra_id}/decision",
            json={"oportunidad_id": vista["pendiente"]["oportunidad"]["id"], "decision": "aceptar"},
        )
        await busqueda
    compra = mundo.proxy.compras.get(compra_id)
    originales = _ficha_computadoras()["requisitos"]
    for requisito in compra.ficha.requisitos:
        dump = requisito.model_dump(mode="json")
        if dump in originales:
            assert requisito.origen != "aceptado"
        else:
            assert requisito.origen == "aceptado"
    assert any(r.origen == "aceptado" for r in compra.ficha.requisitos)


# -- el panel sin token ---------------------------------------------------------------


@pytest.fixture
def anonimo():
    """Como reenvía los GET el servidor del dashboard: sin `X-Guard-Token`."""

    def abrir(app: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8010", timeout=10
        )

    return abrir


async def test_sin_token_los_get_de_guard_no_muestran_integradores(config_gw, anonimo):
    mundo = armar_gateway(config_gw, evaluador_espia(status="escalar")[0])
    async with cliente_publico(mundo.app) as c:
        compra_id, contexto = await _escalar_retenido(c)
        await c.post(f"/v1/compras/{compra_id}/aprobaciones", json={"cart_id": "cart_1"})
    async with anonimo(mundo.app) as anon:
        historial = (await anon.get("/guard/events/history")).json()
        compras = (await anon.get("/guard/compras")).json()["compras"]
        aprobaciones = (await anon.get("/guard/aprobaciones")).json()
    texto = json.dumps([historial, compras, aprobaciones], ensure_ascii=False)
    assert contexto not in texto and compra_id not in texto and "zapatillas" not in texto
    assert compras == [] and aprobaciones == {"pendientes": [], "usadas": []}

    # Con el token (la consola del operador en la máquina) se ve todo.
    async with cliente_local(mundo.app) as panel:
        historial = (await panel.get("/guard/events/history")).json()
        compras = (await panel.get("/guard/compras")).json()["compras"]
        aprobaciones = (await panel.get("/guard/aprobaciones")).json()
    assert any(e["contexto_id"] == contexto for e in historial)
    assert [x["compra_id"] for x in compras] == [compra_id]
    assert len(aprobaciones["pendientes"]) == 1


async def test_sin_token_la_demo_se_sigue_viendo(armar_proxy, anonimo):
    from proxy.tests.ayudantes import buscar as buscar_demo
    from proxy.tests.ayudantes import checkout as checkout_demo

    app = armar_proxy()
    async with anonimo(app) as anon:
        contexto = await buscar_demo(anon)
        await checkout_demo(anon, contexto, "cart_1")
        historial = (await anon.get("/guard/events/history")).json()
    assert any(e["tipo"] == "veredicto" and e["contexto_id"] == contexto for e in historial)


async def _backlog_sse(proxy: Any, headers: dict[str, str]) -> list[Evento]:
    """Los eventos que manda `GET /guard/events` antes del primer ping."""
    proxy.ping_segundos = 0.01

    async def desconectado() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    scope = {
        "type": "http", "method": "GET", "path": "/guard/events", "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    respuesta = await proxy.eventos_sse(Request(scope, desconectado))
    eventos = []
    async for bloque in respuesta.body_iterator:
        texto = bloque.decode() if isinstance(bloque, bytes) else bloque
        for linea in texto.splitlines():
            if linea.startswith("data:"):
                eventos.append(Evento.model_validate_json(linea[5:].strip()))
    return eventos


async def test_sin_token_el_sse_no_manda_eventos_de_integradores(config_gw):
    mundo = armar_gateway(config_gw, evaluador_espia(status="escalar")[0])
    async with cliente_publico(mundo.app) as c:
        _, contexto = await _escalar_retenido(c)
    sin = await _backlog_sse(mundo.proxy, {})
    con = await _backlog_sse(mundo.proxy, {HEADER_TOKEN: mundo.proxy.api_token})
    assert all(e.contexto_id != contexto for e in sin)
    assert "busqueda_enviada" not in [e.tipo for e in sin]
    assert any(e.contexto_id == contexto for e in con)


# -- estado persistente y concurrencia ------------------------------------------------


class AlmacenLento(EstadoMemoria):
    """Como PostgREST con dos upserts en vuelo: el de `negociando` tarda más."""

    async def guardar_compra(self, fila: dict[str, Any]) -> None:
        if fila["estado"] == "negociando":
            await asyncio.sleep(0.2)
        await super().guardar_compra(fila)


async def test_bloqueada_no_se_pisa_con_una_escritura_vieja(config_gw):
    almacen = AlmacenLento(durable=True)
    merchant = FakeMerchant()
    viejo = armar_gateway(
        config_gw, evaluador_espia()[0], merchant=merchant, almacen=almacen,
        filtro=lambda m: ResultadoFiltro(pasa=False, senales=["heuristica:orden_al_agente"]),
    )
    async with cliente_publico(viejo.app) as c:
        compra_id = (await crear_compra(c))["compra_id"]
        await buscar(c, compra_id)
        assert viejo.proxy.compras.get(compra_id).estado == "bloqueada"
        await viejo.esperar_fondo()
    assert (await almacen.cargar_compra(compra_id))["estado"] == "bloqueada"

    nuevo = armar_gateway(
        config_gw, evaluador_espia()[0], merchant=merchant, almacen=almacen,
        filtro=lambda m: ResultadoFiltro(pasa=True),
    )
    async with cliente_publico(nuevo.app) as c:
        assert (await c.get(f"/v1/compras/{compra_id}")).json()["estado"] == "bloqueada"
        antes = len(merchant.recibidos)
        resp = await buscar(c, compra_id)
    assert resp.status_code == 409 and len(merchant.recibidos) == antes


async def test_un_estado_terminal_se_guarda_aunque_haya_demasiadas_escrituras(config_gw):
    mundo = armar_gateway(config_gw, evaluador_espia()[0])
    proxy = mundo.proxy
    async with cliente_publico(mundo.app) as c:
        compra = proxy.compras.get((await crear_compra(c))["compra_id"])
    proxy.en_fondo._max = 0  # noqa: SLF001 - el tope de tareas en vuelo, lleno
    compra.estado = "negociando"
    proxy.guardar_compra(compra)  # se descarta (no es terminal)
    compra.estado = "bloqueada"
    proxy.guardar_compra(compra)  # no se descarta
    await proxy.en_fondo.esperar()
    assert (await proxy.almacen.cargar_compra(compra.id))["estado"] == "bloqueada"
    assert proxy.en_fondo.descartadas == 1


class ReclamoLento(EstadoMemoria):
    """La base aplica primero el reclamo de A, pero su respuesta llega
    después que la de B (dos pedidos HTTP en vuelo)."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.n = 0

    async def reclamar_aprobacion(self, aprobacion_id: str, *, usada: str, task_id: str | None) -> bool:
        self.n += 1
        ok = await super().reclamar_aprobacion(aprobacion_id, usada=usada, task_id=task_id)
        if self.n == 1:
            await asyncio.sleep(0.2)
        return ok


async def test_dos_checkouts_concurrentes_no_queman_la_aprobacion(config_gw):
    almacen = ReclamoLento(durable=True)
    mundo = armar_gateway(config_gw, evaluador_espia(status="escalar")[0], almacen=almacen)
    async with cliente_publico(mundo.app) as c:
        compra_id, contexto = await _escalar_retenido(c)
        r = await c.post(f"/v1/compras/{compra_id}/aprobaciones", json={"cart_id": "cart_1"})
        assert r.status_code == 201
        a, b = await asyncio.gather(
            checkout(c, compra_id, contexto, "cart_1"),
            checkout(c, compra_id, contexto, "cart_1"),
        )
    pasaron = [x for x in (a, b) if checkouts(x.json()["result"])]
    assert len(pasaron) == 1  # exactamente uno: un solo uso, y no se quema
    assert [f["estado"] for f in almacen._aprobaciones.values()] == ["usada"]  # noqa: SLF001
    usadas = mundo.proxy.aprobaciones.listar()["usadas"]
    assert len(usadas) == 1 and mundo.proxy.aprobaciones.listar()["pendientes"] == []


async def test_el_listado_por_estado_ve_las_compras_con_la_espera_cerrada(config_gw):
    almacen = EstadoMemoria(durable=True)
    merchant = FakeMerchant()
    viejo = armar_gateway(
        config_gw, evaluador_espia()[0], merchant=merchant, almacen=almacen, agente=AgenteFalso()
    )
    async with cliente_publico(viejo.app) as c:
        compra_id = (await crear_compra(
            c, pedido="20 computadoras", ficha=_ficha_computadoras(),
            intencion={"porque": RESPUESTA_USUARIO},
        ))["compra_id"]
        busqueda = asyncio.create_task(buscar(c, compra_id, escenario="computadoras"))
        await _compra_en(c, compra_id, "esperando_decision")
        await viejo.esperar_fondo()
        # "Reinicio": otro proceso con el mismo almacén (el viejo sigue esperando).
        nuevo = armar_gateway(config_gw, evaluador_espia()[0], merchant=merchant, almacen=almacen)
        async with cliente_publico(nuevo.app) as c2:
            vencidas = (await c2.get("/v1/compras?estado=vencida")).json()["compras"]
            esperando = (await c2.get("/v1/compras?estado=esperando_decision")).json()["compras"]
            await nuevo.esperar_fondo()
        await _cancelar(busqueda)
    assert [x["compra_id"] for x in vencidas] == [compra_id]
    assert esperando == []
    # La fila quedó normalizada: el conteo de activas ya no la cuenta.
    assert (await almacen.cargar_compra(compra_id))["estado"] == "vencida"


async def test_al_hidratar_una_compra_la_fila_queda_normalizada(config_gw):
    almacen = EstadoMemoria(durable=True)
    viejo = armar_gateway(config_gw, evaluador_espia()[0], almacen=almacen, agente=AgenteFalso())
    async with cliente_publico(viejo.app) as c:
        compra_id = (await crear_compra(
            c, pedido="20 computadoras", ficha=_ficha_computadoras(),
            intencion={"preguntar": True},
        ))["compra_id"]
        busqueda = asyncio.create_task(buscar(c, compra_id, escenario="computadoras"))
        await _compra_en(c, compra_id, "esperando_contexto")
        await viejo.esperar_fondo()
        nuevo = armar_gateway(config_gw, evaluador_espia()[0], almacen=almacen)
        async with cliente_publico(nuevo.app) as c2:
            assert (await c2.get(f"/v1/compras/{compra_id}")).json()["estado"] == "negociando"
            await nuevo.esperar_fondo()
        await _cancelar(busqueda)
    assert (await almacen.cargar_compra(compra_id))["estado"] == "negociando"


class ConteoLento(EstadoMemoria):
    """`contar_activas` es un GET a PostgREST: tarda."""

    async def contar_activas(self, integrador_id: str, desde: Any) -> int:
        n = await super().contar_activas(integrador_id, desde)
        await asyncio.sleep(0.05)
        return n


async def test_el_limite_de_compras_activas_aguanta_pedidos_concurrentes(config_gw):
    datos = datos_integradores(webhook=False)
    datos["integradores"][0]["max_compras_activas"] = 1
    mundo = armar_gateway(
        config_gw, evaluador_espia()[0],
        integradores=RegistroIntegradores.desde_dict(datos, {}),
        almacen=ConteoLento(durable=True),
    )
    async with cliente_publico(mundo.app) as c:
        resps = await asyncio.gather(*[
            c.post("/v1/compras", json={"pedido": "x", "ficha": FICHA}) for _ in range(10)
        ])
    codigos = sorted(r.status_code for r in resps)
    assert codigos == [201] + [429] * 9
