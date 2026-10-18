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
- Categorías variadas (si vienen): `gtin` (validado y como GTIN-14),
  `categoria_taxonomia` (ID de la taxonomía de Google) y `atributos`
  (nombre -> `{valor, unidad}`, como schema.org; ver `guard.atributos`).

Si un cart no tiene offer_details, sus campos quedan vacíos (`""`, `[]`,
`None`): nunca se infieren del texto libre.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from contracts import Chequeo, Entrega, Oferta, OfertasVistas
from guard.atributos import atributos_validos
from guard.normalizar import entregas_validas, gtin_valido, monto_valido
from guard.textos import acotar

# Claves de texto libre que se descartan (se guardan aparte, no se leen).
_CLAVE_TEXTO_CART = "item_label"
_CLAVE_TEXTO_DETALLE = "description"


@dataclass(frozen=True)
class Descarte:
    """Algo que llegó del comercio y no se evaluó. Nunca se descarta en silencio:
    el chequeo (integridad, `escalar`) va al veredicto y el motivo al recibo."""

    cart_id: str | None   # None: el cart no tenía un cart_id válido
    chequeo: Chequeo


@dataclass(frozen=True)
class Extraccion:
    ofertas: list[Oferta]
    descartes: list[Descarte] = field(default_factory=list)


def extraer_ofertas(ofertas_vistas: OfertasVistas) -> list[Oferta]:
    """Una `Oferta` por cada ap2.cart, en el orden en que llegaron."""
    return extraer(ofertas_vistas).ofertas


def extraer(ofertas_vistas: OfertasVistas) -> Extraccion:
    """Ofertas y descartes. No modifica `ofertas_vistas`.

    - Un cart sin `cart_id` (string no vacío) no se puede elegir ni cobrar:
      se descarta y se escala.
    - Un `cart_id` repetido (en ap2.cart o en demo.offer_details) es
      ambiguo: se usa la primera aparición y se escala.
    - Un offer_details sin cart correspondiente se ignora: no se puede cobrar.
    """
    detalles_por_cart: dict[str, dict] = {}
    repetidos_detalle: dict[str, int] = {}
    for detalle in ofertas_vistas.offer_details:
        cart_id = detalle.get("cart_id")
        if not isinstance(cart_id, str):
            continue
        if cart_id in detalles_por_cart:
            repetidos_detalle[cart_id] = repetidos_detalle.get(cart_id, 1) + 1
            continue
        detalles_por_cart[cart_id] = detalle

    ofertas: list[Oferta] = []
    repetidos_cart: dict[str, int] = {}
    sin_id = 0
    for cart in ofertas_vistas.carts:
        cart_id = cart.get("cart_id")
        if not isinstance(cart_id, str) or not cart_id:
            sin_id += 1
            continue
        if any(o.cart_id == cart_id for o in ofertas):
            repetidos_cart[cart_id] = repetidos_cart.get(cart_id, 1) + 1
            continue
        ofertas.append(_oferta(cart_id, cart, detalles_por_cart.get(cart_id, {})))

    descartes = [
        Descarte(cart_id, _repetido(cart_id, veces, "ap2.cart", "se evaluó la primera"))
        for cart_id, veces in repetidos_cart.items()
    ]
    descartes += [
        Descarte(cart_id, _repetido(cart_id, veces, "demo.offer_details", "se usó el primero"))
        for cart_id, veces in repetidos_detalle.items()
        if any(o.cart_id == cart_id for o in ofertas)
    ]
    if sin_id:
        descartes.append(Descarte(None, _sin_cart_id(sin_id)))
    return Extraccion(ofertas=ofertas, descartes=descartes)


def _repetido(cart_id: str, veces: int, fuente: str, cual: str) -> Chequeo:
    return Chequeo(
        tipo="integridad",
        resultado="escalar",
        detalle=(
            f"El cart_id de este carrito aparece {veces} veces en {fuente}: es "
            f"ambiguo ({cual}); no se descarta en silencio."
        ),
        campo="cart_id",
        observado=acotar(cart_id),
        origen="cart_ap2" if fuente == "ap2.cart" else "declarado",
    )


def _sin_cart_id(cantidad: int) -> Chequeo:
    return Chequeo(
        tipo="integridad",
        resultado="escalar",
        detalle=(
            f"El comercio mandó {cantidad} carrito(s) sin cart_id válido en "
            "ap2.cart: no se pueden evaluar ni elegir; no se descartan en silencio."
        ),
        campo="cart_id",
        observado=cantidad,
        origen="cart_ap2",
    )


def _oferta(cart_id: str, cart: dict, detalle: dict) -> Oferta:
    return Oferta(
        cart_id=cart_id,
        sku=_texto(detalle.get("sku")),
        titulo=_texto(detalle.get("title")),
        marca=_texto(detalle.get("brand")),
        categoria=_texto(detalle.get("category")),
        uso=_texto(detalle.get("use")),
        precio=_monto(cart.get("amount")),
        moneda=_texto(cart.get("currency")).upper(),
        stock=_entero(detalle.get("stock")),
        talles=_talles(detalle.get("sizes")),
        cantidad=_cantidad(detalle.get("quantity")),
        entregas=_entregas(detalle.get("deliveries")),
        gtin=gtin_valido(detalle.get("gtin")),
        categoria_taxonomia=_categoria_taxonomia(detalle.get("categoria_taxonomia")),
        atributos=atributos_validos(detalle.get("atributos")),
        texto_libre_descartado=_texto_libre(cart, detalle),
    )


def _categoria_taxonomia(valor: Any) -> str | None:
    """ID de la taxonomía de Google (dígitos). Una ruta en texto u otra cosa: sin dato."""
    if isinstance(valor, bool) or not isinstance(valor, (str, int)):
        return None
    texto = str(valor).strip()
    return texto if texto.isascii() and texto.isdigit() and len(texto) <= 10 else None


def _cantidad(valor: Any) -> int | None:
    cantidad = _entero(valor)
    return cantidad if cantidad is not None and cantidad > 0 else None


def _entregas(valor: Any) -> list[Entrega]:
    """Cronograma declarado [{"quantity", "date"}]. Si algún elemento no es
    válido no se toma ninguno: sin dato, el chequeo escala (nunca se infiere)."""
    cronograma = entregas_validas(valor)
    if cronograma is None:
        return []
    return [Entrega(cantidad=cantidad, fecha=fecha) for fecha, cantidad in cronograma]


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
