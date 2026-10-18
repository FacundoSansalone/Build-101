"""Escenarios del comercio malicioso.

Construye lo que el Merchant Agent responde en cada escenario a partir de
data/catalog.json (la verdad que el comercio publica), data/unlisted.json (lo
que vende sin publicar) y data/scenarios.json (lo que el comercio dice). Las
fichas de los fabricantes por GTIN están en data/manufacturers.json.

Los escenarios de otras categorías (notebooks, auriculares) y el de
computadoras de la etapa 2 traen su propio `intent` (con `attributes` y
`google_product_category`); los de zapatillas usan el global.

El comercio responde dos cosas:
- Carts de AP2 (ap2.models.cart.Cart): es lo único que ve el Shopping Agent
  del sample. El texto libre (y la inyección) viaja en `item_label`.
- `demo.offer_details`: los mismos productos en campos estructurados, tal como
  los DECLARA el comercio (pueden ser falsos). Es lo que usaría un feed de
  productos y lo que el Guard puede verificar contra el catálogo público.
"""

from __future__ import annotations

import copy
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
  return json.loads((DATA_DIR / "catalog.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_unlisted() -> dict[str, Any]:
  """Productos que el comercio vende y NO publica en su catálogo público."""
  return json.loads((DATA_DIR / "unlisted.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_manufacturers() -> dict[str, Any]:
  """Fichas de los fabricantes por GTIN (otra fuente de verdad)."""
  return json.loads((DATA_DIR / "manufacturers.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_scenarios() -> dict[str, Any]:
  data = json.loads((DATA_DIR / "scenarios.json").read_text(encoding="utf-8"))
  # Un escenario puede reusar el intent de otro ("intent_from"): queda copiado
  # en "intent" para quien lo lea directo (proxy/fuentes.py).
  scenarios = data["scenarios"]
  for info in scenarios.values():
    if "intent_from" in info and "intent" not in info:
      info["intent"] = scenarios[info["intent_from"]]["intent"]
  return data


def products_by_sku() -> dict[str, dict[str, Any]]:
  """Los productos del catálogo PÚBLICO (lo que sirve :8011)."""
  return {p["sku"]: p for p in load_catalog()["products"]}


def all_products_by_sku() -> dict[str, dict[str, Any]]:
  """La verdad de todo lo que vende el comercio: el catálogo público más lo
  que no publica. Sirve para armar las ofertas y para el benchmark."""
  return {**{p["sku"]: p for p in load_unlisted()["products"]}, **products_by_sku()}


def manufacturer_attributes(gtin: str) -> dict[str, dict[str, Any]]:
  """Atributos de la ficha del fabricante, con el nombre canónico."""
  product = load_manufacturers()["products"].get(gtin) or {}
  attributes = {}
  for feature in product.get("features", []):
    if "atributo" not in feature:
      continue
    value = feature["value"]
    if feature.get("type") == "multi_dropdown":
      value = [v.strip() for v in value.split(",") if v.strip()]
    attributes[feature["atributo"]] = {"valor": value, "unidad": feature.get("unit")}
  return attributes


def intent() -> dict[str, Any]:
  """El pedido global (zapatillas): el de los escenarios sin intent propio."""
  return load_scenarios()["intent"]


def intent_for(name: str) -> dict[str, Any]:
  """El pedido de un escenario: el suyo (otras categorías) o el global."""
  return get_scenario(name).get("intent") or intent()


def scenario_names() -> list[str]:
  return list(load_scenarios()["scenarios"].keys())


def benchmark_names() -> list[str]:
  """Los escenarios que corre el benchmark por defecto (no los de etapa 2 que
  no tienen un objetivo de ataque ni una mejor opción literal)."""
  return [
      name
      for name, info in load_scenarios()["scenarios"].items()
      if info.get("benchmark", True)
  ]


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
  catalog = all_products_by_sku()
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
    }
    # Otras categorías: GTIN, categoría de Google y atributos (schema.org).
    for key in ("gtin", "categoria_taxonomia"):
      if key in truth:
        declared[key] = truth[key]
    if "atributos" in truth:
      declared["atributos"] = copy.deepcopy(truth["atributos"])
    # Etapa 2: lo que el comercio se compromete a entregar.
    for key in ("quantity", "deliveries"):
      if key in spec:
        declared[key] = copy.deepcopy(spec[key])
    declared["description"] = truth["description"]

    overrides = copy.deepcopy(spec.get("declared_overrides", {}))
    if "atributos" in overrides:  # se pisan solo los atributos indicados
      declared["atributos"] = {**declared.get("atributos", {}), **overrides.pop("atributos")}
    declared.update(overrides)

    injection_text = None
    if spec.get("injection"):
      injection_text = injections[spec["injection"]]
      declared["description"] = f"{declared['description']} {injection_text}"

    item_label = spec.get("item_label") or f"{declared['title']}. {declared['description']}"

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
