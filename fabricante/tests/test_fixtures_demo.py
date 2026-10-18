"""Los fixtures del fabricante que exporta world/ (fixtures/fabricante/)."""

import pytest

from fabricante import FixtureFabricante

GTINS_DEMO = [
    "2001234000017", "2001234000024", "2001234000031", "2001234000048",
    "2004567000013", "2004567000020", "2004567000037", "2004567000044",
]


@pytest.mark.parametrize("gtin", GTINS_DEMO)
def test_cada_gtin_de_la_demo_tiene_su_ficha_rotulada(gtin):
    resultado = FixtureFabricante().consultar(gtin)
    assert resultado.motivo == "ok" and resultado.fixture and resultado.fuente == "fixture"
    assert resultado.ficha.marca and resultado.ficha.atributos
    assert resultado.ficha.atribucion == "Specs Icecat"


def test_los_nombres_de_icecat_quedan_canonicos():
    notebook = FixtureFabricante().consultar("2001234000031").ficha
    assert set(notebook.atributos) >= {"memoria_ram", "almacenamiento", "tamano_pantalla", "weight"}
    auriculares = FixtureFabricante().consultar("2004567000037").ficha
    assert auriculares.atributos["cancelacion_ruido"].valor == "N"
    assert auriculares.atributos["conectividad"].valor == ["Bluetooth"]
