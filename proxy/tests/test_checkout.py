"""create_checkout: el proxy evalúa y aplica el veredicto sin tocar lo firmado."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from a2a.types import SendMessageResponse, Task

from contracts import GUARD_VEREDICTO_KEY, Veredicto
from proxy.evaluador import EvaluadorCargado
from proxy.tests.fake_world import (
    HEADERS_A2A,
    FakeMerchant,
    cuerpo,
    data_parts_de,
    parsear_sse,
    pedido_create_checkout,
    pedido_find_products,
)

pytestmark = pytest.mark.anyio

RPC = "/a2a/merchant_agent"


async def _buscar(cliente: httpx.AsyncClient, escenario: str = "B") -> str:
    resp = await cliente.post(
        RPC, content=cuerpo(pedido_find_products(escenario)), headers=HEADERS_A2A
    )
    return resp.json()["result"]["contextId"]


async def _checkout(
    cliente: httpx.AsyncClient, contexto: str, cart_id: str
) -> httpx.Response:
    return await cliente.post(
        RPC,
        content=cuerpo(pedido_create_checkout(contexto, cart_id)),
        headers=HEADERS_A2A,
    )


def _checkouts(task: dict[str, Any]) -> list[dict[str, Any]]:
    return [d["ap2.checkout"] for d in data_parts_de(task) if "ap2.checkout" in d]


def _veredictos(task: dict[str, Any]) -> list[dict[str, Any]]:
    return [d[GUARD_VEREDICTO_KEY] for d in data_parts_de(task) if GUARD_VEREDICTO_KEY in d]


async def _eventos(cliente: httpx.AsyncClient) -> list[dict[str, Any]]:
    return (await cliente.get("/guard/events/history")).json()


# ---------------------------------------------------------------------------
# aprobado: checkout intacto + veredicto aparte
# ---------------------------------------------------------------------------


async def test_aprobado_checkout_byte_identico_y_veredicto_aparte(
    cliente: httpx.AsyncClient, merchant: FakeMerchant
):
    contexto = await _buscar(cliente)
    resp = await _checkout(cliente, contexto, "cart_1")

    assert resp.status_code == 200
    original = json.loads(merchant.ultima_respuesta)["result"]
    task = resp.json()["result"]
    # Mismos ids de tarea y contexto, mismo estado.
    assert task["id"] == original["id"]
    assert task["contextId"] == original["contextId"] == contexto
    assert task["status"]["state"] == "completed"
    # El ap2.checkout llega igual, campo por campo.
    assert _checkouts(task) == _checkouts(original) == [merchant.ultimo_checkout]
    checkout = _checkouts(task)[0]
    jwt_original = merchant.ultimo_checkout["checkout_jwt"]
    assert checkout["checkout_jwt"] == jwt_original
    assert checkout["checkout_hash"] == merchant.ultimo_checkout["checkout_hash"]
    # Byte a byte: el JWT y el hash aparecen tal cual en el cuerpo crudo.
    assert jwt_original.encode("ascii") in resp.content
    assert merchant.ultimo_checkout["checkout_hash"].encode("ascii") in resp.content
    # Y la firma del comercio sigue valiendo.
    assert merchant.firma_valida(checkout["checkout_jwt"])
    # Los artefactos originales quedan intactos y en el mismo orden.
    assert task["artifacts"][: len(original["artifacts"])] == original["artifacts"]
    # El veredicto viaja en un artefacto aparte.
    extra = task["artifacts"][len(original["artifacts"]) :]
    assert len(extra) == 1
    # Al agente va la versión reducida (N5): status, motivo, cart y chequeos.
    veredicto = extra[0]["parts"][0]["data"][GUARD_VEREDICTO_KEY]
    assert set(veredicto) == {"status", "motivo", "cart_id_elegido", "chequeos"}
    assert veredicto["status"] == "aprobado"
    assert veredicto["cart_id_elegido"] == "cart_1"
    # El cliente A2A del SDK acepta la respuesta.
    SendMessageResponse.model_validate(resp.json())


async def test_aprobado_eventos(cliente: httpx.AsyncClient, merchant: FakeMerchant):
    contexto = await _buscar(cliente)
    await _checkout(cliente, contexto, "cart_1")

    eventos = await _eventos(cliente)
    tipos = [e["tipo"] for e in eventos][3:]
    assert tipos == ["checkout_pedido", "checkout_recibido", "veredicto", "reenviado"]
    pedido, recibido, veredicto, reenviado = eventos[3:]
    assert pedido["datos"] == {"cart_id": "cart_1"}
    assert pedido["contexto_id"] == contexto
    datos = recibido["datos"]
    assert datos["cart_id"] == "cart_1"
    assert datos["checkout_jwt"] == merchant.ultimo_checkout["checkout_jwt"]
    assert datos["checkout_hash"] == merchant.ultimo_checkout["checkout_hash"]
    assert datos["jwt_header"] == {"alg": "ES256", "typ": "JWT", "kid": "merchant-key-1"}
    assert datos["jwt_payload"]["id"] == "cart_1"
    assert datos["ap2_violaciones"] == []  # 120 USD <= 150: AP2 da OK
    assert recibido["escenario"] == "B"
    Veredicto.model_validate(veredicto["datos"]["veredicto"])
    assert veredicto["datos"]["veredicto"]["status"] == "aprobado"
    assert reenviado["datos"] == {"metodo": "create_checkout"}
    assert all(e["contexto_id"] == contexto for e in eventos[3:])


# ---------------------------------------------------------------------------
# bloqueado: tarea failed con el motivo y sin el checkout
# ---------------------------------------------------------------------------


async def test_bloqueado_tarea_failed_sin_checkout(
    cliente: httpx.AsyncClient, merchant: FakeMerchant
):
    contexto = await _buscar(cliente)
    resp = await _checkout(cliente, contexto, "cart_4")  # el último: el atacante

    assert resp.status_code == 200
    original = json.loads(merchant.ultima_respuesta)["result"]
    task = resp.json()["result"]
    assert task["id"] == original["id"]
    assert task["contextId"] == contexto
    assert task["status"]["state"] == "failed"
    assert _checkouts(task) == []  # no se presenta para firma
    assert merchant.ultimo_checkout["checkout_jwt"].encode() not in resp.content
    veredicto = _veredictos(task)[0]  # versión reducida para el agente (N5)
    assert veredicto["status"] == "bloqueado"
    texto = task["status"]["message"]["parts"][0]["text"]
    assert veredicto["motivo"] in texto
    assert task["status"]["message"]["taskId"] == task["id"]
    assert task["status"]["message"]["contextId"] == contexto
    parsed = SendMessageResponse.model_validate(resp.json())
    assert isinstance(parsed.root.result, Task)


async def test_bloqueado_eventos(cliente: httpx.AsyncClient):
    contexto = await _buscar(cliente)
    await _checkout(cliente, contexto, "cart_4")

    eventos = await _eventos(cliente)
    tipos = [e["tipo"] for e in eventos][3:]
    assert tipos == ["checkout_pedido", "checkout_recibido", "veredicto", "bloqueado"]
    recibido, veredicto, bloqueado = eventos[4:]
    assert recibido["datos"]["ap2_violaciones"] == []  # 149 <= 150: AP2 no lo frena
    assert bloqueado["datos"]["cart_id"] == "cart_4"
    assert bloqueado["datos"]["motivo"] == veredicto["datos"]["veredicto"]["motivo"]


async def test_tasks_get_de_una_tarea_bloqueada_tampoco_trae_el_checkout(
    cliente: httpx.AsyncClient, merchant: FakeMerchant
):
    contexto = await _buscar(cliente)
    bloqueada = (await _checkout(cliente, contexto, "cart_4")).json()["result"]
    eventos_antes = len(await _eventos(cliente))

    pedido = {"id": "g-1", "jsonrpc": "2.0", "method": "tasks/get",
              "params": {"id": bloqueada["id"]}}
    resp = await cliente.post(RPC, content=cuerpo(pedido), headers=HEADERS_A2A)

    # El comercio sí devuelve el checkout guardado; el proxy no lo deja pasar.
    assert _checkouts(json.loads(merchant.ultima_respuesta)["result"])
    task = resp.json()["result"]
    assert task["status"]["state"] == "failed"
    assert _checkouts(task) == []
    nuevos = [e["tipo"] for e in (await _eventos(cliente))[eventos_antes:]]
    assert "veredicto" not in nuevos  # no se vuelve a evaluar
    assert nuevos[-1] == "bloqueado"


async def test_sse_guard_apagado_passthrough(
    cliente: httpx.AsyncClient, merchant: FakeMerchant
):
    await cliente.post("/guard/state", json={"guard_activo": False})
    contexto = await _buscar(cliente)
    resp = await _checkout_stream(cliente, contexto, "cart_4")

    assert parsear_sse(resp.content) == parsear_sse(merchant.ultima_respuesta)
    tipos = [e["tipo"] for e in await _eventos(cliente)]
    assert tipos[-3:] == ["checkout_pedido", "checkout_recibido", "reenviado"]


async def test_escalar_retiene_el_checkout_y_agrega_veredicto(
    armar_proxy, merchant: FakeMerchant
):
    """Decisión del equipo (2026-10-17): escalar retiene hasta que decide un humano."""
    from contracts import Veredicto as V

    def escalar(ficha, ofertas_vistas, cart_id_elegido, catalogo, politica=None,
                checkout_hash=None, ahora=None, checkout=None):
        return V(status="escalar", cart_id_elegido=cart_id_elegido,
                 cart_id_recomendado="cart_1", ranking=["cart_1"], chequeos=[],
                 motivo="precio contradicho")

    app = armar_proxy(evaluador=EvaluadorCargado(evaluar=escalar, nombre="guard"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8010"
    ) as c:
        contexto = await _buscar(c, "C")
        resp = await _checkout(c, contexto, "cart_1")

    task = resp.json()["result"]
    assert task["status"]["state"] == "failed"
    assert _checkouts(task) == []
    assert _veredictos(task)[0]["status"] == "escalar"


# ---------------------------------------------------------------------------
# Entradas de evaluar(): ficha, ofertas, catálogo, checkout_hash
# ---------------------------------------------------------------------------


async def test_evaluar_recibe_las_entradas_del_contrato(
    armar_proxy, merchant: FakeMerchant
):
    from proxy import guard_stub

    llamadas: list[dict[str, Any]] = []

    def espia(ficha, ofertas_vistas, cart_id_elegido, catalogo, politica=None,
              checkout_hash=None, ahora=None, checkout=None):
        llamadas.append(dict(ficha=ficha, ofertas=ofertas_vistas, cart=cart_id_elegido,
                             catalogo=catalogo, politica=politica, hash=checkout_hash))
        return guard_stub.evaluar(ficha, ofertas_vistas, cart_id_elegido, catalogo,
                                  politica, checkout_hash, ahora)

    app = armar_proxy(evaluador=EvaluadorCargado(evaluar=espia, nombre="guard"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8010"
    ) as c:
        contexto = await _buscar(c, "B")
        await _checkout(c, contexto, "cart_2")

    (llamada,) = llamadas
    from proxy.tests.fake_world import cargar_fixture

    fixture = cargar_fixture("B")
    assert llamada["cart"] == "cart_2"
    assert llamada["ofertas"].carts == fixture["carts"]
    assert llamada["ofertas"].offer_details == fixture["offer_details"]
    assert llamada["ofertas"].comercio_id == "merchant_1"
    assert set(llamada["catalogo"]) == {"NK-OUT-01", "NK-IND-02", "VL-OUT-03",
                                        "AX-URB-04", "AX-URB-05"}
    assert llamada["catalogo"]["NK-OUT-01"]["price"] == 120
    assert llamada["politica"] is None
    assert llamada["hash"] == merchant.ultimo_checkout["checkout_hash"]
    assert llamada["ficha"].confirmada is True
    campos = {r.campo for r in llamada["ficha"].requisitos}
    assert {"precio", "moneda", "categoria", "uso", "talle", "marca"} <= campos


async def _evaluar_con(armar_proxy, config, transporte, **config_extra):
    """Compra B/cart_1 y devuelve (catálogo, comercio_id) que recibió evaluar()."""
    import dataclasses

    from proxy import guard_stub

    llamadas: list[tuple[dict, str | None]] = []

    def espia(ficha, ofertas_vistas, cart_id_elegido, catalogo, politica=None,
              checkout_hash=None, ahora=None, checkout=None):
        llamadas.append((catalogo, ofertas_vistas.comercio_id))
        return guard_stub.evaluar(ficha, ofertas_vistas, cart_id_elegido, catalogo)

    app = armar_proxy(
        transport=transporte,
        evaluador=EvaluadorCargado(evaluar=espia, nombre="guard"),
        config=dataclasses.replace(config, **config_extra),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8010"
    ) as c:
        contexto = await _buscar(c, "B")
        resp = await _checkout(c, contexto, "cart_1")
    assert resp.json()["result"]["status"]["state"] == "completed"
    return llamadas[0]


# El catálogo público no siempre existe: sin él el Guard evalúa sin catálogo
# (todo queda "solo declarado"), nunca contra una copia vieja de otro lado.


async def test_catalogo_caido_evalua_sin_catalogo(armar_proxy, config, merchant):
    from proxy.tests.fake_world import MERCHANT_HOST, RuteoPorHost

    catalogo, comercio = await _evaluar_con(
        armar_proxy, config, RuteoPorHost({MERCHANT_HOST: merchant.app})  # sin catálogo
    )
    assert catalogo == {}
    assert comercio == "merchant_1"  # sale del checkout firmado, no del catálogo


async def test_catalogo_caido_con_respaldo_explicito_usa_el_fixture(
    armar_proxy, config, merchant
):
    from proxy.tests.fake_world import MERCHANT_HOST, RuteoPorHost

    catalogo, comercio = await _evaluar_con(
        armar_proxy, config, RuteoPorHost({MERCHANT_HOST: merchant.app}),
        catalogo_respaldo_fixture=True,  # solo para la demo offline
    )
    assert "NK-OUT-01" in catalogo and comercio == "merchant_1"


async def test_sin_catalogo_configurado_no_se_consulta(armar_proxy, config, merchant):
    from proxy.tests.fake_world import MERCHANT_HOST, RuteoPorHost

    # RuteoPorHost falla ante un host desconocido: si el proxy pidiera el
    # catálogo, lo detectaría.
    catalogo, comercio = await _evaluar_con(
        armar_proxy, config, RuteoPorHost({MERCHANT_HOST: merchant.app}),
        catalogo_url=None,
    )
    assert catalogo == {} and comercio == "merchant_1"


async def test_catalogo_de_otro_comercio_no_se_usa(armar_proxy, config, merchant):
    from proxy.tests.fake_world import (
        CATALOG_HOST,
        MERCHANT_HOST,
        RuteoPorHost,
        app_catalogo,
    )

    catalogo, comercio = await _evaluar_con(
        armar_proxy, config,
        RuteoPorHost({
            MERCHANT_HOST: merchant.app,
            CATALOG_HOST: app_catalogo(merchant_id="otro_comercio"),
        }),
    )
    # El checkout lo firmó merchant_1: verificar contra el catálogo de otro
    # comercio sería contrastar con datos ajenos.
    assert catalogo == {}
    assert comercio == "merchant_1"


# ---------------------------------------------------------------------------
# Si no se puede evaluar, no se aprueba en silencio
# ---------------------------------------------------------------------------


async def test_sin_ofertas_previas_no_se_presenta_para_firma(
    cliente: httpx.AsyncClient,
):
    resp = await _checkout(cliente, "ctx-desconocido", "cart_1")

    task = resp.json()["result"]
    assert task["status"]["state"] == "failed"
    assert _checkouts(task) == []
    tipos = [e["tipo"] for e in await _eventos(cliente)]
    assert "error" in tipos and tipos[-1] == "bloqueado"


async def test_evaluar_que_falla_no_aprueba(armar_proxy):
    def roto(*args, **kwargs):
        raise RuntimeError("bug del guard")

    app = armar_proxy(evaluador=EvaluadorCargado(evaluar=roto, nombre="guard"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8010"
    ) as c:
        contexto = await _buscar(c)
        resp = await _checkout(c, contexto, "cart_1")
        eventos = (await c.get("/guard/events/history")).json()

    task = resp.json()["result"]
    assert task["status"]["state"] == "failed"
    assert _checkouts(task) == []
    errores = [e for e in eventos if e["tipo"] == "error"]
    assert errores and "bug del guard" in errores[0]["datos"]["mensaje"]


# ---------------------------------------------------------------------------
# SSE (message/stream)
# ---------------------------------------------------------------------------


async def _checkout_stream(cliente, contexto, cart_id):
    return await cliente.post(
        RPC,
        content=cuerpo(pedido_create_checkout(contexto, cart_id, "message/stream")),
        headers=HEADERS_A2A,
    )


async def test_sse_aprobado_checkout_intacto_mas_veredicto(
    cliente: httpx.AsyncClient, merchant: FakeMerchant
):
    contexto = await _buscar(cliente)
    resp = await _checkout_stream(cliente, contexto, "cart_1")

    assert resp.headers["content-type"].startswith("text/event-stream")
    originales = [e["result"] for e in parsear_sse(merchant.ultima_respuesta)]
    recibidos = [e["result"] for e in parsear_sse(resp.content)]
    # Todos los eventos originales, en orden, más un artifact-update del veredicto.
    sin_veredicto = [
        r for r in recibidos
        if not (r.get("kind") == "artifact-update"
                and GUARD_VEREDICTO_KEY in r["artifact"]["parts"][0]["data"])
    ]
    assert sin_veredicto == originales
    veredictos = [r for r in recibidos if r not in originales]
    assert len(veredictos) == 1
    assert veredictos[0]["taskId"] == originales[0]["id"]
    assert veredictos[0]["contextId"] == contexto
    assert merchant.ultimo_checkout["checkout_jwt"].encode() in resp.content
    assert recibidos[-1]["kind"] == "status-update" and recibidos[-1]["final"] is True


async def test_sse_bloqueado_sin_checkout_y_failed(
    cliente: httpx.AsyncClient, merchant: FakeMerchant
):
    contexto = await _buscar(cliente)
    resp = await _checkout_stream(cliente, contexto, "cart_4")

    recibidos = [e["result"] for e in parsear_sse(resp.content)]
    assert merchant.ultimo_checkout["checkout_jwt"].encode() not in resp.content
    assert not any(
        "ap2.checkout" in p.get("data", {})
        for r in recibidos if r.get("kind") == "artifact-update"
        for p in r["artifact"]["parts"]
    )
    final = recibidos[-1]
    assert final["kind"] == "status-update" and final["final"] is True
    assert final["status"]["state"] == "failed"
    assert "STUB" in final["status"]["message"]["parts"][0]["text"]
    finales = [r for r in recibidos if r.get("kind") == "status-update"]
    assert len(finales) == 1
