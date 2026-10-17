"""Cambia el escenario activo del comercio sin reiniciarlo.

  uv run --package ap2-samples python -m demo_world.set_scenario B

El comercio lee el escenario en cada búsqueda desde $TEMP_DB_DIR/demo_scenario.
"""

import sys

from demo_world import scenario


def main() -> None:
  if len(sys.argv) != 2:
    print(f"Uso: set_scenario <{'|'.join(scenario.scenario_names())}>")
    sys.exit(2)
  name = sys.argv[1]
  path = scenario.set_active_scenario(name)
  info = scenario.get_scenario(name)
  print(f"Escenario activo: {name} — {info['title']} ({path})")


if __name__ == "__main__":
  main()
