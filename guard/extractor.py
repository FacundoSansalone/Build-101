"""Extractor estructurado: de lo que manda el comercio a `Oferta[]`.

Toma solo campos tipados. El texto libre (`item_label` del ap2.cart y
`description` de demo.offer_details) no llega a ningún campo que lean los
chequeos: queda en `Oferta.texto_libre_descartado`, que no se serializa y
solo sirve para mostrarlo en el recibo o el dashboard.

Fuentes de cada campo:

- `cart_id`, `precio` y `moneda`: del ap2.cart (el `amount` es lo que se
  cobra).
- `sku`, `titulo`, `marca`, `categoria`, `uso`, `stock` y `talles`: de
  demo.offer_details, tal como los **declara** el comercio. El verificador
  después los contrasta con el catálogo público.

Si un cart no tiene offer_details, sus campos quedan vacíos (`""`, `[]`,
`None`): nunca se infieren del texto libre.
"""

from __future__ import annotations

from typing import Any

from contracts import Oferta, OfertasVistas
from guard.normalizar import monto_valido

# Claves de texto libre que se descartan (se guardan aparte, no se leen).
_CLAVE_TEXTO_CART = "item_label"
_CLAVE_TEXTO_DETALLE = "description"


def extraer_ofertas(ofertas_vistas: OfertasVistas) -> list[Oferta]:
    """Una `Oferta` por cada ap2.cart, en el orden en que llegaron.

    Un offer_details sin cart correspondiente se ignora: no se puede cobrar.
    No modifica `ofertas_vistas`.
    """
    detalles_por_cart: dict[str, dict] = {}
    for detalle in ofertas_vistas.offer_details:
        cart_id = detalle.get("cart_id")
        if isinstance(cart_id, str):
            detalles_por_cart.setdefault(cart_id, detalle)

    ofertas: list[Oferta] = []
    vistos: set[str] = set()
    for cart in ofertas_vistas.carts:
        cart_id = str(cart.get("cart_id", ""))
        if not cart_id or cart_id in vistos:
            continue
        vistos.add(cart_id)
        ofertas.append(_oferta(cart, detalles_por_cart.get(cart_id, {})))
    return ofertas


def _oferta(cart: dict, detalle: dict) -> Oferta:
    return Oferta(
        cart_id=str(cart["cart_id"]),
        sku=_texto(detalle.get("sku")),
        titulo=_texto(detalle.get("title")),
        marca=_texto(detalle.get("brand")),
        categoria=_texto(detalle.get("category")),
        uso=_texto(detalle.get("use")),
        precio=_monto(cart.get("amount")),
        moneda=_texto(cart.get("currency")).upper(),
        stock=_entero(detalle.get("stock")),
        talles=_talles(detalle.get("sizes")),
        texto_libre_descartado=_texto_libre(cart, detalle),
    )


def _texto_libre(cart: dict, detalle: dict) -> str | None:
    """Junta el texto libre descartado, solo para mostrarlo."""
    partes = [
        valor
        for valor in (cart.get(_CLAVE_TEXTO_CART), detalle.get(_CLAVE_TEXTO_DETALLE))
        if isinstance(valor, str) and valor
    ]
    return "\n".join(partes) if partes else None


def _texto(valor: Any) -> str:
    return valor.strip() if isinstance(valor, str) else ""


def _monto(valor: Any) -> float:
    """Monto como float. Uno inválido (bool, <= 0, NaN, inf, no numérico) queda
    en NaN: es un dato faltante, no cumple ningún tope y el mandato escala."""
    monto = monto_valido(valor)
    return monto if monto is not None else float("nan")


def _entero(valor: Any) -> int | None:
    if isinstance(valor, bool):
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    return int(numero) if numero.is_integer() else None


def _talles(valor: Any) -> list[int]:
    """Talles enteros. Lo que no es un entero se descarta."""
    if not isinstance(valor, (list, tuple)):
        return []
    talles = [_entero(t) for t in valor]
    return [t for t in talles if t is not None]
