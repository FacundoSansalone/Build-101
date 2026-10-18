"""Fuente del fabricante por GTIN: formato Icecat, fixture rotulado y cliente
Icecat apagado por defecto. Sin red: el cliente real usa respuestas simuladas."""

import json

import httpx
import pytest

from contracts import ValorAtributo
from fabricante import (
    FixtureFabricante,
    IcecatFabricante,
    datos_externos,
    fuente_desde_entorno,
    parsear_icecat,
)
from guard.fuentes import DatoExterno

GTIN = "2001234000031"
GTIN14 = "0" + GTIN


def respuesta_icecat(gtin=GTIN, ram="8", msg="OK"):
    return {
        "msg": msg,
        "data": {
            "GeneralInfo": {
                "IcecatId": 1,
                "Title": "Zento Slim 14",
                "Brand": "Zento",
                "ProductName": "Slim 14",
                "GTIN": [gtin],
                "Category": {"CategoryID": "151", "Name": {"Value": "Notebooks"}},
            },
            "FeaturesGroups": [
                {
                    "FeatureGroup": {"Name": {"Value": "Memory"}},
                    "Features": [
                        {
                            "Type": "numerical", "Value": ram, "PresentationValue": f"{ram} GB",
                            "Feature": {"Name": {"Value": "Internal memory"}, "Measure": {"Signs": {"_": "GB"}}},
                        }
                    ],
                },
                {
                    "FeatureGroup": {"Name": {"Value": "Display"}},
                    "Features": [
                        {
                            "Type": "numerical", "Value": "35.6",
                            "Feature": {"Name": {"Value": "Display diagonal"}, "Measure": {"Signs": {"_": "cm"}}},
                        },
                        {
                            "Type": "y_n", "Value": "N",
                            "Feature": {"Name": {"Value": "Touchscreen"}, "Measure": {"Signs": {"_": ""}}},
                        },
                    ],
                },
                {
                    "FeatureGroup": {"Name": {"Value": "Ports & interfaces"}},
                    "Features": [
                        {
                            "Type": "multi_dropdown", "Value": "Bluetooth,USB-C",
                            "Feature": {"Name": {"Value": "Connectivity technology"}},
                        },
                        {"Type": "numerical", "Value": "", "Feature": {"Name": {"Value": "Vacío"}}},
                        "basura",
                    ],
                },
            ],
        },
    }


# -- formato Icecat -------------------------------------------------------------


def test_parsea_marca_y_atributos_con_nombres_canonicos():
    ficha = parsear_icecat(respuesta_icecat(), gtin=GTIN14, fuente="icecat")
    assert ficha.gtin == GTIN14 and ficha.marca == "Zento"
    assert ficha.nombre == "Zento Slim 14" and ficha.categoria == "Notebooks"
    assert ficha.atributos["memoria_ram"] == ValorAtributo(valor="8", unidad="GB")
    assert ficha.atributos["tamano_pantalla"] == ValorAtributo(valor="35.6", unidad="cm")
    assert ficha.atributos["touchscreen"] == ValorAtributo(valor="N")
    assert ficha.atributos["conectividad"] == ValorAtributo(valor=["Bluetooth", "USB-C"])
    assert "vacio" not in ficha.atributos
    assert ficha.atribucion == "Specs Icecat"


@pytest.mark.parametrize(
    "datos",
    [
        respuesta_icecat(gtin="2001234000017"),  # el producto es otro
        respuesta_icecat(msg="Error"),
        {"msg": "OK"},
        {"msg": "OK", "data": {"GeneralInfo": "x"}},
        [],
        "no es json",
    ],
)
def test_respuestas_que_no_sirven(datos):
    assert parsear_icecat(datos, gtin=GTIN14, fuente="icecat") is None


def test_a_dato_externo_lleva_el_origen_y_el_rotulo():
    ficha = parsear_icecat(respuesta_icecat(), gtin=GTIN14, fuente="fixture", fixture=True)
    dato = ficha.a_dato_externo()
    assert isinstance(dato, DatoExterno)
    assert dato.origen == "fabricante" and dato.fixture and dato.fuente == "fixture"
    assert dato["brand"] == "Zento"
    assert dato["atributos"]["memoria_ram"] == {"valor": "8", "unidad": "GB"}


# -- fixture --------------------------------------------------------------------


@pytest.fixture
def carpeta(tmp_path):
    (tmp_path / f"{GTIN}.json").write_text(json.dumps(respuesta_icecat()), "utf-8")
    (tmp_path / "roto.json").write_text("{no es json", "utf-8")
    return tmp_path


def test_fixture_rotula_cada_resultado(carpeta):
    fuente = FixtureFabricante(carpeta)
    encontrado = fuente.consultar(GTIN)
    assert encontrado.fixture and encontrado.fuente == "fixture" and encontrado.motivo == "ok"
    assert encontrado.ficha.fixture and encontrado.ficha.atributos["memoria_ram"].valor == "8"
    falta = fuente.consultar("2001234000017")
    assert falta.ficha is None and falta.sin_dato and falta.motivo == "no_encontrado"
    assert falta.fixture and falta.fuente == "fixture"  # también "sin dato" va rotulado
    invalido = fuente.consultar("123")
    assert invalido.sin_dato and invalido.motivo == "gtin_invalido" and invalido.fixture


def test_datos_externos_solo_con_lo_encontrado(carpeta):
    fuente = FixtureFabricante(carpeta)
    datos = datos_externos([fuente.consultar(GTIN), fuente.consultar("2001234000017")])
    assert len(datos) == 1 and datos[0].fixture


# -- Icecat (respuestas simuladas) -----------------------------------------------


def _transporte(manejar, pedidos=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if pedidos is not None:
            pedidos.append(request)
        return manejar(request)

    return httpx.MockTransport(handler)


def _icecat(tmp_path, manejar, pedidos=None, **kwargs):
    return IcecatFabricante(
        usuario="usuario-demo", api_token="tok-api", cache_dir=tmp_path,
        transport=_transporte(manejar, pedidos), **kwargs,
    )


def test_icecat_pide_por_gtin_con_credenciales_en_cabeceras(tmp_path):
    pedidos: list[httpx.Request] = []
    fuente = _icecat(tmp_path, lambda r: httpx.Response(200, json=respuesta_icecat()), pedidos)
    resultado = fuente.consultar(GTIN)
    assert resultado.motivo == "ok" and not resultado.fixture and resultado.fuente == "icecat"
    (pedido,) = pedidos
    assert pedido.url.host == "live.icecat.biz"
    assert pedido.url.params["GTIN"] == GTIN
    assert pedido.url.params["shopname"] == "usuario-demo"
    assert pedido.url.params["lang"] == "EN"
    assert pedido.headers["api-token"] == "tok-api"
    assert "tok-api" not in str(pedido.url)  # el token no va en la URL


def test_icecat_usa_la_cache(tmp_path):
    pedidos: list = []
    manejar = lambda r: httpx.Response(200, json=respuesta_icecat())  # noqa: E731
    assert _icecat(tmp_path, manejar, pedidos).consultar(GTIN).motivo == "ok"
    # Otra instancia, misma carpeta: sale de la caché en disco.
    otra = _icecat(tmp_path, manejar, pedidos)
    assert otra.consultar(GTIN).ficha.marca == "Zento"
    assert len(pedidos) == 1


def _lanza(error):
    def manejar(request):
        raise error

    return manejar


@pytest.mark.parametrize(
    ("manejar", "motivo"),
    [
        (lambda r: httpx.Response(404, json={"StatusCode": 4, "Message": "not found"}), "no_encontrado"),
        (lambda r: httpx.Response(200, json=respuesta_icecat(msg="Error")), "no_encontrado"),
        (lambda r: httpx.Response(200, json=respuesta_icecat(gtin="2001234000017")), "no_encontrado"),
        (lambda r: httpx.Response(500, text="boom"), "error_http"),
        (lambda r: httpx.Response(401, json={"Message": "auth"}), "error_http"),
        (lambda r: httpx.Response(200, text="<html>"), "formato"),
        (_lanza(httpx.ReadTimeout("lento")), "timeout"),
        (_lanza(httpx.ConnectError("sin red")), "error_http"),
    ],
)
def test_icecat_si_falla_devuelve_sin_dato(tmp_path, manejar, motivo):
    resultado = _icecat(tmp_path, manejar).consultar(GTIN)
    assert resultado.sin_dato and resultado.ficha is None
    assert resultado.motivo == motivo


def test_icecat_no_guarda_en_cache_un_error(tmp_path):
    pedidos: list = []
    _icecat(tmp_path, lambda r: httpx.Response(500), pedidos).consultar(GTIN)
    _icecat(tmp_path, lambda r: httpx.Response(200, json=respuesta_icecat()), pedidos).consultar(GTIN)
    assert len(pedidos) == 2


def test_icecat_gtin_invalido_no_consulta(tmp_path):
    pedidos: list = []
    resultado = _icecat(tmp_path, lambda r: httpx.Response(200), pedidos).consultar("123")
    assert resultado.motivo == "gtin_invalido" and pedidos == []


def test_icecat_respuesta_demasiado_grande(tmp_path):
    grande = {"msg": "OK", "relleno": "x" * (3 * 1024 * 1024)}
    resultado = _icecat(tmp_path, lambda r: httpx.Response(200, json=grande)).consultar(GTIN)
    assert resultado.motivo == "formato"


# -- entorno: fixture por defecto, Icecat apagado ---------------------------------


def test_por_defecto_es_el_fixture():
    assert isinstance(fuente_desde_entorno({}), FixtureFabricante)
    assert isinstance(fuente_desde_entorno({"FABRICANTE_FUENTE": "fixture"}), FixtureFabricante)


def test_icecat_se_prende_con_credenciales(tmp_path):
    fuente = fuente_desde_entorno({
        "FABRICANTE_FUENTE": "icecat", "ICECAT_USUARIO": "yo", "ICECAT_API_TOKEN": "t",
        "ICECAT_TIMEOUT": "2.5", "ICECAT_CACHE_DIR": str(tmp_path), "ICECAT_IDIOMA": "ES",
    })
    assert isinstance(fuente, IcecatFabricante)
    assert (fuente.usuario, fuente.api_token, fuente.timeout, fuente.idioma) == ("yo", "t", 2.5, "ES")


@pytest.mark.parametrize(
    "entorno",
    [
        {"FABRICANTE_FUENTE": "icecat"},  # sin usuario
        {"FABRICANTE_FUENTE": "otra"},
        {"FABRICANTE_FUENTE": "icecat", "ICECAT_USUARIO": "yo", "ICECAT_TIMEOUT": "x"},
        {"FABRICANTE_FUENTE": "icecat", "ICECAT_USUARIO": "yo", "ICECAT_IDIOMA": "../x"},
    ],
)
def test_configuracion_invalida(entorno):
    with pytest.raises(ValueError):
        fuente_desde_entorno(entorno)


def test_ninguna_apaga_la_fuente():
    assert fuente_desde_entorno({"FABRICANTE_FUENTE": "ninguna"}) is None
