"""Consultas al fabricante desde el proxy y el benchmark (`fabricante.consultas`).

Qué GTIN se consultan, que una fuente rota sea "sin dato" (nunca una
excepción) y el resumen que va en el evento `veredicto`. Sin red.
"""

from fabricante import FixtureFabricante, ResultadoFabricante
from fabricante.consultas import consultar_gtins, gtins_de_ofertas, resumen

ZENTO = "2001234000031"
LUMEN = "2001234000017"


class Rota:
    nombre = "rota"

    def consultar(self, gtin):
        raise TimeoutError("sin red")


def test_gtins_declarados_y_del_catalogo_sin_repetir_ni_invalidos():
    detalles = [
        {"sku": "ZT-NB-08", "gtin": ZENTO},
        {"sku": "LM-NB-16", "gtin": "0" + LUMEN},   # el mismo GTIN, en 14 dígitos
        {"sku": "X", "gtin": "123"},                # dígito verificador inválido
        "basura",
    ]
    productos = {"LM-NB-16": {"gtin": LUMEN}, "X": {"gtin": "4006381333931"}}
    assert gtins_de_ofertas(detalles, productos) == ["0" + ZENTO, "0" + LUMEN, "04006381333931"]


def test_una_fuente_rota_es_sin_dato():
    (resultado,) = consultar_gtins(Rota(), ["0" + ZENTO])
    assert isinstance(resultado, ResultadoFabricante)
    assert resultado.sin_dato and resultado.motivo == "error"
    assert resultado.fuente == "rota" and resultado.fixture is False


def test_sin_fuente_no_hay_consultas():
    assert consultar_gtins(None, ["0" + ZENTO]) == []
    assert resumen(None, []) == {"fuente": "ninguna", "fixture": False, "consultas": []}


def test_resumen_rotula_el_fixture():
    fuente = FixtureFabricante()
    resultados = consultar_gtins(fuente, ["0" + ZENTO, "04006381333931"])
    datos = resumen(fuente, resultados)
    assert datos["fuente"] == "fixture" and datos["fixture"] is True
    assert datos["consultas"] == [
        {"gtin": "0" + ZENTO, "con_dato": True, "motivo": "ok"},
        {"gtin": "04006381333931", "con_dato": False, "motivo": "no_encontrado"},
    ]
