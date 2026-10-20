"""El motivo del Guard muestra cada magnitud en la unidad del requisito.

Caso real (comercio general, pedido libre): un monitor de 27" contra la ficha
del fabricante que dice 61 cm. La comparación ya convertía (61 cm ≈ 24 in),
pero el texto decía "tamano_pantalla 61 cm no cumple el obligatorio (27 in)".
Ahora: "tamano_pantalla 24 in (61 cm) no cumple el obligatorio (27 in)", y lo
mismo con memoria, almacenamiento, peso y autonomía.
"""

import pytest

from contracts import Ficha, OfertasVistas, Requisito
from guard import evaluar
from guard.fuentes import con_fuentes, dato_externo

GTIN = "2001234000017"


def _vistas(atributos_declarados: dict) -> OfertasVistas:
    detalle = {
        "cart_id": "cart_1", "sku": "MN-27", "title": "Monitor", "brand": "Lumen",
        "category": "monitors", "use": "design", "price": 300.0, "currency": "USD",
        "stock": 5, "sizes": [], "gtin": GTIN, "atributos": atributos_declarados,
        "description": "Monitor.",
    }
    cart = {"cart_id": "cart_1", "item_label": "Monitor", "amount": 300.0, "currency": "USD"}
    return OfertasVistas(carts=[cart], offer_details=[detalle], comercio_id="merchant_1")


def _ficha(*requisitos: Requisito) -> Ficha:
    return Ficha(
        pedido_original="Un monitor de 27 pulgadas para diseño, hasta USD 400.",
        requisitos=[
            Requisito(campo="precio", operador="maximo", valor=400.0, obligatorio=True),
            Requisito(campo="moneda", operador="igual", valor="USD", obligatorio=True),
            *requisitos,
        ],
        confirmada=True,
    )


def _fabricante(atributos: dict) -> dict:
    # El catálogo publica el GTIN del SKU (como el comercio general): eso ata
    # la ficha del fabricante a la oferta.
    catalogo = {"MN-27": {"sku": "MN-27", "gtin": GTIN}}
    return con_fuentes(catalogo, [dato_externo("fabricante", gtin=GTIN, datos={"atributos": atributos})])


def _cumplimiento(veredicto, campo):
    return next(c for c in veredicto.chequeos if c.tipo == "cumplimiento" and c.campo == campo)


def test_la_pantalla_en_cm_se_lee_en_pulgadas(ahora):
    # Como lo arma el intake: "monitor de 27"" → pantalla igual 27 in.
    ficha = _ficha(Requisito(campo="atributo", atributo="pantalla", operador="igual",
                             valor=27, unidad="in", obligatorio=True))
    catalogo = _fabricante({"Display diagonal": {"valor": 61, "unidad": "cm"}})
    veredicto = evaluar(ficha, _vistas({"pantalla": {"valor": 27, "unidad": "in"}}), "cart_1",
                        catalogo, ahora=ahora)

    assert veredicto.status == "bloqueado"
    assert "tamano_pantalla 24 in (61 cm) no cumple el obligatorio (27 in)." in veredicto.motivo
    assert "61 cm no cumple" not in veredicto.motivo
    chequeo = _cumplimiento(veredicto, "atributo:tamano_pantalla")
    # Lo observado queda como lo dio la fuente; lo pedido, con su unidad.
    assert chequeo.observado == {"valor": 61, "unidad": "cm"}
    assert chequeo.esperado == {"valor": 27, "unidad": "in"}


def test_con_minimo_dice_minimo(ahora):
    ficha = _ficha(Requisito(campo="atributo", atributo="tamano_pantalla", operador="minimo",
                             valor=27, unidad="in", obligatorio=True))
    catalogo = _fabricante({"Display diagonal": {"valor": 61, "unidad": "cm"}})
    veredicto = evaluar(ficha, _vistas({"pantalla": {"valor": 27, "unidad": "in"}}), "cart_1",
                        catalogo, ahora=ahora)
    assert "tamano_pantalla 24 in (61 cm) no cumple el obligatorio (mínimo 27 in)." in veredicto.motivo


@pytest.mark.parametrize(
    ("requisito", "publicado", "texto"),
    [
        (Requisito(campo="atributo", atributo="memoria_ram", operador="minimo", valor=16,
                   unidad="GB", obligatorio=True),
         {"Internal memory": {"valor": 8192, "unidad": "MB"}},
         "memoria_ram 8.19 GB (8192 MB) no cumple el obligatorio (mínimo 16 GB)."),
        (Requisito(campo="atributo", atributo="almacenamiento", operador="minimo", valor=1,
                   unidad="TB", obligatorio=True),
         {"Total storage capacity": {"valor": 512, "unidad": "GB"}},
         "almacenamiento 0.512 TB (512 GB) no cumple el obligatorio (mínimo 1 TB)."),
        (Requisito(campo="atributo", atributo="weight", operador="maximo", valor=1.5,
                   unidad="kg", obligatorio=True),
         {"Weight": {"valor": 1800, "unidad": "g"}},
         "weight 1.8 kg (1800 g) no cumple el obligatorio (máximo 1.5 kg)."),
        (Requisito(campo="atributo", atributo="autonomia_bateria", operador="minimo", valor=20,
                   unidad="h", obligatorio=True),
         {"Battery life": {"valor": 900, "unidad": "min"}},
         "autonomia_bateria 15 h (900 min) no cumple el obligatorio (mínimo 20 h)."),
    ],
)
def test_todas_las_magnitudes_en_la_unidad_del_requisito(requisito, publicado, texto, ahora):
    nombre = next(iter(publicado))
    declarado = {nombre: {"valor": requisito.valor, "unidad": requisito.unidad}}  # el comercio miente
    veredicto = evaluar(_ficha(requisito), _vistas(declarado), "cart_1", _fabricante(publicado),
                        ahora=ahora)
    assert veredicto.status == "bloqueado"
    assert texto in veredicto.motivo


def test_lo_que_cumple_tambien_en_la_unidad_del_requisito(ahora):
    ficha = _ficha(Requisito(campo="atributo", atributo="tamano_pantalla", operador="minimo",
                             valor=27, unidad="in", obligatorio=True))
    catalogo = _fabricante({"Display diagonal": {"valor": 68.6, "unidad": "cm"}})
    veredicto = evaluar(ficha, _vistas({"pantalla": {"valor": 27, "unidad": "in"}}), "cart_1",
                        catalogo, ahora=ahora)
    chequeo = _cumplimiento(veredicto, "atributo:tamano_pantalla")
    assert chequeo.resultado == "ok"
    assert chequeo.detalle == "tamano_pantalla 27 in (68.6 cm) cumple el obligatorio (mínimo 27 in)."


def test_sin_unidad_en_el_requisito_esperado_queda_como_siempre(ahora):
    ficha = _ficha(Requisito(campo="atributo", atributo="cancelacion_ruido", operador="igual",
                             valor=True, obligatorio=True))
    catalogo = _fabricante({"Active Noise Cancelling (ANC)": "N"})
    veredicto = evaluar(ficha, _vistas({"cancelacion_ruido": True}), "cart_1", catalogo, ahora=ahora)
    chequeo = _cumplimiento(veredicto, "atributo:cancelacion_ruido")
    assert chequeo.esperado is True
    assert "cancelacion_ruido (no) no cumple el obligatorio (sí)." in veredicto.motivo
