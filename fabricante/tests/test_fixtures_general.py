"""Las fichas del fabricante del comercio general (etapa 3, `fixtures/fabricante/`).

Salen del mismo dataset que el catálogo público
(`world/.../demo_world/data/productos.json`, que exporta
`demo_world.export_fixtures`): una sola fuente de verdad. Este test lee el
dataset y los fixtures como archivos (no importa world/).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fabricante import FixtureFabricante
from guard.atributos import clave_atributo, iguales_atributo, valor_atributo
from guard.normalizar import gtin_valido
from taxonomia import cargar_subconjunto

RAIZ = Path(__file__).resolve().parents[2]
DATA = RAIZ / "world" / "ap2" / "code" / "samples" / "python" / "src" / "demo_world" / "data"
FIXTURES = RAIZ / "fixtures"

DATOS = json.loads((DATA / "productos.json").read_text("utf-8"))
PRODUCTOS = DATOS["productos"]
CATEGORIAS = DATOS["categorias"]
GTINS = [p["gtin"] for p in PRODUCTOS]
GTINS_ETAPA2 = set(json.loads((DATA / "manufacturers.json").read_text("utf-8"))["products"])
CAMPOS_PUBLICOS = (
    "sku", "title", "brand", "category", "use", "price", "currency", "stock", "sizes",
    "description", "gtin", "categoria_taxonomia", "atributos",
)


def _fixture(gtin: str) -> dict:
    return json.loads((FIXTURES / "fabricante" / f"{gtin}.json").read_text("utf-8"))


def test_el_dataset_es_variado_y_sin_repetidos():
    assert 100 <= len(PRODUCTOS) <= 120
    assert len(CATEGORIAS) == 24
    assert len({p["sku"] for p in PRODUCTOS}) == len(PRODUCTOS)
    assert {p["category"] for p in PRODUCTOS} == set(CATEGORIAS)
    for p in PRODUCTOS:
        assert p["currency"] == "USD" and p["price"] > 0 and p["stock"] > 0
        assert isinstance(p["publicado"], bool) and p["entrega_dias"] >= 1
    # Cada categoría tiene algo publicado (el catálogo puede contradecir lo declarado).
    assert {p["category"] for p in PRODUCTOS if p["publicado"]} == set(CATEGORIAS)


def test_gtin_validos_del_rango_restringido_y_sin_choques():
    assert len(set(GTINS)) == len(GTINS)
    assert not set(GTINS) & GTINS_ETAPA2
    for gtin in GTINS:
        assert len(gtin) == 13 and gtin.isdigit()
        assert gtin.startswith("20")  # circulación restringida de GS1 (20–29)
        assert gtin_valido(gtin) == "0" + gtin  # dígito verificador correcto


@pytest.mark.parametrize("gtin", GTINS)
def test_una_ficha_por_gtin_rotulada_como_datos_de_prueba(gtin):
    producto = next(p for p in PRODUCTOS if p["gtin"] == gtin)
    resultado = FixtureFabricante().consultar(gtin)
    assert resultado.motivo == "ok" and resultado.fixture and resultado.fuente == "fixture"
    assert "NO son datos de Icecat" in _fixture(gtin)["_fixture"]
    assert resultado.ficha.marca == producto["brand"]
    assert resultado.ficha.nombre == producto["title"]
    assert resultado.ficha.gtins == ("0" + gtin,)


def test_los_atributos_tienen_nombre_canonico_y_coinciden_con_la_ficha():
    fuente = FixtureFabricante()
    for producto in PRODUCTOS:
        ficha = fuente.consultar(producto["gtin"]).ficha
        assert set(ficha.atributos) == set(producto["atributos"]), producto["sku"]
        for nombre, valor in producto["atributos"].items():
            assert clave_atributo(nombre) == nombre, nombre
            assert iguales_atributo(valor_atributo(valor), ficha.atributos[nombre]) is True, (
                producto["sku"], nombre)


def test_las_categorias_estan_en_el_subconjunto_de_la_taxonomia():
    arbol = cargar_subconjunto()
    for slug, info in CATEGORIAS.items():
        assert info["id"] in arbol, slug
        for otro in info["tambien"]:  # "computadoras" (278) → notebooks; "silla" (443) → de oficina
            assert otro in arbol and otro != info["id"], (slug, otro)
        assert info["hermana"] in CATEGORIAS and info["hermana"] != slug
        assert any(info["atributo_clave"] in p["atributos"] for p in PRODUCTOS if p["category"] == slug)
    for producto in PRODUCTOS:
        assert producto["categoria_taxonomia"] == CATEGORIAS[producto["category"]]["id"]


def test_los_fixtures_estan_sincronizados_con_el_dataset():
    exportados = {p.stem for p in (FIXTURES / "fabricante").glob("*.json")}
    assert exportados == set(GTINS) | GTINS_ETAPA2
    catalogo = json.loads((FIXTURES / "public_catalog.json").read_text("utf-8"))
    base = json.loads((DATA / "catalog.json").read_text("utf-8"))
    publicados = [{k: p[k] for k in CAMPOS_PUBLICOS} for p in PRODUCTOS if p["publicado"]]
    assert catalogo["merchant"] == base["merchant"]
    assert catalogo["products"] == base["products"] + publicados
