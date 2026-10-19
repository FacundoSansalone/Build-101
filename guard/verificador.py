"""Verificador: contrasta cada oferta con las fuentes independientes. Sin I/O.

Fuentes, de la que más manda a la que menos (ver `guard.fuentes`): datos del
comprador > fabricante por GTIN > página pública del vendedor > catálogo
público. Para cada campo de la oferta (y para el SKU) queda una `Evidencia`:

- `verificado`: el comercio lo declaró y coincide con la primera fuente que
  tiene el dato (`origen_usado` dice cuál);
- `contradicho`: el comercio declara una cosa y la fuente dice otra;
- `solo_declarado`: ninguna fuente tiene el dato, o el comercio no lo
  declaró (faltante: no se rellena con el dato público);
- `no_verificable`: calidad, entrega y reputación del comercio, que no se
  pueden verificar antes de comprar.

Un campo que no declara el comercio ni tiene ninguna fuente (p. ej. talles
de una notebook) no aplica: no tiene evidencia.

Cuando una fuente tiene el dato, la oferta verificada usa el dato de la
fuente (decisión 4 del paso 0). El precio y la moneda siguen siendo los del
ap2.cart (lo que se cobra); se comparan con los de la fuente. El `amount`
es el total y el `price` de la fuente es por unidad: con `cantidad` > 1 se
compara con el precio por la cantidad (`total_publico`).

Categorías variadas: el GTIN (solo contra fuentes por SKU: el fabricante se
busca por GTIN y no puede verificarlo), la categoría de la taxonomía de
Google y los atributos que pide la ficha (`atributos`, por nombre).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Callable

from contracts import Evidencia, Oferta, ValorAtributo
from guard.atributos import atributos_validos, clave_atributo, iguales_atributo
from guard.fuentes import Fuente, fuentes_de
from guard.normalizar import gtin_valido, iguales, monto_valido, numero
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

PREFIJO_ATRIBUTO = "atributo:"


def clave_evidencia_atributo(nombre: str) -> str:
    """Clave de la evidencia de un atributo: `atributo:<nombre canónico>`; el
    GTIN tiene la suya (`gtin`)."""
    clave = clave_atributo(nombre)
    return "gtin" if clave == "gtin" else f"{PREFIJO_ATRIBUTO}{clave}"


def verificar(oferta: Oferta, catalogo: dict[str, dict], atributos: Iterable[str] = ()) -> Oferta:
    """Devuelve una copia de la oferta con la evidencia completa.

    `atributos`: nombres de los atributos que pide la ficha (solo esos se
    verifican: los nombres que inventa el comercio no llegan a ningún texto).

    Un campo que el comercio omitió (o mandó en None/vacío) queda
    `solo_declarado` sin valor: nunca `verificado` y nunca se rellena con el
    dato público. Así un obligatorio sin declarar escala en vez de aprobarse
    con lo que diga el catálogo sobre el SKU que declaró el comercio.
    """
    return verificar_con_rotulos(oferta, catalogo, atributos)[0]


def verificar_con_rotulos(
    oferta: Oferta, catalogo: dict[str, dict], atributos: Iterable[str] = ()
) -> tuple[Oferta, frozenset[str]]:
    """Como `verificar()`, más las claves de evidencia que salieron de una
    fuente rotulada como fixture (datos de prueba, no de la fuente en vivo)."""
    por_sku = fuentes_de(oferta.sku, None, catalogo)
    evidencia: dict[str, Evidencia] = {"sku": _evidencia_sku(oferta.sku, por_sku)}
    cambios: dict[str, Any] = {}
    fixtures: set[str] = set()

    # El GTIN lo atan solo las fuentes por SKU; con él se busca al fabricante.
    gtin_publico, fuente_gtin = _primero(por_sku, lambda p: gtin_valido(p.get("gtin")))
    fuentes = fuentes_de(oferta.sku, gtin_publico or oferta.gtin, catalogo)

    def anotar(clave: str, declarado: Any, publico: Any, fuente: Fuente | None,
               coincide: bool | None, mostrar: Callable[[Any], Any] = acotar,
               mostrar_publico: Callable[[Any], Any] = lambda valor: valor) -> None:
        if declarado is None or fuente is None:
            evidencia[clave] = Evidencia(
                campo=clave, declarado=mostrar(declarado), estado="solo_declarado",
                origen_usado="declarado",
            )
            return
        evidencia[clave] = Evidencia(
            campo=clave, declarado=mostrar(declarado), publico=mostrar_publico(publico),
            estado="verificado" if coincide else "contradicho", origen_usado=fuente.origen,
        )
        if fuente.fixture:
            fixtures.add(clave)

    for atributo, clave in CLAVE_CATALOGO.items():
        declarado = _o_none(getattr(oferta, atributo))
        publico, fuente = _primero(fuentes, lambda p, c=clave, a=atributo: _valor_publico(p, c, a))
        if declarado is None and publico is None:
            continue  # no aplica a este producto
        anotar(atributo, declarado, publico, fuente,
               declarado is not None and publico is not None and iguales(declarado, publico))
        if declarado is not None and publico is not None:
            cambios[atributo] = publico  # decisión 4: los chequeos usan el dato público

    for atributo, clave in CLAVE_CATALOGO_COBRO.items():
        cobrado = _o_none(getattr(oferta, atributo))
        publico, _ = _primero(fuentes, lambda p, c=clave, a=atributo: _valor_publico(p, c, a))
        if atributo == "precio":
            # El amount es el total; el precio de la fuente, por unidad (H1).
            publico = total_publico(publico, oferta.cantidad)
        if publico is None:
            estado = "solo_declarado"
        else:
            estado = "verificado" if iguales(cobrado, publico) else "contradicho"
        evidencia[atributo] = Evidencia(
            campo=atributo, declarado=acotar(cobrado), publico=publico, estado=estado,
            origen_usado="cart_ap2",
        )

    if oferta.gtin is not None:
        anotar("gtin", oferta.gtin, gtin_publico, fuente_gtin, oferta.gtin == gtin_publico)
        if gtin_publico is not None:
            cambios["gtin"] = gtin_publico

    if oferta.categoria_taxonomia is not None:
        publico, fuente = _primero(fuentes, lambda p: _categoria(p.get("categoria_taxonomia")))
        anotar("categoria_taxonomia", oferta.categoria_taxonomia, publico, fuente,
               oferta.categoria_taxonomia == publico)
        if publico is not None:
            cambios["categoria_taxonomia"] = publico

    nuevos_atributos = dict(oferta.atributos)
    for nombre in dict.fromkeys(clave_atributo(n) for n in atributos):
        if nombre == "gtin":
            continue  # tiene su propia evidencia
        declarado = oferta.atributos.get(nombre)
        publico, fuente = _primero(
            fuentes, lambda p, n=nombre: atributos_validos(p.get("atributos")).get(n)
        )
        coincide = (
            declarado is not None and publico is not None
            and iguales_atributo(declarado, publico) is True
        )
        anotar(clave_evidencia_atributo(nombre), declarado, publico, fuente, coincide,
               mostrar=acotar_atributo, mostrar_publico=acotar_atributo)
        if declarado is not None and publico is not None:
            nuevos_atributos[nombre] = publico
    if nuevos_atributos != oferta.atributos:
        cambios["atributos"] = nuevos_atributos

    for campo in NO_VERIFICABLES:
        evidencia[campo] = Evidencia(
            campo=campo, estado="no_verificable", origen_usado="declarado"
        )

    return oferta.model_copy(update={**cambios, "evidencia": evidencia}), frozenset(fixtures)


def verificar_todas(
    ofertas: list[Oferta], catalogo: dict[str, dict], atributos: Iterable[str] = ()
) -> list[Oferta]:
    nombres = list(atributos)
    return [verificar(o, catalogo, nombres) for o in ofertas]


def total_publico(unitario: float | None, cantidad: int | None) -> float | None:
    """Lo que tendría que cobrarse según la fuente: el precio por unidad por la
    cantidad declarada si es > 1; si no (o sin precio), el precio tal cual.

    El redondeo a 6 decimales saca el ruido del float (19.99 × 3) sin tocar
    centavos ni milésimos.
    """
    if unitario is None or cantidad is None or cantidad <= 1:
        return unitario
    return round(unitario * cantidad, 6)


def _primero(fuentes: list[Fuente], extraer: Callable[[dict], Any]) -> tuple[Any, Fuente | None]:
    """El dato de la primera fuente (en orden) que lo tiene."""
    for fuente in fuentes:
        valor = extraer(fuente.producto)
        if valor is not None:
            return valor, fuente
    return None, None


def _evidencia_sku(sku: str, por_sku: list[Fuente]) -> Evidencia:
    """El SKU está (verificado) o no está (solo declarado) en una fuente por SKU."""
    if not por_sku:
        return Evidencia(
            campo="sku", declarado=acotar(sku) or None, estado="solo_declarado",
            origen_usado="declarado",
        )
    return Evidencia(
        campo="sku", declarado=sku, publico=sku, estado="verificado",
        origen_usado=por_sku[0].origen,
    )


def sku_publico(oferta: Oferta) -> str | None:
    """El SKU si está en una fuente pública (un valor de esa fuente); si no, None."""
    evidencia = oferta.evidencia.get("sku")
    return oferta.sku if evidencia is not None and evidencia.estado == "verificado" else None


def acotar_atributo(valor: ValorAtributo | None) -> dict | None:
    """`{valor, unidad}` con los textos acotados (va en `declarado`/`publico`)."""
    if valor is None:
        return None
    return {"valor": acotar(valor.valor), "unidad": acotar(valor.unidad)}


def _categoria(valor: Any) -> str | None:
    if isinstance(valor, bool) or not isinstance(valor, (str, int)):
        return None
    texto = str(valor).strip()
    return texto if texto.isascii() and texto.isdigit() else None


def _valor_publico(producto: dict | None, clave: str, atributo: str) -> Any:
    """Valor de la fuente ya normalizado al tipo de `Oferta`. None si no hay."""
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
