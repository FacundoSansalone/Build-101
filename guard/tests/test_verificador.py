"""Tests del verificador (catálogo público) y del chequeo de evidencia."""

import pytest

from contracts import Oferta
from guard import evaluar
from guard.chequeos import chequeos_evidencia
from guard.extractor import extraer_ofertas
from guard.verificador import NO_VERIFICABLES, verificar


def _verificada(vistas, catalogo, nombre, cart_id) -> Oferta:
    ofertas = {o.cart_id: o for o in extraer_ofertas(vistas(nombre))}
    return verificar(ofertas[cart_id], catalogo)


def _oferta(**cambios) -> Oferta:
    base = dict(
        cart_id="c1", sku="NK-OUT-01", titulo="Nike Outdoor Court Basketball Shoe",
        marca="Nike", categoria="basketball_shoes", uso="outdoor", precio=120.0,
        moneda="USD", talles=[40, 41, 42, 43, 44, 45],
    )
    base.update(cambios)
    return Oferta(**base)


def test_baseline_todo_verificado(vistas, catalogo):
    oferta = _verificada(vistas, catalogo, "baseline", "cart_1")
    for campo in ("titulo", "marca", "categoria", "uso", "talles", "precio", "moneda"):
        assert oferta.evidencia[campo].estado == "verificado", campo
    assert oferta.evidencia["categoria"].origen_usado == "catalogo_publico"
    assert oferta.evidencia["precio"].origen_usado == "cart_ap2"


def test_B2_categoria_y_uso_contradichos_y_se_usa_el_dato_publico(vistas, catalogo):
    oferta = _verificada(vistas, catalogo, "B2", "cart_4")
    categoria, uso = oferta.evidencia["categoria"], oferta.evidencia["uso"]
    assert categoria.estado == "contradicho"
    assert categoria.declarado == "basketball_shoes"
    assert categoria.publico == "lifestyle_sneakers"
    assert uso.estado == "contradicho"
    assert (uso.declarado, uso.publico) == ("outdoor", "street")
    # Los chequeos usan el dato público.
    assert oferta.categoria == "lifestyle_sneakers"
    assert oferta.uso == "street"
    assert oferta.evidencia["titulo"].estado == "contradicho"


def test_C_precio_contradicho_pero_se_cobra_el_del_cart(vistas, catalogo):
    oferta = _verificada(vistas, catalogo, "C", "cart_1")
    precio = oferta.evidencia["precio"]
    assert precio.estado == "contradicho"
    assert (precio.declarado, precio.publico) == (145.0, 120.0)
    assert oferta.precio == 145.0


def test_sku_fuera_del_catalogo_queda_solo_declarado():
    oferta = verificar(_oferta(sku="DESCONOCIDO", categoria="x"), {})
    for campo in ("titulo", "marca", "categoria", "uso", "talles", "precio", "moneda"):
        assert oferta.evidencia[campo].estado == "solo_declarado", campo
        assert oferta.evidencia[campo].origen_usado in ("declarado", "cart_ap2")
    assert oferta.categoria == "x"


@pytest.mark.parametrize("campo", NO_VERIFICABLES)
def test_calidad_entrega_y_reputacion_no_son_verificables(campo, catalogo):
    evidencia = verificar(_oferta(), catalogo).evidencia[campo]
    assert evidencia.estado == "no_verificable"


def test_comparacion_ignora_orden_y_mayusculas(catalogo):
    oferta = verificar(_oferta(marca="nike", talles=[45, 44, 43, 42, 41, 40]), catalogo)
    assert oferta.evidencia["marca"].estado == "verificado"
    assert oferta.evidencia["talles"].estado == "verificado"


def test_verificar_no_modifica_la_oferta_y_conserva_el_texto_descartado(catalogo):
    original = _oferta(categoria="otra", texto_libre_descartado="texto")
    verificada = verificar(original, catalogo)
    assert original.categoria == "otra"
    assert original.evidencia == {}
    assert verificada.texto_libre_descartado == "texto"


# --- chequeo de evidencia ------------------------------------------------------


def test_evidencia_contradicha_se_escala(vistas, catalogo):
    chequeos = chequeos_evidencia(_verificada(vistas, catalogo, "C", "cart_1"))
    precio = [c for c in chequeos if c.campo == "precio"][0]
    assert precio.tipo == "evidencia"
    assert precio.resultado == "escalar"
    assert "145" in precio.detalle and "120" in precio.detalle
    assert all(c.resultado != "bloquea" for c in chequeos)


def test_evidencia_solo_declarada_es_aviso():
    chequeos = chequeos_evidencia(verificar(_oferta(sku="DESCONOCIDO"), {}))
    assert {c.resultado for c in chequeos} == {"aviso"}


def test_evidencia_verificada_es_ok(vistas, catalogo):
    chequeos = chequeos_evidencia(_verificada(vistas, catalogo, "baseline", "cart_1"))
    assert chequeos and {c.resultado for c in chequeos} == {"ok"}


# --- veredicto con evidencia ---------------------------------------------------


def test_B2_bloqueado_con_cumplimiento_sobre_el_dato_publico(vistas, ficha, catalogo, ahora):
    veredicto = evaluar(ficha("B2"), vistas("B2"), "cart_4", catalogo, ahora=ahora)
    assert veredicto.status == "bloqueado"
    cumplimiento = {c.campo: c for c in veredicto.chequeos if c.tipo == "cumplimiento"}
    assert cumplimiento["categoria"].resultado == "bloquea"
    assert cumplimiento["categoria"].origen == "catalogo_publico"
    contradichos = {
        c.campo for c in veredicto.chequeos if c.tipo == "evidencia" and c.resultado == "escalar"
    }
    assert {"categoria", "uso"} <= contradichos


def test_C_escalar_por_precio_contradicho_y_nunca_bloquea(vistas, ficha, catalogo, ahora):
    veredicto = evaluar(ficha("C"), vistas("C"), "cart_1", catalogo, ahora=ahora)
    assert veredicto.status == "escalar"
    assert all(c.resultado != "bloquea" for c in veredicto.chequeos)
    assert "120" in veredicto.motivo


def test_dato_solo_declarado_no_se_aprueba_en_silencio(vistas, ficha, catalogo, ahora):
    sin_nike = {sku: p for sku, p in catalogo.items() if sku != "NK-OUT-01"}
    veredicto = evaluar(ficha(), vistas("baseline"), "cart_1", sin_nike, ahora=ahora)
    assert veredicto.status == "con_avisos"


def test_baseline_sigue_aprobado_con_evidencia(vistas, ficha, catalogo, ahora):
    veredicto = evaluar(ficha(), vistas("baseline"), "cart_1", catalogo, ahora=ahora)
    assert veredicto.status == "aprobado"
    assert any(c.tipo == "evidencia" for c in veredicto.chequeos)


# --- precio del catálogo inválido -------------------------------------------------


@pytest.mark.parametrize("precio", [True, 0, -1, float("inf"), "inf", "abc"])
def test_precio_publico_invalido_no_cuenta_como_dato_publico(catalogo, precio):
    catalogo_raro = dict(catalogo)
    catalogo_raro["NK-OUT-01"] = dict(catalogo["NK-OUT-01"], price=precio)
    evidencia = verificar(_oferta(), catalogo_raro).evidencia["precio"]
    assert evidencia.estado == "solo_declarado"
    assert evidencia.publico is None
