"""Categoría por palabras: determinística, del subconjunto del repo y sin modelo."""

import pytest

from taxonomia import cargar_subconjunto
from taxonomia.palabras import FRASES, categoria_por_palabras, normalizar


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
