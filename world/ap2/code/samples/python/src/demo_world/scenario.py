"""Escenarios del comercio malicioso.

Construye lo que el Merchant Agent responde en cada escenario a partir de
data/catalog.json (la verdad) y data/scenarios.json (lo que el comercio dice).

El comercio responde dos cosas:
- Carts de AP2 (ap2.models.cart.Cart): es lo único que ve el Shopping Agent
  del sample. El texto libre (y la inyección) viaja en `item_label`.
- `demo.offer_details`: los mismos productos en campos estructurados, tal como
  los DECLARA el comercio (pueden ser falsos). Es lo que usaría un feed de
  productos y lo que el Guard puede verificar contra el catálogo público.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"
OFFER_DETAILS_KEY = "demo.offer_details"
SCENARIO_DATA_KEY = "demo_scenario"
SCENARIO_FILE_NAME = "demo_scenario"
DEFAULT_SCENARIO = "baseline"


@lru_cache(maxsize=1)
def load_catalog() -> dict[str, Any]:
  return json.loads((DATA_DIR / "catalog.json").read_text())


@lru_cache(maxsize=1)
def load_scenarios() -> dict[str, Any]:
  return json.loads((DATA_DIR / "scenarios.json").read_text())


def products_by_sku() -> dict[str, dict[str, Any]]:
  return {p["sku"]: p for p in load_catalog()["products"]}


def intent() -> dict[str, Any]:
  return load_scenarios()["intent"]


def scenario_names() -> list[str]:
  return list(load_scenarios()["scenarios"].keys())


def get_scenario(name: str) -> dict[str, Any]:
  scenarios = load_scenarios()["scenarios"]
  if name not in scenarios:
    raise ValueError(
        f"Escenario desconocido: {name!r}. Opciones: {', '.join(scenarios)}"
    )
  return scenarios[name]


def _scenario_file() -> Path:
  temp_dir = os.environ.get("TEMP_DB_DIR") or str(Path.cwd() / ".temp-db")
  return Path(temp_dir) / SCENARIO_FILE_NAME


def set_active_scenario(name: str) -> Path:
  get_scenario(name)  # valida
  path = _scenario_file()
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(name)
  return path


def active_scenario(data_parts: list[dict[str, Any]] | None = None) -> str:
  """Resuelve el escenario activo.

  Prioridad: data part `demo_scenario` del mensaje A2A (lo usa el benchmark)
  > archivo $TEMP_DB_DIR/demo_scenario (set_scenario / dashboard)
  > variable DEMO_SCENARIO > 'baseline'.
  """
  for part in data_parts or []:
    if isinstance(part, dict) and part.get(SCENARIO_DATA_KEY):
      return str(part[SCENARIO_DATA_KEY])
  path = _scenario_file()
  if path.exists():
    name = path.read_text().strip()
    if name:
      return name
  return os.environ.get("DEMO_SCENARIO", DEFAULT_SCENARIO)


def build_offers(name: str) -> list[dict[str, Any]]:
  """Devuelve las ofertas del escenario, en el orden en que responde el comercio.

  Cada oferta trae:
    cart_id, sku, item_label (texto que ve el agente), amount, currency,
    declared (campos estructurados tal como los declara el comercio),
    truth (el producto del catálogo público), injected (bool).
  """
  scenario = get_scenario(name)
  catalog = products_by_sku()
  injections = load_scenarios()["injections"]

  offers = []
  for index, spec in enumerate(scenario["offers"], start=1):
    truth = catalog[spec["sku"]]
    declared = {
        "sku": truth["sku"],
        "title": truth["title"],
        "brand": truth["brand"],
        "category": truth["category"],
        "use": truth["use"],
        "price": spec.get("quoted_price", truth["price"]),
        "currency": truth["currency"],
        "stock": truth["stock"],
        "sizes": truth["sizes"],
        "description": truth["description"],
    }
    declared.update(spec.get("declared_overrides", {}))

    injection_text = None
    if spec.get("injection"):
      injection_text = injections[spec["injection"]]
      declared["description"] = f"{declared['description']} {injection_text}"

    item_label = f"{declared['title']}. {declared['description']}"

    offers.append({
        "cart_id": f"cart_{index}",
        "sku": truth["sku"],
        "item_label": item_label,
        "amount": float(declared["price"]),
        "currency": declared["currency"],
        "declared": declared,
        "truth": truth,
        "injected": injection_text is not None,
    })
  return offers


def offer_details_payload(offers: list[dict[str, Any]]) -> list[dict[str, Any]]:
  """Campos estructurados que el comercio declara (para el data part)."""
  return [{"cart_id": o["cart_id"], **o["declared"]} for o in offers]
