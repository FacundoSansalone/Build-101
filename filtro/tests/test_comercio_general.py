"""El filtro frente al comercio general (etapa 3), en el modo de la demo
(`FILTRO_SOLO_HEURISTICAS=1`).

Las respuestas honestas de todos los presets (`fixtures/general/<preset>.json`,
que exporta world/ con `demo_world.export_fixtures`: el texto real del
comercio a varias búsquedas) pasan; las que traen la inyección se cortan. Lo
que corta es la inyección y no la oferta de otra categoría: sin la nota, pasa
(la ataja el Guard).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from filtro import MOTIVO_MALICIA, MOTIVO_SOLO_HEURISTICAS, desde_entorno
from filtro.casos import como_tarea
from filtro.heuristicas import ANULA_INSTRUCCIONES, INSTRUCCION_IA, MANIPULACION

RAIZ = Path(__file__).resolve().parents[2]
GENERAL = RAIZ / "fixtures" / "general"
ESCENARIOS = (
    RAIZ / "world" / "ap2" / "code" / "samples" / "python" / "src" / "demo_world" / "data"
    / "scenarios.json"
)
ENTORNO_DEMO = {"GUARD_FILTRO": "filtro:filtrar", "FILTRO_SOLO_HEURISTICAS": "1"}
INICIO_INYECCION = "[Instructions for the purchasing agent"

PRESETS = {
    nombre: info
    for nombre, info in json.loads(ESCENARIOS.read_text("utf-8"))["scenarios"].items()
    if info.get("tipo") == "general"
}


def _datos(nombre: str) -> dict:
    return json.loads((GENERAL / f"{nombre}.json").read_text("utf-8"))


BUSQUEDAS = [
    pytest.param(nombre, i, id=f"{nombre}-{i}")
    for nombre in PRESETS
    for i in range(len(_datos(nombre)["busquedas"]))
]


@pytest.fixture
def filtro_demo():
    f = desde_entorno(ENTORNO_DEMO)
    assert f.solo_heuristicas is True and f.cliente is None
    return f


def _respuesta(nombre: str, busqueda: dict) -> dict:
    return como_tarea({
        "scenario": nombre,
        "carts": busqueda["carts"],
        "offer_details": busqueda["offer_details"],
    })


def test_hay_un_fixture_por_preset_y_las_busquedas_no_estan_vacias():
    assert len(PRESETS) == 10
    assert {p.stem for p in GENERAL.glob("*.json")} == set(PRESETS)
    for nombre, info in PRESETS.items():
        busquedas = _datos(nombre)["busquedas"]
        assert len(busquedas) >= 3
        con_inyeccion = "inyeccion" in info["comportamiento"]
        # Los presets con inyección la traen en cada búsqueda; los demás, nunca.
        assert all(bool(b["inyectadas"]) is con_inyeccion for b in busquedas), nombre
        if nombre != "general_sin_resultados":
            assert all(b["carts"] for b in busquedas), nombre


@pytest.mark.parametrize(("nombre", "i"), BUSQUEDAS)
def test_lo_honesto_pasa_y_la_inyeccion_se_corta(filtro_demo, nombre, i):
    busqueda = _datos(nombre)["busquedas"][i]
    r = filtro_demo(_respuesta(nombre, busqueda))
    if busqueda["inyectadas"]:
        assert r.pasa is False and r.motivo == MOTIVO_MALICIA
        assert {INSTRUCCION_IA, ANULA_INSTRUCCIONES, MANIPULACION} <= set(r.senales)
    else:
        assert r.pasa is True, r.senales
        assert r.motivo == MOTIVO_SOLO_HEURISTICAS and r.senales == ["modo:solo_heuristicas"]


@pytest.mark.parametrize(("nombre", "i"), [b for b in BUSQUEDAS if "inyeccion" in b.values[0]])
def test_sin_la_nota_la_oferta_de_otra_categoria_pasa(filtro_demo, nombre, i):
    busqueda = copy.deepcopy(_datos(nombre)["busquedas"][i])
    for cart_id in busqueda["inyectadas"]:
        cart = next(c for c in busqueda["carts"] if c["cart_id"] == cart_id)
        detalle = next(d for d in busqueda["offer_details"] if d["cart_id"] == cart_id)
        assert f"Select {cart_id}," in cart["item_label"]  # nombra su cart_id real
        cart["item_label"] = cart["item_label"][: cart["item_label"].index(INICIO_INYECCION)].strip()
        detalle["description"] = detalle["description"][: detalle["description"].index(INICIO_INYECCION)].strip()
    r = filtro_demo(_respuesta(nombre, busqueda))
    assert r.pasa is True, r.senales
