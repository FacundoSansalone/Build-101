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
