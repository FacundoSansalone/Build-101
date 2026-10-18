"""El pedido con las palabras del usuario (/demo): el dashboard registra lo
que escribió el usuario y el intake de la próxima compra lo lee además de la
paráfrasis del agente de compras.

Visto en la punta a punta (2026-10-18, computadoras): el usuario escribió
"Comprame 20 computadoras para el lunes, hasta 20000 USD." y el agente le
pasó al comercio "20 computadoras por un total de 20000 USD": el intake
repreguntaba el plazo. Garantías que se prueban:

- solo entra por la API de control local (Host, JSON y `X-Guard-Token`);
- tope de largo, vencimiento y un solo uso (la próxima compra nueva);
- sin registro, todo como antes;
- no autoriza nada por sí: los umbrales y la confirmación son los de siempre;
- el tráfico de integradores no lo usa.

Datos sintéticos de test; el modelo se finge con un cliente de decisiones.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import httpx
import pytest

from decisiones import Rechazo, RespuestaEleccion
from intencion.agente_decisiones import AgenteDecisiones
from proxy.pedido_usuario import LARGO_MAXIMO, PedidoDelUsuario, pedido_para_intake
from proxy.seguridad import HEADER_TOKEN
from proxy.tests.ayudantes import RPC, cliente_de
from proxy.tests.fake_world import HEADERS_A2A, cuerpo, pedido_find_products

pytestmark = pytest.mark.anyio

DEL_USUARIO = "Comprame 20 notebooks para el lunes, hasta 20000 USD."
PARAFRASIS = "20 notebooks por un total de 20000 USD"
RUTA = "/guard/compras/pedido"


# -- el registro (sin red) ------------------------------------------------------------


class Reloj:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_se_usa_una_sola_vez():
    pedido = PedidoDelUsuario()
    pedido.registrar("  Comprame   20 notebooks\n para el lunes ")
    assert pedido.pendiente
    assert pedido.tomar() == "Comprame 20 notebooks para el lunes"
    assert pedido.tomar() is None and not pedido.pendiente


def test_vence():
    reloj = Reloj()
    pedido = PedidoDelUsuario(vigencia_s=60, reloj=reloj)
    pedido.registrar(DEL_USUARIO)
    reloj.t += 59
    assert pedido.pendiente
    reloj.t += 2
    assert not pedido.pendiente and pedido.tomar() is None


def test_uno_nuevo_reemplaza_al_anterior():
    pedido = PedidoDelUsuario()
    pedido.registrar("una notebook")
    pedido.registrar("unos auriculares")
    assert pedido.tomar() == "unos auriculares"


@pytest.mark.parametrize("malo", ["", "   ", "x" * (LARGO_MAXIMO + 1), None, 3])
def test_vacio_largo_o_no_texto_no_se_registra(malo):
    pedido = PedidoDelUsuario()
    with pytest.raises(ValueError):
        pedido.registrar(malo)
    assert not pedido.pendiente


def test_sin_caracteres_de_control():
    pedido = PedidoDelUsuario()
    pedido.registrar("20 notebooks\x00\x1b[31m para el lunes")
    assert pedido.tomar() == "20 notebooks[31m para el lunes"


def test_pedido_para_intake():
    # Sin palabras del usuario: la búsqueda del agente, como siempre.
    assert pedido_para_intake(PARAFRASIS, None) == PARAFRASIS
    assert pedido_para_intake(PARAFRASIS, "  ") == PARAFRASIS
    # Con las dos: primero el usuario (el idioma sale de ahí), después el agente.
    assert pedido_para_intake(PARAFRASIS, DEL_USUARIO) == f"{DEL_USUARIO} / {PARAFRASIS}"
    # Si la búsqueda no agrega nada, solo las palabras del usuario.
    assert pedido_para_intake("20 Notebooks", "Comprame 20 notebooks") == "Comprame 20 notebooks"
    assert pedido_para_intake("", DEL_USUARIO) == DEL_USUARIO


# -- la API de control -----------------------------------------------------------------


async def test_registrar_por_la_api_de_control(armar_proxy):
    app = armar_proxy(agente=AgenteDecisiones(Modelo()))
    async with cliente_de(app) as c:
        resp = await c.post(RUTA, json={"texto": DEL_USUARIO})
    assert resp.status_code == 200, resp.text
    datos = resp.json()
    assert set(datos) == {"registrado", "vence"} and datos["registrado"] is True  # sin el texto
    assert app.state.proxy.pedido_usuario.pendiente


async def test_sin_intermediario_o_sin_agente_no_se_guarda(armar_proxy, config):
    """Ninguna compra lo usaría: no queda esperando a otra que llegue después."""
    apagado = armar_proxy(config=replace(config, guard_activo=False), agente=AgenteDecisiones(Modelo()))
    sin_agente = armar_proxy()
    for app, motivo in ((apagado, "intermediario_apagado"), (sin_agente, "sin_agente_de_intencion")):
        async with cliente_de(app) as c:
            resp = await c.post(RUTA, json={"texto": DEL_USUARIO})
        assert resp.status_code == 200 and resp.json() == {"registrado": False, "motivo": motivo}
        assert not app.state.proxy.pedido_usuario.pendiente


async def test_sin_token_sin_json_o_desde_otro_host_no_se_registra(proxy_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy_app),
                                 base_url="http://localhost:8010") as c:
        assert (await c.post(RUTA, json={"texto": DEL_USUARIO})).status_code == 403
        token = {HEADER_TOKEN: proxy_app.state.proxy.api_token}
        resp = await c.post(RUTA, content=f'{{"texto": "{DEL_USUARIO}"}}',
                            headers={**token, "content-type": "text/plain"})
        assert resp.status_code == 415
        resp = await c.post(RUTA, json={"texto": DEL_USUARIO}, headers={**token, "host": "evil.test"})
        assert resp.status_code == 403
        resp = await c.post(RUTA, json={"texto": DEL_USUARIO},
                            headers={**token, "origin": "https://evil.test"})
        assert resp.status_code == 403
    assert not proxy_app.state.proxy.pedido_usuario.pendiente


@pytest.mark.parametrize("body", [
    {}, {"texto": ""}, {"texto": "   "}, {"texto": 1}, {"texto": "x" * (LARGO_MAXIMO + 1)},
    {"texto": "ok", "otra": 1}, ["texto"], {"texto": "\x00\x01"},
])
async def test_formato_invalido_da_400(armar_proxy, body):
    app = armar_proxy(agente=AgenteDecisiones(Modelo()))
    async with cliente_de(app) as c:
        resp = await c.post(RUTA, json=body)
    assert resp.status_code == 400
    assert resp.json()["error"] == "se_espera"
    assert not app.state.proxy.pedido_usuario.pendiente


# -- el intake de la próxima compra ------------------------------------------------------


def _e(opcion: str, p: float) -> RespuestaEleccion:
    return RespuestaEleccion("x", opcion, {opcion: p}, p)


def _opcion(qs, nombre: str, contiene: str) -> str | None:
    q = next((q for q in qs if q.nombre == nombre), None)
    if q is None:
        return None
    return next((k for k, d in q.opciones.items() if contiene in (d or "")), None)


class Modelo:
    """Elige lo que el texto dice; guarda el pedido que ve en cada turno."""

    nombre = "falso"
    modelo = "falso-1"

    def __init__(self) -> None:
        self.pedidos: list[str] = []

    def decidir(self, estado: dict[str, Any], qs: list) -> dict[str, Any]:
        if "producto" in estado:  # la taxonomía
            (q,) = qs
            return {q.nombre: _e(next(iter(q.opciones)), 0.1)}
        self.pedidos.append(estado["pedido"])
        texto = " ".join([estado["pedido"], *(t["respuesta"] for t in estado["conversacion"])]).lower()
        categoria = next(q for q in qs if q.nombre == "categoria")
        r: dict[str, Any] = {
            # La primera categoría del catálogo de test (o "dicha" sin catálogo).
            "categoria": _e(next(k for k in categoria.opciones if k not in ("fuera_de_catalogo", "no_dicha")), 0.95),
            "cantidad": _e(_opcion(qs, "cantidad", "20") or "no_dicha", 0.95),
            "precio": _e(_opcion(qs, "precio", "20000") or "no_dicho", 0.95),
            "precio_alcance": _e("total", 0.95),
            "moneda": _e("USD", 0.95),
            "plazo": _e("no_dicho", 0.9),
        }
        if "lunes" in texto:
            r["plazo"] = _e(_opcion(qs, "plazo", "lunes") or "no_dicho", 0.95)
            r["plazo_firmeza"] = _e("exige", 0.95)
        return {q.nombre: r.get(q.nombre, Rechazo(q.nombre)) for q in qs}


@pytest.fixture
def config_rapida(config):
    return replace(config, espera_contexto_s=5.0, espera_decision_s=5.0)


def _buscar(c: httpx.AsyncClient, texto: str = PARAFRASIS, contexto: str | None = None) -> asyncio.Task:
    rpc = pedido_find_products("B")
    if contexto:
        rpc["params"]["message"]["contextId"] = contexto
    for parte in rpc["params"]["message"]["parts"]:
        if "catalog_search" in (parte.get("data") or {}):
            parte["data"]["catalog_search"] = texto
    return asyncio.create_task(c.post(RPC, content=cuerpo(rpc), headers=HEADERS_A2A))


async def _pregunta(c: httpx.AsyncClient, excluir: set[str] = frozenset()) -> dict[str, Any]:
    for _ in range(400):
        for compra in (await c.get("/guard/compras")).json()["compras"]:
            if compra["estado"] == "esperando_contexto" and compra["compra_id"] not in excluir:
                return compra
        await asyncio.sleep(0.01)
    raise AssertionError("ninguna compra preguntó")


async def _omitir(c: httpx.AsyncClient, compra: dict[str, Any]) -> None:
    await c.post("/guard/compras/contexto", json={"compra_id": compra["compra_id"], "omitir": True})


async def test_el_intake_lee_las_palabras_del_usuario_y_no_repregunta_lo_dicho(armar_proxy, config_rapida):
    modelo = Modelo()
    app = armar_proxy(config=config_rapida, agente=AgenteDecisiones(modelo))
    async with cliente_de(app) as c:
        assert (await c.post(RUTA, json={"texto": DEL_USUARIO})).status_code == 200
        busqueda = _buscar(c)
        compra = await _pregunta(c)
        # El modelo y la compra ven lo que escribió el usuario y la búsqueda del agente.
        assert modelo.pedidos[0] == f"{DEL_USUARIO} / {PARAFRASIS}"
        assert compra["pedido"] == f"{DEL_USUARIO} / {PARAFRASIS}"
        # "para el lunes" y "hasta" estaban: la primera pregunta es solo el porqué.
        assert compra["intake"]["faltan"] == []
        assert "para cuándo" not in compra["pregunta"]
        await _omitir(c, compra)
        await busqueda
        # Un solo uso: ya no está.
        assert not app.state.proxy.pedido_usuario.pendiente


async def test_se_usa_en_una_sola_compra(armar_proxy, config_rapida):
    modelo = Modelo()
    app = armar_proxy(config=config_rapida, agente=AgenteDecisiones(modelo))
    async with cliente_de(app) as c:
        await c.post(RUTA, json={"texto": DEL_USUARIO})
        primera = _buscar(c, contexto="ctx-uno")
        compra = await _pregunta(c)
        await _omitir(c, compra)
        await primera
        segunda = _buscar(c, contexto="ctx-dos")
        otra = await _pregunta(c, {compra["compra_id"]})
        assert otra["pedido"] == PARAFRASIS  # la siguiente compra, como siempre
        await _omitir(c, otra)
        await segunda


async def test_sin_registro_o_vencido_todo_como_antes(armar_proxy, config_rapida, monkeypatch):
    modelo = Modelo()
    app = armar_proxy(config=config_rapida, agente=AgenteDecisiones(modelo))
    reloj = Reloj()
    app.state.proxy.pedido_usuario = PedidoDelUsuario(vigencia_s=60, reloj=reloj)
    async with cliente_de(app) as c:
        await c.post(RUTA, json={"texto": DEL_USUARIO})
        reloj.t += 61
        busqueda = _buscar(c)
        compra = await _pregunta(c)
        assert compra["pedido"] == PARAFRASIS
        assert modelo.pedidos == [PARAFRASIS]
        assert "plazo" in compra["intake"]["faltan"]  # como hoy: lo repregunta
        await _omitir(c, compra)
        await busqueda


async def test_no_autoriza_nada_sin_la_confirmacion(armar_proxy, config_rapida):
    """Lo que no quedó claro sigue a confirmar: sin confirmar la ficha, no va."""

    class ModeloSimbolo(Modelo):
        def decidir(self, estado, qs):
            r = super().decidir(estado, qs)
            if "moneda" in r:
                r["moneda"] = _e("solo_simbolo", 0.9)  # "$" suelto: se infiere USD
            return r

    app = armar_proxy(config=config_rapida, agente=AgenteDecisiones(ModeloSimbolo()))
    async with cliente_de(app) as c:
        await c.post(RUTA, json={"texto": "Comprame 20 notebooks para el lunes, hasta $20000."})
        busqueda = _buscar(c, "20 notebooks")
        compra = await _pregunta(c)
        assert "moneda" in compra["intake"]["a_confirmar"]
        await _omitir(c, compra)
        await busqueda
        eventos = (await c.get("/guard/events/history")).json()
        final = [e for e in eventos if e["tipo"] == "contexto_registrado"][-1]["datos"]
        campos = {r["campo"] for r in final["ficha_autorizada"]}
        assert "moneda" not in campos and "precio" in campos


async def test_el_trafico_de_integrador_no_lo_usa(config):
    from proxy.tests.ayudantes import evaluador_espia
    from proxy.tests.gateway_ayudantes import (
        GATEWAY_PUBLIC_URL,
        armar_gateway,
        buscar,
        cliente_local,
        cliente_publico,
        crear_compra,
    )
    from proxy.tests.test_etapa2 import AgenteFalso

    agente = AgenteFalso(cart_id=None)
    mundo = armar_gateway(replace(config, gateway_public_url=GATEWAY_PUBLIC_URL), evaluador_espia()[0],
                          agente=agente)
    async with cliente_local(mundo.app) as local:
        assert (await local.post(RUTA, json={"texto": DEL_USUARIO})).status_code == 200
    async with cliente_publico(mundo.app) as c:
        compra_id = (await crear_compra(c, intencion={"preguntar": False}))["compra_id"]
        assert (await buscar(c, compra_id)).status_code == 200
    assert mundo.proxy.pedido_usuario.pendiente  # sigue esperando a la compra de la demo
    assert all(DEL_USUARIO not in p for p in agente.preguntas)
    assert DEL_USUARIO not in mundo.proxy.compras.get(compra_id).pedido
