"""Normalización de valores para comparar (chequeos y verificador).

Ninguna comparación usa `set` ni `in` sobre conjuntos: un `Requisito.valor`
puede ser una lista o un dict (no hasheables) y eso no puede lanzar.
"""

from __future__ import annotations

import math
import re
from typing import Any

# Tolerancia para comparar montos (medio centavo).
EPSILON_MONTO = 0.005

_NUMERO = re.compile(r"^-?\d+(\.\d+)?$")


def numero(valor: Any) -> float | None:
    """Float finito si el valor es un número (o un string numérico); si no, None.

    `bool`, NaN, infinito y los enteros que no entran en un float (JSON
    admite `10**400`) no son números para el Guard.
    """
    if isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        try:
            como_float = float(valor)
        except OverflowError:
            return None
        return como_float if math.isfinite(como_float) else None
    if isinstance(valor, str) and _NUMERO.match(valor.strip()):
        return float(valor.strip())
    return None


def monto_valido(valor: Any) -> float | None:
    """Monto (amount, price, tope) como float finito y > 0; si no, None.

    `bool`, `<= 0`, NaN, infinito y lo que no es numérico son inválidos: el
    campo queda faltante y el chequeo que lo necesita escala.
    """
    como_numero = numero(valor)
    return como_numero if como_numero is not None and como_numero > 0 else None


def normalizar(valor: Any) -> Any:
    """Sin importar mayúsculas, espacios ni "42" frente a 42."""
    como_numero = numero(valor)
    if como_numero is not None:
        return como_numero
    if isinstance(valor, str):
        return valor.strip().casefold()
    return valor


def iguales(a: Any, b: Any) -> bool:
    """Igualdad normalizada. Listas: mismos elementos. Montos: medio centavo."""
    if isinstance(a, (list, tuple)) or isinstance(b, (list, tuple)):
        if not (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple))):
            return False
        return contiene_todos(a, b) and contiene_todos(b, a)
    na, nb = normalizar(a), normalizar(b)
    if isinstance(na, float) and isinstance(nb, float):
        return abs(na - nb) <= EPSILON_MONTO
    return na == nb


def contiene(elementos: list | tuple, valor: Any) -> bool:
    """`valor` está en `elementos` (comparación normalizada, sin hashear)."""
    return any(iguales(elemento, valor) for elemento in elementos)


def contiene_todos(elementos: list | tuple, valores: list | tuple) -> bool:
    return all(contiene(elementos, valor) for valor in valores)
