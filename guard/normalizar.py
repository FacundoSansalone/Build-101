"""Normalización de valores para comparar (chequeos y verificador).

Ninguna comparación usa `set` ni `in` sobre conjuntos: un `Requisito.valor`
puede ser una lista o un dict (no hasheables) y eso no puede lanzar.
"""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import date, datetime
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


_LARGOS_GTIN = (8, 12, 13, 14)


def gtin_valido(valor: Any) -> str | None:
    """GTIN-8/12/13/14 con dígito verificador correcto, como GTIN-14 (con ceros
    adelante); si no, None. Acepta espacios y guiones; no acepta `bool`."""
    if isinstance(valor, bool):
        return None
    if isinstance(valor, int):
        # Un entero pierde los ceros de adelante (un UPC 0xxxxxxxxxxx queda
        # con 11 dígitos): vale si entra en 14 dígitos.
        digitos = str(valor)
        if valor <= 0 or not 7 <= len(digitos) <= 14:
            return None
    elif isinstance(valor, str):
        digitos = re.sub(r"[\s-]", "", valor)
        if not (digitos.isascii() and digitos.isdigit() and len(digitos) in _LARGOS_GTIN):
            return None
    else:
        return None
    gtin = digitos.zfill(14)
    if not gtin.strip("0"):
        return None
    cuerpo, verificador = gtin[:-1], int(gtin[-1])
    total = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(cuerpo)))
    return gtin if (10 - total % 10) % 10 == verificador else None


def monto_valido(valor: Any) -> float | None:
    """Monto (amount, price, tope) como float finito y > 0; si no, None.

    `bool`, `<= 0`, NaN, infinito y lo que no es numérico son inválidos: el
    campo queda faltante y el chequeo que lo necesita escala.
    """
    como_numero = numero(valor)
    return como_numero if como_numero is not None and como_numero > 0 else None


# Valores de texto que significan lo mismo en inglés y en castellano (colores
# del comercio general frente a un pedido en inglés). Se aplica a los dos
# lados de toda comparación, así que no puede hacer pasar algo distinto.
_EQUIVALENTES = {
    "black": "negro", "white": "blanco", "gray": "gris", "grey": "gris",
    "silver": "plateado", "blue": "azul", "red": "rojo", "green": "verde",
    "pink": "rosa", "gold": "dorado", "yellow": "amarillo", "brown": "marron",
}


def normalizar(valor: Any) -> Any:
    """Sin importar mayúsculas, tildes, espacios ni "42" frente a 42."""
    como_numero = numero(valor)
    if como_numero is not None:
        return como_numero
    if isinstance(valor, str):
        texto = "".join(
            c for c in unicodedata.normalize("NFKD", valor.strip().casefold())
            if not unicodedata.combining(c)
        )
        return _EQUIVALENTES.get(texto, texto)
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


def mismo_id(a: Any, b: Any) -> bool:
    """Igualdad de identificadores (el `merchant.id`): exacta salvo los espacios
    de los bordes. Sin normalizar: "MERCHANT_1" no es "merchant_1" ni "7.0" es "7"."""
    return isinstance(a, str) and isinstance(b, str) and a.strip() == b.strip()


def contiene(elementos: list | tuple, valor: Any) -> bool:
    """`valor` está en `elementos` (comparación normalizada, sin hashear)."""
    return any(iguales(elemento, valor) for elemento in elementos)


def contiene_todos(elementos: list | tuple, valores: list | tuple) -> bool:
    return all(contiene(elementos, valor) for valor in valores)


def _entrega(valor: Any) -> tuple[date, int] | None:
    """(fecha, cantidad) de una `Entrega`, o de un dict con `cantidad`/`fecha`
    (o `quantity`/`date`, como lo declara el comercio). Si no, None."""
    if isinstance(valor, dict):
        cantidad = valor.get("cantidad", valor.get("quantity"))
        fecha = valor.get("fecha", valor.get("date"))
    else:
        cantidad = getattr(valor, "cantidad", None)
        fecha = getattr(valor, "fecha", None)
    if isinstance(cantidad, bool) or not isinstance(cantidad, int) or cantidad <= 0:
        return None
    if isinstance(fecha, str):
        try:
            fecha = date.fromisoformat(fecha.strip())
        except ValueError:
            return None
    if isinstance(fecha, datetime) or not isinstance(fecha, date):
        return None
    return fecha, cantidad


def entregas_validas(valor: Any) -> list[tuple[date, int]] | None:
    """Cronograma [(fecha, cantidad)] ordenado por fecha, o None.

    Una lista vacía o con algún elemento inválido no es un cronograma: None
    (el chequeo que lo necesita no puede comparar y escala).
    """
    if not isinstance(valor, (list, tuple)) or not valor:
        return None
    cronograma = [_entrega(v) for v in valor]
    if any(e is None for e in cronograma):
        return None
    return sorted(cronograma)  # type: ignore[arg-type]


def cubre_entregas(ofrecidas: Any, pedidas: Any) -> bool | None:
    """¿El cronograma ofrecido cubre el pedido?

    Para cada fecha pedida, lo ofrecido acumulado hasta esa fecha tiene que
    alcanzar lo pedido acumulado. [10 lun, 10 mié] cubre [10 lun, 10 jue]
    pero no [20 lun]. None si alguno no es un cronograma válido.
    """
    oferta, pedido = entregas_validas(ofrecidas), entregas_validas(pedidas)
    if oferta is None or pedido is None:
        return None
    pedido_acumulado = 0
    for fecha, cantidad in pedido:
        pedido_acumulado += cantidad
        if sum(c for f, c in oferta if f <= fecha) < pedido_acumulado:
            return False
    return True
