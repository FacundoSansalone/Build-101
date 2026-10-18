"""Intake en el proxy: repreguntar hasta tener todo, y confirmar la ficha.

Con un agente que sabe evaluar el intake (`AgenteDecisiones`), el proxy no
suelta la búsqueda después de la primera respuesta: si falta algo
necesario, vuelve a preguntar (`pedido_detectado` con `turno`, `faltan` y
`ficha_parcial`), dentro del mismo presupuesto de espera. Al final pide
confirmar la ficha: lo inferido solo se autoriza si el usuario confirma.

El modelo se finge con un cliente de decisiones que lee la conversación; el
intake y el agente son los reales.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import httpx
import pytest

from decisiones import DecisionesError, Rechazo, RespuestaEleccion, RespuestaPredicado
from intencion.agente_decisiones import AgenteDecisiones
from intencion.intake import MAX_REPREGUNTAS, PORQUE
from proxy.tests.ayudantes import RPC, cliente_de
from proxy.tests.fake_world import HEADERS_A2A, cuerpo, pedido_find_products

pytestmark = pytest.mark.anyio

PEDIDO = "Comprame unas zapatillas Nike talle 42 negras para jugar al básquet"


def _e(opcion: str, p: float) -> RespuestaEleccion:
    return RespuestaEleccion("x", opcion, {opcion: p}, p)


def _opcion(qs, nombre: str, contiene: str) -> str | None:
    q = next((q for q in qs if q.nombre == nombre), None)
    if q is None:
        return None
    return next((k for k, d in q.opciones.items() if contiene in (d or "")), None)


class Modelo:
    """Contesta según lo que dijo el usuario hasta ese turno."""

    nombre = "falso"
    modelo = "falso-1"

    def __init__(self, falla_en: int | None = None) -> None:
        self.llamadas = 0  # las del intake (una por turno)
        self.taxonomia = 0  # las de taxonomia.clasificar (una por nivel)
        self.falla_en = falla_en

    def decidir(self, estado: dict[str, Any], qs: list) -> dict[str, Any]:
        if "producto" in estado:  # la taxonomía: Apparel & Accessories > Shoes
            self.taxonomia += 1
            (q,) = qs
            opcion = next((i for i in ("166", "187") if i in q.opciones), "ninguna")
            return {q.nombre: _e(opcion, 0.95)}
        self.llamadas += 1
        if self.falla_en is not None and self.llamadas >= self.falla_en:
            raise DecisionesError("caído")
        texto = " ".join(
            [estado["pedido"], *(t["respuesta"] for t in estado["conversacion"])]
        ).lower()
        r: dict[str, Any] = {
            "categoria": _e("basketball_shoes", 0.95),
            "cantidad": _e("una", 0.9),
            "precio": _e("no_dicho", 0.9),
            "moneda": _e("no_dicha", 0.9),
            "plazo": _e("no_dicho", 0.9),
        }
        if "150" in texto:
            r["precio"] = _e(_opcion(qs, "precio", "150") or "no_dicho", 0.95)
            r["precio_alcance"] = _e("total", 0.9)
            r["moneda"] = _e("USD", 0.95)
        if "viernes" in texto:
            r["plazo"] = _e(_opcion(qs, "plazo", "viernes") or "no_dicho", 0.9)
            r["plazo_firmeza"] = _e("exige", 0.9)
        for q in qs:
            if q.nombre.startswith("pide_"):
                r[q.nombre] = RespuestaPredicado(q.nombre, 0.95)
            elif q.nombre.startswith("firmeza_"):
                r[q.nombre] = _e("exige", 0.9)
        return {q.nombre: r.get(q.nombre, Rechazo(q.nombre)) for q in qs}


@pytest.fixture
def config_rapida(config):
    return replace(config, espera_contexto_s=5.0, espera_decision_s=5.0)


def _buscar(c: httpx.AsyncClient, pedido: str = PEDIDO) -> asyncio.Task:
    rpc = pedido_find_products("B")
    for parte in rpc["params"]["message"]["parts"]:
        if "catalog_search" in (parte.get("data") or {}):
            parte["data"]["catalog_search"] = pedido
    return asyncio.create_task(c.post(RPC, content=cuerpo(rpc), headers=HEADERS_A2A))


async def _turno(c: httpx.AsyncClient, turno: int, intentos: int = 400) -> dict[str, Any]:
    """Espera a que la compra pregunte el turno `turno`."""
    compras: list[dict[str, Any]] = []
    for _ in range(intentos):
        compras = (await c.get("/guard/compras")).json()["compras"]
        for compra in compras:
            intake = compra.get("intake") or {}
            if compra["estado"] == "esperando_contexto" and intake.get("turno") == turno:
                return compra
        await asyncio.sleep(0.01)
    raise AssertionError(f"Ninguna compra llegó al turno {turno}: {compras}")


async def _responder(c: httpx.AsyncClient, compra: dict[str, Any], texto: str) -> None:
    resp = await c.post(
        "/guard/compras/contexto", json={"compra_id": compra["compra_id"], "respuesta": texto}
    )
    assert resp.status_code == 200, resp.text


async def _confirmar(c: httpx.AsyncClient, compra: dict[str, Any]) -> httpx.Response:
    return await c.post(
        "/guard/compras/contexto", json={"compra_id": compra["compra_id"], "confirmar": True}
    )


async def _eventos(c: httpx.AsyncClient, tipo: str) -> list[dict[str, Any]]:
    return [e for e in (await c.get("/guard/events/history")).json() if e["tipo"] == tipo]


def _carts(task: dict[str, Any]) -> list[str]:
    return [
        p["data"]["ap2.cart"]["cart_id"]
        for a in task.get("artifacts") or []
        for p in a.get("parts") or []
        if isinstance(p.get("data"), dict) and "ap2.cart" in p["data"]
    ]


def _ficha_llm(app, compra_id: str) -> dict[str, Any]:
    compra = app.state.proxy.compras.get(compra_id)
    assert compra.ficha_llm is not None
    return {(r.campo, r.atributo): r for r in compra.ficha_llm.requisitos}


# -- el flujo completo -------------------------------------------------------------------


async def test_repregunta_hasta_tener_todo_y_confirma_la_ficha(armar_proxy, config_rapida):
    modelo = Modelo()
    app = armar_proxy(config=config_rapida, agente=AgenteDecisiones(modelo))
    async with cliente_de(app) as c:
        busqueda = _buscar(c)

        primera = await _turno(c, 0)
        assert primera["pregunta"].startswith(PORQUE["es"])
        assert "el tope de precio y la moneda" in primera["pregunta"]
        assert primera["intake"]["faltan"] == ["precio", "moneda", "plazo"]
        await _responder(c, primera, "Juego los sábados en una cancha de cemento.")

        # Contestó el porqué, pero no lo que faltaba: se repregunta.
        segunda = await _turno(c, 1)
        assert segunda["pregunta"] == (
            "Para hacer la compra me falta saber el tope de precio y la moneda "
            "y para cuándo lo necesitás."
        )
        assert not busqueda.done()  # el agente del cliente sigue esperando
        await _responder(c, segunda, "Hasta 150 dólares, y para el viernes.")

        # Ya está todo: pide confirmar la ficha (con lo inferido marcado).
        tercera = await _turno(c, 2)
        assert tercera["intake"]["confirmar"] is True
        assert tercera["intake"]["faltan"] == []
        assert "cantidad 1" in tercera["pregunta"]
        resp = await _confirmar(c, tercera)
        assert resp.status_code == 200, resp.text

        task = (await busqueda).json()["result"]
        assert _carts(task) == ["cart_1", "cart_2", "cart_3", "cart_4"]

        detectados = await _eventos(c, "pedido_detectado")
        assert [e["datos"]["turno"] for e in detectados] == [0, 1, 2]
        assert [e["datos"]["confirmar"] for e in detectados] == [False, False, True]
        assert detectados[0]["datos"]["ficha_parcial"]  # la ficha parcial va en cada turno

        registrados = await _eventos(c, "contexto_registrado")
        assert [e["datos"]["turno"] for e in registrados] == [0, 1, 2]
        assert registrados[0]["datos"]["respuesta"].startswith("Juego los sábados")
        final = registrados[-1]["datos"]
        assert final["final"] is True and final["confirmada"] is True
        autorizada = {r["campo"]: r for r in final["ficha_autorizada"]}
        assert autorizada["precio"]["valor"] == 150.0
        assert autorizada["moneda"]["valor"] == "USD"
        assert autorizada["marca"]["valor"] == "Nike" and autorizada["talle"]["valor"] == 42
        # El color (campo atributo) el Guard ya lo chequea: va a la ficha autorizada.
        assert final["sin_verificar_guard"] == []
        assert [r["atributo"] for r in final["ficha_autorizada"] if r["campo"] == "atributo"] == [
            "color"
        ]
        # El contexto acumula las respuestas.
        assert [t["respuesta"] for t in final["contexto"]["historial"]] == [
            "Hasta 150 dólares, y para el viernes."
        ]

        ficha = _ficha_llm(app, tercera["compra_id"])
        assert ficha[("precio", None)].valor == 150.0
        assert ficha[("moneda", None)].valor == "USD"
        assert ficha[("atributo", "color")].valor == "negro"
        # Una llamada por turno: el pedido y las dos respuestas (la confirmación no llama).
        assert modelo.llamadas == 3
        # Al cerrar, la taxonomía de Google (un nivel por llamada) va en la ficha.
        assert final["categoria_taxonomia"] == "187"
        assert app.state.proxy.compras.get(tercera["compra_id"]).ficha_llm.categoria_taxonomia == "187"
        assert modelo.taxonomia >= 2


async def test_omitir_una_repregunta_sigue_con_lo_dicho(armar_proxy, config_rapida):
    app = armar_proxy(config=config_rapida, agente=AgenteDecisiones(Modelo()))
    async with cliente_de(app) as c:
        busqueda = _buscar(c)
        await _responder(c, await _turno(c, 0), "Para jugar.")
        segunda = await _turno(c, 1)
        await c.post("/guard/compras/contexto", json={"compra_id": segunda["compra_id"], "omitir": True})
        task = (await busqueda).json()["result"]
        assert _carts(task)
        final = (await _eventos(c, "contexto_registrado"))[-1]["datos"]
        assert final["omitido"] is True and final["confirmada"] is False
        campos = {r["campo"] for r in final["ficha_autorizada"]}
        assert "precio" not in campos and "moneda" not in campos
        assert {"categoria", "marca", "talle"} <= campos
        assert final["intake"]["faltan"] == ["precio", "moneda", "plazo"]


async def test_sin_confirmar_lo_inferido_no_se_autoriza(armar_proxy, config_rapida):
    pedido = "Zapatillas Nike talle 42 para básquet, hasta $150, para el viernes"

    class ModeloSimbolo(Modelo):
        def decidir(self, estado, qs):
            r = super().decidir(estado, qs)
            if "moneda" in r:
                r["moneda"] = _e("solo_simbolo", 0.9)  # "$" suelto: se infiere USD
            return r

    app = armar_proxy(config=config_rapida, agente=AgenteDecisiones(ModeloSimbolo()))
    async with cliente_de(app) as c:
        busqueda = _buscar(c, pedido)
        await _responder(c, await _turno(c, 0), "Para jugar los sábados.")
        confirmacion = await _turno(c, 1)
        assert confirmacion["intake"]["confirmar"] is True
        assert "moneda" in confirmacion["intake"]["a_confirmar"]
        await c.post(
            "/guard/compras/contexto", json={"compra_id": confirmacion["compra_id"], "omitir": True}
        )
        await busqueda
        final = (await _eventos(c, "contexto_registrado"))[-1]["datos"]
        campos = {r["campo"] for r in final["ficha_autorizada"]}
        assert "precio" in campos and "moneda" not in campos


async def test_una_correccion_en_la_confirmacion_se_vuelve_a_evaluar(armar_proxy, config_rapida):
    modelo = Modelo()
    app = armar_proxy(config=config_rapida, agente=AgenteDecisiones(modelo))
    async with cliente_de(app) as c:
        busqueda = _buscar(c, PEDIDO + ", hasta 150 dólares para el viernes")
        await _responder(c, await _turno(c, 0), "Para jugar.")
        confirmacion = await _turno(c, 1)
        await _responder(c, confirmacion, "Ojo que es para cancha de cemento.")
        otra = await _turno(c, 2)
        assert otra["intake"]["confirmar"] is True
        assert (await _confirmar(c, otra)).status_code == 200
        await busqueda
        assert modelo.llamadas == 3


async def test_corta_despues_de_las_repreguntas_y_lo_dice(armar_proxy, config_rapida):
    app = armar_proxy(config=config_rapida, agente=AgenteDecisiones(Modelo()))
    async with cliente_de(app) as c:
        busqueda = _buscar(c)
        for turno in range(MAX_REPREGUNTAS + 1):
            await _responder(c, await _turno(c, turno), "No sé, lo que te parezca.")
        ultima = await _turno(c, MAX_REPREGUNTAS + 1)
        assert ultima["intake"]["confirmar"] is True and ultima["intake"]["cortado"] is True
        assert f"Ya pregunté {MAX_REPREGUNTAS} veces" in ultima["pregunta"]
        await _confirmar(c, ultima)
        task = (await busqueda).json()["result"]
        assert _carts(task)
        final = (await _eventos(c, "contexto_registrado"))[-1]["datos"]
        assert final["intake"]["cortado"] is True
        assert "precio" not in {r["campo"] for r in final["ficha_autorizada"]}


async def test_si_vence_una_repregunta_sigue_la_busqueda(armar_proxy, config):
    app = armar_proxy(
        config=replace(config, espera_contexto_s=0.3, espera_decision_s=5.0),
        agente=AgenteDecisiones(Modelo()),
    )
    async with cliente_de(app) as c:
        busqueda = _buscar(c)
        await _responder(c, await _turno(c, 0), "Para jugar.")
        task = (await busqueda).json()["result"]
        assert _carts(task)
        final = (await _eventos(c, "contexto_registrado"))[-1]["datos"]
        assert final["vencido"] is True and final["turno"] == 1


async def test_sin_tiempo_no_repregunta(armar_proxy, config_rapida, monkeypatch):
    import proxy.app as modulo

    monkeypatch.setattr(modulo, "_MINIMO_REPREGUNTA_S", 10_000.0)
    app = armar_proxy(config=config_rapida, agente=AgenteDecisiones(Modelo()))
    async with cliente_de(app) as c:
        busqueda = _buscar(c)
        await _responder(c, await _turno(c, 0), "Para jugar.")
        assert _carts((await busqueda).json()["result"])
        final = (await _eventos(c, "contexto_registrado"))[-1]["datos"]
        assert final["final"] is True and final["sin_tiempo"] is True
        assert len(await _eventos(c, "pedido_detectado")) == 1


async def test_si_el_modelo_falla_en_una_repregunta_sigue_con_lo_que_hay(armar_proxy, config_rapida):
    app = armar_proxy(config=config_rapida, agente=AgenteDecisiones(Modelo(falla_en=2)))
    async with cliente_de(app) as c:
        busqueda = _buscar(c)
        await _responder(c, await _turno(c, 0), "Para jugar.")
        assert _carts((await busqueda).json()["result"])
        assert await _eventos(c, "error")
        final = (await _eventos(c, "contexto_registrado"))[-1]["datos"]
        assert final["final"] is True
        # Sigue con lo que dijo en el pedido (lo del primer turno).
        assert {"categoria", "marca", "talle"} <= {r["campo"] for r in final["ficha_autorizada"]}


# -- API de control ---------------------------------------------------------------------


async def test_confirmar_fuera_de_la_confirmacion_da_409(armar_proxy, config_rapida):
    app = armar_proxy(config=config_rapida, agente=AgenteDecisiones(Modelo()))
    async with cliente_de(app) as c:
        busqueda = _buscar(c)
        primera = await _turno(c, 0)
        resp = await _confirmar(c, primera)
        assert resp.status_code == 409
        assert resp.json() == {"error": "estado", "estado": "esperando_contexto"}
        await c.post("/guard/compras/contexto", json={"compra_id": primera["compra_id"], "omitir": True})
        await busqueda


async def test_confirmar_una_compra_desconocida_da_404(cliente: httpx.AsyncClient):
    resp = await cliente.post("/guard/compras/contexto", json={"compra_id": "cmp-x", "confirmar": True})
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"compra_id": "cmp-1", "confirmar": False},
        {"compra_id": "cmp-1", "confirmar": "true"},
        {"compra_id": "cmp-1", "confirmar": True, "omitir": True},
    ],
)
async def test_confirmar_con_formato_invalido_da_400(cliente: httpx.AsyncClient, body):
    resp = await cliente.post("/guard/compras/contexto", json=body)
    assert resp.status_code == 400


async def test_un_agente_sin_intake_pregunta_una_sola_vez(armar_proxy, config_rapida):
    """Gemini (sin evaluar_intake): el flujo de siempre, con `turno` 0."""
    from proxy.tests.test_etapa2 import AgenteFalso

    app = armar_proxy(config=config_rapida, agente=AgenteFalso(cart_id=None))
    async with cliente_de(app) as c:
        busqueda = _buscar(c)
        primera = await _turno(c, 0)
        await _responder(c, primera, "Para la oficina.")
        assert _carts((await busqueda).json()["result"])
        assert len(await _eventos(c, "pedido_detectado")) == 1
        (registrado,) = await _eventos(c, "contexto_registrado")
        assert registrado["datos"]["turno"] == 0
        assert "final" not in registrado["datos"]
