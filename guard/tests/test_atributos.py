"""Atributos genéricos: nombres, valores, unidades y cumplimiento por tipo.

Todo determinístico: número (igual/máximo/mínimo, con GB/TB/MB, in/cm,
kg/g), enumerado, conjunto (`incluye`) y booleano.
"""

import pytest

from contracts import Requisito, ValorAtributo
from guard.atributos import (
    atributos_validos,
    clave_atributo,
    comparar,
    como_booleano,
    formatear_atributo,
    formatear_en_unidad,
    iguales_atributo,
    valor_atributo,
)
from guard.normalizar import gtin_valido


def V(valor, unidad=None):
    return ValorAtributo(valor=valor, unidad=unidad)


# -- GTIN -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [
        ("4006381333931", "04006381333931"),       # EAN-13
        ("036000291452", "00036000291452"),        # UPC-A (GTIN-12)
        ("96385074", "00000096385074"),            # GTIN-8
        ("00036000291452", "00036000291452"),      # GTIN-14
        (" 4006-3813-3393-1 ", "04006381333931"),  # con espacios y guiones
        (4006381333931, "04006381333931"),         # número
    ],
)
def test_gtin_valido_se_normaliza_a_14(crudo, esperado):
    assert gtin_valido(crudo) == esperado


@pytest.mark.parametrize(
    "crudo",
    ["4006381333932", "123", "40063813339311234", "abc4006381333931", "", None, True, 1.5, []],
)
def test_gtin_invalido(crudo):
    assert gtin_valido(crudo) is None


# -- nombres --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("nombre", "clave"),
    [
        ("memoria_ram", "memoria_ram"),
        ("Memoria RAM", "memoria_ram"),
        ("Internal memory", "memoria_ram"),       # nombre de Icecat
        ("RAM", "memoria_ram"),
        ("Total storage capacity", "almacenamiento"),
        ("Display diagonal", "tamano_pantalla"),
        ("Tamaño de pantalla", "tamano_pantalla"),
        ("peso", "weight"),                       # schema.org: weight
        ("Weight", "weight"),
        ("Colour", "color"),
        ("Active Noise Cancelling (ANC)", "cancelacion_ruido"),
        ("Battery life", "autonomia_bateria"),
        ("Connectivity technology", "conectividad"),
        ("gtin13", "gtin"),
        ("Algo raro-que no conozco", "algo_raro_que_no_conozco"),
    ],
)
def test_clave_atributo(nombre, clave):
    assert clave_atributo(nombre) == clave


# -- valores --------------------------------------------------------------------


def test_valor_en_formato_schema_org_o_propio():
    assert valor_atributo({"valor": 16, "unidad": "GB"}) == V(16, "GB")
    assert valor_atributo({"value": "16", "unitCode": "E34"}) == V("16", "E34")
    assert valor_atributo({"value": 16, "unitText": "GB"}) == V(16, "GB")
    assert valor_atributo(True) == V(True)
    assert valor_atributo(["bluetooth", "usb-c"]) == V(["bluetooth", "usb-c"])


@pytest.mark.parametrize(
    "crudo",
    [None, {}, {"unidad": "GB"}, float("nan"), "x" * 500, {"valor": 16, "unidad": "G" * 40}, [{"a": 1}], list(range(50))],
)
def test_valores_invalidos_se_descartan(crudo):
    assert valor_atributo(crudo) is None


def test_atributos_validos_canonicaliza_y_descarta_ambiguos():
    atributos = atributos_validos(
        {
            "Memoria RAM": {"valor": 16, "unidad": "GB"},
            "ram": {"valor": 8, "unidad": "GB"},          # misma clave, otro valor: ambiguo
            "Weight": {"valor": 1.3, "unidad": "kg"},
            "peso": {"valor": 1300, "unidad": "g"},        # misma clave, mismo valor: vale
            "conectividad": ["bluetooth"],
            "basura": {"x": 1},
        }
    )
    assert "memoria_ram" not in atributos  # sin dato: el obligatorio escala
    assert atributos["weight"] == V(1.3, "kg")
    assert atributos["conectividad"] == V(["bluetooth"])
    assert "basura" not in atributos


@pytest.mark.parametrize("crudo", [None, "16 GB", [1, 2], 42])
def test_atributos_que_no_son_un_dict(crudo):
    assert atributos_validos(crudo) == {}


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [(True, True), (False, False), ("Y", True), ("N", False), ("sí", True), ("no", False),
     ("yes", True), ("false", False), ("16", None), (1, None), (None, None)],
)
def test_como_booleano(crudo, esperado):
    assert como_booleano(crudo) is esperado


# -- cumplimiento por tipo ------------------------------------------------------


@pytest.mark.parametrize(
    ("observado", "operador", "valor", "unidad", "esperado"),
    [
        # número con unidades de datos (decimales: 1 TB = 1000 GB)
        (V(16, "GB"), "minimo", 16, "GB", True),
        (V(8, "GB"), "minimo", 16, "GB", False),
        (V(1, "TB"), "minimo", 512, "GB", True),
        (V(512000, "MB"), "igual", 512, "GB", True),
        (V("16", "E34"), "minimo", 16, "GB", True),   # código UN/CEFACT de schema.org
        (V("16 GB"), "minimo", 16, "GB", True),       # número y unidad en el texto
        (V(32, "GB"), "maximo", 16, "GB", False),
        # longitud: 35,6 cm ≈ 14" (redondeo de la conversión publicada)
        (V(35.6, "cm"), "minimo", 14, "in", True),
        (V(35.6, "cm"), "igual", 14, "in", True),
        (V(33.8, "cm"), "minimo", 14, "in", False),
        (V(15.6, '"'), "minimo", 14, "in", True),
        # masa: 1300 g = 1,3 kg
        (V(1300, "g"), "maximo", 1.5, "kg", True),
        (V(1.8, "kg"), "maximo", 1.5, "kg", False),
        (V(250, "g"), "maximo", 0.3, "kg", True),
        # tiempo
        (V(30, "h"), "minimo", 20, "h", True),
        # sin unidad en los dos lados
        (V(4), "minimo", 2, None, True),
        # incomparables: dimensiones distintas, unidad de un solo lado, no número
        (V(16, "GB"), "minimo", 14, "in", None),
        (V(16), "minimo", 16, "GB", None),
        (V("mucha"), "minimo", 16, "GB", None),
        # unidad desconocida: solo si es la misma
        (V(40, "dB"), "minimo", 30, "dB", True),
        (V(40, "dB"), "minimo", 30, "W", None),
    ],
)
def test_numeros_con_unidades(observado, operador, valor, unidad, esperado):
    assert comparar(observado, operador, valor, unidad) is esperado


@pytest.mark.parametrize(
    ("observado", "operador", "valor", "esperado"),
    [
        # booleano
        (V(True), "igual", True, True),
        (V("Y"), "igual", True, True),
        (V("N"), "igual", True, False),
        (V("tal vez"), "igual", True, None),
        # enumerado
        (V("Space Gray"), "igual", "space gray", True),
        (V("Silver"), "igual", "space gray", False),
        # conjunto
        (V(["Bluetooth", "USB-C"]), "incluye", "bluetooth", True),
        (V(["Bluetooth", "USB-C"]), "incluye", ["bluetooth", "usb c"], True),
        (V(["Bluetooth"]), "incluye", ["bluetooth", "usb-c"], False),
        (V("bluetooth"), "incluye", "bluetooth", True),
        (V(["bluetooth"]), "incluye", [], None),
        # un operador que no aplica
        (V(16, "GB"), "cubre", 16, None),
    ],
)
def test_booleanos_enumerados_y_conjuntos(observado, operador, valor, esperado):
    assert comparar(observado, operador, valor, None) is esperado


@pytest.mark.parametrize(
    ("observado", "operador", "valor", "unidad", "esperado"),
    [
        # "1,500" y "1.500" son 1500 o 1,5 según el país: no se interpretan
        (V("1,500 g"), "maximo", 1, "kg", None),
        (V("1.500 g"), "maximo", 1, "kg", None),
        (V("1,500,000 g"), "maximo", 1, "kg", None),
        (V("1.500", "g"), "maximo", 1, "kg", None),
        (V("1,000 GB"), "igual", 1, "TB", None),
        # un solo separador que no agrupa miles: decimal
        (V("1,5 kg"), "maximo", 1.5, "kg", True),
        (V("1.5 kg"), "maximo", 1.5, "kg", True),
        (V("1,5 kg"), "minimo", 1.6, "kg", False),
        (V("0,500 kg"), "maximo", 1, "kg", True),   # un grupo de miles no empieza con 0
        (V("1500 g"), "maximo", 1, "kg", False),
        # miles y decimales con separadores distintos: solo si es inequívoco
        (V("1,234.5 g"), "igual", 1234.5, "g", True),
        (V("1.234,5 g"), "igual", 1234.5, "g", True),
        (V("1.23,4 g"), "igual", 123.4, "g", None),
        (V("1,2.3 g"), "igual", 12.3, "g", None),
        (V("1.234.5 g"), "igual", 1234.5, "g", None),
    ],
)
def test_numeros_con_separadores_de_miles(observado, operador, valor, unidad, esperado):
    assert comparar(observado, operador, valor, unidad) is esperado


@pytest.mark.parametrize(
    ("observado", "valor", "esperado"),
    [
        # letras de cualquier alfabeto: no se borran
        ("черный", "белый", False),
        ("黑色", "白色", False),
        ("黑色", "黑色", True),
        ("Черный", "черный", True),     # sin mayúsculas
        ("ガ", "カ", False),             # la marca de sonorización no es una tilde
        ("😀", "😎", False),             # sin letras: se compara el texto
        ("ＵＳＢ-Ｃ", "usb c", True),     # ancho completo (NFKC)
        ("USB_C", "usb c", True),        # `_` separa como `-` y el espacio
        # el castellano sigue sin tildes
        ("Básquet", "basquet", True),
        ("Año", "ano", True),
        ("USB-C", "USBC", False),
    ],
)
def test_enumerados_en_cualquier_alfabeto(observado, valor, esperado):
    assert comparar(V(observado), "igual", valor) is esperado
    assert iguales_atributo(V(observado), V(valor)) is esperado


def test_iguales_atributo_para_la_evidencia():
    assert iguales_atributo(V(16, "GB"), V("16", "E34")) is True
    assert iguales_atributo(V(14, "in"), V(35.6, "cm")) is True
    assert iguales_atributo(V(16, "GB"), V(8, "GB")) is False
    assert iguales_atributo(V(True), V("Y")) is True
    assert iguales_atributo(V(["usb-c", "Bluetooth"]), V(["bluetooth", "USB C"])) is True
    assert iguales_atributo(V(16, "GB"), V(16)) is None  # unidad de un solo lado


def test_formatear_atributo():
    assert formatear_atributo(V(16, "GB")) == "16 GB"
    assert formatear_atributo(V(1.5, "kg")) == "1.5 kg"
    assert formatear_atributo(V(True)) == "sí"
    assert formatear_atributo(V(["bluetooth", "usb-c"])) == "bluetooth, usb-c"
    assert formatear_atributo(None) == "sin dato"


def test_formatear_atributo_muestra_los_codigos_un_cefact_como_simbolos():
    # schema.org trae `unitCode`; en un texto se lee el símbolo.
    assert formatear_atributo(V(16, "E34")) == "16 GB"
    assert formatear_atributo(V(61, "CMT")) == "61 cm"
    assert formatear_atributo(V(1.35, "KGM")) == "1.35 kg"
    assert formatear_atributo(V(40, "dB")) == "40 dB"  # desconocida: tal cual


# -- el valor en la unidad del requisito ------------------------------------------


@pytest.mark.parametrize(
    ("observado", "unidad", "esperado"),
    [
        # otra unidad de la misma dimensión: la del requisito y, entre paréntesis, la original
        (V(61, "cm"), "in", "24 in (61 cm)"),
        (V(35.6, "cm"), "in", "14 in (35.6 cm)"),
        (V(68.6, "CMT"), "in", "27 in (68.6 cm)"),
        (V(310, "g"), "kg", "0.31 kg (310 g)"),
        (V(1.8, "kg"), "g", "1800 g (1.8 kg)"),
        (V(8, "GiB"), "GB", "8.59 GB (8 GiB)"),
        (V(1, "TB"), "GB", "1000 GB (1 TB)"),
        (V(512, "GB"), "TB", "0.512 TB (512 GB)"),
        (V(90, "min"), "h", "1.5 h (90 min)"),
        (V(1080, "min"), "horas", "18 horas (1080 min)"),
        (V("61 cm"), "in", "24 in (61 cm)"),       # número y unidad en el texto
        # misma unidad con otro nombre: solo la del requisito
        (V(16, "E34"), "GB", "16 GB"),
        (V(27, "pulgadas"), "in", "27 in"),
        (V(15.6, '"'), "in", "15.6 in"),
        # sin conversión: igual que formatear_atributo
        (V(16, "GB"), "GB", "16 GB"),
        (V(16, "GB"), None, "16 GB"),               # requisito sin unidad
        (V(16, "GB"), "in", "16 GB"),               # otra dimensión
        (V(40, "dB"), "W", "40 dB"),                # unidad desconocida
        (V(4), "GB", "4"),                          # unidad de un solo lado
        (V(True), "GB", "sí"),
        (V(["bluetooth", "usb-c"]), "GB", "bluetooth, usb-c"),
        (None, "in", "sin dato"),
    ],
)
def test_formatear_en_unidad(observado, unidad, esperado):
    assert formatear_en_unidad(observado, unidad) == esperado


def test_formatear_en_unidad_acepta_un_dict():
    assert formatear_en_unidad({"valor": 61, "unidad": "cm"}, "in") == "24 in (61 cm)"


@pytest.mark.parametrize(
    ("unidad_observada", "factor", "unidad_pedida", "pedido"),
    [("cm", 2.54, "in", 27), ("g", 1000, "kg", 1.5), ("MB", 1000, "GB", 16), ("min", 60, "h", 20)],
)
def test_lo_que_no_cumple_nunca_se_muestra_igual_a_lo_pedido(
    unidad_observada, factor, unidad_pedida, pedido
):
    """El redondeo para mostrar no puede hacer que un valor que no cumple se
    lea igual a lo pedido ("27 in (68 cm) no cumple 27 in")."""
    texto_pedido = formatear_atributo(V(pedido, unidad_pedida))
    for paso in range(-300, 301):
        observado = round(pedido * factor * (1 + paso / 10_000), 4)  # ±3 % alrededor
        if comparar(V(observado, unidad_observada), "igual", pedido, unidad_pedida) is False:
            mostrado = formatear_en_unidad(V(observado, unidad_observada), unidad_pedida)
            assert not mostrado.startswith(f"{texto_pedido} ("), (observado, mostrado)


def test_requisito_de_atributo_necesita_nombre():
    with pytest.raises(ValueError):
        Requisito(campo="atributo", operador="minimo", valor=16, obligatorio=True)


def test_wireless_es_inalambrico():
    # Icecat dice "Wireless"; el dataset y el intake, "inalambrico".
    from guard.atributos import clave_atributo

    assert clave_atributo("Wireless") == "inalambrico"
    assert clave_atributo("Inalámbrica") == "inalambrico"
