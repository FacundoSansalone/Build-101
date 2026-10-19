"""Precio por unidad (hallazgo H1) y la regla de cantidad.

El `amount` del ap2.cart es el total que se cobra; el `price` del catálogo
público es por unidad. Con `cantidad` > 1 se compara el total con el precio
del catálogo por la cantidad. Una oferta de varias unidades frente a una
ficha que no dice cuántas escala: el usuario no autorizó más de una.
"""

import pytest

from contracts import Ficha, OfertasVistas, Requisito
from guard import evaluar
from guard.extractor import extraer_ofertas
from guard.verificador import verificar

UNITARIO = 850.0


def _producto(sku="LM-NB-14", precio=UNITARIO, titulo="Lumen Notebook 14", marca="Lumen"):
    return {
        "sku": sku, "title": titulo, "brand": marca, "category": "laptops", "use": "office",
        "price": precio, "currency": "USD", "stock": 40, "sizes": [],
    }


def _vistas(*ofertas):
    """`ofertas`: (producto, amount, cantidad) por cart, en orden."""
    carts, detalles = [], []
    for i, (producto, amount, cantidad) in enumerate(ofertas, start=1):
        cart_id = f"cart_{i}"
        carts.append({"cart_id": cart_id, "item_label": producto["title"], "amount": amount,
                      "currency": "USD"})
        detalle = {"cart_id": cart_id, **producto}
        if cantidad is not None:
            detalle["quantity"] = cantidad
        detalles.append(detalle)
    return OfertasVistas(carts=carts, offer_details=detalles, comercio_id="merchant_1")


def _ficha(tope=20000.0, cantidad=None):
    requisitos = [
        Requisito(campo="precio", operador="maximo", valor=tope, obligatorio=True),
        Requisito(campo="moneda", operador="igual", valor="USD", obligatorio=True),
    ]
    if cantidad is not None:
        requisitos.append(Requisito(campo="cantidad", operador="igual", valor=cantidad, obligatorio=True))
    return Ficha(pedido_original="Computadoras para la oficina.", requisitos=requisitos, confirmada=True)


def _catalogo(*productos):
    return {p["sku"]: p for p in productos or (_producto(),)}


def _evidencia_precio(veredicto):
    return next(c for c in veredicto.chequeos if c.tipo == "evidencia" and c.campo == "precio")


def _cantidad(veredicto):
    return [c for c in veredicto.chequeos if c.tipo == "cumplimiento" and c.campo == "cantidad"]


# -- precio por unidad --------------------------------------------------------------


def test_20_por_850_publicado_es_verificado(ahora):
    vistas = _vistas((_producto(), 17000.0, 20))
    veredicto = evaluar(_ficha(cantidad=20), vistas, "cart_1", _catalogo(), ahora=ahora)
    precio = _evidencia_precio(veredicto)
    assert precio.resultado == "ok"
    assert (precio.observado, precio.esperado) == (17000.0, 17000.0)
    assert precio.detalle == (
        "Precio: se cobra USD 17000.00 por 20 unidades; el catálogo público dice "
        "USD 850.00 c/u."
    )
    assert veredicto.status == "aprobado", veredicto.motivo


def test_el_verificador_compara_el_total_con_el_precio_por_la_cantidad():
    oferta = verificar(extraer_ofertas(_vistas((_producto(), 17000.0, 20)))[0], _catalogo())
    evidencia = oferta.evidencia["precio"]
    assert evidencia.estado == "verificado"
    assert evidencia.origen_usado == "cart_ap2"
    assert (evidencia.declarado, evidencia.publico) == (17000.0, 17000.0)
    assert oferta.precio == 17000.0  # lo que se cobra no cambia


def test_un_total_que_no_coincide_con_el_precio_por_la_cantidad_es_contradicho(ahora):
    vistas = _vistas((_producto(), 18000.0, 20))
    veredicto = evaluar(_ficha(cantidad=20), vistas, "cart_1", _catalogo(), ahora=ahora)
    precio = _evidencia_precio(veredicto)
    assert precio.resultado == "escalar"
    assert precio.detalle == (
        "Precio: se cobra USD 18000.00 por 20 unidades pero el catálogo público dice "
        "USD 850.00 c/u (USD 17000.00 por 20)."
    )
    assert veredicto.status == "escalar"


def test_cobrar_el_precio_de_una_unidad_por_varias_es_contradicho():
    oferta = verificar(extraer_ofertas(_vistas((_producto(), UNITARIO, 20)))[0], _catalogo())
    assert oferta.evidencia["precio"].estado == "contradicho"


def test_los_centavos_se_multiplican_sin_error_de_redondeo():
    producto = _producto(precio=19.99)
    oferta = verificar(extraer_ofertas(_vistas((producto, 59.97, 3)))[0], _catalogo(producto))
    assert oferta.evidencia["precio"].estado == "verificado"
    assert oferta.evidencia["precio"].publico == 59.97


@pytest.mark.parametrize("cantidad", [None, 1])
def test_sin_cantidad_o_una_unidad_es_como_antes(cantidad, ahora):
    vistas = _vistas((_producto(), UNITARIO, cantidad))
    veredicto = evaluar(_ficha(tope=1000.0), vistas, "cart_1", _catalogo(), ahora=ahora)
    precio = _evidencia_precio(veredicto)
    assert precio.resultado == "ok"
    assert precio.detalle == "Precio USD 850.00 coincide con el catálogo público."
    assert _cantidad(veredicto) == []
    assert veredicto.status == "aprobado", veredicto.motivo
    # Sin cantidad, un total de 20 unidades contra el precio unitario no coincide.
    oferta = verificar(extraer_ofertas(_vistas((_producto(), 17000.0, cantidad)))[0], _catalogo())
    assert oferta.evidencia["precio"].estado == "contradicho"


def test_varias_unidades_sin_catalogo_quedan_solo_declaradas(ahora):
    vistas = _vistas((_producto(), 17000.0, 20))
    veredicto = evaluar(_ficha(cantidad=20), vistas, "cart_1", {}, ahora=ahora)
    precio = _evidencia_precio(veredicto)
    assert precio.resultado == "aviso"
    assert precio.detalle == (
        "Precio USD 17000.00 por 20 unidades: no está en el catálogo público para compararlo."
    )


# -- regla de cantidad --------------------------------------------------------------


def test_2_unidades_sin_requisito_de_cantidad_escala(ahora):
    vistas = _vistas((_producto(), 1700.0, 2))
    veredicto = evaluar(_ficha(tope=2000.0), vistas, "cart_1", _catalogo(), ahora=ahora)
    [chequeo] = _cantidad(veredicto)
    assert chequeo.resultado == "escalar"
    assert (chequeo.esperado, chequeo.observado) == (1, 2)
    assert chequeo.origen == "declarado"
    assert chequeo.detalle == (
        "Cantidad: la oferta es por 2 unidades y el pedido no dice cuántas; comprar "
        "más de una no está autorizado."
    )
    assert _evidencia_precio(veredicto).resultado == "ok"  # el precio sí coincide
    assert veredicto.status == "escalar"
    assert chequeo.detalle in veredicto.motivo
    assert veredicto.recibo.filtradas["cart_1"] == [chequeo.detalle]


def test_con_requisito_de_cantidad_no_se_agrega_la_regla(ahora):
    vistas = _vistas((_producto(), 1700.0, 2))
    veredicto = evaluar(_ficha(tope=2000.0, cantidad=2), vistas, "cart_1", _catalogo(), ahora=ahora)
    [chequeo] = _cantidad(veredicto)
    assert chequeo.resultado == "ok"
    assert veredicto.status == "aprobado", veredicto.motivo


def test_21_unidades_con_ficha_de_20_bloquea(ahora):
    vistas = _vistas((_producto(), 21 * UNITARIO, 21))
    veredicto = evaluar(_ficha(cantidad=20), vistas, "cart_1", _catalogo(), ahora=ahora)
    [chequeo] = _cantidad(veredicto)
    assert chequeo.resultado == "bloquea"
    assert _evidencia_precio(veredicto).resultado == "ok"
    assert veredicto.status == "bloqueado"


def test_un_pack_mas_barato_sin_autorizar_no_domina_a_la_unidad(ahora):
    # El pedido no dice cuántas: la oferta de una unidad es la correcta y un
    # pack de dos más barato (en total) no la "supera en todo".
    unidad = _producto("AU-HP-1", precio=199.0, titulo="Auralis One", marca="Auralis")
    pack = _producto("AU-HP-2", precio=90.0, titulo="Auralis Basic", marca="Auralis")
    vistas = _vistas((unidad, 199.0, None), (pack, 180.0, 2))
    veredicto = evaluar(_ficha(tope=250.0), vistas, "cart_1", _catalogo(unidad, pack), ahora=ahora)
    integridad = next(c for c in veredicto.chequeos if c.tipo == "integridad")
    assert integridad.resultado == "ok", integridad.detalle
    assert veredicto.ranking[0] == "cart_1"
    assert "3/3 obligatorios" in integridad.detalle
    assert veredicto.status == "aprobado", veredicto.motivo
    # El pack, elegido, escala por la cantidad.
    assert evaluar(_ficha(tope=250.0), vistas, "cart_2", _catalogo(unidad, pack),
                   ahora=ahora).status == "escalar"


def test_sin_ofertas_de_varias_unidades_el_ranking_no_cambia(ahora):
    vistas = _vistas((_producto(), UNITARIO, None))
    veredicto = evaluar(_ficha(tope=1000.0), vistas, "cart_1", _catalogo(), ahora=ahora)
    integridad = next(c for c in veredicto.chequeos if c.tipo == "integridad")
    assert "2/2 obligatorios" in integridad.detalle
