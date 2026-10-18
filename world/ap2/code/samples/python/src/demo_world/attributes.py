"""Comparación de atributos para la VERDAD del benchmark (independiente del Guard).

El benchmark mide al Guard: por eso world/ no usa el código del Guard para
decidir qué cumple el producto real. Esto alcanza para los datos de la demo:
números con unidades (GB/TB/MB, in/cm, kg/g, h), booleanos (Y/N, true/false),
textos y conjuntos (`incluye`). Lo que no se puede comparar cuenta como que
no cumple.
"""

from __future__ import annotations

from typing import Any

_UNITS: dict[str, tuple[str, float]] = {
    "mb": ("data", 1e6), "gb": ("data", 1e9), "tb": ("data", 1e12),
    "mm": ("length", 1e-3), "cm": ("length", 1e-2), "in": ("length", 0.0254),
    "g": ("mass", 1e-3), "kg": ("mass", 1.0),
    "min": ("time", 60.0), "h": ("time", 3600.0),
}
_TRUE = {"y", "yes", "true", "si", "sí"}
_FALSE = {"n", "no", "false"}
_TOLERANCE = 0.005  # 35,6 cm publicados ≈ 14"


def _number(value: Any, unit: str | None) -> tuple[float, str | None] | None:
  if isinstance(value, bool):
    return None
  try:
    number = float(value)
  except (TypeError, ValueError):
    return None
  if unit is None:
    return number, None
  dimension, factor = _UNITS.get(unit.strip().lower(), (f"unit:{unit.lower()}", 1.0))
  return number * factor, dimension


def _bool(value: Any) -> bool | None:
  if isinstance(value, bool):
    return value
  if isinstance(value, str):
    text = value.strip().lower()
    return True if text in _TRUE else False if text in _FALSE else None
  return None


def _text(value: Any) -> str:
  return "".join(c if c.isalnum() else " " for c in str(value).lower()).strip()


def _items(value: Any) -> list[str]:
  return [_text(v) for v in (value if isinstance(value, list) else [value])]


def _numbers(a: dict, value: Any, unit: str | None) -> tuple[float, float] | None:
  x, y = _number(a.get("valor"), a.get("unidad")), _number(value, unit)
  if x is None or y is None or x[1] != y[1]:
    return None
  return x[0], y[0]


def satisfies(observed: dict | None, operator: str, value: Any, unit: str | None = None) -> bool:
  """¿El valor real (`{"valor", "unidad"}`) cumple `operator value unit`?"""
  if observed is None:
    return False
  if operator in ("minimo", "maximo"):
    numbers = _numbers(observed, value, unit)
    if numbers is None:
      return False
    x, y = numbers
    margin = _TOLERANCE * max(abs(x), abs(y))
    return x >= y - margin if operator == "minimo" else x <= y + margin
  if operator == "incluye":
    wanted = _items(value)
    return bool(wanted) and all(w in _items(observed.get("valor")) for w in wanted)
  if isinstance(value, bool):
    return _bool(observed.get("valor")) is value
  numbers = _numbers(observed, value, unit)
  if numbers is not None:
    x, y = numbers
    return abs(x - y) <= _TOLERANCE * max(abs(x), abs(y))
  return _text(observed.get("valor")) == _text(value)


def same(a: dict, b: dict) -> bool:
  """¿Dos valores (`{"valor", "unidad"}`) del mismo atributo coinciden?"""
  if isinstance(a.get("valor"), list) or isinstance(b.get("valor"), list):
    return sorted(_items(a.get("valor"))) == sorted(_items(b.get("valor")))
  if _bool(a.get("valor")) is not None or _bool(b.get("valor")) is not None:
    return _bool(a.get("valor")) == _bool(b.get("valor"))
  return satisfies(a, "igual", b.get("valor"), b.get("unidad"))
