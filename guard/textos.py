"""Textos seguros: nada que escriba el comercio llega a `motivo` ni a `detalle`.

El proxy le manda `Veredicto.motivo` al agente comprador: es un canal de
inyección. Por eso `motivo` y `Chequeo.detalle` se arman con plantillas
fijas (nombres de campo, valores de la ficha y del catálogo público, montos
numéricos). Lo que declara el comercio (cart_id, SKU fuera del catálogo,
marca, moneda, ids del JWT...) va solo en `Chequeo.observado`/`esperado`,
pasado por `acotar()`: sin caracteres de control y con 80 caracteres como
máximo.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

LARGO_MAXIMO = 80
ELEMENTOS_MAXIMOS = 20

_ISO_4217 = re.compile(r"[A-Z]{3}")
# Cc/Cf/Cs/Co/Cn (control, formato, ...) y separadores de línea/párrafo.
_CATEGORIAS_PROHIBIDAS = ("C", "Zl", "Zp")


def sin_control(texto: str) -> str:
    """Saca caracteres de control, de formato (bidi, ancho cero) y saltos."""
    return "".join(
        c for c in texto if not unicodedata.category(c).startswith(_CATEGORIAS_PROHIBIDAS)
    )


def acotar(valor: Any) -> Any:
    """Valor del comercio apto para `observado`: strings limpios y cortos.

    Números, booleanos y None quedan igual; las listas se acotan elemento por
    elemento (hasta 20); cualquier otra cosa se pasa a JSON y se acota.
    """
    if valor is None or isinstance(valor, (bool, int, float)):
        return valor
    if isinstance(valor, str):
        return sin_control(valor)[:LARGO_MAXIMO]
    if isinstance(valor, (list, tuple)):
        return [acotar(v) for v in list(valor)[:ELEMENTOS_MAXIMOS]]
    try:
        texto = json.dumps(valor, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        texto = repr(valor)
    return sin_control(texto)[:LARGO_MAXIMO]


def moneda_legible(moneda: Any) -> str:
    """Código ISO 4217 (3 letras mayúsculas) o "" si no lo es."""
    return moneda if isinstance(moneda, str) and _ISO_4217.fullmatch(moneda) else ""
