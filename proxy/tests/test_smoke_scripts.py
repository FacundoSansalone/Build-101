"""Los scripts de prueba del proxy, contra el comercio y el catálogo falsos.

- `smoke_v1_general.py`: una compra de integrador por `/v1` de punta a punta,
  sin LLM de nuestro lado (gateway de prueba en el proceso o uno levantado).
- `smoke_checkout.py`: exige un proxy con las fichas de la demo.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from proxy.evaluador import EvaluadorNoDisponible, cargar_evaluador
from proxy.scripts.smoke_checkout import falta_ficha_demo
from proxy.scripts.smoke_v1_general import correr, elegir_cart, ficha_de, leer_args
from proxy.tests.ayudantes import evaluador_espia, solo_en_checkout
from proxy.tests.test_etapa3 import _sin_resultados
from proxy.tests.fake_world import (
    CATALOG_HOST,
    CATALOG_URL,
    MERCHANT_HOST,
    MERCHANT_URL,
    FakeMerchant,
    RuteoPorHost,
    app_catalogo,
)
from proxy.tests.gateway_ayudantes import CLAVE_ACME, armar_gateway

pytestmark = pytest.mark.anyio

ZAPATILLAS = [
    "--merchant-url", MERCHANT_URL, "--catalogo-url", CATALOG_URL, "--escenario", "baseline",
    "--pedido", "zapatillas Nike para exterior, talle 42", "--tope", "150",
    "--categoria", "187",
]


@pytest.fixture
def merchant() -> FakeMerchant:
    return FakeMerchant()


@pytest.fixture
def mundo(merchant: FakeMerchant) -> RuteoPorHost:
    return RuteoPorHost({MERCHANT_HOST: merchant.app, CATALOG_HOST: app_catalogo(por_sku=True)})


async def _correr(argv: list[str], **kwargs: Any) -> tuple[int, list[str]]:
    lineas: list[str] = []
    codigo = await correr(leer_args(argv), salida=lineas.append, **kwargs)
    return codigo, lineas


# -- la ficha y el cart ---------------------------------------------------------------


def test_la_ficha_lleva_tope_moneda_cantidad_y_categoria():
    ficha = ficha_de(leer_args(["--tope", "400", "--categoria", "305"]))
    assert ficha == {
        "requisitos": [
            {"campo": "precio", "operador": "maximo", "valor": 400.0, "obligatorio": True},
            {"campo": "moneda", "operador": "igual", "valor": "USD", "obligatorio": True},
            {"campo": "cantidad", "operador": "minimo", "valor": 1, "obligatorio": True},
        ],
        "categoria_taxonomia": "305",
    }
    sin = ficha_de(leer_args(["--cantidad", "0", "--categoria", ""]))
    assert [r["campo"] for r in sin["requisitos"]] == ["precio", "moneda"]
    assert "categoria_taxonomia" not in sin


def test_elige_el_mas_barato_dentro_del_tope():
    carts = [
        {"cart_id": "a", "amount": 90.0, "currency": "EUR"},
        {"cart_id": "b", "amount": 120.0, "currency": "USD"},
        {"cart_id": "c", "amount": 100.0, "currency": "USD"},
        {"cart_id": "d", "amount": 300.0, "currency": "USD"},
        {"cart_id": "e", "amount": True, "currency": "USD"},
    ]
    assert elegir_cart(carts, 150, "USD")[0]["cart_id"] == "c"
    # Ninguno entra: el más barato igual (así el Guard evalúa y frena).
    assert elegir_cart(carts, 50, "USD")[0]["cart_id"] == "a"
    assert elegir_cart([{"cart_id": "x"}], 50, "USD")[0] is None


# -- gateway de prueba en el proceso ---------------------------------------------------


async def test_compra_por_v1_de_punta_a_punta(mundo, merchant):
    evaluador, llamadas = evaluador_espia("aprobado")
    codigo, lineas = await _correr([*ZAPATILLAS, "--esperado", "aprobado"],
                                   transporte=mundo, evaluador=evaluador)
    assert codigo == 0, lineas
    assert "status=aprobado" in lineas and lineas[-1] == "OK"
    assert "  ap2.checkout: llegó (para firmar)" in lineas
    # El Guard evaluó con la ficha de la compra (la del integrador).
    (llamada,) = llamadas
    ficha = llamada["ficha"]
    assert ficha.categoria_taxonomia == "187"
    assert [r.campo for r in ficha.requisitos] == ["precio", "moneda", "cantidad"]
    # El más barato de B dentro de 150 USD, y el checkout salió del comercio.
    assert llamada["cart"] == min(
        (c for c in llamada["ofertas"].carts if c["amount"] <= 150), key=lambda c: c["amount"]
    )["cart_id"]
    assert merchant.checkouts
    # La búsqueda llevó el pedido y el escenario al comercio.
    (busqueda, _), *_ = merchant.recibidos
    assert b"zapatillas Nike para exterior" in busqueda
    assert b'"demo_scenario": "baseline"' in busqueda


async def test_un_comercio_que_inyecta_se_corta_en_la_busqueda(mundo):
    """B lleva una inyección en el item_label: el filtro (solo heurísticas,
    sin LLM) corta la búsqueda y no se llega al checkout."""
    evaluador, llamadas = evaluador_espia("aprobado")
    argv = [a if a != "baseline" else "B" for a in ZAPATILLAS]
    codigo, lineas = await _correr(argv, transporte=mundo, evaluador=evaluador)
    assert codigo == 1
    assert "find_products: estado=failed" in " ".join(lineas)
    assert "FALLA: find_products no devolvió carts" in lineas
    assert llamadas == []


async def test_un_status_distinto_del_esperado_falla(mundo):
    evaluador, _ = evaluador_espia("escalar")
    codigo, lineas = await _correr([*ZAPATILLAS, "--esperado", "aprobado"],
                                   transporte=mundo, evaluador=evaluador)
    assert codigo == 1
    assert "status=escalar" in lineas
    assert "  ap2.checkout: no llegó" in lineas
    assert "FALLA: se esperaba aprobado y llegó escalar" in lineas


async def test_con_el_guard_real(mundo):
    try:
        guard = cargar_evaluador()
    except EvaluadorNoDisponible:
        pytest.skip("guard.evaluar() con checkout= no está disponible")
    codigo, lineas = await _correr(ZAPATILLAS, transporte=mundo, evaluador=guard)
    assert codigo == 0, lineas
    (status,) = [linea for linea in lineas if linea.startswith("status=")]
    assert status.removeprefix("status=") in ("aprobado", "con_avisos", "escalar", "bloqueado")
    assert any(
        linea.startswith("GET /v1/compras/cmp-") and "recibo=rcb-" in linea for linea in lineas
    )


async def test_un_cart_pedido_que_no_esta_falla(mundo):
    evaluador, llamadas = evaluador_espia("aprobado")
    codigo, lineas = await _correr([*ZAPATILLAS, "--cart", "cart_99"],
                                   transporte=mundo, evaluador=evaluador)
    assert codigo == 1 and "FALLA: no hay cart_99 entre los carts" in lineas
    assert llamadas == []


async def test_un_checkout_retenido_con_aprobado_es_una_falla(mundo, merchant):
    """Coherencia: si el veredicto dice aprobado, el checkout tiene que llegar."""
    evaluador, _ = evaluador_espia("aprobado")

    def sin_checkout(respuesta, pedido):
        for artefacto in respuesta["result"].get("artifacts") or []:
            artefacto["parts"] = [
                p for p in artefacto["parts"] if "ap2.checkout" not in (p.get("data") or {})
            ]
        return respuesta

    merchant.manipular = solo_en_checkout(sin_checkout)
    codigo, lineas = await _correr(ZAPATILLAS, transporte=mundo, evaluador=evaluador)
    assert codigo == 1


async def test_sin_comercio_levantado_falla_con_un_mensaje(merchant):
    evaluador, _ = evaluador_espia("aprobado")
    sin_comercio = RuteoPorHost({CATALOG_HOST: app_catalogo()})  # el comercio no responde
    codigo, lineas = await _correr(ZAPATILLAS, transporte=sin_comercio, evaluador=evaluador)
    assert codigo == 1
    assert any(linea.startswith("find_products: HTTP 502") for linea in lineas), lineas


async def test_sin_el_guard_real_el_gateway_de_prueba_no_arranca(mundo, monkeypatch):
    import proxy.evaluador as evaluador_mod

    def sin_guard(permitir_stub: bool = False):
        raise EvaluadorNoDisponible("guard/ no está")

    monkeypatch.setattr(evaluador_mod, "cargar_evaluador", sin_guard)
    codigo, lineas = await _correr(ZAPATILLAS, transporte=mundo)
    assert codigo == 1 and "guard/ no está" in lineas[-1]


# -- contra un gateway levantado ------------------------------------------------------


async def test_contra_un_gateway_levantado_con_su_clave(config):
    evaluador, llamadas = evaluador_espia("aprobado")
    gateway = armar_gateway(config, evaluador)
    transporte = httpx.ASGITransport(app=gateway.app)
    codigo, lineas = await _correr(
        [*ZAPATILLAS, "--gateway", "http://gw.test:8030", "--clave", CLAVE_ACME,
         "--esperado", "aprobado"],
        transporte=transporte,
    )
    assert codigo == 0, lineas
    assert llamadas[0]["ficha"].categoria_taxonomia == "187"


async def test_contra_un_gateway_sin_clave_no_sigue(monkeypatch):
    monkeypatch.delenv("KAIAI_CLAVE", raising=False)
    codigo, lineas = await _correr(["--gateway", "http://gw.test:8030"])
    assert codigo == 1 and "hace falta la clave" in lineas[-1]


async def test_contra_un_gateway_con_otra_clave_falla(config):
    evaluador, _ = evaluador_espia("aprobado")
    gateway = armar_gateway(config, evaluador)
    codigo, lineas = await _correr(
        [*ZAPATILLAS, "--gateway", "http://gw.test:8030", "--clave", "psg_000000000000_nada"],
        transporte=httpx.ASGITransport(app=gateway.app),
    )
    assert codigo == 1
    assert lineas[0].startswith("POST /v1/compras → 401")


async def test_contra_un_gateway_caido(monkeypatch):
    def caido(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no responde", request=request)

    codigo, lineas = await _correr(
        ["--gateway", "http://gw.test:8030", "--clave", CLAVE_ACME],
        transporte=httpx.MockTransport(caido),
    )
    assert codigo == 1 and "no se pudo hablar con el gateway" in lineas[-1]


# -- (f) robustez: sin resultados y moneda distinta ----------------------------------


async def test_sin_resultados_no_pide_checkout(merchant):
    merchant.manipular = _sin_resultados
    transporte = RuteoPorHost({MERCHANT_HOST: merchant.app, CATALOG_HOST: app_catalogo()})
    evaluador, llamadas = evaluador_espia("aprobado")
    lineas: list[str] = []
    codigo = await correr(
        leer_args(["--merchant-url", MERCHANT_URL, "--catalogo-url", CATALOG_URL,
                   "--escenario", "baseline", "--pedido", "un monitor"]),
        transporte=transporte, evaluador=evaluador, salida=lineas.append,
    )
    assert codigo == 1 and "FALLA: find_products no devolvió carts" in lineas
    assert llamadas == [] and merchant.checkouts == []


async def test_moneda_distinta_de_la_del_comercio_bloquea_por_mandato(merchant):
    """Un pedido en ARS a un comercio que cotiza en USD: el Guard real
    bloquea por el mandato (no llega a la firma)."""
    try:
        guard = cargar_evaluador()
    except EvaluadorNoDisponible:
        pytest.skip("guard.evaluar() con checkout= no está disponible")
    transporte = RuteoPorHost({MERCHANT_HOST: merchant.app, CATALOG_HOST: app_catalogo()})
    lineas: list[str] = []
    codigo = await correr(
        leer_args(["--merchant-url", MERCHANT_URL, "--catalogo-url", CATALOG_URL,
                   "--escenario", "baseline", "--pedido", "zapatillas Nike talle 42",
                   "--moneda", "ARS", "--tope", "200000", "--cantidad", "0",
                   "--categoria", "187", "--esperado", "bloqueado"]),
        transporte=transporte, evaluador=guard, salida=lineas.append,
    )
    assert codigo == 0, lineas
    assert "  chequeo mandato: bloquea" in lineas
    assert "  ap2.checkout: no llegó" in lineas


# -- smoke_checkout: exige las fichas de la demo -----------------------------------------


@pytest.mark.parametrize(
    ("estado", "sirve"),
    [
        ({"ficha_demo": True}, True),
        ({"guard_activo": True}, True),  # un proxy de antes (la ficha de la demo por defecto)
        ({"ficha_demo": False}, False),
        (None, False),
        (["no", "es", "objeto"], False),
    ],
)
def test_smoke_checkout_exige_la_ficha_de_la_demo(estado, sirve):
    falla = falta_ficha_demo(estado)
    assert (falla is None) is sirve
    if estado == {"ficha_demo": False}:
        assert "PROXY_FICHA_DEMO=1" in falla
