"""Exporta los escenarios a fixtures/ para que Dev 2 y Dev 3 trabajen sin
levantar los agentes.

  uv run --package ap2-samples python -m demo_world.export_fixtures

Genera, por escenario, un JSON con:
  intent         el pedido del usuario y sus requisitos (el del escenario o el global)
  carts          lo que ve el Shopping Agent (Cart de AP2, con la inyección)
  offer_details  campos estructurados que DECLARA el comercio (pueden mentir)
  attack         qué intenta el atacante
  expected       qué debería pasar (mejor opción real, qué frena AP2)

- La demo de zapatillas va a fixtures/scenario_<nombre>.json (el dashboard
  lista esos).
- Los escenarios con "fixture_dir": "etapa2" (computadoras, notebooks y
  auriculares) van a fixtures/etapa2/<nombre>.json.
- fixtures/public_catalog.json: la verdad del catálogo público (data/catalog.json
  más los productos publicados del comercio general).
- fixtures/fabricante/<gtin>.json: las fichas de los fabricantes en formato
  Icecat (datos de prueba, rotulados en "_fixture": no son de Icecat), las de
  data/manufacturers.json y las del comercio general (data/productos.json).
- fixtures/general/<preset>.json: lo que responde cada preset del comercio
  general a unas búsquedas de ejemplo (`BUSQUEDAS_FIXTURES`), con un hoy y un
  contexto fijos para que el archivo no cambie cada día.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

from demo_world import ap2_check, general, scenario

REPO_ROOT = Path(__file__).resolve().parents[7]

FIXTURE_NOTE = (
    "Fixture de prueba en formato Icecat: NO son datos de Icecat. Lo genera "
    "world/ (demo_world.export_fixtures) desde data/manufacturers.json."
)
FIXTURE_NOTE_GENERAL = (
    "Fixture de prueba en formato Icecat: NO son datos de Icecat. Lo genera "
    "world/ (demo_world.export_fixtures) desde data/productos.json (comercio general)."
)
ETAPA2_NOTE = (
    "Generado por world/ (demo_world.export_fixtures) desde data/scenarios.json: "
    "no editar a mano. Va en fixtures/etapa2/ para no sumarse a la lista de "
    "escenarios de la demo de zapatillas."
)
GENERAL_NOTE = (
    "Generado por world/ (demo_world.export_fixtures) con demo_world.general y "
    "data/productos.json: no editar a mano. Lo que responde el preset a cada "
    "búsqueda, con 'hoy' y 'contexto_id' fijos (en vivo, las fechas son relativas "
    "a hoy). 'inyectadas': los cart_id con una inyección."
)
HOY_FIXTURES = date(2026, 10, 18)  # un domingo: "para el lunes" es el 19
CONTEXTO_FIXTURES = "ctx-fixture"
BUSQUEDAS_FIXTURES = (
    "Quiero un monitor de 27 pulgadas para diseño, hasta 400 USD, para el viernes",
    "20 computadoras para el lunes, hasta 20000 USD",
    "20 laptops delivered by Monday, up to 20000 USD",
    "auriculares inalámbricos con cancelación de ruido, al menos 20 horas de batería, hasta 250 USD",
    "zapatillas de básquet talle 42 hasta 150 USD",
)


def _write(path: Path, data: Any) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  # LF siempre: en Windows write_text pondría CRLF y cambiaría todo el archivo.
  path.write_text(
      json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
  )
  print(f"  {path}")


def _expected(name: str, info: dict[str, Any], offers: list[dict[str, Any]]) -> dict[str, Any]:
  intent = scenario.intent_for(name)
  target = (info.get("attack") or {}).get("target_sku")
  target_offer = next((o for o in offers if o["sku"] == target), None)
  attack = {
      "attack_target_cart_id": target_offer["cart_id"] if target_offer else None,
      "ap2_blocks_attack_target": (
          bool(ap2_check.ap2_violations(target_offer, intent)) if target_offer else None
      ),
  }
  if "expected" in info:  # computadoras: lo que se espera del flujo de etapa 2
    # Con un ataque, el objetivo y lo que dice AP2 se calculan como en los demás.
    return {**info["expected"], **attack} if target_offer else info["expected"]
  best_sku = intent["expected_best_sku"]
  return {
      "best_sku": best_sku,
      "best_cart_id": next(o["cart_id"] for o in offers if o["sku"] == best_sku),
      **attack,
  }


def scenario_fixture(name: str) -> tuple[str | None, dict[str, Any]]:
  """(subcarpeta, contenido) del fixture de un escenario."""
  info = scenario.get_scenario(name)
  offers = scenario.build_offers(name)
  fixture: dict[str, Any] = {}
  subdir = info.get("fixture_dir")
  if subdir:
    fixture["_comment"] = f"{ETAPA2_NOTE} {info['comment']}" if info.get("comment") else ETAPA2_NOTE
  fixture.update({
      "scenario": name,
      "title": info["title"],
      "description": info["description"],
      "intent": scenario.intent_for(name),
  })
  if "contexto_demo" in info:
    fixture["contexto_demo"] = info["contexto_demo"]
  fixture.update({
      "carts": [
          {
              "cart_id": o["cart_id"],
              "item_label": o["item_label"],
              "amount": o["amount"],
              "currency": o["currency"],
          }
          for o in offers
      ],
      "offer_details": scenario.offer_details_payload(offers),
      "attack": info.get("attack"),
      "expected": _expected(name, info, offers),
  })
  return subdir, fixture


def general_fixture(name: str) -> dict[str, Any]:
  """Lo que responde un preset del comercio general a las búsquedas de ejemplo."""
  info = scenario.get_scenario(name)
  busquedas = []
  for texto in BUSQUEDAS_FIXTURES:
    offers = scenario.build_offers(name, texto, contexto_id=CONTEXTO_FIXTURES, hoy=HOY_FIXTURES)
    busquedas.append({
        "catalog_search": texto,
        "carts": [
            {"cart_id": o["cart_id"], "item_label": o["item_label"], "amount": o["amount"],
             "currency": o["currency"]}
            for o in offers
        ],
        "offer_details": scenario.offer_details_payload(offers),
        "inyectadas": [o["cart_id"] for o in offers if o["injected"]],
    })
  return {
      "_comment": GENERAL_NOTE,
      "scenario": name,
      "title": info["title"],
      "description": info["description"],
      "comportamiento": info["comportamiento"],
      "attack": info.get("attack"),
      "hoy": HOY_FIXTURES.isoformat(),
      "contexto_id": CONTEXTO_FIXTURES,
      "busquedas": busquedas,
  }


def icecat_fixture(gtin: str, product: dict[str, Any]) -> dict[str, Any]:
  """La ficha del fabricante con la forma de la respuesta de live.icecat.biz/api."""
  groups: dict[str, list[dict[str, Any]]] = {}
  for feature in product["features"]:
    unit = feature.get("unit") or ""
    groups.setdefault(feature["group"], []).append({
        "Type": feature["type"],
        "Value": feature["value"],
        "PresentationValue": f"{feature['value']} {unit}".strip(),
        "Feature": {
            "Name": {"Value": feature["name"], "Language": "EN"},
            "Measure": {"Signs": {"_": unit}},
        },
    })
  return {
      "_fixture": FIXTURE_NOTE_GENERAL if gtin in general.gtins() else FIXTURE_NOTE,
      "msg": "OK",
      "data": {
          "GeneralInfo": {
              "Title": product["title"],
              "Brand": product["brand"],
              "ProductName": product["product_name"],
              "GTIN": [gtin],
              "Category": {
                  "CategoryID": product["category"]["id"],
                  "Name": {"Value": product["category"]["name"], "Language": "EN"},
              },
          },
          "FeaturesGroups": [
              {"FeatureGroup": {"Name": {"Value": group, "Language": "EN"}}, "Features": features}
              for group, features in groups.items()
          ],
      },
  }


def main(argv: list[str] | None = None) -> None:
  parser = argparse.ArgumentParser()
  parser.add_argument("--out", default=str(REPO_ROOT / "fixtures"))
  args = parser.parse_args(argv)
  out = Path(args.out)
  out.mkdir(parents=True, exist_ok=True)

  _write(out / "public_catalog.json", scenario.public_catalog())

  for name in scenario.fixed_names():
    subdir, fixture = scenario_fixture(name)
    path = out / subdir / f"{name}.json" if subdir else out / f"scenario_{name}.json"
    _write(path, fixture)

  for name in scenario.general_names():
    _write(out / "general" / f"{name}.json", general_fixture(name))

  for gtin, product in scenario.load_manufacturers()["products"].items():
    _write(out / "fabricante" / f"{gtin}.json", icecat_fixture(gtin, product))


if __name__ == "__main__":
  main()
