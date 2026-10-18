"""De dónde saca el proxy la ficha, el catálogo público y el chequeo de AP2.

El catálogo público no siempre existe: sin él, el Guard evalúa sin catálogo
(todo queda "solo declarado" y escala). El respaldo en `fixtures/` es solo
para la demo offline y hay que pedirlo (`CATALOGO_RESPALDO_FIXTURE=1`).
`demo_world` se importa en solo lectura (no se modifica).
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
    lista = catalogo.get("products")
    productos = {
        p["sku"]: p
        for p in (lista if isinstance(lista, list) else [])
        if isinstance(p, dict) and isinstance(p.get("sku"), str)
    }
    merchant = catalogo.get("merchant") or {}
    comercio_id = merchant.get("id") if isinstance(merchant, dict) else None
    return productos, comercio_id if isinstance(comercio_id, str) else None


MAX_CATALOGO_BYTES = 2 * 1024 * 1024


async def cargar_catalogo(
    client: httpx.AsyncClient,
    catalogo_url: str | None,
    respaldo_fixture: bool = False,
) -> tuple[dict[str, dict], str | None, str]:
    """`GET <catalogo>/products` → (sku -> producto, merchant.id, fuente).

    `fuente` es "http", "fixture" o "ninguno":
    - sin `catalogo_url` (el comercio no publica catálogo) no se consulta nada;
    - si el catálogo no responde (o responde algo ilegible) se evalúa sin
      catálogo, salvo que se pida el respaldo de `fixtures/` para la demo.
    """
    if not catalogo_url:
        return {}, None, "ninguno"
    try:
        resp = await client.get(f"{catalogo_url}/products", timeout=5.0)
        resp.raise_for_status()
        if len(resp.content) > MAX_CATALOGO_BYTES:
            raise ValueError("el catálogo es demasiado grande")
        datos = json.loads(resp.content, parse_constant=_sin_constantes)
        if not isinstance(datos, dict):
            raise ValueError("el catálogo no devolvió un objeto")
        productos, comercio_id = _indexar(datos)
        return productos, comercio_id, "http"
    except (httpx.HTTPError, ValueError, RecursionError) as e:
        if not respaldo_fixture:
            logger.warning("Catálogo público no disponible (%s); evalúo sin catálogo", e)
            return {}, None, "ninguno"
        logger.warning("Catálogo público no disponible (%s); uso fixtures/ (demo)", e)
        datos = json.loads((FIXTURES / "public_catalog.json").read_text("utf-8"))
        productos, comercio_id = _indexar(datos)
        return productos, comercio_id, "fixture"


def _sin_constantes(nombre: str) -> None:
    raise ValueError(f"constante JSON no estándar en el catálogo: {nombre}")


def violaciones_ap2(cart: dict[str, Any]) -> list[str] | None:
    """Lo que frena el SDK de AP2 para este cart ([] = OK). None si no se pudo."""
    try:
        from demo_world.ap2_check import ap2_violations

        return list(ap2_violations(cart))
    except Exception as e:  # noqa: BLE001 - es solo para mostrar
        logger.warning("No se pudo correr ap2_check para %s: %s", cart.get("cart_id"), e)
        return None
