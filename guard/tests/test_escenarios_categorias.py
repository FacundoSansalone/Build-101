"""Escenarios de otras categorías exportados por world/ (fixtures/etapa2/).

Notebooks y auriculares, honesto y malicioso. El comercio no publica esos
productos en su catálogo público (el catálogo no siempre existe): la única
fuente independiente es la ficha del fabricante del GTIN que declara el
comercio (`fixtures/fabricante/`, rotulada como fixture). Esa ficha desmiente
lo declarado (el ataque se bloquea), pero no confirma un obligatorio: sin una
fuente por SKU que ate el GTIN, puede ser el de otro producto, así que la
honesta escala a una persona. Sin ninguna fuente, lo obligatorio escala.
"""

import json

import pytest

from contracts import OfertasVistas
from fabricante import FixtureFabricante, datos_externos
from guard import evaluar
from guard.ficha import ficha_desde_intent
from guard.fuentes import con_fuentes
from guard.tests.conftest import FIXTURES

CATEGORIAS = ["notebook", "auriculares"]


def _datos(nombre: str) -> dict:
    return json.loads((FIXTURES / "etapa2" / f"{nombre}.json").read_text("utf-8"))


def _catalogo_publico() -> dict[str, dict]:
    datos = json.loads((FIXTURES / "public_catalog.json").read_text("utf-8"))
    return {p["sku"]: p for p in datos["products"]}


def _evaluar(nombre: str, cart_id: str, con_fabricante: bool, ahora):
    datos = _datos(nombre)
    catalogo = _catalogo_publico()
    if con_fabricante:
        fuente = FixtureFabricante()
        resultados = [fuente.consultar(d["gtin"]) for d in datos["offer_details"]]
        catalogo = con_fuentes(catalogo, datos_externos(resultados))
    vistas = OfertasVistas(
        carts=datos["carts"], offer_details=datos["offer_details"], comercio_id="merchant_1"
    )
    return evaluar(ficha_desde_intent(datos["intent"]), vistas, cart_id, catalogo, ahora=ahora)


def _esperado(nombre: str, clave: str) -> str:
    return _datos(nombre)["expected"][clave]


def _chequeo(veredicto, tipo: str, campo: str):
    return next(c for c in veredicto.chequeos if c.tipo == tipo and c.campo == campo)


@pytest.mark.parametrize("categoria", CATEGORIAS)
def test_el_catalogo_publico_no_lista_esos_productos(categoria):
    publicos = _catalogo_publico()
    assert not any(d["sku"] in publicos for d in _datos(f"{categoria}_honesto")["offer_details"])


@pytest.mark.parametrize("categoria", CATEGORIAS)
def test_con_el_fabricante_y_sin_catalogo_la_honesta_escala(categoria, ahora):
    nombre = f"{categoria}_honesto"
    mejor = _esperado(nombre, "best_cart_id")
    veredicto = _evaluar(nombre, mejor, True, ahora)
    # El GTIN solo lo declara el comercio: los obligatorios escalan, nada se bloquea.
    assert veredicto.status == "escalar", veredicto.motivo
    assert veredicto.ranking[0] == mejor
    assert _chequeo(veredicto, "integridad", "cart_id").resultado == "ok"
    cumplimiento = [c for c in veredicto.chequeos if c.tipo == "cumplimiento"]
    assert cumplimiento and all(c.resultado != "bloquea" for c in cumplimiento)
    atributos = [c for c in cumplimiento if c.campo.startswith("atributo:")]
    assert atributos and all(c.origen == "fabricante" for c in atributos)
    assert any(c.resultado == "escalar" and "GTIN" in c.detalle for c in atributos)


@pytest.mark.parametrize("categoria", CATEGORIAS)
def test_con_el_fabricante_el_ataque_se_bloquea(categoria, ahora):
    nombre = f"{categoria}_malicioso"
    veredicto = _evaluar(nombre, _esperado(nombre, "attack_target_cart_id"), True, ahora)
    assert veredicto.status == "bloqueado"
    assert veredicto.cart_id_recomendado == _esperado(nombre, "best_cart_id")
    assert _chequeo(veredicto, "mandato", "precio").resultado == "ok"  # dentro del tope


def test_el_dato_falso_sin_inyeccion_tambien_se_bloquea(ahora):
    """notebook_dato_falso: sin texto hostil (el filtro no tiene nada que
    ver); el Guard no lee texto libre y bloquea por el dato del fabricante."""
    nombre = "notebook_dato_falso"
    veredicto = _evaluar(nombre, _esperado(nombre, "attack_target_cart_id"), True, ahora)
    assert veredicto.status == "bloqueado"
    assert _chequeo(veredicto, "cumplimiento", "atributo:memoria_ram").resultado == "bloquea"
    assert _chequeo(veredicto, "mandato", "precio").resultado == "ok"  # dentro del tope
    sin_fabricante = _evaluar(nombre, _esperado(nombre, "attack_target_cart_id"), False, ahora)
    assert sin_fabricante.status == "escalar"  # solo lo declara el comercio: nunca aprueba


def test_sin_ninguna_fuente(ahora):
    # Lo obligatorio solo declarado escala; la RAM falsa tampoco se aprueba.
    honesto = _evaluar("notebook_honesto", _esperado("notebook_honesto", "best_cart_id"), False, ahora)
    assert honesto.status == "escalar"
    mentira = _evaluar("notebook_malicioso", _esperado("notebook_malicioso", "attack_target_cart_id"), False, ahora)
    assert mentira.status == "escalar"
    # Los auriculares sin cancelación de ruido lo declaran: no cumplen y se bloquea.
    sin_anc = _evaluar("auriculares_malicioso", _esperado("auriculares_malicioso", "attack_target_cart_id"), False, ahora)
    assert sin_anc.status == "bloqueado"


def test_la_mentira_de_la_ram_queda_en_la_evidencia_rotulada(ahora):
    nombre = "notebook_malicioso"
    veredicto = _evaluar(nombre, _esperado(nombre, "attack_target_cart_id"), True, ahora)
    evidencia = _chequeo(veredicto, "evidencia", "atributo:memoria_ram")
    assert evidencia.resultado == "escalar" and evidencia.origen == "fabricante"
    assert evidencia.observado == {"valor": 16, "unidad": "GB"}  # lo que declaró
    assert "8 GB" in evidencia.detalle and "fixture de prueba" in evidencia.detalle
    assert "memoria_ram 8 GB no cumple el obligatorio (mínimo 16 GB)" in veredicto.motivo


def test_los_auriculares_muestran_si_y_no(ahora):
    nombre = "auriculares_malicioso"
    veredicto = _evaluar(nombre, _esperado(nombre, "attack_target_cart_id"), True, ahora)
    assert "cancelacion_ruido (no) no cumple el obligatorio (sí)" in veredicto.motivo


def test_el_peso_en_gramos_contra_un_tope_en_kilos(ahora):
    # Auralis Travel: 310 g > 0,3 kg (preferido) -> aviso; la Sonora (250 g) lo cumple.
    veredicto = _evaluar("auriculares_honesto", "cart_2", True, ahora)
    assert _chequeo(veredicto, "cumplimiento", "atributo:weight").resultado == "aviso"
    mejor = _evaluar("auriculares_honesto", "cart_1", True, ahora)
    assert _chequeo(mejor, "cumplimiento", "atributo:weight").resultado == "ok"
