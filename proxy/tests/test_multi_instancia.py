"""Varias instancias sin memoria compartida (lote C del plan de Vercel).

Dos `create_app` con `instancias="varias"`, cada una con su `EstadoSupabase`
sobre la misma `BaseFalsa` (PostgREST en memoria): son dos instancias del
gateway contra la misma base, como en Vercel. El comercio es el FakeMerchant
de siempre, por `httpx.MockTransport`.

Lo que importa: una espera abierta en una instancia (A) se contesta por
otra (B) y A sigue con esa respuesta; gana una sola respuesta; si nadie
contesta, vence del lado seguro; si A muere, quien lea la compra después de
`vence + gracia` la cierra del lado seguro (y antes la ve esperando); una
aprobación pedida en dos instancias es una sola; lo que no quedó guardado no
se entrega; y el checkout en B usa las ofertas que registró A.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from contracts import GUARD_PARAMETROS_KEY, Ficha
from proxy.config import Config
from proxy.estado import EstadoSupabase
from proxy.tests.ayudantes import checkouts, evaluador_espia, texto_estado, veredictos
from proxy.tests.fake_world import FakeMerchant, parsear_sse
from proxy.tests.gateway_ayudantes import (
    GATEWAY_PUBLIC_URL,
    ReceptorWebhooks,
    armar_gateway,
    buscar,
    checkout,
    cliente_publico,
    crear_compra,
    despachador,
    registro_integradores,
)
from proxy.tests.postgrest_falso import BaseFalsa
from proxy.tests.test_etapa2 import DATOS, RESPUESTA_USUARIO, AgenteFalso

pytestmark = pytest.mark.anyio

URL_SUPABASE = "https://base.supabase.test"


@pytest.fixture
def config_varias(config: Config) -> Config:
    return replace(
        config,
        gateway_public_url=GATEWAY_PUBLIC_URL,
        instancias="varias",
        sondeo_s=0.02,
        gracia_espera_s=0.4,
        barrera_s=3.0,
        espera_contexto_s=2.0,
        espera_decision_s=2.0,
    )


@pytest.fixture
def config_corta(config_varias: Config) -> Config:
    """Esperas cortas: para los casos en los que nadie contesta."""
    return replace(config_varias, espera_contexto_s=0.5, espera_decision_s=0.5)


def _instancia(config: Config, base: BaseFalsa, merchant: FakeMerchant, **kwargs: Any):
    """Una instancia del gateway: su propio almacén sobre la base compartida,
    sin la demo (`demo=None`, como el motor desplegado)."""
    evaluador = kwargs.pop("evaluador", None) or evaluador_espia()[0]
    kwargs.setdefault("demo", None)
    kwargs.setdefault("almacen", _almacen(base))
    return armar_gateway(config, evaluador, merchant=merchant, **kwargs)


def _almacen(base: BaseFalsa) -> EstadoSupabase:
    return EstadoSupabase(URL_SUPABASE, "clave-de-prueba", transport=base.transporte())


def _ficha_computadoras() -> dict[str, Any]:
    ficha = Ficha.desde_intent(DATOS["intent"])
    return {"requisitos": [r.model_dump(mode="json") for r in ficha.requisitos]}


async def _compra_computadoras(c: httpx.AsyncClient, **intencion: Any) -> str:
    compra = await crear_compra(
        c, pedido="20 computadoras para el lunes", ficha=_ficha_computadoras(),
        intencion=intencion,
    )
    return compra["compra_id"]


async def _compra_en(c: httpx.AsyncClient, compra_id: str, estado: str, intentos: int = 400) -> dict:
    vista: dict[str, Any] = {}
    for _ in range(intentos):
        vista = (await c.get(f"/v1/compras/{compra_id}")).json()
        if vista.get("estado") == estado:
            return vista
        await asyncio.sleep(0.01)
    raise AssertionError(f"La compra no llegó a {estado}: {vista}")


def _datos(task: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        p["data"] for a in task.get("artifacts") or [] for p in a.get("parts") or []
        if p.get("kind") == "data"
    ]


async def _cancelar(tarea: asyncio.Task) -> None:
    tarea.cancel()
    try:
        await tarea
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 - la instancia "muere"
        pass


# -- el porqué y la oportunidad entre instancias --------------------------------------


async def test_el_porque_de_a_lo_contesta_b_y_a_sigue_con_esa_respuesta(config_varias):
    base, merchant = BaseFalsa(), FakeMerchant()
    a = _instancia(config_varias, base, merchant, agente=AgenteFalso(cart_id=None))
    b = _instancia(config_varias, base, merchant, agente=AgenteFalso(cart_id=None))
    async with cliente_publico(a.app) as ca, cliente_publico(b.app) as cb:
        compra_id = await _compra_computadoras(ca, preguntar=True)
        busqueda = asyncio.create_task(buscar(ca, compra_id, escenario="computadoras"))

        vista = await _compra_en(cb, compra_id, "esperando_contexto")  # B lo lee de la base
        assert vista["pendiente"]["tipo"] == "porque"
        assert vista["pendiente"]["pregunta"] == "¿Para qué necesitás las 20 computadoras?"
        # Ni el GET ni el listado de otra instancia cierran una espera viva.
        listado = (await cb.get("/v1/compras")).json()["compras"]
        assert [c["estado"] for c in listado] == ["esperando_contexto"]
        assert not busqueda.done()
        (espera,) = base.filas("gw_esperas")
        assert espera["estado"] == "abierta" and espera["tipo"] == "porque"
        (fila,) = base.filas("gw_compras")
        assert fila["estado"] == "esperando_contexto" and fila["espera_id"] == espera["id"]

        resp = await cb.post(
            f"/v1/compras/{compra_id}/respuesta", json={"respuesta": RESPUESTA_USUARIO}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["estado"] == "negociando"  # el estado que va a tener
        assert resp.json()["pendiente"] is None

        task = (await busqueda).json()["result"]
        assert task["status"]["state"] == "completed"
        assert a.proxy.agente.respuestas == [RESPUESTA_USUARIO]  # A armó el contexto
        assert b.proxy.agente.preguntas == [] and b.proxy.agente.respuestas == []
        (espera,) = base.filas("gw_esperas")
        assert espera["estado"] == "respondida"
        assert espera["respuesta"] == {"valor": RESPUESTA_USUARIO}
        vista = (await cb.get(f"/v1/compras/{compra_id}")).json()
        assert vista["estado"] == "sin_oportunidad"
        (fila,) = base.filas("gw_compras")
        assert fila["espera_id"] is None
        assert fila["datos"]["contexto"]["respuesta"] == RESPUESTA_USUARIO


async def test_la_oportunidad_de_a_la_acepta_b_y_a_devuelve_los_parametros(config_varias):
    base, merchant = BaseFalsa(), FakeMerchant()
    a = _instancia(config_varias, base, merchant, agente=AgenteFalso())
    b = _instancia(config_varias, base, merchant, agente=AgenteFalso())
    async with cliente_publico(a.app) as ca, cliente_publico(b.app) as cb:
        compra_id = await _compra_computadoras(ca, porque=RESPUESTA_USUARIO)
        busqueda = asyncio.create_task(buscar(ca, compra_id, escenario="computadoras"))

        vista = await _compra_en(cb, compra_id, "esperando_decision")
        oportunidad = vista["pendiente"]["oportunidad"]
        assert oportunidad["cart_id"] == "cart_2"
        (espera,) = base.filas("gw_esperas")
        assert espera["tipo"] == "decision" and espera["oportunidad_id"] == oportunidad["id"]

        distinta = await cb.post(
            f"/v1/compras/{compra_id}/decision",
            json={"oportunidad_id": "opo-otra", "decision": "aceptar"},
        )
        assert distinta.status_code == 409
        assert distinta.json()["error"]["codigo"] == "oportunidad_distinta"
        resp = await cb.post(
            f"/v1/compras/{compra_id}/decision",
            json={"oportunidad_id": oportunidad["id"], "decision": "aceptar"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["estado"] == "aceptada"
        assert any(r["campo"] == "entregas" for r in resp.json()["ficha"]["requisitos"])

        task = (await busqueda).json()["result"]
        parametros = [d[GUARD_PARAMETROS_KEY] for d in _datos(task) if GUARD_PARAMETROS_KEY in d]
        assert parametros and parametros[0]["cart_id"] == "cart_2"
        assert parametros[0]["oportunidad_id"] == oportunidad["id"]
        vista = (await ca.get(f"/v1/compras/{compra_id}")).json()
        assert vista["estado"] == "aceptada" and vista["pendiente"] is None
        assert b.proxy.agente.respuestas == []  # B no corrió nada de la búsqueda


async def test_dos_respuestas_en_a_y_en_b_gana_una_sola(config_varias):
    base, merchant = BaseFalsa(), FakeMerchant()
    agente_a = AgenteFalso(cart_id=None)
    a = _instancia(config_varias, base, merchant, agente=agente_a)
    b = _instancia(config_varias, base, merchant, agente=AgenteFalso(cart_id=None))
    async with cliente_publico(a.app) as ca, cliente_publico(b.app) as cb:
        compra_id = await _compra_computadoras(ca, preguntar=True)
        busqueda = asyncio.create_task(buscar(ca, compra_id, escenario="computadoras"))
        await _compra_en(cb, compra_id, "esperando_contexto")

        ruta = f"/v1/compras/{compra_id}/respuesta"
        resp_a, resp_b = await asyncio.gather(
            ca.post(ruta, json={"respuesta": "para un aula, por A"}),
            cb.post(ruta, json={"respuesta": "para un aula, por B"}),
        )
        assert sorted([resp_a.status_code, resp_b.status_code]) == [200, 409]
        perdedora = resp_a if resp_a.status_code == 409 else resp_b
        assert perdedora.json()["error"]["codigo"] == "estado"
        ganadora = "para un aula, por A" if resp_a.status_code == 200 else "para un aula, por B"

        assert (await busqueda).json()["result"]["status"]["state"] == "completed"
        assert agente_a.respuestas == [ganadora]  # A sigue con la que ganó
        (espera,) = base.filas("gw_esperas")
        assert espera["respuesta"] == {"valor": ganadora}


# -- sin respuesta: vence del lado seguro -----------------------------------------------


async def test_sin_respuesta_el_porque_vence_y_sigue_literal(config_corta):
    base, merchant = BaseFalsa(), FakeMerchant()
    agente = AgenteFalso()
    a = _instancia(config_corta, base, merchant, agente=agente)
    async with cliente_publico(a.app) as ca:
        compra_id = await _compra_computadoras(ca, preguntar=True)
        task = (await buscar(ca, compra_id, escenario="computadoras")).json()["result"]
    assert task["status"]["state"] == "completed"  # el pedido literal
    assert agente.respuestas == []
    (espera,) = base.filas("gw_esperas")
    assert espera["estado"] == "vencida"
    (fila,) = base.filas("gw_compras")
    assert fila["estado"] == "sin_oportunidad"  # sin contexto no hay oportunidad
    assert fila["espera_id"] is None and fila["datos"]["contexto"] is None


async def test_sin_decision_la_oportunidad_vence_y_no_se_entrega_nada(config_corta):
    base, merchant = BaseFalsa(), FakeMerchant()
    a = _instancia(config_corta, base, merchant, agente=AgenteFalso())
    b = _instancia(config_corta, base, merchant, agente=AgenteFalso())
    async with cliente_publico(a.app) as ca, cliente_publico(b.app) as cb:
        compra_id = await _compra_computadoras(ca, porque=RESPUESTA_USUARIO)
        task = (await buscar(ca, compra_id, escenario="computadoras")).json()["result"]
        assert task["status"]["state"] == "failed"
        assert "en pausa" in texto_estado(task)
        assert not any(GUARD_PARAMETROS_KEY in d for d in _datos(task))
        vista = (await cb.get(f"/v1/compras/{compra_id}")).json()
        assert vista["estado"] == "vencida"  # el silencio no es autorización
        tarde = await cb.post(
            f"/v1/compras/{compra_id}/decision",
            json={"oportunidad_id": "opo-x", "decision": "aceptar"},
        )
        assert tarde.status_code == 409
        otra = await buscar(cb, compra_id, escenario="computadoras")
        assert otra.status_code == 409
        assert otra.json()["error"]["data"]["codigo"] == "compra_vencida"
    (espera,) = base.filas("gw_esperas")
    assert espera["estado"] == "vencida"


# -- la instancia que retiene muere ---------------------------------------------------------


async def test_si_a_muere_el_porque_se_ve_esperando_y_despues_de_la_gracia_sigue_literal(
    config_corta,
):
    base, merchant = BaseFalsa(), FakeMerchant()
    a = _instancia(config_corta, base, merchant, agente=AgenteFalso())
    b = _instancia(config_corta, base, merchant, agente=AgenteFalso())
    async with cliente_publico(a.app) as ca, cliente_publico(b.app) as cb:
        compra_id = await _compra_computadoras(ca, preguntar=True)
        busqueda = asyncio.create_task(buscar(ca, compra_id, escenario="computadoras"))
        await _compra_en(cb, compra_id, "esperando_contexto")
        await _cancelar(busqueda)  # A se congela: no escribe nada más

        # Antes de vence + gracia, cualquier instancia la ve esperando.
        vista = (await cb.get(f"/v1/compras/{compra_id}")).json()
        assert vista["estado"] == "esperando_contexto"
        assert vista["pendiente"]["tipo"] == "porque"
        (espera,) = base.filas("gw_esperas")
        assert espera["estado"] == "abierta"

        await asyncio.sleep(config_corta.espera_contexto_s + config_corta.gracia_espera_s + 0.2)
        vista = (await cb.get(f"/v1/compras/{compra_id}")).json()
        assert vista["estado"] == "negociando" and vista["pendiente"] is None
        (espera,) = base.filas("gw_esperas")
        assert espera["estado"] == "vencida"
        (fila,) = base.filas("gw_compras")
        assert fila["estado"] == "negociando" and fila["espera_id"] is None
        tarde = await cb.post(f"/v1/compras/{compra_id}/respuesta", json={"omitir": True})
        assert tarde.status_code == 409
        # La búsqueda siguiente (en otra instancia) sigue el pedido literal.
        task = (await buscar(cb, compra_id, escenario="computadoras")).json()["result"]
        assert task["status"]["state"] == "completed"
        assert b.proxy.agente.preguntas == []


async def test_si_a_muere_esperando_la_decision_despues_de_la_gracia_queda_vencida(config_corta):
    base, merchant = BaseFalsa(), FakeMerchant()
    a = _instancia(config_corta, base, merchant, agente=AgenteFalso())
    b = _instancia(config_corta, base, merchant, agente=AgenteFalso())
    async with cliente_publico(a.app) as ca, cliente_publico(b.app) as cb:
        compra_id = await _compra_computadoras(ca, porque=RESPUESTA_USUARIO)
        busqueda = asyncio.create_task(buscar(ca, compra_id, escenario="computadoras"))
        await _compra_en(cb, compra_id, "esperando_decision")
        await _cancelar(busqueda)

        vista = (await cb.get(f"/v1/compras/{compra_id}")).json()
        assert vista["estado"] == "esperando_decision"
        assert vista["pendiente"]["tipo"] == "oportunidad"

        await asyncio.sleep(config_corta.espera_decision_s + config_corta.gracia_espera_s + 0.2)
        vista = (await cb.get(f"/v1/compras/{compra_id}")).json()
        assert vista["estado"] == "vencida" and vista["pendiente"] is None
        (espera,) = [e for e in base.filas("gw_esperas") if e["tipo"] == "decision"]
        assert espera["estado"] == "vencida"
        otra = await buscar(cb, compra_id, escenario="computadoras")
        assert otra.json()["error"]["data"]["codigo"] == "compra_vencida"


# -- aprobaciones y checkout entre instancias ---------------------------------------------


async def test_la_misma_aprobacion_pedida_en_dos_instancias_es_una_sola(config_varias):
    base, merchant = BaseFalsa(), FakeMerchant()
    a = _instancia(config_varias, base, merchant, evaluador=evaluador_espia(status="escalar")[0])
    b = _instancia(config_varias, base, merchant, evaluador=evaluador_espia(status="escalar")[0])
    async with cliente_publico(a.app) as ca, cliente_publico(b.app) as cb:
        compra_id = (await crear_compra(ca))["compra_id"]
        contexto = (await buscar(ca, compra_id)).json()["result"]["contextId"]
        retenido = (await checkout(ca, compra_id, contexto, "cart_1")).json()["result"]
        assert not checkouts(retenido)
        assert veredictos(retenido)[0]["status"] == "escalar"

        ruta = f"/v1/compras/{compra_id}/aprobaciones"
        resp_a, resp_b = await asyncio.gather(
            ca.post(ruta, json={"cart_id": "cart_1"}), cb.post(ruta, json={"cart_id": "cart_1"})
        )
        assert sorted([resp_a.status_code, resp_b.status_code]) == [200, 201]
        assert resp_a.json()["id"] == resp_b.json()["id"]
        otra_vez = await cb.post(ruta, json={"cart_id": "cart_1"})
        assert otra_vez.status_code == 200 and otra_vez.json()["id"] == resp_a.json()["id"]
        (fila,) = base.filas("gw_aprobaciones")
        assert fila["estado"] == "pendiente" and fila["compra_id"] == compra_id

        # La usa el checkout en la otra instancia, una sola vez.
        pasa = (await checkout(cb, compra_id, contexto, "cart_1")).json()["result"]
        assert checkouts(pasa) == [merchant.ultimo_checkout]
        (fila,) = base.filas("gw_aprobaciones")
        assert fila["estado"] == "usada"
        de_nuevo = (await checkout(ca, compra_id, contexto, "cart_1")).json()["result"]
        assert not checkouts(de_nuevo)
        vista = (await ca.get(f"/v1/compras/{compra_id}")).json()
        assert [x["estado"] for x in vista["aprobaciones"]] == ["usada"]
        assert vista["contextos"] == [contexto]


async def test_el_checkout_en_b_usa_las_ofertas_que_registro_a(config_varias):
    base, merchant = BaseFalsa(), FakeMerchant()
    evaluador, llamadas = evaluador_espia()
    a = _instancia(config_varias, base, merchant)
    b = _instancia(config_varias, base, merchant, evaluador=evaluador)
    async with cliente_publico(a.app) as ca, cliente_publico(b.app) as cb:
        compra_id = (await crear_compra(ca))["compra_id"]
        contexto = (await buscar(ca, compra_id)).json()["result"]["contextId"]
        assert b.proxy._ofertas == {}
        task = (await checkout(cb, compra_id, contexto, "cart_1")).json()["result"]
        assert checkouts(task) == [merchant.ultimo_checkout]
        assert [c["cart_id"] for c in llamadas[0]["ofertas"].carts] == [
            "cart_1", "cart_2", "cart_3", "cart_4",
        ]
        vista = (await ca.get(f"/v1/compras/{compra_id}")).json()  # A lee lo que escribió B
        assert vista["veredictos"][-1]["status"] == "aprobado"
        assert vista["contextos"] == [contexto]
    (ultimo,) = base.filas("gw_ultimos_veredictos")
    assert ultimo["status"] == "aprobado"


def _hay_checkout(resultados: list[dict[str, Any]]) -> bool:
    def partes(r: dict[str, Any]) -> list[dict[str, Any]]:
        if r.get("kind") == "artifact-update":
            return r["artifact"]["parts"]
        if r.get("kind") == "task":
            return [p for a in r.get("artifacts") or [] for p in a["parts"]]
        return []

    return any("ap2.checkout" in p.get("data", {}) for r in resultados for p in partes(r))


async def test_por_stream_el_checkout_en_b_usa_las_ofertas_de_a(config_varias):
    base, merchant = BaseFalsa(), FakeMerchant()
    a = _instancia(config_varias, base, merchant)
    b = _instancia(config_varias, base, merchant)
    async with cliente_publico(a.app) as ca, cliente_publico(b.app) as cb:
        compra_id = (await crear_compra(ca))["compra_id"]
        busqueda = await buscar(ca, compra_id, metodo="message/stream")
        resultados = [e["result"] for e in parsear_sse(busqueda.content)]
        contexto = resultados[-1]["contextId"]
        assert base.filas("gw_ofertas")[0]["contexto_id"] == contexto  # antes del evento final
        resp = await checkout(cb, compra_id, contexto, "cart_1", metodo="message/stream")
        resultados = [e["result"] for e in parsear_sse(resp.content)]
        assert _hay_checkout(resultados)
        vista = (await ca.get(f"/v1/compras/{compra_id}")).json()
        assert vista["estado"] == "sin_oportunidad"
        assert vista["veredictos"][-1]["status"] == "aprobado"


async def test_por_stream_si_las_ofertas_no_quedan_guardadas_termina_con_error(config_varias):
    base, merchant = BaseFalsa(), FakeMerchant()
    a = _instancia(config_varias, base, merchant)
    async with cliente_publico(a.app) as ca:
        compra_id = (await crear_compra(ca))["compra_id"]
        _falla(base, "POST", "gw_ofertas")
        resp = await buscar(ca, compra_id, metodo="message/stream")
    eventos = parsear_sse(resp.content)
    assert [e["error"]["data"]["codigo"] for e in eventos] == ["estado_no_disponible"]
    assert "cart_1" not in resp.text  # ninguna oferta salió antes del error


async def test_el_tope_de_contextos_es_de_la_compra_y_no_de_cada_instancia(config_varias):
    base, merchant = BaseFalsa(), FakeMerchant()
    a = _instancia(config_varias, base, merchant)
    b = _instancia(config_varias, base, merchant)
    async with cliente_publico(a.app) as ca, cliente_publico(b.app) as cb:
        compra_id = (await crear_compra(ca))["compra_id"]
        for n in range(20):
            c = ca if n % 2 else cb
            resp = await buscar(c, compra_id, contexto=f"ctx-propio-{n}")
            assert resp.status_code == 200, resp.text
        # Uno que ya ató la otra instancia sigue valiendo (no es nuevo).
        assert (await buscar(ca, compra_id, contexto="ctx-propio-0")).status_code == 200
        for c in (ca, cb):
            resp = await buscar(c, compra_id, contexto="ctx-propio-20")
            assert resp.status_code == 409
            assert resp.json()["error"]["data"]["codigo"] == "contexto_ajeno"


# -- lo que no quedó guardado no se entrega -------------------------------------------------


def _falla(base: BaseFalsa, metodo: str, tabla: str) -> None:
    def interceptar(request: httpx.Request) -> httpx.Response | None:
        if request.method == metodo and request.url.path.endswith(f"/rest/v1/{tabla}"):
            return httpx.Response(500, json={"message": "caída de prueba"})
        return None

    base.interceptar = interceptar


async def test_si_las_ofertas_no_quedan_guardadas_find_products_da_503(config_varias):
    base, merchant = BaseFalsa(), FakeMerchant()
    a = _instancia(config_varias, base, merchant)
    async with cliente_publico(a.app) as ca:
        compra_id = (await crear_compra(ca))["compra_id"]
        _falla(base, "POST", "gw_ofertas")
        resp = await buscar(ca, compra_id)
    assert resp.status_code == 503
    assert resp.json()["error"]["data"]["codigo"] == "estado_no_disponible"
    assert "cart_1" not in resp.text  # las ofertas no se reenvían
    assert base.filas("gw_ofertas") == []


async def test_si_el_veredicto_no_queda_guardado_el_checkout_se_bloquea(config_varias):
    base, merchant = BaseFalsa(), FakeMerchant()
    a = _instancia(config_varias, base, merchant)
    async with cliente_publico(a.app) as ca:
        compra_id = (await crear_compra(ca))["compra_id"]
        contexto = (await buscar(ca, compra_id)).json()["result"]["contextId"]
        _falla(base, "POST", "gw_ultimos_veredictos")
        resp = await checkout(ca, compra_id, contexto, "cart_1")
    assert resp.status_code == 200
    task = resp.json()["result"]
    assert not checkouts(task)  # no llega a la firma
    assert veredictos(task)[0]["status"] == "bloqueado"


async def test_si_la_espera_no_se_puede_abrir_el_porque_sigue_literal(config_varias):
    base, merchant = BaseFalsa(), FakeMerchant()
    agente = AgenteFalso()
    a = _instancia(config_varias, base, merchant, agente=agente)
    async with cliente_publico(a.app) as ca:
        compra_id = await _compra_computadoras(ca, preguntar=True)
        _falla(base, "POST", "gw_esperas")
        task = (await buscar(ca, compra_id, escenario="computadoras")).json()["result"]
        await a.esperar_fondo()
    assert task["status"]["state"] == "completed"  # literal, sin pregunta
    assert "compra.pregunta" not in a.receptor.tipos()  # sin fila no hay webhook
    assert agente.respuestas == []


# -- webhooks en línea ------------------------------------------------------------------------


class ReceptorLento(ReceptorWebhooks):
    async def handler(self, request: httpx.Request) -> httpx.Response:  # type: ignore[override]
        await asyncio.sleep(0.2)
        return super().handler(request)


async def test_con_en_linea_el_webhook_se_entrega_antes_de_responder(config_varias):
    config = replace(config_varias, webhooks_perfil="en_linea")
    base, merchant = BaseFalsa(), FakeMerchant()
    integradores = registro_integradores()
    receptor = ReceptorLento()
    almacen = _almacen(base)
    a = _instancia(
        config, base, merchant, integradores=integradores, receptor=receptor, almacen=almacen,
        despachador=despachador(integradores, receptor, perfil="en_linea", almacen=almacen),
    )
    b = _instancia(config, base, merchant, integradores=integradores, con_webhooks=True)
    async with cliente_publico(a.app) as ca, cliente_publico(b.app) as cb:
        compra_id = (await crear_compra(ca))["compra_id"]
        contexto = (await buscar(ca, compra_id)).json()["result"]["contextId"]
        resp = await checkout(ca, compra_id, contexto, "cart_1")
        assert checkouts(resp.json()["result"])
        # Sin esperar nada más: la entrega ya salió y quedó registrada.
        assert "compra.veredicto" in receptor.tipos()
        (cuerpo,) = [c for c in receptor.cuerpos() if c["tipo"] == "compra.veredicto"]
        assert cuerpo["compra"]["veredictos"][-1]["status"] == "aprobado"
        # Las entregas de otra instancia se leen del almacén.
        entregas = (await cb.get("/v1/webhooks/entregas")).json()["entregas"]
        assert [e["tipo"] for e in entregas] == ["compra.veredicto"]
        assert entregas[0]["estado"] == "entregada"


def test_el_perfil_en_linea_son_dos_intentos_cortos():
    receptor = ReceptorWebhooks()
    en_linea = despachador(registro_integradores(), receptor, perfil="en_linea")
    fondo = despachador(registro_integradores(), receptor)
    assert (en_linea._max_intentos, en_linea._backoff_s, en_linea._timeout_s) == (2, (1.0,), 5.0)
    assert fondo._max_intentos == 6 and fondo._timeout_s == 10.0


# -- proxy/esperas.py ---------------------------------------------------------------------------


def _oportunidad():
    from contracts import Oportunidad, Requisito

    return Oportunidad(
        id="opo-0123456789ab", cart_id="cart_2", cambios=[], por_que="cubre la necesidad",
        requisitos_propuestos=[
            Requisito(campo="precio", operador="maximo", valor=20000, obligatorio=True)
        ],
    )


async def _compra_esperando(almacen, tipo: str, *, vence_hace_s: float):
    """Una compra guardada esperando (`tipo`), con su espera abierta y un
    `vence` de hace `vence_hace_s` segundos (como si su instancia hubiera muerto)."""
    from proxy.compras import RegistroCompras
    from proxy.estado import fila_espera

    compra = RegistroCompras().crear_api(
        integrador_id="acme", comercio_id="merchant_1", pedido="20 computadoras",
        ficha=Ficha.desde_intent(DATOS["intent"]), referencia=None, porque=None,
        preguntar_porque=True, intencion_activa=True,
    )
    await almacen.guardar_compra(compra.a_fila())
    ahora = datetime.now(timezone.utc)
    vence = ahora - timedelta(seconds=vence_hace_s)
    if tipo == "decision":
        compra.oportunidad = _oportunidad()
        compra.estado = "esperando_decision"
    else:
        compra.nueva_espera("¿para qué?")
    compra.vence = vence
    fila = fila_espera(
        compra.id, tipo, vence, creada=ahora - timedelta(seconds=60),
        oportunidad_id=compra.oportunidad.id if tipo == "decision" else None,
    )
    assert await almacen.abrir_espera(fila) is not None
    compra.espera_id = fila["id"]
    compra.tocar()
    await almacen.guardar_compra(compra.a_fila(espera=True))
    return compra, fila["id"]


def _esperas(almacen, gracia_s: float = 1.0):
    from proxy.esperas import EsperaCompartida

    async def escribir(compra, estado_esperado, espera_esperada):
        compra.tocar()
        return await almacen.guardar_compra_condicionada(
            compra.a_fila(espera=True), estado_esperado=estado_esperado,
            espera_id_esperado=espera_esperada,
        )

    return EsperaCompartida(almacen, escribir, sondeo_s=0.01, gracia_s=gracia_s)


async def test_una_huerfana_con_la_decision_tomada_queda_con_esa_decision():
    """El usuario decidió antes de vencer y la instancia murió sin aplicarlo:
    quien la lee aplica esa decisión (es la que ya le devolvió el POST)."""
    from proxy.estado import EstadoMemoria

    almacen = EstadoMemoria(durable=True)
    compra, espera_id = await _compra_esperando(almacen, "decision", vence_hace_s=-60)
    assert await almacen.responder_espera(
        espera_id, {"valor": "aceptar"}, datetime.now(timezone.utc),
        tipo="decision", oportunidad_id=compra.oportunidad.id,
    )
    # Se lee pasada la gracia (la espera vencía en 60 s).
    despues = datetime.now(timezone.utc) + timedelta(seconds=120)
    leida = await _esperas(almacen).compra_de_fila(
        await almacen.cargar_compra(compra.id), ahora=despues
    )
    assert leida.estado == "aceptada" and leida.espera_id is None
    fila = await almacen.cargar_compra(compra.id)
    assert fila["estado"] == "aceptada" and fila["espera_id"] is None


async def test_una_huerfana_viva_no_se_cierra_y_pasada_la_gracia_si():
    from proxy.estado import EstadoMemoria

    almacen = EstadoMemoria(durable=True)
    compra, espera_id = await _compra_esperando(almacen, "porque", vence_hace_s=0.5)
    esperas = _esperas(almacen, gracia_s=10.0)
    viva = await esperas.compra_de_fila(await almacen.cargar_compra(compra.id))
    assert viva.estado == "esperando_contexto" and viva.espera_id == espera_id
    assert not viva.contexto_listo.is_set() and not viva.resuelta.is_set()
    assert (await almacen.leer_espera(espera_id))["estado"] == "abierta"

    despues = datetime.now(timezone.utc) + timedelta(seconds=11)
    cerrada = await esperas.compra_de_fila(await almacen.cargar_compra(compra.id), ahora=despues)
    assert cerrada.estado == "negociando" and cerrada.pregunta == ""
    assert (await almacen.leer_espera(espera_id))["estado"] == "vencida"


@pytest.mark.parametrize(
    ("tipo", "respuesta", "esperado"),
    [
        ("porque", {"valor": "para un aula"}, ("para un aula", False)),
        ("porque", {"valor": None}, (None, False)),          # omitió
        ("porque", {"valor": 3}, (None, True)),              # no es texto: no vale
        ("porque", {"valor": "x" * 5000}, (None, True)),     # demasiado largo
        ("decision", {"valor": "aceptar"}, ("aceptar", False)),
        ("decision", {"valor": "comprar todo"}, (None, True)),  # no autoriza nada
        ("decision", "aceptar", (None, True)),
    ],
)
async def test_la_respuesta_de_una_espera_se_valida(tipo, respuesta, esperado):
    from proxy.compras import RegistroCompras
    from proxy.esperas import EsperaCompartida

    compra = RegistroCompras().crear_api(
        integrador_id="acme", comercio_id="merchant_1", pedido="x",
        ficha=Ficha.desde_intent(DATOS["intent"]), referencia=None, porque=None,
        preguntar_porque=True, intencion_activa=True,
    )
    compra.oportunidad = _oportunidad()
    fila = {
        "estado": "respondida", "tipo": tipo, "respuesta": respuesta,
        "oportunidad_id": compra.oportunidad.id if tipo == "decision" else None,
    }
    fin = EsperaCompartida.fin(fila, tipo, compra)
    assert (fin.valor, fin.vencida) == esperado
    # De otra oportunidad, una decisión no vale.
    if tipo == "decision":
        otra = {**fila, "oportunidad_id": "opo-otra"}
        assert EsperaCompartida.fin(otra, tipo, compra).vencida


# -- config ------------------------------------------------------------------------------------


def test_los_defaults_dejan_una_sola_instancia():
    config = Config.desde_entorno({})
    assert config.instancias == "una" and not config.varias_instancias
    assert config.presupuesto_llamada_s == 570.0
    assert (config.sondeo_s, config.gracia_espera_s, config.barrera_s) == (1.0, 30.0, 10.0)
    assert config.webhooks_perfil == "fondo"


def test_las_variables_de_varias_instancias():
    config = Config.desde_entorno({
        "GATEWAY_INSTANCIAS": "varias",
        "PROXY_PRESUPUESTO_LLAMADA_S": "270",
        "GATEWAY_SONDEO_S": "0.5",
        "GATEWAY_GRACIA_ESPERA_S": "0",
        "GATEWAY_BARRERA_S": "8",
        "GATEWAY_WEBHOOKS_PERFIL": "en_linea",
    })
    assert config.varias_instancias and config.presupuesto_llamada_s == 270.0
    assert (config.sondeo_s, config.gracia_espera_s, config.barrera_s) == (0.5, 0.0, 8.0)
    assert config.webhooks_perfil == "en_linea"
    # Como las va a forzar el arranque de Vercel.
    forzada = replace(Config.desde_entorno({}), instancias="varias", webhooks_perfil="en_linea")
    assert forzada.varias_instancias


@pytest.mark.parametrize(
    ("variable", "valor"),
    [
        ("GATEWAY_INSTANCIAS", "muchas"),
        ("GATEWAY_WEBHOOKS_PERFIL", "rapido"),
        ("PROXY_PRESUPUESTO_LLAMADA_S", "0"),
        ("PROXY_PRESUPUESTO_LLAMADA_S", "600"),
        ("PROXY_PRESUPUESTO_LLAMADA_S", "nan"),
        ("GATEWAY_SONDEO_S", "0"),
        ("GATEWAY_SONDEO_S", "11"),
        ("GATEWAY_SONDEO_S", "inf"),
        ("GATEWAY_GRACIA_ESPERA_S", "-1"),
        ("GATEWAY_BARRERA_S", "0"),
        ("GATEWAY_BARRERA_S", "61"),
        ("GATEWAY_BARRERA_S", "diez"),
    ],
)
def test_un_valor_invalido_no_arma_la_config(variable, valor):
    with pytest.raises(ValueError):
        Config.desde_entorno({variable: valor})


@pytest.mark.parametrize(
    "cambio",
    [
        {"instancias": "muchas"},
        {"webhooks_perfil": "rapido"},
        {"presupuesto_llamada_s": 600.0},
        {"sondeo_s": 0.0},
        {"gracia_espera_s": float("nan")},
        {"barrera_s": -1.0},
    ],
)
def test_replace_con_un_valor_invalido_tampoco(cambio):
    with pytest.raises(ValueError):
        replace(Config(), **cambio)


async def test_la_fila_de_una_instancia_no_lleva_espera_id():
    """Con una sola instancia la base puede tener solo 003 (sin la columna);
    con varias, la fila lleva el `espera_id`, también en None (lo borra)."""
    from proxy.compras import RegistroCompras

    compra = RegistroCompras().crear_api(
        integrador_id="acme", comercio_id="merchant_1", pedido="20 computadoras",
        ficha=Ficha.desde_intent(DATOS["intent"]), referencia=None, porque=None,
        preguntar_porque=True, intencion_activa=True,
    )
    assert "espera_id" not in compra.a_fila()
    assert compra.a_fila(espera=True)["espera_id"] is None
    compra.espera_id = "esp-0123456789abcdef"
    assert json.loads(json.dumps(compra.a_fila(espera=True)))["espera_id"] == compra.espera_id


# -- revisión adversarial: el corte entra, nada queda trabado, nada se pisa ---------------


def _filtro_que_corta(objetivo: Any) -> Any:
    from contracts import ResultadoFiltro

    return ResultadoFiltro(pasa=False, senales=["inyeccion"])


async def test_el_corte_entra_aunque_una_copia_vieja_haya_llegado_antes(config_varias):
    """Otra instancia (con el reloj adelantado) escribió una copia de la
    compra: el corte del filtro llega con una `actualizada` menor y el
    trigger igual lo deja entrar. La conversación queda cortada."""
    base, merchant = BaseFalsa(), FakeMerchant()
    adelantada = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    def copia_vieja_y_corte(objetivo: Any) -> Any:
        # Justo antes del corte, otra instancia escribe su copia (reloj adelantado).
        (fila,) = base.tablas["gw_compras"].filas
        fila["actualizada"] = adelantada
        return _filtro_que_corta(objetivo)

    a = _instancia(config_varias, base, merchant, filtro=copia_vieja_y_corte)
    b = _instancia(config_varias, base, merchant)
    async with cliente_publico(a.app) as ca, cliente_publico(b.app) as cb:
        compra_id = (await crear_compra(ca))["compra_id"]
        task = (await buscar(ca, compra_id)).json()["result"]
        assert task["status"]["state"] == "failed"  # cortada por el filtro
        (fila,) = base.filas("gw_compras")
        assert fila["estado"] == "bloqueada"
        # `actualizada` no retrocede (el corte traía una marca menor).
        assert datetime.fromisoformat(fila["actualizada"]) >= datetime.fromisoformat(adelantada)
        otra = await buscar(cb, compra_id)
        assert otra.status_code == 409
        assert otra.json()["error"]["data"]["codigo"] == "compra_bloqueada"


async def test_si_el_corte_no_queda_en_la_base_el_pedido_falla_cerrado(config_varias):
    """El upsert del corte responde bien pero la fila no quedó terminal: se
    relee, es una escritura decisiva fallida y no se entrega nada (503)."""
    base, merchant = BaseFalsa(), FakeMerchant()
    a = _instancia(config_varias, base, merchant, filtro=_filtro_que_corta)
    async with cliente_publico(a.app) as ca:
        compra_id = (await crear_compra(ca))["compra_id"]

        def sin_aplicar(request: httpx.Request) -> httpx.Response | None:
            es_upsert = request.method == "POST" and request.url.path.endswith("/gw_compras")
            if es_upsert and json.loads(request.content).get("estado") == "bloqueada":
                return httpx.Response(201)  # "bien", pero no quedó
            return None

        base.interceptar = sin_aplicar
        resp = await buscar(ca, compra_id)
    assert resp.status_code == 503
    assert resp.json()["error"]["data"]["codigo"] == "estado_no_disponible"
    assert "cart_1" not in resp.text


async def _espera_huerfana(base: BaseFalsa, compra_id: str, *, vence_hace_s: float) -> str:
    """Una espera abierta que nadie retiene (la instancia que la abrió murió
    antes de escribir la compra): la compra no la menciona."""
    from proxy.estado import fila_espera

    ahora = datetime.now(timezone.utc)
    fila = fila_espera(
        compra_id, "porque", ahora - timedelta(seconds=vence_hace_s),
        creada=ahora - timedelta(seconds=abs(vence_hace_s) + 60),
    )
    assert await _almacen(base).abrir_espera(fila) is not None
    return fila["id"]


async def test_una_espera_huerfana_no_traba_la_compra(config_varias):
    base, merchant = BaseFalsa(), FakeMerchant()
    a = _instancia(config_varias, base, merchant, agente=AgenteFalso(cart_id=None))
    b = _instancia(config_varias, base, merchant, agente=AgenteFalso(cart_id=None))
    async with cliente_publico(a.app) as ca, cliente_publico(b.app) as cb:
        compra_id = await _compra_computadoras(ca, preguntar=True)
        huerfana = await _espera_huerfana(base, compra_id, vence_hace_s=5)
        busqueda = asyncio.create_task(buscar(ca, compra_id, escenario="computadoras"))
        vista = await _compra_en(cb, compra_id, "esperando_contexto")  # se abrió la nueva
        assert vista["pendiente"]["tipo"] == "porque"
        resp = await cb.post(
            f"/v1/compras/{compra_id}/respuesta", json={"respuesta": RESPUESTA_USUARIO}
        )
        assert resp.status_code == 200, resp.text
        assert (await busqueda).json()["result"]["status"]["state"] == "completed"
    esperas = {e["id"]: e for e in base.filas("gw_esperas")}
    assert esperas[huerfana]["estado"] == "vencida"
    assert sorted(e["estado"] for e in esperas.values()) == ["respondida", "vencida"]


async def test_si_el_insert_de_la_espera_queda_pero_falla_la_respuesta_se_cierra(config_varias):
    base, merchant = BaseFalsa(), FakeMerchant()
    agente = AgenteFalso(cart_id=None)
    a = _instancia(config_varias, base, merchant, agente=agente)
    async with cliente_publico(a.app) as ca:
        compra_id = await _compra_computadoras(ca, preguntar=True)

        def aplica_y_falla(request: httpx.Request) -> httpx.Response | None:
            if request.method == "POST" and request.url.path.endswith("/gw_esperas"):
                base.interceptar = None
                try:
                    base(request)  # el insert queda en la base...
                finally:
                    base.interceptar = aplica_y_falla
                return httpx.Response(504)  # ...pero la respuesta no llega
            return None

        base.interceptar = aplica_y_falla
        task = (await buscar(ca, compra_id, escenario="computadoras")).json()["result"]
    assert task["status"]["state"] == "completed"  # el porqué sigue literal
    assert agente.respuestas == []
    (espera,) = base.filas("gw_esperas")
    assert espera["estado"] == "vencida"  # no quedó una abierta que nadie retiene


async def test_otra_busqueda_no_espera_el_presupuesto_por_una_espera_huerfana(
    config_varias, monkeypatch
):
    """La fila de la compra no está esperando pero hay una espera abierta:
    se acota con el `vence` de esa espera y, pasada la gracia, se cierra."""
    import proxy.app as app_mod
    from proxy import a2a

    monkeypatch.setattr(app_mod, "_MARGEN_ESPERA_SECUNDARIA_S", 0.05)
    base, merchant = BaseFalsa(), FakeMerchant()
    a = _instancia(config_varias, base, merchant)
    async with cliente_publico(a.app) as ca:
        compra_id = (await crear_compra(ca))["compra_id"]
        huerfana = await _espera_huerfana(base, compra_id, vence_hace_s=-0.2)  # vence en 0,2 s
        compra = await a.proxy.hidratar_compra(compra_id)
        llamada = app_mod.Llamada(pedido=a2a.Pedido(), guard_activo=True, compra=compra)
        inicio = asyncio.get_running_loop().time()
        assert await asyncio.wait_for(a.proxy._esperar_otra_instancia(llamada, compra), 5)
        assert asyncio.get_running_loop().time() - inicio < 3
    (espera,) = [e for e in base.filas("gw_esperas") if e["id"] == huerfana]
    assert espera["estado"] == "vencida"


async def _decidida_en_a(a, base: BaseFalsa, compra_id: str) -> dict[str, Any]:
    """A acepta una oportunidad (la ficha cambia, la espera se cierra)."""
    compra = await a.proxy.hidratar_compra(compra_id)
    compra.estado = "esperando_decision"
    compra.oportunidad = _oportunidad()
    compra.aplicar_decision("aceptar")
    a.proxy.guardar_compra(compra)
    await a.proxy.en_fondo.esperar()
    (fila,) = base.filas("gw_compras")
    assert fila["estado"] == "aceptada"
    return fila


async def test_una_copia_vieja_no_pisa_lo_que_decidio_otra_instancia(config_varias):
    base, merchant = BaseFalsa(), FakeMerchant()
    a = _instancia(config_varias, base, merchant)
    b = _instancia(config_varias, base, merchant)
    async with cliente_publico(a.app) as ca:
        compra_id = (await crear_compra(ca))["compra_id"]
    copia = await b.proxy.hidratar_compra(compra_id)  # B la lee antes de la decisión
    aceptada = await _decidida_en_a(a, base, compra_id)

    # B termina su análisis con la copia vieja: sin oportunidad. No pisa.
    copia.estado = "sin_oportunidad"
    b.proxy.guardar_compra(copia)
    await b.proxy.en_fondo.esperar()
    (fila,) = base.filas("gw_compras")
    assert fila["estado"] == "aceptada"
    assert fila["datos"]["ficha"] == aceptada["datos"]["ficha"]
    assert fila["datos"]["requisitos_autorizados"] == aceptada["datos"]["requisitos_autorizados"]
    assert b.proxy.en_fondo.fallas_decisivas == 0  # no es una falla: ya no aplicaba

    # Lo que B agrega sin chocar (un veredicto) sí entra, sobre la fila de A.
    copia.registrar_veredicto({"status": "aprobado", "cart_id": "cart_1"})
    b.proxy.guardar_compra(copia)
    await b.proxy.en_fondo.esperar()
    (fila,) = base.filas("gw_compras")
    assert fila["estado"] == "aceptada"
    assert fila["datos"]["ficha"] == aceptada["datos"]["ficha"]
    assert fila["datos"]["veredictos"] == [{"status": "aprobado", "cart_id": "cart_1"}]


async def test_una_copia_vieja_no_revive_una_aprobacion_pendiente(config_varias):
    base, merchant = BaseFalsa(), FakeMerchant()
    a = _instancia(config_varias, base, merchant)
    b = _instancia(config_varias, base, merchant)
    async with cliente_publico(a.app) as ca:
        compra_id = (await crear_compra(ca))["compra_id"]
    compra = await a.proxy.hidratar_compra(compra_id)
    compra.estado = "sin_oportunidad"
    compra.aprobacion_pendiente = {"contexto_id": "ctx-1", "cart_id": "cart_1"}
    a.proxy.guardar_compra(compra)
    await a.proxy.en_fondo.esperar()

    copia = await b.proxy.hidratar_compra(compra_id)  # con la pendiente
    compra.aprobacion_pendiente = None  # A la aprueba
    a.proxy.guardar_compra(compra)
    await a.proxy.en_fondo.esperar()

    copia.registrar_veredicto({"status": "escalar", "cart_id": "cart_2"})
    b.proxy.guardar_compra(copia)
    await b.proxy.en_fondo.esperar()
    (fila,) = base.filas("gw_compras")
    assert fila["estado"] == "sin_oportunidad"
    assert fila["datos"]["aprobacion_pendiente"] is None
    assert fila["datos"]["veredictos"] == [{"status": "escalar", "cart_id": "cart_2"}]


async def test_una_instancia_que_escribe_seguido_no_se_pisa_a_si_misma(config_varias):
    base, merchant = BaseFalsa(), FakeMerchant()
    a = _instancia(config_varias, base, merchant)
    async with cliente_publico(a.app) as ca:
        compra_id = (await crear_compra(ca))["compra_id"]
    compra = await a.proxy.hidratar_compra(compra_id)
    for estado in ("negociando", "sin_oportunidad"):
        compra.estado = estado
        compra.registrar_veredicto({"status": "aprobado", "estado": estado})
        a.proxy.guardar_compra(compra)
    await a.proxy.en_fondo.esperar()
    assert a.proxy.en_fondo.fallas_decisivas == 0
    (fila,) = base.filas("gw_compras")
    assert fila["estado"] == "sin_oportunidad"
    assert [v["estado"] for v in fila["datos"]["veredictos"]] == ["negociando", "sin_oportunidad"]


async def test_si_la_compra_cambia_en_cada_intento_el_checkout_se_bloquea(config_varias):
    base, merchant = BaseFalsa(), FakeMerchant()
    a = _instancia(config_varias, base, merchant)
    async with cliente_publico(a.app) as ca:
        compra_id = (await crear_compra(ca))["compra_id"]
        contexto = (await buscar(ca, compra_id)).json()["result"]["contextId"]

        def nunca_coincide(request: httpx.Request) -> httpx.Response | None:
            if request.method == "PATCH" and request.url.path.endswith("/gw_compras"):
                return httpx.Response(200, json=[])  # "otra instancia escribió en el medio"
            return None

        base.interceptar = nunca_coincide
        task = (await checkout(ca, compra_id, contexto, "cart_1")).json()["result"]
    assert not checkouts(task)  # no se sabe qué quedó: no llega a la firma
    assert veredictos(task)[0]["status"] == "bloqueado"


def test_fusionar_filas_cambio_por_cambio():
    from proxy.compras import fusionar_filas

    def fila(estado: str, **datos: Any) -> dict[str, Any]:
        datos = {"veredictos": [], "contexto": None, "aprobacion_pendiente": None, **datos}
        return {"id": "cmp-x", "estado": estado, "espera_id": None, "referencia": None,
                "actualizada": "t", "creada": "t", "datos": datos}

    base = fila("negociando")
    # Solo cambió este pedido: entra.
    assert fusionar_filas(base, fila("sin_oportunidad"), base)["estado"] == "sin_oportunidad"
    # Solo cambió la base: queda la de la base.
    suya = fila("aceptada", contexto={"x": 1})
    assert fusionar_filas(base, base, suya) == suya
    # Los dos cambiaron el estado distinto: ya no aplica.
    assert fusionar_filas(base, fila("sin_oportunidad"), suya) is None
    # Los veredictos se juntan.
    nuestra = fila("negociando", veredictos=[{"status": "aprobado"}])
    suya = fila("negociando", veredictos=[{"status": "escalar"}])
    assert fusionar_filas(base, nuestra, suya)["datos"]["veredictos"] == [
        {"status": "escalar"}, {"status": "aprobado"},
    ]


# -- la instancia que retiene la espera también la muestra y la contesta -------------------
#
# Prueba de punta a punta con varias instancias: con una sola viva, todo va a
# la que retiene la búsqueda. Sus GET leen la compra mientras la búsqueda la
# trabaja; un GET antes de abrir la espera dejaba en memoria otra copia, y
# después la instancia mostraba esa (sin la pendiente) y no la dejaba contestar.


async def test_la_que_retiene_muestra_la_oportunidad_y_la_decide(config_varias):
    base, merchant = BaseFalsa(), FakeMerchant()
    a = _instancia(config_varias, base, merchant, agente=AgenteFalso())
    async with cliente_publico(a.app) as ca:
        compra_id = await _compra_computadoras(ca, porque=RESPUESTA_USUARIO)
        busqueda = asyncio.create_task(buscar(ca, compra_id, escenario="computadoras"))
        vista = await _compra_en(ca, compra_id, "esperando_decision")  # GET en A, desde el principio
        pendiente = vista["pendiente"]
        assert pendiente["tipo"] == "oportunidad" and pendiente["vence"]
        assert pendiente["oportunidad"]["cart_id"] == "cart_2"
        listado = (await ca.get("/v1/compras")).json()["compras"]
        assert [c["pendiente"]["tipo"] for c in listado] == ["oportunidad"]

        resp = await ca.post(
            f"/v1/compras/{compra_id}/decision",
            json={"oportunidad_id": pendiente["oportunidad"]["id"], "decision": "aceptar"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["estado"] == "aceptada"
        task = (await busqueda).json()["result"]
        assert any(GUARD_PARAMETROS_KEY in d for d in _datos(task))
        assert (await ca.get(f"/v1/compras/{compra_id}")).json()["estado"] == "aceptada"


async def test_la_que_retiene_muestra_el_porque_y_lo_contesta(config_varias):
    base, merchant = BaseFalsa(), FakeMerchant()
    agente = AgenteFalso(cart_id=None)
    a = _instancia(config_varias, base, merchant, agente=agente)
    async with cliente_publico(a.app) as ca:
        compra_id = await _compra_computadoras(ca, preguntar=True)
        busqueda = asyncio.create_task(buscar(ca, compra_id, escenario="computadoras"))
        vista = await _compra_en(ca, compra_id, "esperando_contexto")
        assert vista["pendiente"]["tipo"] == "porque" and vista["pendiente"]["vence"]
        assert vista["pendiente"]["pregunta"] == "¿Para qué necesitás las 20 computadoras?"
        listado = (await ca.get("/v1/compras")).json()["compras"]
        assert [c["pendiente"]["tipo"] for c in listado] == ["porque"]
        resp = await ca.post(
            f"/v1/compras/{compra_id}/respuesta", json={"respuesta": RESPUESTA_USUARIO}
        )
        assert resp.status_code == 200, resp.text
        assert (await busqueda).json()["result"]["status"]["state"] == "completed"
        assert agente.respuestas == [RESPUESTA_USUARIO]


async def test_la_que_retiene_aprueba_con_la_compra_al_dia(config_varias):
    """Las aprobaciones también: un GET en la misma instancia en el medio no
    deja una copia vieja que tape el `escalar` retenido."""
    base, merchant = BaseFalsa(), FakeMerchant()
    a = _instancia(config_varias, base, merchant, evaluador=evaluador_espia(status="escalar")[0])
    async with cliente_publico(a.app) as ca:
        compra_id = (await crear_compra(ca))["compra_id"]
        contexto = (await buscar(ca, compra_id)).json()["result"]["contextId"]
        assert (await ca.get(f"/v1/compras/{compra_id}")).status_code == 200
        await checkout(ca, compra_id, contexto, "cart_1")
        vista = (await ca.get(f"/v1/compras/{compra_id}")).json()
        assert vista["pendiente"]["tipo"] == "aprobacion"
        resp = await ca.post(f"/v1/compras/{compra_id}/aprobaciones", json={"cart_id": "cart_1"})
        assert resp.status_code == 201, resp.text
        assert checkouts((await checkout(ca, compra_id, contexto, "cart_1")).json()["result"])
