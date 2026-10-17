"""De dónde saca el proxy la ficha, el catálogo público y el chequeo de AP2.

Todo con respaldo en `fixtures/` para que la demo no dependa de que esté
levantado todo. `demo_world` se importa en solo lectura (no se modifica).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from proxy.config import REPO_ROOT

logger = logging.getLogger("proxy.fuentes")

FIXTURES = REPO_ROOT / "fixtures"


def cargar_intent() -> dict[str, Any]:
    """`demo_world.scenario.intent()`; si no se puede, el del fixture baseline."""
    try:
        from demo_world import scenario

        return scenario.intent()
    except Exception as e:  # noqa: BLE001 - cualquier falla cae al fixture
        logger.warning("demo_world no disponible (%s); intent desde fixtures/", e)
        datos = json.loads((FIXTURES / "scenario_baseline.json").read_text("utf-8"))
        return datos["intent"]


def _indexar(catalogo: dict[str, Any]) -> tuple[dict[str, dict], str | None]:
    productos = {
        p["sku"]: p
        for p in catalogo.get("products") or []
        if isinstance(p, dict) and "sku" in p
    }
    merchant = catalogo.get("merchant") or {}
    return productos, merchant.get("id") if isinstance(merchant, dict) else None


async def cargar_catalogo(
    client: httpx.AsyncClient, catalogo_url: str
) -> tuple[dict[str, dict], str | None]:
    """`GET <catalogo>/products` → (sku -> producto, merchant.id).

    Si el catálogo no responde, usa `fixtures/public_catalog.json`.
    """
    try:
        resp = await client.get(f"{catalogo_url}/products", timeout=5.0)
        resp.raise_for_status()
        datos = resp.json()
        if not isinstance(datos, dict):
            raise ValueError("el catálogo no devolvió un objeto")
        return _indexar(datos)
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("Catálogo público no disponible (%s); uso fixtures/", e)
        datos = json.loads((FIXTURES / "public_catalog.json").read_text("utf-8"))
        return _indexar(datos)


def violaciones_ap2(cart: dict[str, Any]) -> list[str] | None:
    """Lo que frena el SDK de AP2 para este cart ([] = OK). None si no se pudo."""
    try:
        from demo_world.ap2_check import ap2_violations

        return list(ap2_violations(cart))
    except Exception as e:  # noqa: BLE001 - es solo para mostrar
        logger.warning("No se pudo correr ap2_check para %s: %s", cart.get("cart_id"), e)
        return None
