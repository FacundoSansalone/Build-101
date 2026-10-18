"""N2: la aprobación humana se ata EXACTAMENTE al total firmado.

El monto atado es el `totals[type=total].amount` del payload del
`checkout_jwt` (entero, en centavos) + la `currency` firmada. Sin fallbacks a
`line_items` (los arma el comercio) ni al data part. Si no hay un total
entero exacto, no se ofrece ni se acepta aprobación (409) y el checkout queda
retenido.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from typing import Any

import pytest

from proxy import a2a
from proxy.tests.ayudantes import (
    buscar,
    checkout,
    checkouts,
    cliente_de,
    evaluador_espia,
    solo_en_checkout,
)
from proxy.tests.fake_world import FakeMerchant, cargar_fixture

APROBACIONES = "/guard/aprobaciones"


def _b64url(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode("ascii")


def _jwt(payload: dict | str) -> str:
    texto = payload if isinstance(payload, str) else json.dumps(payload)
    return f"eyJhbGciOiJFUzI1NiJ9.{_b64url(texto.encode())}.c2ln"


def _cart(cart_id: str = "cart_1", escenario: str = "C") -> dict[str, Any]:
    return next(c for c in cargar_fixture(escenario)["carts"] if c["cart_id"] == cart_id)


def _checkout_firmado(
    merchant: FakeMerchant, cambiar: Callable[[dict[str, Any]], None]
) -> dict[str, Any]:
    """Un ap2.checkout real del comercio falso, con el payload del JWT
    cambiado por `cambiar` y vuelto a firmar (firma válida)."""
    base = merchant.armar_checkout(_cart())
    _, payload = a2a.decodificar_jwt(base["checkout_jwt"])
    cambiar(payload)
    jwt = merchant.firmar_jwt({"alg": "ES256", "typ": "JWT", "kid": "merchant-key-1"}, payload)
    return {
        **base,
        "checkout_jwt": jwt,
        "checkout_hash": _b64url(hashlib.sha256(jwt.encode()).digest()),
    }


def _entregar(merchant: FakeMerchant, nuevo: dict[str, Any]) -> None:
    def cambiar(respuesta, pedido):
        merchant.ultimo_checkout = nuevo
        respuesta["result"]["artifacts"][0]["parts"][0]["data"]["ap2.checkout"] = nuevo
        return respuesta

    merchant.manipular = solo_en_checkout(cambiar)


def _total(valor: Any) -> Callable[[dict[str, Any]], None]:
    def cambiar(payload: dict[str, Any]) -> None:
        for t in payload["totals"]:
            if t["type"] == "total":
                t["amount"] = valor

    return cambiar


# ---------------------------------------------------------------------------
# Unidad: monto_firmado()
# ---------------------------------------------------------------------------


def test_monto_firmado_es_el_total_entero_y_la_moneda_del_jwt():
    checkout = {
        "checkout_jwt": _jwt({"currency": "USD", "totals": [
            {"type": "subtotal", "amount": 1}, {"type": "total", "amount": 14500}]}),
        "amount": 1.0, "amount_cents": 100, "currency": "EUR",
    }
    assert a2a.monto_firmado(checkout) == (14500, "USD")


@pytest.mark.parametrize(
    "payload",
    [
        {"currency": "USD", "totals": [{"type": "total", "amount": 149.99}]},
        {"currency": "USD", "totals": [{"type": "total", "amount": 14500.0}]},
        {"currency": "USD", "totals": [{"type": "total", "amount": "14500"}]},
        {"currency": "USD", "totals": [{"type": "total", "amount": True}]},
        {"currency": "USD", "totals": [{"type": "total", "amount": -1}]},
        {"currency": "USD", "totals": [{"type": "total", "amount": 14500},
                                       {"type": "total", "amount": 14999}]},
        {"currency": "USD", "totals": [{"type": "subtotal", "amount": 14500}]},
        {"currency": "USD", "line_items": [{"item": {"price": 14500}, "quantity": 1}]},
        {"totals": [{"type": "total", "amount": 14500}]},
        {"currency": 840, "totals": [{"type": "total", "amount": 14500}]},
        {"currency": "", "totals": [{"type": "total", "amount": 14500}]},
    ],
    ids=["float", "float-entero", "string", "bool", "negativo", "dos-totales",
         "sin-total", "solo-line-items", "sin-moneda", "moneda-no-string", "moneda-vacia"],
)
def test_monto_firmado_sin_total_entero_exacto_es_none(payload):
    checkout = {"checkout_jwt": _jwt(payload), "amount": 145.0, "amount_cents": 14500,
                "currency": "USD"}
    assert a2a.monto_firmado(checkout) is None


@pytest.mark.parametrize(
    "jwt",
    [
        "roto",
        None,
        _jwt('{"currency":"USD","totals":[{"type":"total","amount":14999}],'
             '"totals":[{"type":"total","amount":14500}]}'),
        _jwt('{"currency":"USD","totals":[{"type":"total","amount":1e999}]}'),
        _jwt("[1, 2]"),
    ],
    ids=["ilegible", "sin-jwt", "claves-duplicadas", "infinito", "no-objeto"],
)
def test_monto_firmado_de_un_jwt_ambiguo_o_ilegible_es_none(jwt):
    # Ni el data part (amount, amount_cents) ni line_items sirven de respaldo.
    checkout = {"checkout_jwt": jwt, "amount": 145.0, "amount_cents": 14500, "currency": "USD"}
    assert a2a.monto_firmado(checkout) is None


# ---------------------------------------------------------------------------
# Integración: el exploit y los 409
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_aprobar_145_no_deja_pasar_un_total_firmado_float_149_99(
    armar_proxy, merchant: FakeMerchant
):
    """Exploit: se aprueba 145.00 y en el reintento el JWT firma total 149.99
    (float); line_items sigue sumando 14500. No tiene que pasar."""
    evaluador, _ = evaluador_espia("escalar")
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto = await buscar(c, "C")
        await checkout(c, contexto, "cart_1")
        r = await c.post(APROBACIONES, json={"contexto_id": contexto, "cart_id": "cart_1"})
        assert r.status_code == 201 and r.json()["centavos"] == 14500

        tramposo = _checkout_firmado(merchant, _total(149.99))
        _entregar(merchant, tramposo)
        resp = await checkout(c, contexto, "cart_1")
        lista = (await c.get(APROBACIONES)).json()

    task = resp.json()["result"]
    assert task["status"]["state"] == "failed"
    assert checkouts(task) == []
    assert tramposo["checkout_jwt"].encode() not in resp.content
    assert lista["usadas"] == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "cambiar",
    [
        _total(14500.0),
        _total(149.99),
        lambda p: p.pop("totals"),
        lambda p: p.pop("currency"),
        lambda p: p["totals"].append({"type": "total", "amount": 14500}),
    ],
    ids=["float-entero", "float", "sin-totals", "sin-moneda", "dos-totales"],
)
async def test_sin_total_firmado_exacto_no_se_puede_aprobar(
    armar_proxy, merchant: FakeMerchant, cambiar
):
    evaluador, _ = evaluador_espia("escalar")
    _entregar(merchant, _checkout_firmado(merchant, cambiar))
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto = await buscar(c, "C")
        retenido = (await checkout(c, contexto, "cart_1")).json()["result"]
        r = await c.post(APROBACIONES, json={"contexto_id": contexto, "cart_id": "cart_1"})
        reintento = (await checkout(c, contexto, "cart_1")).json()["result"]

    assert retenido["status"]["state"] == "failed"
    assert r.status_code == 409
    assert reintento["status"]["state"] == "failed"
    assert checkouts(reintento) == []


@pytest.mark.anyio
async def test_aprobacion_atada_a_la_moneda_firmada(armar_proxy, merchant: FakeMerchant):
    evaluador, _ = evaluador_espia("escalar")
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto = await buscar(c, "C")
        await checkout(c, contexto, "cart_1")
        r = await c.post(APROBACIONES, json={"contexto_id": contexto, "cart_id": "cart_1"})
        assert r.status_code == 201 and r.json()["moneda"] == "USD"

        def en_euros(payload):
            payload["currency"] = "EUR"

        _entregar(merchant, _checkout_firmado(merchant, en_euros))
        resp = await checkout(c, contexto, "cart_1")

    assert resp.json()["result"]["status"]["state"] == "failed"
    assert checkouts(resp.json()["result"]) == []
