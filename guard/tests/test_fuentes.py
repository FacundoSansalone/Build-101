"""Fuentes de verdad que entran a `evaluar()` dentro del catálogo (sin I/O).

Orden: datos del comprador > fabricante por GTIN > página pública del
vendedor (JSON-LD) > catálogo público > solo declarado. Las fuentes externas
viajan como `DatoExterno` (subclase de dict): JSON nunca la produce, así que
un catálogo del comercio no puede hacerse pasar por el fabricante.
"""

import json

import pytest

from guard.fuentes import (
    DatoExterno,
    clave_externa,
    con_fuentes,
    dato_externo,
    desde_jsonld,
    fuentes_de,
)

GTIN = "2001234000017"
GTIN14 = "02001234000017"


def _catalogo():
    return {"LM-NB-16": {"sku": "LM-NB-16", "brand": "Lumen", "gtin": GTIN}}


def test_orden_de_las_fuentes():
    externos = [
        dato_externo("pagina_publica", sku="LM-NB-16", datos={"brand": "P"}),
        dato_externo("fabricante", gtin=GTIN, datos={"brand": "F"}),
        dato_externo("ficha", gtin=GTIN, datos={"brand": "C"}),
    ]
    catalogo = con_fuentes(_catalogo(), externos)
    fuentes = fuentes_de("LM-NB-16", GTIN14, catalogo)
    assert [f.origen for f in fuentes] == ["ficha", "fabricante", "pagina_publica", "catalogo_publico"]
    assert [f.producto["brand"] for f in fuentes] == ["C", "F", "P", "Lumen"]
    assert [f.por for f in fuentes] == ["gtin", "gtin", "sku", "sku"]


def test_sin_externos_solo_el_catalogo():
    fuentes = fuentes_de("LM-NB-16", None, _catalogo())
    assert [f.origen for f in fuentes] == ["catalogo_publico"]
    assert fuentes_de("OTRO", None, _catalogo()) == []


def test_el_catalogo_no_puede_falsificar_al_fabricante():
    # El catálogo es del comercio: una entrada con la clave y la forma del
    # fabricante es un dict común (salió de JSON), no un DatoExterno.
    falso = json.loads(json.dumps(dato_externo("fabricante", gtin=GTIN, datos={"brand": "X"})))
    catalogo = {clave_externa("fabricante", gtin=GTIN): falso, **_catalogo()}
    assert [f.origen for f in fuentes_de("LM-NB-16", GTIN14, catalogo)] == ["catalogo_publico"]


def test_un_sku_igual_a_una_clave_externa_no_encuentra_la_fuente_externa():
    externo = dato_externo("fabricante", gtin=GTIN, datos={"brand": "F"})
    catalogo = con_fuentes({}, [externo])
    sku_trucho = clave_externa("fabricante", gtin=GTIN)
    assert fuentes_de(sku_trucho, None, catalogo) == []


def test_dato_externo_rotula_fixture_y_es_inmutable_por_copia():
    dato = dato_externo("fabricante", gtin=GTIN, datos={"brand": "F"}, fixture=True, fuente="fixture")
    assert isinstance(dato, DatoExterno) and isinstance(dato, dict)
    assert dato.origen == "fabricante" and dato.fixture is True
    (fuente,) = fuentes_de("SKU", GTIN14, con_fuentes({}, [dato]))
    assert fuente.fixture is True


@pytest.mark.parametrize(
    ("origen", "kwargs"),
    [
        ("fabricante", {"sku": "LM-NB-16"}),   # el fabricante se busca por GTIN
        ("pagina_publica", {"gtin": GTIN}),    # la página es la del SKU del comercio
        ("catalogo_publico", {"sku": "X"}),    # el catálogo no es una fuente externa
        ("ficha", {}),
        ("fabricante", {"gtin": "123"}),       # GTIN inválido
    ],
)
def test_dato_externo_invalido(origen, kwargs):
    with pytest.raises(ValueError):
        dato_externo(origen, datos={}, **kwargs)


def test_con_fuentes_no_modifica_el_catalogo():
    catalogo = _catalogo()
    nuevo = con_fuentes(catalogo, [dato_externo("fabricante", gtin=GTIN, datos={})])
    assert len(catalogo) == 1 and len(nuevo) == 2


JSONLD = {
    "@context": "https://schema.org",
    "@type": "Product",
    "sku": "LM-NB-16",
    "gtin13": GTIN,
    "name": "Lumen Book 14 Pro",
    "brand": {"@type": "Brand", "name": "Lumen"},
    "color": "Silver",
    "weight": {"@type": "QuantitativeValue", "value": 1.35, "unitCode": "KGM"},
    "additionalProperty": [
        {"@type": "PropertyValue", "name": "memoria_ram", "value": 16, "unitCode": "E34"},
        {"@type": "PropertyValue", "name": "Total storage capacity", "value": 512, "unitText": "GB"},
    ],
    "offers": {"@type": "Offer", "price": "1099.00", "priceCurrency": "USD"},
}


def test_jsonld_de_la_pagina_publica():
    dato = desde_jsonld(JSONLD, sku="LM-NB-16")
    assert dato.origen == "pagina_publica"
    assert dato["title"] == "Lumen Book 14 Pro"
    assert dato["brand"] == "Lumen"
    assert dato["gtin"] == GTIN14
    assert dato["price"] == 1099.0 and dato["currency"] == "USD"
    assert dato["atributos"]["memoria_ram"] == {"valor": 16, "unidad": "E34"}
    assert dato["atributos"]["almacenamiento"] == {"valor": 512, "unidad": "GB"}
    assert dato["atributos"]["weight"] == {"valor": 1.35, "unidad": "KGM"}
    assert dato["atributos"]["color"] == {"valor": "Silver", "unidad": None}


def test_jsonld_en_un_grafo_o_lista_y_con_otro_sku():
    grafo = {"@context": "https://schema.org", "@graph": [{"@type": "WebPage"}, JSONLD]}
    assert desde_jsonld(grafo, sku="LM-NB-16")["brand"] == "Lumen"
    assert desde_jsonld([{"@type": "Organization"}, JSONLD], sku="LM-NB-16") is not None
    # La página dice ser de otro SKU: no se usa.
    assert desde_jsonld(JSONLD, sku="OTRO") is None
    assert desde_jsonld({"@type": "Organization"}, sku="LM-NB-16") is None
    assert desde_jsonld("no es json-ld", sku="LM-NB-16") is None
