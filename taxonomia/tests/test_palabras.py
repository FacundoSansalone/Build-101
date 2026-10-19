"""Categoría por palabras: determinística, del subconjunto del repo y sin modelo."""

import pytest

from taxonomia import cargar_subconjunto
from taxonomia.palabras import FRASES, categoria_por_palabras, nombradas, normalizar


@pytest.fixture(scope="module")
def arbol():
    return cargar_subconjunto()


def test_todos_los_ids_estan_en_el_subconjunto(arbol):
    faltan = [i for i in FRASES if i not in arbol]
    assert not faltan, f"IDs fuera del subconjunto: {faltan}"


def test_las_frases_ya_estan_normalizadas_y_no_se_repiten():
    vistas: dict[str, str] = {}
    for id_categoria, frases in FRASES.items():
        for frase in frases:
            assert frase == normalizar(frase), frase
            assert frase not in vistas, f"{frase!r} en {vistas.get(frase)} y {id_categoria}"
            vistas[frase] = id_categoria


@pytest.mark.parametrize(
    ("pedido", "esperado"),
    [
        ("Quiero un monitor de 27 pulgadas para diseño, hasta 400 USD", "305"),
        ("Comprame 20 computadoras para el lunes, hasta 20000 USD.", "278"),
        ("20 laptops delivered by Monday, up to 20000 USD", "328"),
        ("Necesito una notebook para programar: mínimo 16 GB de RAM", "328"),
        ("una computadora de escritorio para la oficina", "325"),
        ("Zapatillas Nike talle 42 para correr", "187"),
        ("auriculares con cancelación de ruido", "543626"),
        ("auriculares con micrófono para llamadas", "543627"),
        ("una silla de oficina ergonómica", "2045"),
        ("un escritorio de madera", "4191"),
        ("Tablet de 10 pulgadas", "4745"),
        ("una cámara web para videollamadas", "312"),
        ("cafetera espresso", "736"),
        ("una silla de escritorio cómoda", "2045"),  # no "escritorio" (4191)
    ],
)
def test_pedidos_comunes(arbol, pedido, esperado):
    assert categoria_por_palabras(pedido, arbol) == esperado


def test_gana_la_frase_mas_larga_aunque_aparezcan_varias(arbol):
    # "computadora de escritorio" contiene "computadora" y "escritorio".
    assert categoria_por_palabras("computadora de escritorio", arbol) == "325"
    assert categoria_por_palabras("computadora de escritorio") == "325"  # sin árbol, igual


def test_sin_categoria_conocida_devuelve_none(arbol):
    assert categoria_por_palabras("algo lindo para regalar", arbol) is None
    assert categoria_por_palabras("", arbol) is None
    assert categoria_por_palabras("pantalla", arbol) is None  # ambigua: tele o monitor
    assert categoria_por_palabras("una silla", arbol) is None  # ambigua: comedor u oficina


def test_palabras_sueltas_no_matchean_dentro_de_otras(arbol):
    # "pc" no está dentro de "pcie": se busca por palabra entera.
    assert categoria_por_palabras("adaptador pcie", arbol) is None


def test_el_resultado_siempre_es_una_categoria_del_arbol(arbol):
    for frases in FRASES.values():
        for frase in frases:
            assert categoria_por_palabras(frase, arbol) in arbol


# -- nombradas: lo que se compra, sin los complementos ------------------------------


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("Comprame 20 computadoras para el lunes, hasta 20000 USD.", ["278"]),
        # El complemento ("para la notebook") no es lo que se compra.
        ("una mochila para la notebook", ["100"]),
        ("un mouse para mi notebook", ["304"]),
        ("a backpack for my laptop", ["100"]),
        ("un monitor para la pc", ["305"]),
        ("notebook con mouse", ["328"]),
        # Una frase dentro de otra más larga no cuenta aparte.
        ("una computadora de escritorio para la oficina", ["325"]),
        ("auriculares con micrófono para llamadas", ["543627"]),
        # Varias: de la mejor a la peor (la frase más larga primero).
        ("20 computadoras, notebooks si puede ser", ["278", "328"]),
    ],
)
def test_nombradas_sin_complementos(arbol, texto, esperado):
    assert [i for i, _ in nombradas(texto, arbol)] == esperado


def test_nombradas_devuelve_la_frase_de_la_tabla(arbol):
    assert nombradas("Comprame 20 Computadoras", arbol) == [("278", "computadoras")]


def test_nombradas_sin_lo_que_se_compra_queda_vacia(arbol):
    # Solo un complemento: no dice qué se compra.
    assert nombradas("algo para mi computadora", arbol) == []
    assert nombradas("un cargador para la notebook", arbol) == []
    assert nombradas("", arbol) == []
    assert nombradas("pantalla", arbol) == []


def test_categoria_por_palabras_no_cambia(arbol):
    """`nombradas` es aparte: el proxy y el comercio siguen igual."""
    assert categoria_por_palabras("una mochila para la notebook", arbol) == "328"


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        # "de": complemento, salvo después de un número o una palabra de cantidad.
        ("Comprame un cargador de notebook", []),
        ("una funda de computadora", []),
        ("un teclado de notebook", ["303"]),
        ("una mochila de notebook", ["100"]),
        ("un par de zapatillas", ["187"]),
        ("una docena de notebooks", ["328"]),
        ("20 de las computadoras", ["278"]),
        ("10 unidades de pc", ["278"]),
        # En inglés, lo que va antes de otro producto o de un accesorio lo modifica.
        ("a laptop charger", []),
        ("a computer desk", ["4191"]),
        ("laptop backpack", ["100"]),
        ("Buy me 5 computers by Friday", ["278"]),
    ],
)
def test_nombradas_con_de_y_modificadores(arbol, texto, esperado):
    assert [i for i, _ in nombradas(texto, arbol)] == esperado
