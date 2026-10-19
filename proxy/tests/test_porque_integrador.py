"""La pregunta del porqué de una compra de integrador.

La ficha la fija el integrador al crear la compra: la pregunta no pide lo que
ya está ahí (tope, moneda), aunque el agente del intake lo note faltante en el
texto del pedido.
"""

from __future__ import annotations

from types import SimpleNamespace

from intencion.intake import PORQUE
from proxy.app import _PREGUNTA_POR_DEFECTO, _pregunta_porque_integrador


def test_con_el_agente_del_intake_va_solo_la_plantilla_del_porque():
    pregunta = SimpleNamespace(
        pregunta=PORQUE["es"] + " Además, para hacer la compra me falta saber el tope de precio y la moneda.",
        intake=SimpleNamespace(idioma="es"),
    )
    assert _pregunta_porque_integrador(pregunta) == PORQUE["es"]


def test_respeta_el_idioma_del_intake():
    pregunta = SimpleNamespace(pregunta="…", intake=SimpleNamespace(idioma="en"))
    assert _pregunta_porque_integrador(pregunta) == PORQUE["en"]


def test_sin_intake_usa_la_pregunta_del_agente():
    pregunta = SimpleNamespace(pregunta="¿Para qué necesitás las 20 computadoras?")
    assert _pregunta_porque_integrador(pregunta) == "¿Para qué necesitás las 20 computadoras?"


def test_sin_texto_usa_la_pregunta_por_defecto():
    assert _pregunta_porque_integrador(SimpleNamespace(pregunta="  ")) == _PREGUNTA_POR_DEFECTO
    assert _pregunta_porque_integrador(SimpleNamespace(pregunta=None)) == _PREGUNTA_POR_DEFECTO
