"""Fixtures compartidas de los tests de guard/ (solo archivos de fixtures/).

Los tests pueden usar SKUs y cart_ids de la demo; la lógica del Guard no.
"""

import base64
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from contracts import CheckoutVisto, Ficha, OfertasVistas

RAIZ = Path(__file__).resolve().parents[2]
FIXTURES = RAIZ / "fixtures"

# Igual que create_checkout() del comercio del sample (merchant_agent/tools.py).
ENCABEZADO_CHECKOUT = {"alg": "ES256", "typ": "JWT", "kid": "merchant-key-1"}


@pytest.fixture
def ahora() -> datetime:
    """Fecha fija para que el recibo (y por lo tanto el Veredicto) sea comparable."""
    return datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)


def _leer(nombre: str) -> dict:
    ruta = FIXTURES / f"scenario_{nombre}.json"
    return json.loads(ruta.read_text(encoding="utf-8"))


@pytest.fixture
def escenario():
    """Devuelve una copia del fixture del escenario (se puede modificar)."""

    def _cargar(nombre: str) -> dict:
        return copy.deepcopy(_leer(nombre))

    return _cargar


@pytest.fixture
def catalogo() -> dict[str, dict]:
    """Catálogo público como sku -> producto (lo que recibe evaluar())."""
    datos = json.loads((FIXTURES / "public_catalog.json").read_text(encoding="utf-8"))
    return {p["sku"]: p for p in datos["products"]}


@pytest.fixture
def vistas(escenario):
    """OfertasVistas del escenario, con el comercio del catálogo público."""

    def _armar(nombre: str, comercio_id: str | None = "merchant_1") -> OfertasVistas:
        datos = escenario(nombre)
        return OfertasVistas(
            carts=datos["carts"],
            offer_details=datos["offer_details"],
            comercio_id=comercio_id,
        )

    return _armar


@pytest.fixture
def ficha(escenario):
    def _armar(nombre: str = "baseline") -> Ficha:
        return Ficha.desde_intent(escenario(nombre)["intent"])

    return _armar


@pytest.fixture
def objetivo(escenario):
    """cart_id que eligiría el atacante (o el mejor en baseline)."""

    def _cart(nombre: str) -> str:
        esperado = escenario(nombre)["expected"]
        return esperado["attack_target_cart_id"] or esperado["best_cart_id"]

    return _cart


# ---------------------------------------------------------------------------
# ap2.checkout firmado (como create_checkout() del comercio del sample)
# ---------------------------------------------------------------------------


def _b64url(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode("ascii")


def _crear_jwt(encabezado: dict, payload: dict, clave: ec.EllipticCurvePrivateKey) -> str:
    """Copia de `_create_jwt()` del comercio: JWS compacto ES256 (r || s)."""
    h = _b64url(json.dumps(encabezado, separators=(",", ":")).encode())
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    der = clave.sign(f"{h}.{p}".encode("ascii"), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return f"{h}.{p}.{_b64url(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


@pytest.fixture(scope="session")
def clave_comercio() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
def firmar_jwt(clave_comercio):
    """Firma un payload como el comercio (encabezado ES256 por defecto)."""

    def _firmar(payload: dict, encabezado: dict | None = None) -> str:
        return _crear_jwt(encabezado or ENCABEZADO_CHECKOUT, payload, clave_comercio)

    return _firmar


@pytest.fixture
def hash_jwt():
    """checkout_hash como lo calcula el comercio: b64url(sha256(jwt))."""
    return lambda jwt: _b64url(hashlib.sha256(jwt.encode()).digest())


@pytest.fixture
def payload_checkout(escenario):
    """Payload del checkout_jwt para un cart del escenario (Checkout de AP2)."""

    def _armar(nombre: str, cart_id: str, merchant_id: str = "merchant_1") -> dict:
        cart = next(c for c in escenario(nombre)["carts"] if c["cart_id"] == cart_id)
        centavos = int(round(cart["amount"] * 100))
        totales = [{"type": "subtotal", "amount": centavos}, {"type": "total", "amount": centavos}]
        return {
            "id": cart_id,
            "merchant": {
                "id": merchant_id,
                "name": "Generic Merchant",
                "website": "https://demo-merchant.example",
            },
            "line_items": [
                {
                    "id": "li_prueba",
                    "item": {"id": cart_id, "title": cart["item_label"], "price": centavos},
                    "quantity": 1,
                    "totals": copy.deepcopy(totales),
                }
            ],
            "status": "incomplete",
            "currency": cart["currency"],
            "totals": totales,
            "links": [{"type": "privacy_policy", "url": "https://demo-merchant.example/privacy"}],
            "iat": 1_792_238_400,
            "exp": 1_792_242_000,
        }

    return _armar


@pytest.fixture
def checkout(escenario, payload_checkout, firmar_jwt, hash_jwt):
    """Data part ap2.checkout coherente; `payload`, `jwt` o campos lo cambian."""

    def _armar(
        nombre: str,
        cart_id: str,
        /,
        payload: dict | None = None,
        jwt: str | None = None,
        **data_part,
    ) -> CheckoutVisto:
        cart = next(c for c in escenario(nombre)["carts"] if c["cart_id"] == cart_id)
        if jwt is None:
            jwt = firmar_jwt(payload if payload is not None else payload_checkout(nombre, cart_id))
        datos = {
            "cart_id": cart_id,
            "checkout_jwt": jwt,
            "checkout_hash": hash_jwt(jwt),
            "item_label": cart["item_label"],
            "amount": cart["amount"],
            "amount_cents": int(round(cart["amount"] * 100)),
            "currency": cart["currency"],
        }
        datos.update(data_part)
        return CheckoutVisto.model_validate(datos)

    return _armar
