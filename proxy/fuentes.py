"""De dónde saca el proxy la ficha, el catálogo público y el chequeo de AP2.

Todo con respaldo en `fixtures/` para que la demo no dependa de que esté
levantado todo. `demo_world` se importa en solo lectura (no se modifica).
"""

from __future__ import annotations

import json
import logging
import re
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


_NOMBRE_ESCENARIO = re.compile(r"[A-Za-z0-9_-]{1,40}")


def intent_del_escenario(nombre: str | None) -> dict[str, Any] | None:
    """La ficha confirmada de la demo para ese escenario, o None.

    1. `demo_world`: el `intent` propio del escenario (si world/ lo define)
       o, si el escenario existe, el `intent` global de scenarios.json.
    2. Si no: `fixtures/scenario_<nombre>.json` y, mientras world/ no tenga
       el escenario de computadoras, `fixtures/etapa2/<nombre>.json`.

    None si el escenario no se conoce: con un comercio real no hay
    `demo_scenario` y manda la ficha que armó el agente con el pedido.
    El nombre viene en datos A2A: solo se acepta un identificador simple.
    """
    if not isinstance(nombre, str) or not _NOMBRE_ESCENARIO.fullmatch(nombre):
        return None
    try:
        from demo_world import scenario

        if nombre in scenario.scenario_names():
            propio = scenario.get_scenario(nombre).get("intent")
            return propio if isinstance(propio, dict) else scenario.intent()
    except Exception as e:  # noqa: BLE001 - cualquier falla cae a los fixtures
        logger.warning("demo_world no disponible (%s); intent desde fixtures/", e)
    for ruta in (FIXTURES / f"scenario_{nombre}.json", FIXTURES / "etapa2" / f"{nombre}.json"):
        if ruta.is_file():
            try:
                intent = json.loads(ruta.read_text("utf-8")).get("intent")
            except (OSError, ValueError, AttributeError):
                return None
            return intent if isinstance(intent, dict) else None
    return None


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
    client: httpx.AsyncClient, catalogo_url: str
) -> tuple[dict[str, dict], str | None, str]:
    """`GET <catalogo>/products` → (sku -> producto, merchant.id, fuente).

    `fuente` es "http" o "fixture": si el catálogo no responde (o responde
    algo ilegible), usa `fixtures/public_catalog.json` y lo dice.
    """
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
        logger.warning("Catálogo público no disponible (%s); uso fixtures/", e)
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
