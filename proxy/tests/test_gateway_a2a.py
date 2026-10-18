"""Tráfico A2A del gateway (diseño del gateway, §1.2 a §1.4).

- La clave y la compra del integrador nunca llegan al comercio.
- Cada pedido de integrador es de una compra suya y del comercio de la ruta.
- Un `contextId` es de una sola compra: el de otra se corta en el pedido, en
  la respuesta JSON y en cada evento SSE.
- Las ofertas de un contexto tienen dueño (el comercio que las respondió) y
  lo firmado tiene que ser del comercio de la ruta.
- El oyente público solo sirve `/a2a` y `/v1`; sin claves no existe y el
  tráfico anónimo local sigue igual que siempre.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from proxy.claves import RegistroIntegradores
from proxy.tests.ayudantes import (
    RPC,
    checkout as checkout_anonimo,
    checkouts,
    evaluador_espia,
    veredictos,
)
from proxy.tests.fake_world import (
    HEADERS_A2A,
    cuerpo,
    data_parts_de,
    parsear_sse,
    pedido_create_checkout,
    pedido_find_products,
)
from proxy.tests.gateway_ayudantes import (
    CLAVE_ACME,
    CLAVE_OTRO,
    GATEWAY_PUBLIC_URL,
    armar_gateway,
    buscar,
    checkout,
    cliente_local,
    cliente_publico,
    crear_compra,
)
from proxy.tipos_gateway import HEADER_COMPRA

pytestmark = pytest.mark.anyio


@pytest.fixture
def config_gw(config):
    return replace(config, gateway_public_url=GATEWAY_PUBLIC_URL)


def _codigo(resp) -> str:
    return resp.json()["error"]["data"]["codigo"]


def _carts(task: dict[str, Any]) -> list[str]:
    return [d["ap2.cart"]["cart_id"] for d in data_parts_de(task) if "ap2.cart" in d]


# -- headers ---------------------------------------------------------------------------


async def test_la_clave_y_la_compra_no_llegan_al_comercio(config_gw):
    mundo = armar_gateway(config_gw, evaluador_espia()[0])
    async with cliente_publico(mundo.app) as c:
        compra_id = (await crear_compra(c))["compra_id"]
        contexto = (await buscar(c, compra_id)).json()["result"]["contextId"]
        await checkout(c, compra_id, contexto, "cart_1")
    assert len(mundo.merchant.recibidos) == 2
    for _, headers in mundo.merchant.recibidos:
        nombres = {k.lower() for k in headers}
        assert "authorization" not in nombres
        assert HEADER_COMPRA.lower() not in nombres
        assert CLAVE_ACME not in " ".join(headers.values())
        assert headers.get("x-a2a-extensions")  # lo de A2A sigue pasando


async def test_sin_compra_da_400_y_con_una_de_otro_comercio_409(config_gw):
    mundo = armar_gateway(config_gw, evaluador_espia()[0])
    async with cliente_publico(mundo.app) as c:
        sin = await c.post(
            "/a2a/m/merchant_1", content=cuerpo(pedido_find_products("B")), headers=HEADERS_A2A
        )
        assert sin.status_code == 400 and _codigo(sin) == "falta_compra"
        assert sin.json()["error"]["code"] == -32001
        mal = await buscar(c, "cmp-no-es-un-id")
        assert mal.status_code == 400 and _codigo(mal) == "falta_compra"
        inexistente = await buscar(c, "cmp-0123456789abcdef")
        assert inexistente.status_code == 404 and _codigo(inexistente) == "compra_desconocida"

        compra_id = (await crear_compra(c))["compra_id"]  # de merchant_1
        otra = await buscar(c, compra_id, comercio="tienda_b")
        assert otra.status_code == 409 and _codigo(otra) == "comercio_distinto"
        # Con clave, un comercio que no existe da el mismo 403 que uno no
        # permitido (como /v1): no se enumeran los comercios del operador.
        desconocido = await buscar(c, compra_id, comercio="no_existe")
        assert desconocido.status_code == 403 and _codigo(desconocido) == "comercio_no_permitido"
    assert mundo.merchant.recibidos == [] and mundo.merchant_b.recibidos == []


async def test_un_comercio_inexistente_y_uno_no_permitido_dan_lo_mismo(config_gw):
    """Revisión adversarial: 404 frente a 403 dejaba enumerar qué comercios
    tiene registrados el operador (en el JSON-RPC y en la Agent Card)."""
    mundo = armar_gateway(config_gw, evaluador_espia()[0])
    async with cliente_publico(mundo.app, CLAVE_OTRO) as c:  # solo merchant_1
        compra_id = (await crear_compra(c))["compra_id"]
        no_permitido = await buscar(c, compra_id, comercio="tienda_b")
        inexistente = await buscar(c, compra_id, comercio="no_existe")
        card_np = await c.get("/a2a/m/tienda_b/.well-known/agent-card.json")
        card_ie = await c.get("/a2a/m/no_existe/.well-known/agent-card.json")
    assert (no_permitido.status_code, _codigo(no_permitido)) == (403, "comercio_no_permitido")
    assert (inexistente.status_code, _codigo(inexistente)) == (403, "comercio_no_permitido")
    assert inexistente.json() == no_permitido.json()
    assert card_np.status_code == card_ie.status_code == 403
    assert card_np.json() == card_ie.json() == {"error": "comercio_no_permitido"}
    # Sin clave (tráfico anónimo local, la demo) sigue el 404 de siempre.
    async with cliente_local(mundo.app) as local:
        resp = await local.post(
            "/a2a/m/no_existe", content=cuerpo(pedido_find_products("B")), headers=HEADERS_A2A
        )
    assert resp.status_code == 404 and _codigo(resp) == "comercio_desconocido"


@pytest.mark.parametrize(
    "sufijo",
    [b', "x": NaN}', b', "x": Infinity}', b', "x": 1e999}'],
)
async def test_un_pedido_que_no_se_lee_en_estricto_no_se_reenvia(config_gw, sufijo):
    """Revisión adversarial: un cuerpo con NaN (que `json.loads` del comercio
    sí lee) salteaba el chequeo del `contextId` y entraba en la conversación
    de la compra de otro integrador."""
    mundo = armar_gateway(config_gw, evaluador_espia()[0])
    async with cliente_publico(mundo.app) as a, cliente_publico(mundo.app, CLAVE_OTRO) as b:
        compra_b = (await crear_compra(b))["compra_id"]
        ctx_b = (await buscar(b, compra_b)).json()["result"]["contextId"]
        compra_a = (await crear_compra(a))["compra_id"]
        pedido = pedido_find_products("B")
        pedido["params"]["message"]["contextId"] = ctx_b
        raw = cuerpo(pedido)[:-1] + sufijo
        antes = len(mundo.merchant.recibidos)
        resp = await a.post(
            "/a2a/m/merchant_1", content=raw, headers={**HEADERS_A2A, HEADER_COMPRA: compra_a}
        )
    assert resp.status_code == 400 and _codigo(resp) == "pedido_ilegible"
    assert len(mundo.merchant.recibidos) == antes


@pytest.mark.parametrize("contexto", [123, ["ctx"], {"a": 1}, True])
async def test_un_context_id_que_no_es_texto_no_se_reenvia(config_gw, contexto):
    mundo = armar_gateway(config_gw, evaluador_espia()[0])
    async with cliente_publico(mundo.app) as c:
        compra_id = (await crear_compra(c))["compra_id"]
        pedido = pedido_find_products("B")
        pedido["params"]["message"]["contextId"] = contexto
        resp = await c.post(
            "/a2a/m/merchant_1", content=cuerpo(pedido),
            headers={**HEADERS_A2A, HEADER_COMPRA: compra_id},
        )
    assert resp.status_code == 400 and _codigo(resp) == "pedido_ilegible"
    assert mundo.merchant.recibidos == []


async def test_cuerpo_demasiado_grande_da_413(config_gw):
    mundo = armar_gateway(replace(config_gw, max_cuerpo_a2a=100), evaluador_espia()[0])
    async with cliente_publico(mundo.app) as c:
        compra_id = (await crear_compra(c))["compra_id"]
        resp = await buscar(c, compra_id)
    assert resp.status_code == 413 and _codigo(resp) == "cuerpo_demasiado_grande"
    assert mundo.merchant.recibidos == []


async def test_compra_vencida_por_ttl_da_409(config_gw):
    mundo = armar_gateway(config_gw, evaluador_espia()[0])
    async with cliente_publico(mundo.app) as c:
        compra_id = (await crear_compra(c))["compra_id"]
        mundo.proxy.config = replace(mundo.proxy.config, compra_ttl_s=1)
        compra = mundo.proxy.compras.get(compra_id)
        compra.creada = compra.creada.replace(year=compra.creada.year - 1)
        resp = await buscar(c, compra_id)
        assert resp.status_code == 409 and _codigo(resp) == "compra_vencida"
        # Sigue legible por GET.
        assert (await c.get(f"/v1/compras/{compra_id}")).status_code == 200


# -- contextId de otra compra --------------------------------------------------------


async def test_un_contexto_de_otra_compra_en_el_pedido_da_409(config_gw):
    mundo = armar_gateway(config_gw, evaluador_espia()[0])
    async with cliente_publico(mundo.app) as c:
        compra_a = (await crear_compra(c))["compra_id"]
        compra_b = (await crear_compra(c))["compra_id"]
        contexto_a = (await buscar(c, compra_a)).json()["result"]["contextId"]
        recibidos = len(mundo.merchant.recibidos)

        resp = await buscar(c, compra_b, contexto=contexto_a)
        assert resp.status_code == 409 and _codigo(resp) == "contexto_ajeno"
        resp = await checkout(c, compra_b, contexto_a, "cart_1")
        assert resp.status_code == 409 and _codigo(resp) == "contexto_ajeno"
        assert len(mundo.merchant.recibidos) == recibidos  # no se reenvió nada


async def test_un_contexto_de_otra_compra_en_la_respuesta_json_se_corta(config_gw):
    mundo = armar_gateway(config_gw, evaluador_espia()[0])
    async with cliente_publico(mundo.app) as c:
        compra_a = (await crear_compra(c))["compra_id"]
        compra_b = (await crear_compra(c))["compra_id"]
        contexto_a = (await buscar(c, compra_a)).json()["result"]["contextId"]

        def mudar(respuesta: dict[str, Any], pedido: dict[str, Any]) -> dict[str, Any]:
            respuesta["result"]["contextId"] = contexto_a  # el comercio "muda" la compra
            return respuesta

        mundo.merchant.manipular = mudar
        resp = await buscar(c, compra_b)
        task = resp.json()["result"]
        assert resp.status_code == 200
        assert task["status"]["state"] == "failed"
        assert _carts(task) == []  # nada del comercio
        assert task.get("contextId") != contexto_a
        assert veredictos(task)[0]["status"] == "bloqueado"
        vista = (await c.get(f"/v1/compras/{compra_b}")).json()
        assert contexto_a not in vista["contextos"]


async def test_un_contexto_de_otra_compra_en_el_sse_se_corta(config_gw):
    mundo = armar_gateway(config_gw, evaluador_espia()[0])
    async with cliente_publico(mundo.app) as c:
        compra_a = (await crear_compra(c))["compra_id"]
        compra_b = (await crear_compra(c))["compra_id"]
        contexto_a = (await buscar(c, compra_a)).json()["result"]["contextId"]

        def mudar(eventos: list[dict[str, Any]], pedido: Any) -> list[dict[str, Any]]:
            for evento in eventos[1:]:
                evento["contextId"] = contexto_a
            return eventos

        mundo.merchant.manipular_sse = mudar
        resp = await buscar(c, compra_b, metodo="message/stream")
        eventos = parsear_sse(resp.content)
        resultados = [e["result"] for e in eventos]
        # El primer evento (con su propio contexto) pasa; el que trae el de
        # otra compra no, y el stream termina con la tarea fallida.
        assert all("ap2.cart" not in str(r) for r in resultados)
        assert resultados[-1]["status"]["state"] == "failed"
        assert all(r.get("contextId") != contexto_a for r in resultados)


async def test_mas_de_veinte_contextos_por_compra_se_cortan(config_gw):
    mundo = armar_gateway(config_gw, evaluador_espia()[0])
    async with cliente_publico(mundo.app) as c:
        compra_id = (await crear_compra(c))["compra_id"]
        for n in range(20):
            resp = await buscar(c, compra_id, contexto=f"ctx-propio-{n}")
            assert resp.status_code == 200
        resp = await buscar(c, compra_id, contexto="ctx-propio-20")
        assert resp.status_code == 409 and _codigo(resp) == "contexto_ajeno"


# -- ofertas y lo firmado ---------------------------------------------------------------


async def test_las_ofertas_de_otro_comercio_no_se_pisan(config_gw):
    """Tráfico anónimo local: un segundo comercio que responde con el contexto
    del primero no pisa sus ofertas ni puede usarlas para un checkout."""
    mundo = armar_gateway(config_gw, evaluador_espia()[0])
    async with cliente_local(mundo.app) as c:
        primera = await c.post(RPC, content=cuerpo(pedido_find_products("B")), headers=HEADERS_A2A)
        contexto = primera.json()["result"]["contextId"]
        antes = mundo.proxy._ofertas[contexto]

        pedido = pedido_find_products("A")
        pedido["params"]["message"]["contextId"] = contexto
        resp = await c.post("/a2a/m/tienda_b", content=cuerpo(pedido), headers=HEADERS_A2A)
        task = resp.json()["result"]
        assert task["status"]["state"] == "failed" and _carts(task) == []
        assert mundo.proxy._ofertas[contexto] is antes  # siguen las de merchant_1

        # Un checkout por tienda_b con ese contexto no usa ofertas ajenas.
        pedido_b = await c.post(
            "/a2a/m/tienda_b",
            content=cuerpo(pedido_create_checkout(contexto, "cart_1")),
            headers=HEADERS_A2A,
        )
        task_b = pedido_b.json()["result"]
        assert not checkouts(task_b)
        assert "otro comercio" in veredictos(task_b)[0]["motivo"]

        # Y por merchant_1 todo sigue igual.
        ok = (await checkout_anonimo(c, contexto, "cart_1")).json()["result"]
        assert checkouts(ok)


async def test_un_merchant_id_firmado_distinto_de_la_ruta_se_bloquea(config_gw):
    # tienda_b exige merchant_ids=("tienda-b",) y este comercio firma como merchant_1.
    evaluador, llamadas = evaluador_espia()
    mundo = armar_gateway(config_gw, evaluador, merchant_b=_merchant_b_que_firma_como_1())
    async with cliente_publico(mundo.app) as c:
        compra_id = (await crear_compra(c, comercio="tienda_b"))["compra_id"]
        contexto = (await buscar(c, compra_id, comercio="tienda_b")).json()["result"]["contextId"]
        task = (await checkout(c, compra_id, contexto, "cart_1", comercio="tienda_b")).json()["result"]
    assert not checkouts(task)
    (veredicto,) = veredictos(task)
    assert veredicto["status"] == "bloqueado"
    assert "comercio distinto" in veredicto["motivo"]
    assert llamadas == []  # ni siquiera se evaluó


def _merchant_b_que_firma_como_1():
    from proxy.tests.gateway_ayudantes import FakeMerchantB

    return FakeMerchantB(merchant_id="merchant_1")


async def test_el_comercio_correcto_pasa_con_su_politica(config_gw):
    evaluador, llamadas = evaluador_espia()
    mundo = armar_gateway(config_gw, evaluador)
    async with cliente_publico(mundo.app) as c:
        compra_id = (await crear_compra(c, comercio="tienda_b"))["compra_id"]
        contexto = (await buscar(c, compra_id, comercio="tienda_b")).json()["result"]["contextId"]
        task = (await checkout(c, compra_id, contexto, "cart_1", comercio="tienda_b")).json()["result"]
    assert checkouts(task) == [mundo.merchant_b.ultimo_checkout]
    assert llamadas[0]["politica"].comercios_aprobados == ["tienda-b"]
    assert llamadas[0]["ofertas"].comercio_id == "tienda-b"
    assert mundo.merchant.recibidos == []  # nada fue al otro comercio


# -- Agent Card y oyentes -----------------------------------------------------------------


async def test_la_agent_card_se_reescribe_con_la_url_de_cada_oyente(config_gw):
    mundo = armar_gateway(config_gw, evaluador_espia()[0])
    ruta = "/a2a/m/tienda_b/.well-known/agent-card.json"
    async with cliente_publico(mundo.app) as c:
        card = (await c.get(ruta)).json()
        assert card["url"] == f"{GATEWAY_PUBLIC_URL}/a2a/m/tienda_b"
        vieja = (await c.get("/a2a/merchant_agent/.well-known/agent-card.json")).json()
        assert vieja["url"] == f"{GATEWAY_PUBLIC_URL}/a2a/merchant_agent"
    async with cliente_publico(mundo.app, None) as sin_clave:
        assert (await sin_clave.get(ruta)).status_code == 401
    async with cliente_local(mundo.app) as c:
        card = (await c.get(ruta)).json()
        assert card["url"] == "http://localhost:8010/a2a/m/tienda_b"
        vieja = (await c.get("/a2a/merchant_agent/.well-known/agent-card.json")).json()
        assert vieja["url"] == "http://localhost:8010/a2a/merchant_agent"  # como siempre
        assert (await c.get("/a2a/m/no_existe/.well-known/agent-card.json")).status_code == 404


async def test_el_oyente_publico_no_sirve_guard(config_gw):
    mundo = armar_gateway(config_gw, evaluador_espia()[0])
    async with cliente_publico(mundo.app) as c:
        for ruta in ("/guard/state", "/guard/events/history", "/guard"):
            resp = await c.get(ruta)
            assert resp.status_code == 404, ruta
        assert (await c.get("/otra-cosa")).status_code == 404
        assert (await c.get("/v1/openapi.json")).status_code == 200


async def test_el_interruptor_no_apaga_el_guard_del_integrador(config_gw):
    evaluador, llamadas = evaluador_espia()
    mundo = armar_gateway(config_gw, evaluador)
    async with cliente_local(mundo.app) as panel:
        assert (await panel.post("/guard/state", json={"guard_activo": False})).status_code == 200
    async with cliente_publico(mundo.app) as c:
        compra_id = (await crear_compra(c))["compra_id"]
        contexto = (await buscar(c, compra_id)).json()["result"]["contextId"]
        task = (await checkout(c, compra_id, contexto, "cart_4")).json()["result"]
    assert not checkouts(task)  # el stub bloquea el último cart
    assert veredictos(task)[0]["status"] == "bloqueado"
    assert len(llamadas) == 1


async def test_el_trafico_anonimo_local_sigue_igual(config_gw):
    evaluador, llamadas = evaluador_espia()
    mundo = armar_gateway(config_gw, evaluador)
    async with cliente_local(mundo.app) as c:
        busqueda = await c.post(RPC, content=cuerpo(pedido_find_products("B")), headers=HEADERS_A2A)
        contexto = busqueda.json()["result"]["contextId"]
        task = (await checkout_anonimo(c, contexto, "cart_1")).json()["result"]
        assert checkouts(task) == [mundo.merchant.ultimo_checkout]
        assert llamadas[0]["politica"] is not None  # merchant_1 del registro tiene merchant_ids
        # Sin X-Compra-Id ni clave no se crea ninguna compra de API.
        compras = (await c.get("/guard/compras")).json()["compras"]
        assert all(x["origen"] != "api" for x in compras)
    await mundo.esperar_fondo()
    assert mundo.receptor.recibidos == []


async def test_sin_claves_no_hay_oyente_publico_y_v1_pide_clave(armar_proxy):
    app = armar_proxy()  # la app de siempre: sin integradores
    async with cliente_publico(app) as c:
        # El puerto 8030 no es "público" sin claves: es un pedido local más.
        resp = await c.post("/v1/compras", json={})
        assert resp.status_code == 401
        assert resp.json()["error"]["codigo"] == "clave_invalida"
    async with cliente_local(app) as c:
        assert (await c.post("/v1/compras", json={})).status_code == 401
        estado = (await c.get("/guard/state")).json()
        assert "gateway" not in estado  # /guard/state igual que siempre
        # El anónimo local de siempre.
        busqueda = await c.post(RPC, content=cuerpo(pedido_find_products("B")), headers=HEADERS_A2A)
        assert busqueda.status_code == 200


async def test_sin_claves_activas_no_hay_puerto_publico(config_gw):
    from proxy.tests.gateway_ayudantes import datos_integradores

    datos = datos_integradores(webhook=False)
    for integrador in datos["integradores"]:
        for clave in integrador["claves"]:
            clave["activa"] = False
    integradores = RegistroIntegradores.desde_dict(datos, {})
    mundo = armar_gateway(config_gw, evaluador_espia()[0], integradores=integradores)
    async with cliente_publico(mundo.app, None) as c:
        # Sin oyente público, /guard es local (ControlLocal lo frena por Host).
        assert (await c.get("/guard/state")).status_code == 403
    async with cliente_publico(mundo.app) as c:
        assert (await c.get("/v1/compras")).status_code == 401  # clave inactiva


async def test_estado_del_gateway_en_guard_state(config_gw):
    mundo = armar_gateway(config_gw, evaluador_espia()[0])
    async with cliente_local(mundo.app) as c:
        estado = (await c.get("/guard/state")).json()
    assert estado["gateway"] == {
        "integradores": 2, "comercios": ["merchant_1", "tienda_b"],
        "almacen": "memoria", "publico": True,
    }

