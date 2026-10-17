"""Tests del ap2.checkout firmado: el Guard lo ata al cart que evaluó.

El JWT de prueba se firma con ES256 igual que `_create_jwt()` del comercio
(conftest). El Guard no verifica esa firma (no tiene la clave pública del
comercio): decodifica el payload, recalcula el hash y compara.
"""

import base64
import json

import pytest

from contracts import CheckoutVisto, Ficha, Politica
from guard import evaluar

ESCENARIOS = ["baseline", "A", "B", "B2", "C"]


def _del_checkout(veredicto):
    return [c for c in veredicto.chequeos if c.origen == "checkout_firmado"]


def _peor(veredicto, campo):
    """El chequeo más grave del checkout para un campo."""
    orden = {"bloquea": 0, "escalar": 1, "aviso": 2, "ok": 3}
    candidatos = [c for c in _del_checkout(veredicto) if c.campo == campo]
    assert candidatos, f"sin chequeo del checkout para {campo}"
    return min(candidatos, key=lambda c: orden[c.resultado])


def _b64(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode("ascii")


@pytest.fixture
def evaluar_con(vistas, ficha, catalogo, ahora):
    """evaluar() del escenario con un checkout (y extras) ya armado."""

    def _evaluar(nombre, cart_id, checkout, **extra):
        return evaluar(
            extra.pop("ficha", None) or ficha(nombre),
            extra.pop("vistas", None) or vistas(nombre),
            cart_id,
            catalogo,
            ahora=ahora,
            checkout=checkout,
            **extra,
        )

    return _evaluar


# --- checkout coherente: mismos veredictos ---------------------------------------


@pytest.mark.parametrize(
    ("nombre", "cart_id", "status"),
    [
        ("baseline", "cart_1", "aprobado"),
        ("A", "cart_4", "bloqueado"),
        ("B", "cart_4", "bloqueado"),
        ("B2", "cart_4", "bloqueado"),
        ("C", "cart_1", "escalar"),
        ("baseline", "cart_3", "con_avisos"),
    ],
)
def test_checkout_coherente_mantiene_el_veredicto(
    vistas, ficha, catalogo, ahora, checkout, evaluar_con, nombre, cart_id, status
):
    sin_checkout = evaluar(ficha(nombre), vistas(nombre), cart_id, catalogo, ahora=ahora)
    con_checkout = evaluar_con(nombre, cart_id, checkout(nombre, cart_id))
    assert sin_checkout.status == con_checkout.status == status
    del_checkout = _del_checkout(con_checkout)
    assert {c.campo for c in del_checkout} >= {"checkout_hash", "cart_id", "total", "moneda", "comercio"}
    assert all(c.resultado == "ok" for c in del_checkout)


def test_checkout_armado_con_el_modelo_checkout_de_ap2(firmar_jwt, hash_jwt, evaluar_con):
    """El payload real de create_checkout() (Checkout de AP2, sin None) se lee igual."""
    tipos = pytest.importorskip("ap2.sdk.generated.types.checkout")
    from ap2.sdk.generated.types.item import Item
    from ap2.sdk.generated.types.line_item import LineItem
    from ap2.sdk.generated.types.link import Link
    from ap2.sdk.generated.types.merchant import Merchant
    from ap2.sdk.generated.types.total import Total

    totales = [Total(type="subtotal", amount=12000), Total(type="total", amount=12000)]
    modelo = tipos.Checkout(
        id="cart_1",
        merchant=Merchant(
            id="merchant_1", name="Generic Merchant", website="https://demo-merchant.example"
        ),
        line_items=[
            LineItem(
                id="li_1",
                item=Item(id="cart_1", title="cualquier texto", price=12000),
                quantity=1,
                totals=totales,
            )
        ],
        status=tipos.Status.incomplete,
        currency="USD",
        totals=totales,
        links=[Link(type="privacy_policy", url="https://demo-merchant.example/privacy")],
    )
    payload = modelo.model_dump(mode="json", exclude_none=True)
    payload["iat"], payload["exp"] = 1_792_238_400, 1_792_242_000
    jwt = firmar_jwt(payload)
    visto = CheckoutVisto(
        checkout_jwt=jwt, checkout_hash=hash_jwt(jwt), cart_id="cart_1", amount=120.0,
        currency="USD", item_label="cualquier texto", amount_cents=12000,
    )
    veredicto = evaluar_con("baseline", "cart_1", visto)
    assert veredicto.status == "aprobado"
    assert all(c.resultado == "ok" for c in _del_checkout(veredicto))


@pytest.mark.parametrize("nombre", ESCENARIOS)
def test_sin_checkout_no_hay_chequeos_de_checkout(vistas, ficha, catalogo, ahora, objetivo, nombre):
    veredicto = evaluar(ficha(nombre), vistas(nombre), objetivo(nombre), catalogo, ahora=ahora)
    assert _del_checkout(veredicto) == []


def test_el_title_del_jwt_no_hace_falta(payload_checkout, checkout, evaluar_con):
    """Sin line_items (donde viaja el title) el veredicto es el mismo."""
    payload = payload_checkout("baseline", "cart_1")
    del payload["line_items"]
    veredicto = evaluar_con("baseline", "cart_1", checkout("baseline", "cart_1", payload=payload))
    assert veredicto.status == "aprobado"


# --- hash -------------------------------------------------------------------------


def test_recibo_lleva_el_hash_recalculado(checkout, hash_jwt, evaluar_con):
    visto = checkout("baseline", "cart_1")
    veredicto = evaluar_con("baseline", "cart_1", visto, checkout_hash=visto.checkout_hash)
    assert veredicto.recibo.checkout_hash == hash_jwt(visto.checkout_jwt)


def test_hash_declarado_distinto_bloquea(checkout, hash_jwt, evaluar_con):
    visto = checkout("baseline", "cart_1", checkout_hash="hash-que-no-es")
    veredicto = evaluar_con("baseline", "cart_1", visto)
    assert veredicto.status == "bloqueado"
    chequeo = _peor(veredicto, "checkout_hash")
    assert chequeo.resultado == "bloquea" and chequeo.tipo == "integridad"
    # El recibo referencia el hash del JWT que se va a firmar, no el declarado.
    assert veredicto.recibo.checkout_hash == hash_jwt(visto.checkout_jwt)


def test_hash_pasado_por_parametro_distinto_bloquea(checkout, evaluar_con):
    visto = checkout("baseline", "cart_1")
    veredicto = evaluar_con("baseline", "cart_1", visto, checkout_hash="otro-hash")
    assert veredicto.status == "bloqueado"
    assert _peor(veredicto, "checkout_hash").resultado == "bloquea"


def test_hash_no_declarado_no_se_aprueba(checkout, evaluar_con):
    veredicto = evaluar_con("baseline", "cart_1", checkout("baseline", "cart_1", checkout_hash=None))
    assert veredicto.status == "escalar"
    assert _peor(veredicto, "checkout_hash").resultado == "escalar"


# --- cart_id ----------------------------------------------------------------------


def test_id_firmado_distinto_del_elegido_bloquea(payload_checkout, checkout, evaluar_con):
    payload = payload_checkout("baseline", "cart_1")
    payload["id"] = "cart_3"
    veredicto = evaluar_con("baseline", "cart_1", checkout("baseline", "cart_1", payload=payload))
    assert veredicto.status == "bloqueado"
    assert _peor(veredicto, "cart_id").resultado == "bloquea"


def test_cart_id_del_checkout_distinto_del_elegido_bloquea(checkout, evaluar_con):
    visto = checkout("baseline", "cart_1", cart_id="cart_3")
    veredicto = evaluar_con("baseline", "cart_1", visto)
    assert veredicto.status == "bloqueado"
    assert _peor(veredicto, "cart_id").resultado == "bloquea"


def test_checkout_de_otro_cart_bloquea(checkout, evaluar_con):
    """El comercio firma el checkout de cart_4 cuando se eligió cart_1."""
    veredicto = evaluar_con("B", "cart_1", checkout("B", "cart_4"))
    assert veredicto.status == "bloqueado"
    assert _peor(veredicto, "cart_id").resultado == "bloquea"


# --- total firmado ------------------------------------------------------------------


def _con_total(payload: dict, centavos) -> dict:
    payload["totals"] = [{"type": "subtotal", "amount": centavos}, {"type": "total", "amount": centavos}]
    return payload


def test_total_firmado_sobre_el_tope_bloquea(payload_checkout, checkout, evaluar_con):
    payload = _con_total(payload_checkout("baseline", "cart_1"), 18900)
    veredicto = evaluar_con("baseline", "cart_1", checkout("baseline", "cart_1", payload=payload))
    assert veredicto.status == "bloqueado"
    mandato = [c for c in _del_checkout(veredicto) if c.tipo == "mandato"]
    assert [c.resultado for c in mandato if c.campo == "precio"] == ["bloquea"]
    assert any(c.observado == 189.0 for c in mandato)


def test_total_firmado_distinto_dentro_del_tope_escala(payload_checkout, checkout, evaluar_con):
    payload = _con_total(payload_checkout("baseline", "cart_1"), 13000)
    veredicto = evaluar_con("baseline", "cart_1", checkout("baseline", "cart_1", payload=payload))
    assert veredicto.status == "escalar"
    total = _peor(veredicto, "total")
    assert total.resultado == "escalar"
    assert (total.esperado, total.observado) == (120.0, 130.0)


def test_total_firmado_igual_al_cart_en_centavos_es_ok(payload_checkout, checkout, evaluar_con):
    payload = _con_total(payload_checkout("baseline", "cart_1"), 12000.0)
    veredicto = evaluar_con("baseline", "cart_1", checkout("baseline", "cart_1", payload=payload))
    assert veredicto.status == "aprobado"


@pytest.mark.parametrize(("campo", "valor"), [("amount", 95.0), ("amount_cents", 9500)])
def test_monto_del_data_part_distinto_del_firmado_escala(checkout, evaluar_con, campo, valor):
    """El PaymentMandate del sample usa amount_cents del data part: tiene que coincidir."""
    veredicto = evaluar_con("baseline", "cart_1", checkout("baseline", "cart_1", **{campo: valor}))
    assert veredicto.status == "escalar"
    assert _peor(veredicto, campo).resultado == "escalar"


def test_monto_del_data_part_sobre_el_tope_bloquea(checkout, evaluar_con):
    veredicto = evaluar_con("baseline", "cart_1", checkout("baseline", "cart_1", amount_cents=18900))
    assert veredicto.status == "bloqueado"


# --- moneda firmada -----------------------------------------------------------------


def test_moneda_firmada_distinta_de_la_obligatoria_bloquea(payload_checkout, checkout, evaluar_con):
    payload = payload_checkout("baseline", "cart_1")
    payload["currency"] = "EUR"
    veredicto = evaluar_con("baseline", "cart_1", checkout("baseline", "cart_1", payload=payload))
    assert veredicto.status == "bloqueado"
    assert _peor(veredicto, "moneda").resultado == "bloquea"


def test_moneda_firmada_distinta_del_cart_escala(escenario, payload_checkout, checkout, evaluar_con):
    """Sin moneda obligatoria en la ficha, la diferencia escala (no bloquea)."""
    intent = escenario("baseline")["intent"]
    del intent["hard"]["currency"]
    payload = payload_checkout("baseline", "cart_1")
    payload["currency"] = "EUR"
    veredicto = evaluar_con(
        "baseline", "cart_1", checkout("baseline", "cart_1", payload=payload),
        ficha=Ficha.desde_intent(intent),
    )
    assert veredicto.status == "escalar"
    assert _peor(veredicto, "moneda").resultado == "escalar"


def test_moneda_del_data_part_distinta_de_la_firmada_bloquea(checkout, evaluar_con):
    veredicto = evaluar_con("baseline", "cart_1", checkout("baseline", "cart_1", currency="EUR"))
    assert veredicto.status == "bloqueado"


# --- comercio firmado ---------------------------------------------------------------


def test_comercio_firmado_fuera_de_la_politica_bloquea(payload_checkout, checkout, evaluar_con):
    payload = payload_checkout("baseline", "cart_1", merchant_id="merchant_trucho")
    veredicto = evaluar_con("baseline", "cart_1", checkout("baseline", "cart_1", payload=payload))
    assert veredicto.status == "bloqueado"
    chequeo = _peor(veredicto, "comercio")
    assert chequeo.resultado == "bloquea" and chequeo.tipo == "politica"
    assert "merchant_trucho" not in chequeo.detalle  # texto del comercio: solo en observado
    assert chequeo.observado == "merchant_trucho"


def test_comercio_firmado_faltante_escala(payload_checkout, checkout, evaluar_con):
    payload = payload_checkout("baseline", "cart_1")
    del payload["merchant"]
    veredicto = evaluar_con("baseline", "cart_1", checkout("baseline", "cart_1", payload=payload))
    assert veredicto.status == "escalar"
    assert _peor(veredicto, "comercio").resultado == "escalar"


def test_comercio_firmado_aprobado_pero_distinto_del_verificado_escala(
    payload_checkout, checkout, evaluar_con
):
    payload = payload_checkout("baseline", "cart_1", merchant_id="merchant_2")
    veredicto = evaluar_con(
        "baseline", "cart_1", checkout("baseline", "cart_1", payload=payload),
        politica=Politica(comercios_aprobados=["merchant_1", "merchant_2"]),
    )
    assert veredicto.status == "escalar"
    assert _peor(veredicto, "comercio").resultado == "escalar"


# --- JWT ilegible ---------------------------------------------------------------------


def _jwt_crudo(encabezado, payload, firma: bytes = b"\x01" * 64) -> str:
    def parte(valor):
        if isinstance(valor, bytes):
            return _b64(valor)
        return _b64(json.dumps(valor).encode())

    return f"{parte(encabezado)}.{parte(payload)}.{_b64(firma)}"


_ENCABEZADO = {"alg": "ES256", "typ": "JWT"}

JWT_ILEGIBLES = {
    "vacio": "",
    "dos_segmentos": "a.b",
    "cuatro_segmentos": "a.b.c.d",
    "base64_invalido": "!!!.###.$$$",
    "encabezado_no_json": _jwt_crudo(b"no es json", {"id": "cart_1"}),
    "payload_no_json": _jwt_crudo(_ENCABEZADO, b"\xff\xfe no es json"),
    "payload_lista": _jwt_crudo(_ENCABEZADO, ["cart_1"]),
    "alg_none": _jwt_crudo({"alg": "none"}, {"id": "cart_1"}, firma=b""),
}


@pytest.mark.parametrize("jwt", JWT_ILEGIBLES.values(), ids=JWT_ILEGIBLES.keys())
def test_jwt_ilegible_bloquea(checkout, evaluar_con, jwt):
    veredicto = evaluar_con("baseline", "cart_1", checkout("baseline", "cart_1", jwt=jwt))
    assert veredicto.status == "bloqueado"
    chequeo = _peor(veredicto, "checkout_jwt")
    assert chequeo.resultado == "bloquea" and chequeo.tipo == "integridad"


def _sin_totals(payload):
    del payload["totals"]


def _totals_sin_total(payload):
    payload["totals"] = [{"type": "subtotal", "amount": 12000}]


def _dos_totales(payload):
    payload["totals"].append({"type": "total", "amount": 1})


def _total_texto(payload):
    payload["totals"][1]["amount"] = "doce mil"


def _total_bool(payload):
    payload["totals"][1]["amount"] = True


def _total_negativo(payload):
    payload["totals"][1]["amount"] = -12000


def _total_nan(payload):
    payload["totals"][1]["amount"] = float("nan")  # json.dumps escribe NaN


def _totals_no_lista(payload):
    payload["totals"] = {"type": "total", "amount": 12000}


@pytest.mark.parametrize(
    "romper",
    [_sin_totals, _totals_sin_total, _dos_totales, _total_texto, _total_bool,
     _total_negativo, _total_nan, _totals_no_lista],
)
def test_totals_ilegible_bloquea(payload_checkout, checkout, evaluar_con, romper):
    payload = payload_checkout("baseline", "cart_1")
    romper(payload)
    veredicto = evaluar_con("baseline", "cart_1", checkout("baseline", "cart_1", payload=payload))
    assert veredicto.status == "bloqueado"
    assert _peor(veredicto, "checkout_jwt").resultado == "bloquea"


def test_checkout_con_cart_elegido_desconocido_aplica_el_mandato_al_total(
    payload_checkout, checkout, evaluar_con
):
    """Sin cart evaluado, el total firmado igual se compara con el tope."""
    payload = _con_total(payload_checkout("A", "cart_4"), 18900)
    payload["id"] = "cart_99"
    visto = checkout("A", "cart_4", payload=payload, cart_id="cart_99")
    veredicto = evaluar_con("A", "cart_99", visto)
    assert veredicto.status == "bloqueado"
