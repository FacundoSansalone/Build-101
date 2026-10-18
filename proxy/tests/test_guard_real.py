"""El proxy con el `guard.evaluar()` real (no el stub), contra el comercio falso.

Mismos casos que la prueba real de punta a punta: B/cart_4 bloqueado; C/cart_1
escalar → retenido → "Aprobar igual" → el reintento pasa con el checkout
intacto.
"""

from __future__ import annotations

import pytest

from proxy.evaluador import EvaluadorNoDisponible, cargar_evaluador
from proxy.tests.ayudantes import (
    buscar,
    checkout,
    checkouts,
    cliente_de,
    eventos,
    veredictos,
)
from proxy.tests.fake_world import FakeMerchant

pytestmark = pytest.mark.anyio


@pytest.fixture
def guard_real():
    try:
        cargado = cargar_evaluador()
    except EvaluadorNoDisponible:
        pytest.skip("guard.evaluar() con checkout= no está disponible")
    assert cargado.nombre == "guard" and cargado.acepta_checkout
    return cargado


async def test_b_cart_4_bloqueado_por_el_guard_real(armar_proxy, guard_real, merchant: FakeMerchant):
    async with cliente_de(armar_proxy(evaluador=guard_real)) as c:
        contexto = await buscar(c, "B")
        resp = await checkout(c, contexto, "cart_4")
        evs = await eventos(c)

    task = resp.json()["result"]
    assert task["status"]["state"] == "failed"
    assert checkouts(task) == []
    (v,) = veredictos(task)
    assert v["status"] == "bloqueado"
    (ev_veredicto,) = [e for e in evs if e["tipo"] == "veredicto"]
    chequeos = ev_veredicto["datos"]["veredicto"]["chequeos"]
    # El Guard ató el checkout firmado al cart evaluado y no encontró diferencias.
    atadura = [q for q in chequeos if q.get("origen") == "checkout_firmado"]
    assert atadura and all(q["resultado"] == "ok" for q in atadura)
    assert ev_veredicto["datos"]["recibo_firmado"] is True


async def test_c_cart_1_escalar_aprobacion_y_reintento(armar_proxy, guard_real, merchant: FakeMerchant):
    async with cliente_de(armar_proxy(evaluador=guard_real)) as c:
        contexto = await buscar(c, "C")
        retenido = (await checkout(c, contexto, "cart_1")).json()["result"]
        assert retenido["status"]["state"] == "failed"
        assert veredictos(retenido)[0]["status"] == "escalar"

        r = await c.post("/guard/aprobaciones", json={"contexto_id": contexto, "cart_id": "cart_1"})
        assert r.status_code == 201
        reintento = await checkout(c, contexto, "cart_1")

    task = reintento.json()["result"]
    assert task["status"]["state"] == "completed"
    firmado = merchant.checkouts[-1]
    assert checkouts(task) == [firmado]
    assert firmado["checkout_jwt"].encode() in reintento.content
    assert merchant.firma_valida(firmado["checkout_jwt"])
