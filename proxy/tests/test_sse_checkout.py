"""Corrección 3: SSE. Cada evento con ap2.checkout se evalúa; un segundo
checkout en el mismo stream bloquea y corta; el Content-Type se normaliza."""

from __future__ import annotations

import copy

import pytest

from contracts import GUARD_VEREDICTO_KEY
from proxy.tests.ayudantes import (
    buscar,
    checkout,
    cliente_de,
    eventos,
    evaluador_espia,
    tipos,
)
from proxy.tests.fake_world import FakeMerchant, cargar_fixture, parsear_sse

pytestmark = pytest.mark.anyio


def _results(contenido: bytes) -> list[dict]:
    return [e["result"] for e in parsear_sse(contenido)]


def _hay_checkout(results: list[dict]) -> bool:
    def partes(r):
        if r.get("kind") == "artifact-update":
            return r["artifact"]["parts"]
        if r.get("kind") == "task":
            return [p for a in r.get("artifacts") or [] for p in a["parts"]]
        return []

    return any("ap2.checkout" in p.get("data", {}) for r in results for p in partes(r))


def _solo_checkout(fn):
    def manipular(results, pedido):
        partes = pedido["params"]["message"]["parts"]
        if any("cart_id" in (p.get("data") or {}) for p in partes):
            return fn(results)
        return results

    return manipular


async def test_segundo_checkout_mismo_artifact_id_bloquea_y_corta(
    armar_proxy, merchant: FakeMerchant
):
    def duplicar(results):
        con_checkout = next(r for r in results if r["kind"] == "artifact-update")
        otro = copy.deepcopy(con_checkout)
        cart4 = next(c for c in cargar_fixture("B")["carts"] if c["cart_id"] == "cart_4")
        extra = merchant.armar_checkout(cart4, cart_id="cart_1")
        merchant.checkouts.append(extra)
        otro["artifact"]["parts"][0]["data"]["ap2.checkout"] = extra
        i = results.index(con_checkout)
        return results[: i + 1] + [otro] + results[i + 1 :]

    merchant.manipular_sse = _solo_checkout(duplicar)
    evaluador, llamadas = evaluador_espia()
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto = await buscar(c, "B")
        resp = await checkout(c, contexto, "cart_1", metodo="message/stream")
        evs = await eventos(c)

    recibidos = _results(resp.content)
    assert not _hay_checkout(recibidos)  # ni el primero ni el segundo
    assert len(merchant.checkouts) == 2
    for visto in merchant.checkouts:
        assert visto["checkout_jwt"].encode() not in resp.content
    final = recibidos[-1]
    assert final["kind"] == "status-update" and final["final"] is True
    assert final["status"]["state"] == "failed"
    assert sum(1 for r in recibidos if r.get("kind") == "status-update") == 1
    assert tipos(evs)[-1] == "bloqueado"
    assert len(llamadas) <= 1


async def test_checkout_dentro_de_un_task_en_el_stream_se_evalua(
    armar_proxy, merchant: FakeMerchant
):
    """El comercio manda la tarea completa (con el checkout) como un solo evento."""

    def como_task(results):
        task = copy.deepcopy(results[0])
        task["artifacts"] = [r["artifact"] for r in results if r["kind"] == "artifact-update"]
        task["status"] = results[-1]["status"]
        return [task]

    merchant.manipular_sse = _solo_checkout(como_task)
    evaluador, llamadas = evaluador_espia()
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto = await buscar(c, "B")
        bloqueado = await checkout(c, contexto, "cart_4", metodo="message/stream")
        aprobado = await checkout(c, contexto, "cart_1", metodo="message/stream")

    assert [ll["cart"] for ll in llamadas] == ["cart_4", "cart_1"]
    (b,) = _results(bloqueado.content)
    assert b["status"]["state"] == "failed" and not _hay_checkout([b])
    (a,) = _results(aprobado.content)
    assert a["status"]["state"] == "completed" and _hay_checkout([a])
    datos = [p["data"] for art in a["artifacts"] for p in art["parts"]]
    assert any(GUARD_VEREDICTO_KEY in d for d in datos)


async def test_content_type_sse_con_mayusculas_se_detecta_y_se_normaliza(
    cliente, merchant: FakeMerchant
):
    merchant.content_type_sse = "Text/Event-Stream; charset=UTF-8"
    contexto = await buscar(cliente, "B")
    resp = await checkout(cliente, contexto, "cart_4", metodo="message/stream")

    assert resp.headers["content-type"].split(";")[0] == "text/event-stream"
    recibidos = _results(resp.content)
    assert not _hay_checkout(recibidos)
    assert recibidos[-1]["status"]["state"] == "failed"


async def test_evento_sse_ilegible_en_un_checkout_bloquea(cliente, merchant: FakeMerchant):
    def romper(results):
        from proxy.tests.fake_world import sse_de

        cuerpo = sse_de(results)
        return cuerpo.replace(b'"amount": 120.0', b'"amount": NaN', 1)

    merchant.manipular_sse = _solo_checkout(romper)
    contexto = await buscar(cliente, "B")
    resp = await checkout(cliente, contexto, "cart_1", metodo="message/stream")
    evs = await eventos(cliente)

    assert b"NaN" not in resp.content
    recibidos = _results(resp.content)
    assert not _hay_checkout(recibidos)
    assert recibidos[-1]["status"]["state"] == "failed"
    assert "error" in tipos(evs) and tipos(evs)[-1] == "bloqueado"


async def test_stream_sin_evento_final_despues_del_checkout_bloquea(
    cliente, merchant: FakeMerchant
):
    merchant.manipular_sse = _solo_checkout(lambda results: results[:-1])  # sin el final
    contexto = await buscar(cliente, "B")
    resp = await checkout(cliente, contexto, "cart_1", metodo="message/stream")

    recibidos = _results(resp.content)
    assert not _hay_checkout(recibidos)
    assert recibidos[-1]["kind"] == "status-update"
    assert recibidos[-1]["status"]["state"] == "failed"


async def test_checkout_en_un_status_update_bloquea(cliente, merchant: FakeMerchant):
    def en_el_mensaje(results):
        final = results[-1]
        checkout_data = next(
            r for r in results if r["kind"] == "artifact-update"
        )["artifact"]["parts"][0]["data"]
        final["status"]["message"] = {
            "kind": "message", "messageId": "m", "role": "agent",
            "parts": [{"kind": "data", "data": checkout_data}],
        }
        return [r for r in results if r["kind"] != "artifact-update"]

    merchant.manipular_sse = _solo_checkout(en_el_mensaje)
    contexto = await buscar(cliente, "B")
    resp = await checkout(cliente, contexto, "cart_1", metodo="message/stream")

    assert merchant.ultimo_checkout["checkout_jwt"].encode() not in resp.content
    recibidos = _results(resp.content)
    assert recibidos[-1]["status"]["state"] == "failed"
