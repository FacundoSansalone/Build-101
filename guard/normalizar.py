"""Normalización de valores para comparar (chequeos y verificador)."""

from __future__ import annotations

import math
import re
from typing import Any

# Tolerancia para comparar montos (medio centavo).
EPSILON_MONTO = 0.005

_NUMERO = re.compile(r"^-?\d+(\.\d+)?$")


def numero(valor: Any) -> float | None:
    """Float si el valor es un número (o un string numérico); si no, None."""
    if isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return None if math.isnan(float(valor)) else float(valor)
    if isinstance(valor, str) and _NUMERO.match(valor.strip()):
        return float(valor.strip())
    return None


def normalizar(valor: Any) -> Any:
    """Sin importar mayúsculas, espacios ni "42" frente a 42."""
    como_numero = numero(valor)
    if como_numero is not None:
        return como_numero
    if isinstance(valor, str):
        return valor.strip().casefold()
    return valor


def iguales(a: Any, b: Any) -> bool:
    """Igualdad normalizada. Listas: mismo conjunto. Montos: medio centavo."""
    if isinstance(a, (list, tuple)) or isinstance(b, (list, tuple)):
        if not (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple))):
            return False
        return {normalizar(x) for x in a} == {normalizar(x) for x in b}
    na, nb = normalizar(a), normalizar(b)
    if isinstance(na, float) and isinstance(nb, float):
        return abs(na - nb) <= EPSILON_MONTO
    return na == nb
