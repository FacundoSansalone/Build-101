"""Fixtures compartidas de los tests de guard/ (solo archivos de fixtures/).

Los tests pueden usar SKUs y cart_ids de la demo; la lógica del Guard no.
"""

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts import Ficha, OfertasVistas

RAIZ = Path(__file__).resolve().parents[2]
FIXTURES = RAIZ / "fixtures"


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
