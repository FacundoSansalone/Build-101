"""Fuentes de verdad independientes del comercio, como datos (sin I/O).

Orden (la primera que tiene el dato manda):

1. **datos del comprador** (`ficha`), por GTIN o por SKU, si existen;
2. **fabricante** (`fabricante`), por GTIN (p. ej. Icecat);
3. **página pública del vendedor** (`pagina_publica`), JSON-LD schema.org
   de la página del SKU;
4. **catálogo público** (`catalogo_publico`), por SKU;
5. si no hay ninguna: **solo declarado**.

No cambia el contrato de `evaluar()`: las fuentes externas viajan dentro del
mismo `catalogo` (sku -> producto) como `DatoExterno`, una subclase de dict
con una clave reservada (`fabricante:gtin:<gtin14>`, `pagina_publica:sku:<sku>`,
`ficha:gtin:...` o `ficha:sku:...`). Las consulta quien llama (el I/O vive
afuera) y las suma con `con_fuentes()`.

`DatoExterno` no es falsificable desde el catálogo del comercio: lo que se
lee de un JSON es siempre un `dict` común, y un `dict` común con la clave
o la forma de una fuente externa se ignora. Al revés, un SKU declarado que
coincide con una clave reservada no encuentra la fuente externa. Si el
catálogo pasa por JSON (otro proceso), las fuentes externas se pierden y
todo vuelve a "solo declarado": falla cerrado.

El fabricante se busca por GTIN, así que no puede verificar el GTIN (sería
circular): el GTIN lo verifican solo las fuentes por SKU.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from contracts import Origen
from guard.atributos import atributos_validos, valor_atributo
from guard.normalizar import gtin_valido, monto_valido

ORIGENES_EXTERNOS: tuple[Origen, ...] = ("ficha", "fabricante", "pagina_publica")
ORDEN_FUENTES: tuple[Origen, ...] = (*ORIGENES_EXTERNOS, "catalogo_publico")
ORIGENES_INDEPENDIENTES = frozenset(ORDEN_FUENTES)

NOMBRE_FUENTE: dict[str, str] = {
    "ficha": "los datos del comprador",
    "fabricante": "la ficha del fabricante",
    "pagina_publica": "la página pública del comercio",
    "catalogo_publico": "el catálogo público",
}

Por = Literal["gtin", "sku"]
_POR_ORIGEN: dict[str, tuple[Por, ...]] = {
    "ficha": ("gtin", "sku"),
    "fabricante": ("gtin",),
    "pagina_publica": ("sku",),
}


class DatoExterno(dict):
    """Producto según una fuente externa, con las claves del catálogo público
    (`title`, `brand`, `price`, `currency`, `gtin`, `categoria_taxonomia`,
    `atributos`...) más las reservadas `_origen`, `_por`, `_clave`,
    `_fixture` y `_fuente`."""

    __slots__ = ()

    @property
    def origen(self) -> Origen:
        return self["_origen"]

    @property
    def por(self) -> Por:
        return self["_por"]

    @property
    def fixture(self) -> bool:
        return bool(self.get("_fixture"))

    @property
    def fuente(self) -> str | None:
        return self.get("_fuente")


def _clave_por(por: Por, valor: Any) -> str:
    if por == "gtin":
        gtin = gtin_valido(valor)
        if gtin is None:
            raise ValueError("GTIN inválido para una fuente externa")
        return gtin
    if not isinstance(valor, str) or not valor.strip():
        raise ValueError("SKU inválido para una fuente externa")
    return valor.strip()


def clave_externa(origen: str, *, gtin: Any = None, sku: Any = None) -> str:
    """Clave reservada dentro de `catalogo` para una fuente externa."""
    por: Por = "gtin" if gtin is not None else "sku"
    return f"{origen}:{por}:{_clave_por(por, gtin if por == 'gtin' else sku)}"


def dato_externo(
    origen: str,
    *,
    datos: Mapping[str, Any],
    gtin: Any = None,
    sku: Any = None,
    fixture: bool = False,
    fuente: str | None = None,
) -> DatoExterno:
    """Arma un `DatoExterno`. `datos` usa las claves del catálogo público."""
    if origen not in _POR_ORIGEN:
        raise ValueError(f"Origen que no es una fuente externa: {origen!r}")
    if (gtin is None) == (sku is None):
        raise ValueError("Una fuente externa se busca por GTIN o por SKU (uno de los dos)")
    por: Por = "gtin" if gtin is not None else "sku"
    if por not in _POR_ORIGEN[origen]:
        raise ValueError(f"La fuente {origen!r} no se busca por {por}")
    clave = _clave_por(por, gtin if por == "gtin" else sku)
    contenido = {k: v for k, v in dict(datos).items() if not str(k).startswith("_")}
    return DatoExterno(
        {**contenido, "_origen": origen, "_por": por, "_clave": clave,
         "_fixture": bool(fixture), "_fuente": fuente}
    )


def con_fuentes(catalogo: Mapping[str, dict], externos: Iterable[DatoExterno]) -> dict[str, dict]:
    """Un catálogo nuevo con las fuentes externas en sus claves reservadas."""
    nuevo = dict(catalogo)
    for dato in externos:
        if not isinstance(dato, DatoExterno):
            raise TypeError("con_fuentes() solo acepta DatoExterno")
        nuevo[f"{dato.origen}:{dato.por}:{dato['_clave']}"] = dato
    return nuevo


@dataclass(frozen=True)
class Fuente:
    origen: Origen
    producto: dict
    por: Por
    fixture: bool = False


def producto_del_catalogo(catalogo: Mapping[str, Any], sku: str | None) -> dict | None:
    """El producto del catálogo público para ese SKU (nunca un `DatoExterno`)."""
    producto = catalogo.get(sku) if sku else None
    if not isinstance(producto, dict) or isinstance(producto, DatoExterno):
        return None
    return producto


def _externo(catalogo: Mapping[str, Any], origen: str, por: Por, valor: str | None) -> DatoExterno | None:
    if not valor:
        return None
    try:
        clave = _clave_por(por, valor)
    except ValueError:
        return None
    dato = catalogo.get(f"{origen}:{por}:{clave}")
    if (
        isinstance(dato, DatoExterno)
        and dato.get("_origen") == origen
        and dato.get("_por") == por
        and dato.get("_clave") == clave
    ):
        return dato
    return None


def fuentes_de(sku: str | None, gtin: str | None, catalogo: Mapping[str, Any]) -> list[Fuente]:
    """Las fuentes que hay para esta oferta, de la que más manda a la que menos."""
    fuentes: list[Fuente] = []
    for origen, por, valor in (
        ("ficha", "gtin", gtin),
        ("ficha", "sku", sku),
        ("fabricante", "gtin", gtin),
        ("pagina_publica", "sku", sku),
    ):
        dato = _externo(catalogo, origen, por, valor)
        if dato is not None:
            fuentes.append(Fuente(origen, dato, por, dato.fixture))
    producto = producto_del_catalogo(catalogo, sku)
    if producto is not None:
        fuentes.append(Fuente("catalogo_publico", producto, "sku"))
    return fuentes


# ---------------------------------------------------------------------------
# Página pública: JSON-LD schema.org (Product)
# ---------------------------------------------------------------------------

_CLAVES_GTIN = ("gtin", "gtin14", "gtin13", "gtin12", "gtin8")
_PROPIEDADES_SCHEMA = ("color", "weight", "model", "material", "width", "height", "depth")


def _es_producto(nodo: Any) -> bool:
    if not isinstance(nodo, dict):
        return False
    tipo = nodo.get("@type")
    tipos = tipo if isinstance(tipo, list) else [tipo]
    return any(t in ("Product", "ProductModel", "IndividualProduct") for t in tipos)


def _nodos(jsonld: Any) -> list[dict]:
    if isinstance(jsonld, list):
        return [n for item in jsonld for n in _nodos(item)]
    if isinstance(jsonld, dict):
        if isinstance(jsonld.get("@graph"), list):
            return _nodos(jsonld["@graph"])
        return [jsonld]
    return []


def _texto(valor: Any) -> str | None:
    if isinstance(valor, dict):
        valor = valor.get("name")
    return valor.strip() if isinstance(valor, str) and valor.strip() else None


def _oferta_jsonld(offers: Any) -> dict:
    if isinstance(offers, list):
        offers = next((o for o in offers if isinstance(o, dict)), None)
    return offers if isinstance(offers, dict) else {}


def desde_jsonld(
    jsonld: Any, *, sku: str, fixture: bool = False, fuente: str | None = None
) -> DatoExterno | None:
    """El `Product` de la página pública del SKU como `DatoExterno`, o None.

    Si la página dice ser de otro SKU, no se usa. Atributos: los de
    `additionalProperty` (`PropertyValue`: name, value, unitCode/unitText) y
    las propiedades de schema.org `color`, `weight`, `model`...
    """
    productos = [n for n in _nodos(jsonld) if _es_producto(n)]
    con_sku = [p for p in productos if isinstance(p.get("sku"), str)]
    elegido = next((p for p in con_sku if p["sku"].strip() == sku), None)
    if elegido is None and not con_sku and len(productos) == 1:
        elegido = productos[0]
    if elegido is None:
        return None

    datos: dict[str, Any] = {}
    if titulo := _texto(elegido.get("name")):
        datos["title"] = titulo
    if marca := _texto(elegido.get("brand")):
        datos["brand"] = marca
    gtin = next(
        (g for g in (gtin_valido(elegido.get(k)) for k in _CLAVES_GTIN) if g), None
    )
    if gtin:
        datos["gtin"] = gtin
    categoria = elegido.get("category")
    if isinstance(categoria, (str, int)) and not isinstance(categoria, bool) and str(categoria).strip().isdigit():
        datos["categoria_taxonomia"] = str(categoria).strip()
    oferta = _oferta_jsonld(elegido.get("offers"))
    if (precio := monto_valido(oferta.get("price"))) is not None:
        datos["price"] = precio
    if (moneda := _texto(oferta.get("priceCurrency"))) is not None:
        datos["currency"] = moneda.upper()

    crudos: dict[str, Any] = {}
    for nombre in _PROPIEDADES_SCHEMA:
        if nombre in elegido and valor_atributo(elegido[nombre]) is not None:
            crudos[nombre] = elegido[nombre]
    propiedades = elegido.get("additionalProperty")
    for propiedad in propiedades if isinstance(propiedades, list) else []:
        if isinstance(propiedad, dict) and isinstance(propiedad.get("name"), str):
            crudos.setdefault(propiedad["name"], propiedad)
    datos["atributos"] = {
        clave: valor.model_dump() for clave, valor in atributos_validos(crudos).items()
    }
    return dato_externo("pagina_publica", sku=sku, datos=datos, fixture=fixture, fuente=fuente)


def nombre_fuente(origen: str | None, fixture: bool = False) -> str:
    """"la ficha del fabricante" (o "... (fixture de prueba)")."""
    nombre = NOMBRE_FUENTE.get(origen or "", "una fuente independiente")
    return f"{nombre} (fixture de prueba)" if fixture else nombre


__all__ = [
    "NOMBRE_FUENTE",
    "ORDEN_FUENTES",
    "ORIGENES_EXTERNOS",
    "ORIGENES_INDEPENDIENTES",
    "DatoExterno",
    "Fuente",
    "clave_externa",
    "con_fuentes",
    "dato_externo",
    "desde_jsonld",
    "fuentes_de",
    "nombre_fuente",
    "producto_del_catalogo",
]
