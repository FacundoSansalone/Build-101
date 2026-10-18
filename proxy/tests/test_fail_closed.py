"""Correcciones 5 y 11: lo que no se puede leer no llega a la firma.

Respuestas `message` con checkout, JSON inválido o con NaN, tipos inesperados,
artifact-update dentro de una respuesta JSON, respuestas demasiado grandes,
errores de red, timeouts y streams cortados: si era un checkout, se bloquea
con `error` + `bloqueado`.
"""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from proxy.tests.ayudantes import (
    RPC,
    buscar,
    checkout,
    checkouts,
    cliente_de,
    eventos,
    evaluador_espia,
    solo_en_checkout,
    tipos,
    veredictos,
)
from proxy.tests.fake_world import (
    CATALOG_HOST,
    HEADERS_A2A,
    MERCHANT_HOST,
    FakeMerchant,
    RuteoPorHost,
    app_catalogo,
    cuerpo,
    parsear_sse,
    pedido_find_products,
)

pytestmark = pytest.mark.anyio


def _assert_bloqueo_sintetico(resp: httpx.Response, merchant: FakeMerchant, evs: list[dict]):
    assert resp.status_code == 200
    task = resp.json()["result"]
    assert task["kind"] == "task"
    assert task["status"]["state"] == "failed"
    assert checkouts(task) == []
    if merchant.ultimo_checkout:
        assert merchant.ultimo_checkout["checkout_jwt"].encode() not in resp.content
    (v,) = veredictos(task)
    assert v["status"] == "bloqueado"
    assert task["status"]["message"]["parts"][0]["text"].startswith("KaiAI: bloqueado.")
    assert "error" in tipos(evs)
    assert tipos(evs)[-1] == "bloqueado"
    from a2a.types import SendMessageResponse

    SendMessageResponse.model_validate(resp.json())


# ---------------------------------------------------------------------------
# 5. Respuestas que no son una tarea legible
# ---------------------------------------------------------------------------


async def test_respuesta_message_con_checkout_bloquea(cliente, merchant: FakeMerchant):
    def como_message(respuesta, pedido):
        task = respuesta["result"]
        parts = [p for a in task["artifacts"] for p in a["parts"]]
        respuesta["result"] = {
            "kind": "message", "messageId": "m-1", "role": "agent",
            "contextId": task["contextId"], "parts": parts,
        }
        return respuesta

    merchant.manipular = solo_en_checkout(como_message)
    contexto = await buscar(cliente, "B")
    resp = await checkout(cliente, contexto, "cart_1")
    _assert_bloqueo_sintetico(resp, merchant, await eventos(cliente))


@pytest.mark.parametrize(
    "romper",
    [
        lambda raw: raw[:-20],                                   # JSON truncado
        lambda raw: raw.replace(b'"amount": 120.0', b'"amount": NaN', 1),
        lambda raw: raw.replace(b'"amount": 120.0', b'"amount": Infinity', 1),
    ],
    ids=["truncado", "nan", "infinity"],
)
async def test_json_invalido_en_un_checkout_bloquea(cliente, merchant: FakeMerchant, romper):
    merchant.manipular = solo_en_checkout(
        lambda respuesta, pedido: romper(json.dumps(respuesta).encode())
    )
    contexto = await buscar(cliente, "B")
    resp = await checkout(cliente, contexto, "cart_1")
    _assert_bloqueo_sintetico(resp, merchant, await eventos(cliente))


@pytest.mark.parametrize(
    "result",
    [["lista"], "texto", None, {"kind": "otra-cosa"}],
    ids=["lista", "string", "null", "kind-desconocido"],
)
async def test_tipo_inesperado_en_un_checkout_bloquea(cliente, merchant: FakeMerchant, result):
    def cambiar(respuesta, pedido):
        respuesta["result"] = result
        return respuesta

    merchant.manipular = solo_en_checkout(cambiar)
    contexto = await buscar(cliente, "B")
    resp = await checkout(cliente, contexto, "cart_1")
    _assert_bloqueo_sintetico(resp, merchant, await eventos(cliente))


async def test_artifact_update_en_una_respuesta_json_bloquea(cliente, merchant: FakeMerchant):
    def como_update(respuesta, pedido):
        task = respuesta["result"]
        respuesta["result"] = {
            "kind": "artifact-update", "taskId": task["id"], "contextId": task["contextId"],
            "artifact": task["artifacts"][0],
        }
        return respuesta

    merchant.manipular = solo_en_checkout(como_update)
    contexto = await buscar(cliente, "B")
    resp = await checkout(cliente, contexto, "cart_1")
    _assert_bloqueo_sintetico(resp, merchant, await eventos(cliente))


async def test_checkout_en_la_respuesta_de_una_busqueda_bloquea(cliente, merchant: FakeMerchant):
    """Un ap2.checkout fuera de create_checkout no se presenta para firma."""
    from proxy.tests.fake_world import cargar_fixture

    cart = cargar_fixture("B")["carts"][0]

    def con_checkout(respuesta, pedido):
        partes = pedido["params"]["message"]["parts"]
        if any("catalog_search" in (p.get("data") or {}) for p in partes):
            respuesta["result"]["artifacts"].append(
                {"artifactId": "x", "parts": [{"kind": "data", "data": {
                    "ap2.checkout": merchant.armar_checkout(cart)}}]}
            )
        return respuesta

    merchant.manipular = con_checkout
    resp = await cliente.post(
        RPC, content=cuerpo(pedido_find_products("B")), headers=HEADERS_A2A
    )
    task = resp.json()["result"]
    assert checkouts(task) == []
    assert task["status"]["state"] == "failed"


async def test_nan_en_una_busqueda_da_error_y_no_registra_ofertas(cliente, merchant):
    merchant.manipular = lambda respuesta, pedido: json.dumps(respuesta).encode().replace(
        b'"amount": 120.0', b'"amount": NaN', 1
    )
    resp = await cliente.post(
        RPC, content=cuerpo(pedido_find_products("B")), headers=HEADERS_A2A
    )
    evs = await eventos(cliente)

    assert resp.status_code == 200  # no es un checkout: pasa como vino
    assert "error" in tipos(evs)
    assert "ofertas_recibidas" not in tipos(evs)


# ---------------------------------------------------------------------------
# 11. Límites, red, timeouts y streams cortados
# ---------------------------------------------------------------------------


async def test_respuesta_demasiado_grande_en_un_checkout_bloquea(armar_proxy, config, merchant):
    def inflar(respuesta, pedido):
        respuesta["result"]["metadata"] = {"relleno": "x" * 4000}
        return respuesta

    merchant.manipular = solo_en_checkout(inflar)
    app = armar_proxy()
    async with cliente_de(app) as c:
        contexto = await buscar(c, "B")
        # El límite chico recién después de la búsqueda (que es más grande).
        app.state.proxy.config = replace(config, max_respuesta_bytes=4000)
        resp = await checkout(c, contexto, "cart_1")
        evs = await eventos(c)
    _assert_bloqueo_sintetico(resp, merchant, evs)


async def test_respuesta_demasiado_grande_que_no_es_checkout_da_502(armar_proxy, config, merchant):
    app = armar_proxy(config=replace(config, max_respuesta_bytes=500))
    async with cliente_de(app) as c:
        pedido = pedido_find_products("B")
        resp = await c.post(RPC, content=cuerpo(pedido), headers=HEADERS_A2A)
        evs = await eventos(c)

    assert resp.status_code == 502
    assert resp.json()["id"] == pedido["id"] and "error" in resp.json()
    assert tipos(evs)[-1] == "error"


async def test_sse_demasiado_grande_en_un_checkout_bloquea(armar_proxy, config, merchant):
    app = armar_proxy()
    async with cliente_de(app) as c:
        contexto = await buscar(c, "B")
        app.state.proxy.config = replace(config, max_respuesta_bytes=1500)
        resp = await checkout(c, contexto, "cart_1", metodo="message/stream")

    assert merchant.ultimo_checkout["checkout_jwt"].encode() not in resp.content
    resultados = [e["result"] for e in parsear_sse(resp.content)]
    assert resultados[-1]["status"]["state"] == "failed"


class _FallaEnCheckout(httpx.AsyncBaseTransport):
    """Comercio que responde las búsquedas y falla en create_checkout."""

    def __init__(self, merchant: FakeMerchant, falla: str) -> None:
        self.mundo = RuteoPorHost({MERCHANT_HOST: merchant.app, CATALOG_HOST: app_catalogo()})
        self.falla = falla

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        es_checkout = request.method == "POST" and b'"cart_id"' in request.content
        if not es_checkout:
            return await self.mundo.handle_async_request(request)
        if self.falla == "conexion":
            raise httpx.ConnectError("comercio caído", request=request)
        if self.falla == "timeout":
            raise httpx.ReadTimeout("el comercio no contesta", request=request)
        respuesta = await self.mundo.handle_async_request(request)
        contenido = await respuesta.aread()
        tipo = respuesta.headers["content-type"]

        class _Cortado(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield contenido[: len(contenido) // 2]
                raise httpx.ReadError("stream cortado", request=request)

        return httpx.Response(200, headers={"content-type": tipo}, stream=_Cortado())


@pytest.mark.parametrize("falla", ["conexion", "timeout", "cortado"])
@pytest.mark.parametrize("metodo", ["message/send", "message/stream"])
async def test_red_timeout_y_stream_cortado_en_un_checkout_bloquean(
    armar_proxy, merchant, falla, metodo
):
    app = armar_proxy(transport=_FallaEnCheckout(merchant, falla))
    async with cliente_de(app) as c:
        contexto = await buscar(c, "B")
        resp = await checkout(c, contexto, "cart_1", metodo=metodo)
        evs = await eventos(c)

    if merchant.ultimo_checkout:
        assert merchant.ultimo_checkout["checkout_jwt"].encode() not in resp.content
    if resp.headers["content-type"].startswith("text/event-stream"):
        final = [e["result"] for e in parsear_sse(resp.content)][-1]
        assert final["status"]["state"] == "failed"
    else:
        assert resp.status_code == 200
        assert resp.json()["result"]["status"]["state"] == "failed"
    assert "error" in tipos(evs) and tipos(evs)[-1] == "bloqueado"


# ---------------------------------------------------------------------------
# 11. Evento veredicto con la fuente del catálogo y si el recibo quedó firmado
# ---------------------------------------------------------------------------


async def test_veredicto_registra_fuente_http_y_recibo_firmado(cliente):
    contexto = await buscar(cliente, "B")
    await checkout(cliente, contexto, "cart_1")
    (veredicto,) = [e for e in await eventos(cliente) if e["tipo"] == "veredicto"]

    assert veredicto["datos"]["fuente"] == "http"
    assert isinstance(veredicto["datos"]["recibo_firmado"], bool)
    firma = veredicto["datos"]["veredicto"]["recibo"]["firma"]
    assert veredicto["datos"]["recibo_firmado"] is bool(firma)


async def _veredicto_sin_catalogo(armar_proxy, merchant, config, **config_extra):
    import dataclasses

    evaluador, _ = evaluador_espia("aprobado")  # veredicto sin recibo
    app = armar_proxy(
        transport=RuteoPorHost({MERCHANT_HOST: merchant.app}),  # :8011 no responde
        evaluador=evaluador,
        config=dataclasses.replace(config, **config_extra),
    )
    async with cliente_de(app) as c:
        contexto = await buscar(c, "B")
        await checkout(c, contexto, "cart_1")
        (veredicto,) = [e for e in await eventos(c) if e["tipo"] == "veredicto"]
    return veredicto


async def test_veredicto_registra_fuente_ninguno_si_el_catalogo_no_responde(
    armar_proxy, merchant, config
):
    veredicto = await _veredicto_sin_catalogo(armar_proxy, merchant, config)

    assert veredicto["datos"]["fuente"] == "ninguno"
    assert veredicto["datos"]["comercio_id"] == "merchant_1"  # del checkout firmado
    assert veredicto["datos"]["recibo_firmado"] is False  # sin recibo no hay firma


async def test_veredicto_registra_fuente_fixture_solo_con_respaldo_explicito(
    armar_proxy, merchant, config
):
    veredicto = await _veredicto_sin_catalogo(
        armar_proxy, merchant, config, catalogo_respaldo_fixture=True
    )
    assert veredicto["datos"]["fuente"] == "fixture"


async def test_veredicto_avisa_si_el_catalogo_es_de_otro_comercio(armar_proxy, merchant):
    from proxy.tests.fake_world import CATALOG_HOST, app_catalogo

    evaluador, _ = evaluador_espia("aprobado")
    app = armar_proxy(
        transport=RuteoPorHost({
            MERCHANT_HOST: merchant.app,
            CATALOG_HOST: app_catalogo(merchant_id="otro_comercio"),
        }),
        evaluador=evaluador,
    )
    async with cliente_de(app) as c:
        contexto = await buscar(c, "B")
        await checkout(c, contexto, "cart_1")
        (veredicto,) = [e for e in await eventos(c) if e["tipo"] == "veredicto"]

    assert veredicto["datos"]["fuente"] == "ninguno"
    assert "otro_comercio" in veredicto["datos"]["catalogo_descartado"]


def test_la_clave_del_guard_se_crea_al_arrancar(armar_proxy, config):
    pytest.importorskip("guard.recibo")
    assert not config.guard_key_path.exists()
    armar_proxy()
    assert config.guard_key_path.exists()


async def test_tope_de_suscriptores_sse(proxy_app, cliente):
    bus = proxy_app.state.proxy.bus
    colas = [bus.suscribir() for _ in range(20)]
    assert all(c is not None for c in colas)
    resp = await cliente.get("/guard/events")
    assert resp.status_code == 503
