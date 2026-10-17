"""Ranking propio del Guard (criterio "lowest_price_among_compliant").

Decisión 2 del paso 0: se ordena por obligatorios cumplidos (más es mejor),
después por preferidos cumplidos (más es mejor) y después por precio (menor
es mejor). Los empates respetan el orden en que llegaron las ofertas.

Se usa la oferta ya verificada: los campos tienen el dato público cuando el
catálogo lo tiene, y el precio es el `amount` del ap2.cart (lo que se cobra).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from contracts import Chequeo, Ficha, Oferta
from guard.chequeos import cumple, cumple_respaldado, monto
from guard.normalizar import EPSILON_MONTO


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

    @property
    def precio_orden(self) -> float:
        """Precio para comparar: un monto ilegible es el peor."""
        return math.inf if math.isnan(self.precio) else self.precio

    def clave(self) -> tuple:
        """Clave de orden: menor es mejor."""
        return (-self.obligatorios, -self.preferidos, self.precio_orden, self.posicion)

    @property
    def cumple_obligatorios(self) -> bool:
        return self.obligatorios == self.total_obligatorios


def puntuar(
    ficha: Ficha, oferta: Oferta, posicion: int, comercio_id: str | None = None
) -> Puntaje:
    """Un obligatorio cuenta solo si se cumple con un dato que no es solo
    declarado: una oferta inverificable no puede desplazar (ni "dominar") a
    una verificada. Un preferido solo declarado cuenta (es un aviso)."""
    obligatorios = [r for r in ficha.requisitos if r.obligatorio]
    preferidos = [r for r in ficha.requisitos if not r.obligatorio]
    return Puntaje(
        cart_id=oferta.cart_id,
        sku=oferta.sku,
        obligatorios=sum(cumple_respaldado(r, oferta, comercio_id) for r in obligatorios),
        total_obligatorios=len(obligatorios),
        preferidos=sum(cumple(r, oferta, comercio_id) is True for r in preferidos),
        total_preferidos=len(preferidos),
        precio=oferta.precio,
        posicion=posicion,
    )


def rankear(
    ficha: Ficha, ofertas: list[Oferta], comercio_id: str | None = None
) -> list[Puntaje]:
    """Puntajes de todas las ofertas, del mejor al peor."""
    puntajes = [puntuar(ficha, o, i, comercio_id) for i, o in enumerate(ofertas)]
    return sorted(puntajes, key=Puntaje.clave)


def domina(a: Puntaje, b: Puntaje) -> bool:
    """True si `a` es igual o mejor que `b` en todo y estrictamente mejor en algo.

    "Todo" = obligatorios cumplidos, preferidos cumplidos y precio.
    """
    igual_o_mejor = (
        a.obligatorios >= b.obligatorios
        and a.preferidos >= b.preferidos
        and a.precio_orden <= b.precio_orden + EPSILON_MONTO
    )
    estrictamente_mejor = (
        a.obligatorios > b.obligatorios
        or a.preferidos > b.preferidos
        or a.precio_orden < b.precio_orden - EPSILON_MONTO
    )
    return igual_o_mejor and estrictamente_mejor


def describir(puntaje: Puntaje, moneda: str) -> str:
    """Ej.: cart_1 (NK-OUT-01): 5/5 obligatorios, 1/1 preferidos, USD 120.00."""
    sku = f" ({puntaje.sku})" if puntaje.sku else ""
    return (
        f"{puntaje.cart_id}{sku}: {puntaje.obligatorios}/{puntaje.total_obligatorios}"
        f" obligatorios, {puntaje.preferidos}/{puntaje.total_preferidos} preferidos,"
        f" {monto(puntaje.precio, moneda)}"
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
            esperado=mejor.cart_id,
            observado=elegido.cart_id,
        )
    primero = puntajes[0]
    if primero.cart_id == elegido.cart_id:
        detalle = (
            f"La elección coincide con el primero del ranking propio "
            f"({describir(elegido, moneda)})."
        )
    else:
        detalle = (
            f"La elección no es la primera del ranking propio ({primero.cart_id}), "
            "pero ninguna otra opción la supera en todo."
        )
    return Chequeo(
        tipo="integridad",
        resultado="ok",
        detalle=detalle,
        campo="cart_id",
        esperado=primero.cart_id,
        observado=elegido.cart_id,
    )
