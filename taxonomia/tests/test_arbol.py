"""Árbol de la taxonomía de productos de Google: parseo, navegación y subconjunto."""

import pytest

from taxonomia import Taxonomia, cargar_subconjunto, parsear

TEXTO = """\
# Google_Product_Taxonomy_Version: 2021-09-21
166 - Apparel & Accessories
187 - Apparel & Accessories > Shoes
222 - Electronics
223 - Electronics > Audio
2165 - Electronics > Audio > Audio Components
505771 - Electronics > Audio > Audio Components > Headphones & Headsets
543626 - Electronics > Audio > Audio Components > Headphones & Headsets > Headphones
543627 - Electronics > Audio > Audio Components > Headphones & Headsets > Headsets
278 - Electronics > Computers
328 - Electronics > Computers > Laptops
325 - Electronics > Computers > Desktop Computers
"""


@pytest.fixture
def arbol() -> Taxonomia:
    return parsear(TEXTO)


def test_parsea_ids_rutas_y_version(arbol):
    assert arbol.version == "2021-09-21"
    assert len(arbol) == 11
    laptops = arbol["328"]
    assert laptops.nombre == "Laptops"
    assert laptops.ruta == ("Electronics", "Computers", "Laptops")
    assert laptops.ruta_texto == "Electronics > Computers > Laptops"
    assert laptops.padre == "278"
    assert laptops.nivel == 3


def test_raices_e_hijos_en_el_orden_del_archivo(arbol):
    assert [c.id for c in arbol.raices()] == ["166", "222"]
    assert [c.id for c in arbol.hijos(None)] == ["166", "222"]
    assert [c.id for c in arbol.hijos("278")] == ["328", "325"]
    assert arbol.hijos("328") == []


def test_ancestros_y_descendencia(arbol):
    assert [c.id for c in arbol.ancestros("543626")] == ["222", "223", "2165", "505771"]
    assert arbol.es_descendiente("543626", "222")
    assert arbol.es_descendiente("328", "328")  # una categoría está en su propia rama
    assert not arbol.es_descendiente("328", "328", incluir_igual=False)
    assert not arbol.es_descendiente("328", "223")
    assert not arbol.es_descendiente("999", "222")  # desconocida: no


def test_busca_por_ruta(arbol):
    assert arbol.por_ruta("Electronics > Computers > Laptops").id == "328"
    assert arbol.por_ruta("electronics >computers> LAPTOPS").id == "328"
    assert arbol.por_ruta("Electronics > Nada") is None


def test_id_desconocido(arbol):
    assert arbol.get("999") is None
    assert "999" not in arbol and "328" in arbol
    with pytest.raises(KeyError):
        arbol["999"]
    with pytest.raises(KeyError):
        arbol.hijos("999")


@pytest.mark.parametrize(
    "texto",
    [
        "328 - Electronics > Computers > Laptops\n",  # sin sus ancestros
        "abc - Electronics\n",  # id no numérico
        "222 Electronics\n",  # sin separador
        "222 - Electronics\n222 - Electronics\n",  # id repetido
        "222 - Electronics\n223 - Electronics\n",  # ruta repetida
        "222 - \n",  # sin nombre
    ],
)
def test_lineas_invalidas_son_un_error(texto):
    with pytest.raises(ValueError):
        parsear(texto)


def test_ignora_lineas_vacias_y_comentarios():
    arbol = parsear("\n# comentario\n\n222 - Electronics\n\n")
    assert [c.id for c in arbol.raices()] == ["222"]
    assert arbol.version is None


def test_subconjunto_lleva_ancestros_hermanos_y_la_rama_completa(arbol):
    sub = arbol.subconjunto(["505771"])
    ids = {c.id for c in sub}
    # raíces y hermanos en el camino (para poder clasificar nivel por nivel)
    assert {"166", "222", "223", "278"} <= ids
    # la rama completa pedida
    assert {"505771", "543626", "543627"} <= ids
    # lo que no está en el camino ni en la rama, no
    assert "328" not in ids and "187" not in ids
    assert sub.version == arbol.version


def test_subconjunto_se_vuelve_a_parsear_igual(arbol):
    sub = arbol.subconjunto(["278", "187"])
    otra = parsear(sub.a_texto())
    assert [c.id for c in otra] == [c.id for c in sub]
    assert otra.version == sub.version


def test_subconjunto_del_repo_tiene_las_ramas_de_la_demo():
    arbol = cargar_subconjunto()
    assert arbol.version
    assert arbol["187"].ruta_texto == "Apparel & Accessories > Shoes"
    assert arbol["328"].ruta_texto == "Electronics > Computers > Laptops"
    assert arbol["543626"].ruta_texto == (
        "Electronics > Audio > Audio Components > Headphones & Headsets > Headphones"
    )
    # todas las raíces de la taxonomía, para que el primer nivel sea el real
    assert len(arbol.raices()) == 21
