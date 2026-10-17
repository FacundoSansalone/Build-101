"""Verificador: contrasta cada oferta con el catálogo público. Sin I/O.

Para cada campo de la oferta (y para el SKU) queda una `Evidencia`:

- `verificado`: el comercio lo declaró y coincide con el catálogo público;
- `contradicho`: el comercio declara una cosa y el catálogo dice otra;
- `solo_declarado`: el SKU (o el campo) no está en el catálogo, o el
  comercio no lo declaró (faltante: no se rellena con el dato público);
- `no_verificable`: calidad, entrega y reputación del comercio, que no se
  pueden verificar antes de comprar.

Cuando el catálogo tiene el dato, la oferta verificada usa el dato
**público** (decisión 4 del paso 0). El precio y la moneda siguen siendo los
del ap2.cart (lo que se cobra); se comparan con los del catálogo.
"""

from __future__ import annotations

from typing import Any

from contracts import Evidencia, Oferta
from guard.normalizar import iguales, monto_valido, numero
from guard.textos import acotar

# Atributo de Oferta -> clave del producto en el catálogo público.
CLAVE_CATALOGO: dict[str, str] = {
    "titulo": "title",
    "marca": "brand",
    "categoria": "category",
    "uso": "use",
    "talles": "sizes",
}

# Precio y moneda: lo que se cobra (ap2.cart) frente al catálogo público.
CLAVE_CATALOGO_COBRO: dict[str, str] = {"precio": "price", "moneda": "currency"}

# Lo que no se puede verificar antes de comprar (va al recibo).
NO_VERIFICABLES = ("calidad", "entrega", "reputación del comercio")


def verificar(oferta: Oferta, catalogo: dict[str, dict]) -> Oferta:
    """Devuelve una copia de la oferta con la evidencia completa.

    Un campo que el comercio omitió (o mandó en None/vacío) queda
    `solo_declarado` sin valor: nunca `verificado` y nunca se rellena con el
    dato público. Así un obligatorio sin declarar escala en vez de aprobarse
    con lo que diga el catálogo sobre el SKU que declaró el comercio.
    """
    producto = catalogo.get(oferta.sku) if oferta.sku else None
    if not isinstance(producto, dict):
        producto = None
    evidencia: dict[str, Evidencia] = {"sku": _evidencia_sku(oferta.sku, producto)}
    cambios: dict[str, Any] = {}

    for atributo, clave in CLAVE_CATALOGO.items():
        declarado = _o_none(getattr(oferta, atributo))
        publico = _valor_publico(producto, clave, atributo)
        if declarado is None or publico is None:
            evidencia[atributo] = Evidencia(
                campo=atributo, declarado=acotar(declarado), estado="solo_declarado",
                origen_usado="declarado",
            )
            continue
        estado = "verificado" if iguales(declarado, publico) else "contradicho"
        evidencia[atributo] = Evidencia(
            campo=atributo, declarado=acotar(declarado), publico=publico, estado=estado,
            origen_usado="catalogo_publico",
        )
        cambios[atributo] = publico  # decisión 4: los chequeos usan el dato público

    for atributo, clave in CLAVE_CATALOGO_COBRO.items():
        cobrado = _o_none(getattr(oferta, atributo))
        publico = _valor_publico(producto, clave, atributo)
        if publico is None:
            estado = "solo_declarado"
        else:
            estado = "verificado" if iguales(cobrado, publico) else "contradicho"
        evidencia[atributo] = Evidencia(
            campo=atributo, declarado=acotar(cobrado), publico=publico, estado=estado,
            origen_usado="cart_ap2",
        )

    for campo in NO_VERIFICABLES:
        evidencia[campo] = Evidencia(
            campo=campo, estado="no_verificable", origen_usado="declarado"
        )

    return oferta.model_copy(update={**cambios, "evidencia": evidencia})


def verificar_todas(ofertas: list[Oferta], catalogo: dict[str, dict]) -> list[Oferta]:
    return [verificar(o, catalogo) for o in ofertas]


def _evidencia_sku(sku: str, producto: dict | None) -> Evidencia:
    """El SKU está (verificado) o no está (solo declarado) en el catálogo."""
    if producto is None:
        return Evidencia(
            campo="sku", declarado=acotar(sku) or None, estado="solo_declarado",
            origen_usado="declarado",
        )
    return Evidencia(
        campo="sku", declarado=sku, publico=sku, estado="verificado",
        origen_usado="catalogo_publico",
    )


def sku_publico(oferta: Oferta) -> str | None:
    """El SKU si está en el catálogo público (un valor del catálogo); si no, None."""
    evidencia = oferta.evidencia.get("sku")
    return oferta.sku if evidencia is not None and evidencia.estado == "verificado" else None


def _valor_publico(producto: dict | None, clave: str, atributo: str) -> Any:
    """Valor del catálogo ya normalizado al tipo de `Oferta`. None si no hay."""
    if not producto:
        return None
    valor = producto.get(clave)
    if atributo == "talles":
        if not isinstance(valor, (list, tuple)):
            return None
        talles = [numero(t) for t in valor]
        return [int(t) for t in talles if t is not None and t.is_integer()] or None
    if atributo == "precio":
        return monto_valido(valor)
    if isinstance(valor, str) and valor.strip():
        return valor.strip().upper() if atributo == "moneda" else valor.strip()
    return None


def _o_none(valor: Any) -> Any:
    """'' , [] y NaN cuentan como "no declarado"."""
    if valor in ("", None) or valor == []:
        return None
    if isinstance(valor, float) and valor != valor:  # NaN
        return None
    return valor
