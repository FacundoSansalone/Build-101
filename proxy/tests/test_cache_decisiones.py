"""Corrección 4: caché de decisiones por (task_id, sha256(checkout_jwt), cart_id).

tasks/get y tasks/resubscribe de una tarea bloqueada o retenida nunca
devuelven el checkout; si el checkout de una tarea cambió, se reevalúa.
"""

from __future__ import annotations

import pytest

from proxy.tests.ayudantes import (
    RPC,
    buscar,
    checkout,
    checkouts,
    cliente_de,
    eventos,
    evaluador_espia,
    tasks_get,
    tipos,
)
from proxy.tests.fake_world import HEADERS_A2A, FakeMerchant, cargar_fixture, cuerpo, parsear_sse

pytestmark = pytest.mark.anyio


async def test_tasks_get_de_una_tarea_aprobada_no_reevalua(armar_proxy, merchant: FakeMerchant):
    evaluador, llamadas = evaluador_espia()
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto = await buscar(c, "B")
        task = (await checkout(c, contexto, "cart_1")).json()["result"]
        resp = await tasks_get(c, task["id"])

    assert len(llamadas) == 1
    assert checkouts(resp.json()["result"]) == [merchant.ultimo_checkout]


async def test_tasks_get_con_otro_checkout_en_la_misma_tarea_reevalua(
    armar_proxy, merchant: FakeMerchant
):
    evaluador, llamadas = evaluador_espia()
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto = await buscar(c, "B")
        task = (await checkout(c, contexto, "cart_1")).json()["result"]
        # El comercio cambia el checkout guardado en la misma tarea (otro JWT).
        cart1 = next(x for x in cargar_fixture("B")["carts"] if x["cart_id"] == "cart_1")
        nuevo = merchant.armar_checkout(cart1)
        guardada = merchant.tareas[task["id"]]
        guardada["artifacts"][0]["parts"][0]["data"]["ap2.checkout"] = nuevo
        resp = await tasks_get(c, task["id"])

    assert [ll["cart"] for ll in llamadas] == ["cart_1", "cart_1"]
    assert llamadas[1]["checkout"].checkout_jwt == nuevo["checkout_jwt"]
    assert checkouts(resp.json()["result"]) == [nuevo]


async def test_tasks_get_con_checkout_de_otro_cart_bloquea(armar_proxy, merchant: FakeMerchant):
    evaluador, _ = evaluador_espia()
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto = await buscar(c, "B")
        task = (await checkout(c, contexto, "cart_1")).json()["result"]
        cart4 = next(x for x in cargar_fixture("B")["carts"] if x["cart_id"] == "cart_4")
        merchant.tareas[task["id"]]["artifacts"][0]["parts"][0]["data"]["ap2.checkout"] = (
            merchant.armar_checkout(cart4)
        )
        resp = await tasks_get(c, task["id"])

    resultado = resp.json()["result"]
    assert resultado["status"]["state"] == "failed"
    assert checkouts(resultado) == []


async def test_tasks_get_de_una_tarea_retenida_no_trae_el_checkout(armar_proxy):
    evaluador, llamadas = evaluador_espia("escalar")
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto = await buscar(c, "C")
        task = (await checkout(c, contexto, "cart_1")).json()["result"]
        resp = await tasks_get(c, task["id"])
        evs = await eventos(c)

    assert len(llamadas) == 1
    resultado = resp.json()["result"]
    assert resultado["status"]["state"] == "failed"
    assert checkouts(resultado) == []
    assert tipos(evs)[-1] == "bloqueado"


async def test_tasks_get_de_una_tarea_bloqueada_con_el_guard_apagado(cliente, merchant):
    contexto = await buscar(cliente, "B")
    task = (await checkout(cliente, contexto, "cart_4")).json()["result"]
    await cliente.post("/guard/state", json={"guard_activo": False})
    resp = await tasks_get(cliente, task["id"])

    assert checkouts(resp.json()["result"]) == []


async def test_tasks_get_de_una_tarea_que_el_proxy_no_evaluo_bloquea(cliente, merchant):
    await cliente.post("/guard/state", json={"guard_activo": False})
    contexto = await buscar(cliente, "B")
    task = (await checkout(cliente, contexto, "cart_1")).json()["result"]
    await cliente.post("/guard/state", json={"guard_activo": True})
    resp = await tasks_get(cliente, task["id"])

    assert checkouts(resp.json()["result"]) == []
    assert resp.json()["result"]["status"]["state"] == "failed"


async def test_tasks_resubscribe_de_una_tarea_bloqueada_no_trae_el_checkout(
    cliente, merchant: FakeMerchant
):
    contexto = await buscar(cliente, "B")
    task = (await checkout(cliente, contexto, "cart_4")).json()["result"]
    pedido = {"id": "r-1", "jsonrpc": "2.0", "method": "tasks/resubscribe",
              "params": {"id": task["id"]}}
    resp = await cliente.post(RPC, content=cuerpo(pedido), headers=HEADERS_A2A)

    assert merchant.ultimo_checkout["checkout_jwt"].encode() in merchant.ultima_respuesta
    assert merchant.ultimo_checkout["checkout_jwt"].encode() not in resp.content
    (resultado,) = [e["result"] for e in parsear_sse(resp.content)]
    assert resultado["status"]["state"] == "failed"


# ---------------------------------------------------------------------------
# 4b: la clave cubre el data part ap2.checkout completo, no solo el JWT
# ---------------------------------------------------------------------------


def _evaluador_que_ata_el_data_part():
    """Aprueba solo el data part que vio la primera vez; cualquier cambio
    (aunque el JWT sea el mismo) lo bloquea. Registra cada llamada."""
    from contracts import Veredicto
    from proxy.evaluador import EvaluadorCargado

    vistos: list[dict] = []

    def evaluar(ficha, ofertas_vistas, cart_id_elegido, catalogo, politica=None,
                checkout_hash=None, ahora=None, checkout=None):
        datos = checkout.model_dump(mode="json")
        vistos.append(datos)
        status = "aprobado" if datos == vistos[0] else "bloqueado"
        return Veredicto(status=status, cart_id_elegido=cart_id_elegido,
                         cart_id_recomendado=None, ranking=[], chequeos=[],
                         motivo=f"test: {status}")

    return EvaluadorCargado(evaluar=evaluar, nombre="guard"), vistos


def _checkout_guardado(merchant: FakeMerchant, task_id: str) -> dict:
    return merchant.tareas[task_id]["artifacts"][0]["parts"][0]["data"]["ap2.checkout"]


@pytest.mark.parametrize(
    "campo, valor",
    [("amount", 1.0), ("amount_cents", 100), ("checkout_hash", "otro-hash"),
     ("currency", "EUR"), ("item_label", "otro texto")],
)
async def test_tasks_get_con_el_mismo_jwt_y_otro_data_part_reevalua(
    armar_proxy, merchant: FakeMerchant, campo, valor
):
    evaluador, vistos = _evaluador_que_ata_el_data_part()
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto = await buscar(c, "B")
        task = (await checkout(c, contexto, "cart_1")).json()["result"]
        assert task["status"]["state"] == "completed"
        _checkout_guardado(merchant, task["id"])[campo] = valor  # mismo JWT
        resp = await tasks_get(c, task["id"])

    assert len(vistos) == 2
    assert vistos[1][campo] == valor
    resultado = resp.json()["result"]
    assert resultado["status"]["state"] == "failed"
    assert checkouts(resultado) == []


async def test_tasks_resubscribe_con_otro_data_part_reevalua(armar_proxy, merchant: FakeMerchant):
    evaluador, vistos = _evaluador_que_ata_el_data_part()
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto = await buscar(c, "B")
        task = (await checkout(c, contexto, "cart_1")).json()["result"]
        _checkout_guardado(merchant, task["id"])["amount"] = 1.0
        pedido = {"id": "r-1", "jsonrpc": "2.0", "method": "tasks/resubscribe",
                  "params": {"id": task["id"]}}
        resp = await c.post(RPC, content=cuerpo(pedido), headers=HEADERS_A2A)

    assert len(vistos) == 2
    (resultado,) = [e["result"] for e in parsear_sse(resp.content)]
    assert resultado["status"]["state"] == "failed"
    assert merchant.ultimo_checkout["checkout_jwt"].encode() not in resp.content


async def test_tasks_get_con_el_data_part_igual_en_otro_orden_no_reevalua(
    armar_proxy, merchant: FakeMerchant
):
    evaluador, vistos = _evaluador_que_ata_el_data_part()
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto = await buscar(c, "B")
        task = (await checkout(c, contexto, "cart_1")).json()["result"]
        parte = merchant.tareas[task["id"]]["artifacts"][0]["parts"][0]["data"]
        parte["ap2.checkout"] = dict(reversed(list(parte["ap2.checkout"].items())))
        resp = await tasks_get(c, task["id"])

    assert len(vistos) == 1  # JSON canónico: el orden de las claves no cambia nada
    assert checkouts(resp.json()["result"]) == [merchant.ultimo_checkout]
