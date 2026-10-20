"""Ranking propio del Guard (criterio "lowest_price_among_compliant").

Decisión 2 del paso 0: se ordena por obligatorios cumplidos (más es mejor),
después por preferidos cumplidos (más es mejor) y después por precio (menor
es mejor). Los empates respetan el orden en que llegaron las ofertas.

Un obligatorio que se cumple solo según la ficha del fabricante de un GTIN
que declara el comercio (sin una fuente por SKU que lo ate) no cuenta como
cumplido, pero desempata antes que los preferidos: entre ofertas sin
respaldo, la que el fabricante desmiente no queda primera ni "domina".

Se usa la oferta ya verificada: los campos tienen el dato público cuando el
catálogo lo tiene, y el precio es el `amount` del ap2.cart (lo que se cobra).

La categoría de la ficha y, si alguna oferta trae varias unidades sin que
la ficha diga cuántas, "una unidad" cuentan como obligatorios (ver
`puntuar`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from contracts import Chequeo, Ficha, Oferta
from guard.categorias import cumple_categoria, exige_categoria
from guard.chequeos import (
    cantidad_sin_autorizar,
    cumple,
    cumple_respaldado,
    cumple_sin_atar,
    monto,
)
from guard.normalizar import EPSILON_MONTO
from guard.textos import acotar
from guard.verificador import sku_publico


@dataclass(frozen=True)
class Puntaje:
    """Cómo queda una oferta frente a la ficha."""

    cart_id: str
    sku: str
    obligatorios: int        # obligatorios cumplidos
    total_obligatorios: int
    preferidos: int          # preferidos cumplidos
    total_preferidos: int
    precio: float            # lo que se cobra (NaN si es ilegible)
    posicion: int            # orden de llegada, para desempatar
    sku_publico: str | None = None  # el SKU solo si está en el catálogo público
    # obligatorios que se cumplen solo según el fabricante de un GTIN sin atar
    obligatorios_sin_atar: int = 0

    @property
    def precio_orden(self) -> float:
        """Precio para comparar: un monto ilegible es el peor."""
        return math.inf if math.isnan(self.precio) else self.precio

    def clave(self) -> tuple:
        """Clave de orden: menor es mejor."""
        return (
            -self.obligatorios, -self.obligatorios_sin_atar, -self.preferidos,
            self.precio_orden, self.posicion,
        )

    @property
    def cumple_obligatorios(self) -> bool:
        return self.obligatorios == self.total_obligatorios


def puntuar(
    ficha: Ficha,
    oferta: Oferta,
    posicion: int,
    comercio_id: str | None = None,
    cantidad_implicita: bool = False,
) -> Puntaje:
    """Un obligatorio cuenta solo si se cumple con un dato que no es solo
    declarado: una oferta inverificable no puede desplazar (ni "dominar") a
    una verificada. Un preferido solo declarado cuenta (es un aviso).

    Dos obligatorios implícitos, iguales para todas las ofertas del ranking:
    la categoría de la ficha (cumplida solo dentro de la rama con fuente
    independiente, `guard.categorias`) y, con `cantidad_implicita`, "una
    unidad" (lo que autorizó una ficha que no dice cuántas).
    """
    obligatorios = [r for r in ficha.requisitos if r.obligatorio]
    preferidos = [r for r in ficha.requisitos if not r.obligatorio]
    cumplidos = sum(cumple_respaldado(r, oferta, comercio_id) for r in obligatorios)
    sin_atar = sum(cumple_sin_atar(r, oferta, comercio_id) for r in obligatorios)
    total = len(obligatorios)
    if exige_categoria(ficha):
        total += 1
        cumplidos += cumple_categoria(ficha, oferta)
    if cantidad_implicita:
        total += 1
        cumplidos += not cantidad_sin_autorizar(ficha, oferta)
    return Puntaje(
        cart_id=oferta.cart_id,
        sku=oferta.sku,
        obligatorios=cumplidos,
        total_obligatorios=total,
        preferidos=sum(cumple(r, oferta, comercio_id) is True for r in preferidos),
        total_preferidos=len(preferidos),
        precio=oferta.precio,
        posicion=posicion,
        sku_publico=sku_publico(oferta),
        obligatorios_sin_atar=sin_atar,
    )


def rankear(
    ficha: Ficha, ofertas: list[Oferta], comercio_id: str | None = None
) -> list[Puntaje]:
    """Puntajes de todas las ofertas, del mejor al peor.

    "Una unidad" cuenta solo si alguna oferta trae varias y la ficha no dice
    cuántas (así un pack más barato no "domina" a la oferta de una unidad);
    si no, los puntajes quedan como siempre.
    """
    implicita = any(cantidad_sin_autorizar(ficha, o) for o in ofertas)
    puntajes = [
        puntuar(ficha, o, i, comercio_id, cantidad_implicita=implicita)
        for i, o in enumerate(ofertas)
    ]
    return sorted(puntajes, key=Puntaje.clave)


def domina(a: Puntaje, b: Puntaje) -> bool:
    """True si `a` es igual o mejor que `b` en todo y estrictamente mejor en algo.

    "Todo" = obligatorios cumplidos (y, entre los que no cuentan, los que se
    cumplen solo según el fabricante de un GTIN sin atar), preferidos
    cumplidos y precio.
    """
    igual_o_mejor = (
        a.obligatorios >= b.obligatorios
        and a.obligatorios_sin_atar >= b.obligatorios_sin_atar
        and a.preferidos >= b.preferidos
        and a.precio_orden <= b.precio_orden + EPSILON_MONTO
    )
    estrictamente_mejor = (
        a.obligatorios > b.obligatorios
        or a.obligatorios_sin_atar > b.obligatorios_sin_atar
        or a.preferidos > b.preferidos
        or a.precio_orden < b.precio_orden - EPSILON_MONTO
    )
    return igual_o_mejor and estrictamente_mejor


def describir(puntaje: Puntaje, moneda: str) -> str:
    """Ej.: oferta 1 (NK-OUT-01): 5/5 obligatorios, 1/1 preferidos, USD 120.00.

    Sin el cart_id ni un SKU que no esté en el catálogo: son texto del
    comercio. La oferta se nombra por su orden de llegada.
    """
    sku = puntaje.sku_publico or "SKU fuera del catálogo público"
    sin_atar = (
        f" (y {puntaje.obligatorios_sin_atar} solo según el fabricante del GTIN declarado)"
        if puntaje.obligatorios_sin_atar else ""
    )
    return (
        f"oferta {puntaje.posicion + 1} ({sku}): {puntaje.obligatorios}/"
        f"{puntaje.total_obligatorios} obligatorios{sin_atar}, {puntaje.preferidos}/"
        f"{puntaje.total_preferidos} preferidos, {monto(puntaje.precio, moneda)}"
    )


def chequeo_integridad(puntajes: list[Puntaje], elegido: Puntaje, moneda: str) -> Chequeo:
    """Escala solo si otra oferta supera a la elegida en todo (decisión 3).

    Elegir algo que no es el primero del ranking pero que ninguna otra
    oferta supera en todo no se escala: lo que le falte ya aparece en el
    chequeo de cumplimiento (por ejemplo, un preferido como aviso).
    """
    dominantes = [p for p in puntajes if domina(p, elegido)]
    if dominantes:
        mejor = dominantes[0]  # puntajes viene ordenado: el mejor que la domina
        return Chequeo(
            tipo="integridad",
            resultado="escalar",
            detalle=(
                f"Hay una opción igual o mejor en todo que la elegida: "
                f"{describir(mejor, moneda)}; elegida {describir(elegido, moneda)}."
            ),
            campo="cart_id",
            esperado=acotar(mejor.cart_id),
            observado=acotar(elegido.cart_id),
        )
    primero = puntajes[0]
    if primero.cart_id == elegido.cart_id:
        detalle = (
            f"La elección coincide con el primero del ranking propio "
            f"({describir(elegido, moneda)})."
        )
    else:
        detalle = (
            f"La elección no es la primera del ranking propio "
            f"({describir(primero, moneda)}), pero ninguna otra opción la supera en todo."
        )
    return Chequeo(
        tipo="integridad",
        resultado="ok",
        detalle=detalle,
        campo="cart_id",
        esperado=acotar(primero.cart_id),
        observado=acotar(elegido.cart_id),
    )
