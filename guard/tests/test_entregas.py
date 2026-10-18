"""Etapa 2: cantidad y cronograma de entregas (docs/FLUJO.md, paso 7).

El Guard evalúa contra los parámetros autorizados de la compra: el pedido
literal ("20 para el lunes") o el que aceptó el usuario ("10 el lunes y 10
el miércoles"). Una oferta que no los cumple se bloquea aunque el agente la
elija.
"""

import copy
import json
from datetime import date

import pytest

from contracts import Entrega, Ficha, OfertasVistas, Requisito
from guard import evaluar
from guard.chequeos import cumple, formatear
from guard.extractor import extraer_ofertas
from guard.normalizar import cubre_entregas, entregas_validas
from guard.tests.conftest import FIXTURES

LUN, MIE, JUE, VIE = "2026-10-19", "2026-10-21", "2026-10-22", "2026-10-23"


def _e(*pares):
    return [{"cantidad": c, "fecha": f} for c, f in pares]


@pytest.fixture
def computadoras() -> dict:
    return json.loads((FIXTURES / "etapa2" / "computadoras.json").read_text("utf-8"))


def _vistas(datos: dict) -> OfertasVistas:
    return OfertasVistas(
        carts=copy.deepcopy(datos["carts"]),
        offer_details=copy.deepcopy(datos["offer_details"]),
        comercio_id="merchant_1",
    )


def _ficha_aceptada(entregas: list[dict]) -> Ficha:
    """La ficha después de que el usuario aceptó el cronograma de la oferta."""
    return Ficha(
        pedido_original="Comprame 20 computadoras para el lunes, hasta 20000 USD.",
        requisitos=[
            Requisito(campo="precio", operador="maximo", valor=20000.0, obligatorio=True),
            Requisito(campo="moneda", operador="igual", valor="USD", obligatorio=True),
            Requisito(campo="cantidad", operador="igual", valor=20, obligatorio=True),
            Requisito(campo="entregas", operador="cubre", valor=entregas, obligatorio=True),
        ],
        confirmada=True,
    )


# -- cobertura del cronograma ---------------------------------------------------


def test_lo_ofrecido_cubre_la_necesidad_real():
    assert cubre_entregas(_e((10, LUN), (10, MIE)), _e((10, LUN), (10, JUE))) is True


def test_lo_ofrecido_no_cubre_el_pedido_literal():
    assert cubre_entregas(_e((10, LUN), (10, MIE)), _e((20, LUN))) is False


def test_el_viernes_llega_tarde_para_el_jueves():
    assert cubre_entregas(_e((10, LUN), (10, VIE)), _e((10, LUN), (10, JUE))) is False


def test_las_cantidades_se_acumulan():
    # 20 el lunes cubre "10 el lunes y 10 el jueves": llega todo antes.
    assert cubre_entregas(_e((20, LUN)), _e((10, LUN), (10, JUE))) is True
    # 5 + 5 el lunes cubre 10 el lunes.
    assert cubre_entregas(_e((5, LUN), (5, LUN)), _e((10, LUN))) is True


def test_acepta_las_claves_del_comercio_y_objetos_entrega():
    ofrecidas = [{"quantity": 10, "date": LUN}, {"quantity": 10, "date": MIE}]
    pedidas = [
        Entrega(cantidad=10, fecha=date(2026, 10, 19)),
        Entrega(cantidad=10, fecha=date(2026, 10, 22)),
    ]
    assert cubre_entregas(ofrecidas, pedidas) is True


@pytest.mark.parametrize(
    "invalido",
    [
        [],
        None,
        "10 el lunes",
        [{"cantidad": 0, "fecha": LUN}],
        [{"cantidad": -1, "fecha": LUN}],
        [{"cantidad": True, "fecha": LUN}],
        [{"cantidad": 10.5, "fecha": LUN}],
        [{"cantidad": 10, "fecha": "el lunes"}],
        [{"cantidad": 10, "fecha": LUN}, "x"],
        [{"cantidad": 10}],
    ],
)
def test_un_cronograma_invalido_no_se_puede_comparar(invalido):
    assert entregas_validas(invalido) is None
    assert cubre_entregas(invalido, _e((10, LUN))) is None
    assert cubre_entregas(_e((10, LUN)), invalido) is None


# -- extractor ------------------------------------------------------------------


def test_el_extractor_toma_cantidad_y_entregas_declaradas(computadoras):
    ofertas = extraer_ofertas(_vistas(computadoras))
    por_cart = {o.cart_id: o for o in ofertas}
    assert por_cart["cart_2"].cantidad == 20
    assert [(e.cantidad, e.fecha.isoformat()) for e in por_cart["cart_2"].entregas] == [
        (10, LUN),
        (10, MIE),
    ]


def test_un_cronograma_declarado_invalido_queda_sin_dato(computadoras):
    computadoras["offer_details"][1]["deliveries"].append({"quantity": "diez", "date": JUE})
    computadoras["offer_details"][1]["quantity"] = -3
    oferta = {o.cart_id: o for o in extraer_ofertas(_vistas(computadoras))}["cart_2"]
    assert oferta.entregas == []
    assert oferta.cantidad is None
    requisito = Requisito(
        campo="entregas", operador="cubre", valor=_e((20, LUN)), obligatorio=True
    )
    assert cumple(requisito, oferta) is None


# -- evaluar() contra los parámetros autorizados --------------------------------


def test_el_pedido_literal_bloquea_la_alternativa(computadoras):
    ficha = Ficha.desde_intent(computadoras["intent"])
    veredicto = evaluar(ficha, _vistas(computadoras), "cart_2", catalogo={})
    assert veredicto.status == "bloqueado"
    entregas = [c for c in veredicto.chequeos if c.campo == "entregas"]
    assert entregas and entregas[0].resultado == "bloquea"
    assert "no cubren" in entregas[0].detalle


def test_con_los_parametros_aceptados_la_alternativa_llega_a_la_firma(computadoras):
    ficha = _ficha_aceptada(_e((10, LUN), (10, MIE)))
    veredicto = evaluar(ficha, _vistas(computadoras), "cart_2", catalogo={})
    assert veredicto.status in ("aprobado", "con_avisos")
    entregas = next(c for c in veredicto.chequeos if c.campo == "entregas")
    assert entregas.resultado == "ok"
    assert "entrega" in veredicto.recibo.sin_verificar  # compromiso del comercio


def test_una_oferta_que_el_usuario_no_autorizo_se_bloquea(computadoras):
    # El usuario aceptó 10 el lunes y 10 el miércoles (cart_2). Si el agente
    # elige cart_1 (10 el lunes y 10 el viernes), no cumple lo autorizado.
    ficha = _ficha_aceptada(_e((10, LUN), (10, MIE)))
    veredicto = evaluar(ficha, _vistas(computadoras), "cart_1", catalogo={})
    assert veredicto.status == "bloqueado"


def test_los_detalles_muestran_el_cronograma_sin_texto_del_comercio(computadoras):
    ficha = Ficha.desde_intent(computadoras["intent"])
    veredicto = evaluar(ficha, _vistas(computadoras), "cart_2", catalogo={})
    entregas = next(c for c in veredicto.chequeos if c.campo == "entregas")
    assert "10 a más tardar el 2026-10-19" in entregas.detalle
    assert "20 a más tardar el 2026-10-19" in entregas.detalle
    assert "Orion" not in entregas.detalle and "cannot" not in entregas.detalle


def test_formatear_cronograma():
    assert formatear(_e((10, LUN), (10, MIE))) == (
        "10 a más tardar el 2026-10-19, 10 a más tardar el 2026-10-21"
    )
    assert formatear([40, 41]) == "40, 41"  # los talles siguen igual
