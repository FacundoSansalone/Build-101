"""Tests del adaptador del benchmark (sin red: httpx se reemplaza)."""

import json

import httpx
import pytest

from guard import benchmark_hook
from guard.benchmark_hook import cargar_catalogo, evaluate


class _RespuestaFalsa:
    def __init__(self, datos, status_code=200):
        self._datos = datos
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)

    def json(self):
        return self._datos


@pytest.fixture(autouse=True)
def sin_cache_ni_red(monkeypatch):
    """Cada test arranca sin caché y sin servidor (falla la conexión)."""
    monkeypatch.setattr(benchmark_hook, "_CACHE", {})

    def sin_servidor(*args, **kwargs):
        raise httpx.ConnectError("sin servidor")

    monkeypatch.setattr(httpx, "get", sin_servidor)


def _offers_seen(datos: dict) -> dict:
    return {"carts": datos["carts"], "offer_details": datos["offer_details"]}


def test_sin_servidor_usa_el_fixture_del_catalogo():
    catalogo = cargar_catalogo()
    assert catalogo.fuente == "fixture"
    assert catalogo.comercio_id == "merchant_1"
    assert catalogo.productos["NK-OUT-01"]["price"] == 120.0


def test_con_servidor_usa_el_catalogo_en_vivo(monkeypatch):
    llamadas = []

    def get(url, timeout):
        llamadas.append((url, timeout))
        return _RespuestaFalsa(
            {"merchant": {"id": "m_vivo"}, "products": [{"sku": "S1", "price": 1.0}]}
        )

    monkeypatch.setattr(httpx, "get", get)
    catalogo = cargar_catalogo()
    assert catalogo.fuente == "http"
    assert catalogo.comercio_id == "m_vivo"
    assert set(catalogo.productos) == {"S1"}
    assert llamadas[0][0] == "http://localhost:8011/products"
    assert llamadas[0][1] <= 2.0  # timeout corto


@pytest.mark.parametrize(
    "respuesta",
    [_RespuestaFalsa({"products": "no es lista"}), _RespuestaFalsa({}, status_code=500)],
)
def test_respuesta_invalida_cae_al_fixture(monkeypatch, respuesta):
    monkeypatch.setattr(httpx, "get", lambda url, timeout: respuesta)
    assert cargar_catalogo().fuente == "fixture"


def test_el_catalogo_se_carga_una_sola_vez(monkeypatch):
    llamadas = []

    def get(url, timeout):
        llamadas.append(url)
        raise httpx.ConnectError("sin servidor")

    monkeypatch.setattr(httpx, "get", get)
    cargar_catalogo()
    cargar_catalogo()
    assert len(llamadas) == 1
    cargar_catalogo(forzar=True)
    assert len(llamadas) == 2


def test_evaluate_devuelve_dict_json_con_status(escenario):
    datos = escenario("baseline")
    resultado = evaluate(datos["intent"], _offers_seen(datos), "cart_1")
    assert isinstance(resultado, dict)
    assert resultado["status"] == "aprobado"
    assert resultado["cart_id_elegido"] == "cart_1"
    json.dumps(resultado)  # el benchmark lo guarda como JSON


@pytest.mark.parametrize(
    ("nombre", "status"),
    [("A", "bloqueado"), ("B", "bloqueado"), ("B2", "bloqueado"), ("C", "escalar")],
)
def test_evaluate_frena_el_objetivo_del_atacante(escenario, nombre, status):
    datos = escenario(nombre)
    objetivo = datos["expected"]["attack_target_cart_id"]
    assert evaluate(datos["intent"], _offers_seen(datos), objetivo)["status"] == status


def test_evaluate_pasa_el_comercio_del_catalogo(escenario, monkeypatch):
    recibido = {}
    original = benchmark_hook.evaluar

    def espia(ficha, ofertas_vistas, cart_id_elegido, catalogo, **extra):
        recibido["comercio_id"] = ofertas_vistas.comercio_id
        recibido["catalogo"] = catalogo
        return original(ficha, ofertas_vistas, cart_id_elegido, catalogo, **extra)

    monkeypatch.setattr(benchmark_hook, "evaluar", espia)
    datos = escenario("baseline")
    evaluate(datos["intent"], _offers_seen(datos), "cart_1")
    assert recibido["comercio_id"] == "merchant_1"
    assert "NK-OUT-01" in recibido["catalogo"]


def test_evaluate_no_modifica_lo_que_recibe(escenario):
    datos = escenario("B")
    offers_seen = _offers_seen(datos)
    copia = json.loads(json.dumps(offers_seen))
    evaluate(datos["intent"], offers_seen, "cart_4")
    assert offers_seen == copia
