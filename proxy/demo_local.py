"""Las fuentes de la demo local: lo único del proxy que lee `demo_world`.

El motor (lo que se despliega, `[tool.kaiai.motor]` en `pyproject.toml`) no
importa la simulación. Lo que el proxy necesita de la demo entra por
inyección: `create_app(..., demo=FuentesDemo())` (lo pasa `python -m proxy`)
y, sin `demo`, no hay ficha de la demo (como `PROXY_FICHA_DEMO=0`) ni chequeo
del SDK de AP2 (`ap2_violaciones` va en null). El contrato que usa el proxy
es `proxy.fuentes.FuentesDemoProtocolo`.

- `intent_del_escenario`: la ficha confirmada de un escenario fijo
  (`scenarios.json` o, sin `demo_world`, `fixtures/`).
- `violaciones_ap2`: lo que frenaría el SDK de AP2 con el tope de la compra
  (`demo_world.ap2_check`); es solo para mostrar en el dashboard.
- `cargar_intent`: el pedido global de la demo de zapatillas.

`demo_world` se importa en solo lectura (no se modifica) y al usarlo: si no
está, se cae a los fixtures. Este archivo queda fuera del motor (y del
despliegue).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from proxy.config import REPO_ROOT

logger = logging.getLogger("proxy.demo_local")

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

# Los escenarios de zapatillas de la etapa 1: en scenarios.json no tienen
# `intent` propio y usan el pedido global. Ningún otro escenario cae a ese
# pedido (un test lo verifica contra scenarios.json).
ESCENARIOS_DEL_PEDIDO_GLOBAL = frozenset({"baseline", "A", "B", "B2", "C"})
# El comercio general arma las ofertas con el pedido del usuario: nunca hay
# ficha de la demo, aunque el preset traiga un `intent`.
TIPO_GENERAL = "general"


def intent_del_escenario(nombre: str | None) -> dict[str, Any] | None:
    """La ficha confirmada de la demo para ese escenario, o None.

    1. `demo_world`: el `intent` propio del escenario (si world/ lo define)
       o, solo para los de zapatillas (`ESCENARIOS_DEL_PEDIDO_GLOBAL`), el
       `intent` global de scenarios.json.
    2. Si no: `fixtures/scenario_<nombre>.json` y `fixtures/etapa2/<nombre>.json`
       (cada fixture trae su `intent`).

    None si el escenario no se conoce (con un comercio real no hay
    `demo_scenario` y manda la ficha que confirmó el usuario), si es del
    comercio general (`"tipo": "general"`) o si no tiene `intent` propio y no
    es de zapatillas: nunca se usa el pedido de otro escenario.
    El nombre viene en datos A2A: solo se acepta un identificador simple.
    """
    if not isinstance(nombre, str) or not _NOMBRE_ESCENARIO.fullmatch(nombre):
        return None
    try:
        from demo_world import scenario

        if nombre in scenario.scenario_names():
            info = scenario.get_scenario(nombre)
            if nombre in ESCENARIOS_DEL_PEDIDO_GLOBAL and _sin_intent_propio(info):
                return scenario.intent()
            return _intent_propio(info)
    except Exception as e:  # noqa: BLE001 - cualquier falla cae a los fixtures
        logger.warning("demo_world no disponible (%s); intent desde fixtures/", e)
    for ruta in (FIXTURES / f"scenario_{nombre}.json", FIXTURES / "etapa2" / f"{nombre}.json"):
        if ruta.is_file():
            try:
                datos = json.loads(ruta.read_text("utf-8"))
            except (OSError, ValueError):
                return None
            return _intent_propio(datos)
    return None


def _sin_intent_propio(info: Any) -> bool:
    return isinstance(info, dict) and "tipo" not in info and not isinstance(
        info.get("intent"), dict
    )


def _intent_propio(info: Any) -> dict[str, Any] | None:
    """El `intent` del escenario (o del fixture), salvo en el comercio general."""
    if not isinstance(info, dict) or info.get("tipo") == TIPO_GENERAL:
        return None
    intent = info.get("intent")
    return intent if isinstance(intent, dict) else None


def violaciones_ap2(cart: dict[str, Any], intent: dict[str, Any] | None) -> list[str] | None:
    """Lo que frena el SDK de AP2 para este cart con el tope de su compra
    ([] = OK). None si no se pudo, o si la compra no tiene tope (`intent`
    None, ver `proxy.fuentes.intent_ap2`): nunca se usa el tope de otro pedido."""
    if intent is None:
        return None
    try:
        from demo_world.ap2_check import ap2_violations

        return list(ap2_violations(cart, intent))
    except Exception as e:  # noqa: BLE001 - es solo para mostrar
        logger.warning("No se pudo correr ap2_check para %s: %s", cart.get("cart_id"), e)
        return None


class FuentesDemo:
    """Lo que el proxy le pide a la demo local (`FuentesDemoProtocolo`).

    La pasa `python -m proxy`; los tests que prueban con las fichas de los
    escenarios fijos, también. Sin estado: cada llamada lee `demo_world` (o
    los fixtures) de nuevo, como antes de la separación.
    """

    nombre = "demo_world"

    def intent_del_escenario(self, nombre: str | None) -> dict[str, Any] | None:
        return intent_del_escenario(nombre)

    def violaciones_ap2(
        self, cart: dict[str, Any], intent: dict[str, Any] | None
    ) -> list[str] | None:
        return violaciones_ap2(cart, intent)
