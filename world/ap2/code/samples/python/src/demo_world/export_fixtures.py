"""Exporta los escenarios a fixtures/ para que Dev 2 y Dev 3 trabajen sin
levantar los agentes.

  uv run --package ap2-samples python -m demo_world.export_fixtures

Genera, por escenario, fixtures/scenario_<nombre>.json con:
  intent         el pedido del usuario y sus requisitos
  carts          lo que ve el Shopping Agent (Cart de AP2, con la inyección)
  offer_details  campos estructurados que DECLARA el comercio (pueden mentir)
  attack         qué intenta el atacante
  expected       qué debería pasar (mejor opción real, qué frena AP2)
Y además fixtures/public_catalog.json (la verdad del catálogo público).
"""

import argparse
import json
from pathlib import Path

from demo_world import ap2_check, scenario

REPO_ROOT = Path(__file__).resolve().parents[7]


def main() -> None:
  parser = argparse.ArgumentParser()
  parser.add_argument("--out", default=str(REPO_ROOT / "fixtures"))
  args = parser.parse_args()
  out = Path(args.out)
  out.mkdir(parents=True, exist_ok=True)

  (out / "public_catalog.json").write_text(
      json.dumps(scenario.load_catalog(), indent=2, ensure_ascii=False) + "\n"
  )

  for name in scenario.scenario_names():
    info = scenario.get_scenario(name)
    offers = scenario.build_offers(name)
    target = (info.get("attack") or {}).get("target_sku")
    target_offer = next((o for o in offers if o["sku"] == target), None)
    fixture = {
        "scenario": name,
        "title": info["title"],
        "description": info["description"],
        "intent": scenario.intent(),
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
        "expected": {
            "best_sku": scenario.intent()["expected_best_sku"],
            "best_cart_id": next(
                o["cart_id"]
                for o in offers
                if o["sku"] == scenario.intent()["expected_best_sku"]
            ),
            "attack_target_cart_id": (
                target_offer["cart_id"] if target_offer else None
            ),
            "ap2_blocks_attack_target": (
                bool(ap2_check.ap2_violations(target_offer))
                if target_offer
                else None
            ),
        },
    }
    path = out / f"scenario_{name}.json"
    path.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n")
    print(f"  {path}")


if __name__ == "__main__":
  main()
