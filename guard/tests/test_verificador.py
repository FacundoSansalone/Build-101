"""Tests del verificador (catálogo público) y del chequeo de evidencia."""

import pytest

from contracts import Oferta, OfertasVistas
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


def test_sku_fuera_del_catalogo_con_obligatorios_se_escala(vistas, ficha, catalogo, ahora):
    """Obligatorios respaldados solo por lo que declara el comercio → escalar."""
    sin_nike = {sku: p for sku, p in catalogo.items() if sku != "NK-OUT-01"}
    veredicto = evaluar(ficha(), vistas("baseline"), "cart_1", sin_nike, ahora=ahora)
    assert veredicto.status == "escalar"
    cumplimiento = {c.campo: c for c in veredicto.chequeos if c.tipo == "cumplimiento"}
    for campo in ("categoria", "uso", "talle"):
        assert cumplimiento[campo].resultado == "escalar", campo
        assert cumplimiento[campo].origen == "declarado"
    assert cumplimiento["marca"].resultado == "ok"  # preferido: el aviso sale de evidencia


def test_obligatorio_sin_dato_publico_se_escala(vistas, ficha, catalogo, ahora):
    """El SKU está en el catálogo pero el catálogo no publica el uso."""
    catalogo_sin_uso = dict(catalogo)
    catalogo_sin_uso["NK-OUT-01"] = {
        k: v for k, v in catalogo["NK-OUT-01"].items() if k != "use"
    }
    veredicto = evaluar(ficha(), vistas("baseline"), "cart_1", catalogo_sin_uso, ahora=ahora)
    assert veredicto.status == "escalar"
    (uso,) = [c for c in veredicto.chequeos if c.tipo == "cumplimiento" and c.campo == "uso"]
    assert uso.resultado == "escalar"


@pytest.mark.parametrize("clave", ["title", "brand"])
def test_dato_solo_declarado_que_no_es_obligatorio_es_aviso(vistas, ficha, catalogo, ahora, clave):
    catalogo_raro = dict(catalogo)
    catalogo_raro["NK-OUT-01"] = {k: v for k, v in catalogo["NK-OUT-01"].items() if k != clave}
    veredicto = evaluar(ficha(), vistas("baseline"), "cart_1", catalogo_raro, ahora=ahora)
    assert veredicto.status == "con_avisos"


# --- campos omitidos por el comercio ------------------------------------------------

_ATRIBUTO = {"category": "categoria", "use": "uso", "brand": "marca", "sizes": "talles", "title": "titulo"}


def _vistas_omitiendo(vistas, clave: str, como: str):
    original = vistas("baseline")
    detalles = [dict(d) for d in original.offer_details]
    if como == "falta":
        del detalles[0][clave]
    else:
        detalles[0][clave] = None
    return OfertasVistas(carts=original.carts, offer_details=detalles, comercio_id="merchant_1")


@pytest.mark.parametrize("clave", list(_ATRIBUTO))
@pytest.mark.parametrize("como", ["falta", "none"])
def test_campo_omitido_no_es_verificado_ni_se_rellena(vistas, catalogo, clave, como):
    oferta = verificar(extraer_ofertas(_vistas_omitiendo(vistas, clave, como))[0], catalogo)
    atributo = _ATRIBUTO[clave]
    evidencia = oferta.evidencia[atributo]
    assert evidencia.estado == "solo_declarado"
    assert evidencia.declarado is None
    assert evidencia.publico is None  # no se rellena con el dato público
    assert getattr(oferta, atributo) in ("", [])


@pytest.mark.parametrize(("clave", "status"), [("category", "escalar"), ("use", "escalar"),
                                              ("sizes", "escalar"), ("brand", "con_avisos")])
@pytest.mark.parametrize("como", ["falta", "none"])
def test_campo_omitido_no_se_aprueba(vistas, ficha, catalogo, ahora, clave, como, status):
    veredicto = evaluar(ficha(), _vistas_omitiendo(vistas, clave, como), "cart_1", catalogo, ahora=ahora)
    assert veredicto.status == status


def test_sku_tiene_su_evidencia(vistas, catalogo):
    en_catalogo = _verificada(vistas, catalogo, "baseline", "cart_1").evidencia["sku"]
    assert (en_catalogo.estado, en_catalogo.origen_usado) == ("verificado", "catalogo_publico")
    fuera = verificar(_oferta(sku="DESCONOCIDO"), catalogo).evidencia["sku"]
    assert fuera.estado == "solo_declarado"


def test_oferta_inverificable_no_desplaza_a_una_verificada(vistas, ficha, catalogo, ahora):
    """Una oferta más barata con SKU fuera del catálogo no 'domina' a la verificada."""
    original = vistas("baseline")
    falsa = dict(original.offer_details[0], cart_id="cart_9", sku="FALSO-99")
    vistas_con_falsa = OfertasVistas(
        carts=original.carts + [{"cart_id": "cart_9", "item_label": "", "amount": 50.0, "currency": "USD"}],
        offer_details=original.offer_details + [falsa],
        comercio_id="merchant_1",
    )
    veredicto = evaluar(ficha(), vistas_con_falsa, "cart_1", catalogo, ahora=ahora)
    assert veredicto.status == "aprobado"
    assert veredicto.ranking[0] == "cart_1"
    assert veredicto.ranking.index("cart_9") > veredicto.ranking.index("cart_3")


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
