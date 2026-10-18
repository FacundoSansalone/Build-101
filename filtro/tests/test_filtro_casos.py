"""Los escenarios de la demo y sus variantes, como respuestas A2A reales.

Lo que pide PLAN.md §7 (tarea 1 de Dev 1): bloquear A, B, B2 y el proveedor
malicioso de computadoras; no bloquear baseline, C ni el proveedor honesto.
"""

from __future__ import annotations

import json

import pytest

from filtro import FiltroSeguridad
from filtro.casos import FIXTURES, Caso, casos, como_tarea, computadoras_malicioso
from filtro.evaluar import main
from filtro.tests.falsos import ClienteFalso

CASOS = {c.nombre: c for c in casos()}
SOLO_HEURISTICAS = FiltroSeguridad(solo_heuristicas=True)


def test_estan_los_escenarios_que_pide_el_plan():
    for nombre in ("A", "B", "B2", "C", "baseline", "computadoras", "computadoras_malicioso"):
        assert nombre in CASOS
    assert {n for n, c in CASOS.items() if c.malicioso} >= {"A", "B", "B2", "computadoras_malicioso"}
    assert not any(CASOS[n].malicioso for n in ("C", "baseline", "computadoras"))


def test_como_tarea_arma_una_tarea_a2a_como_la_del_comercio():
    tarea = como_tarea({"carts": [{"cart_id": "cart_1", "item_label": "x", "amount": 1.0,
                                   "currency": "USD"}], "offer_details": []})
    assert tarea["kind"] == "task" and tarea["status"]["state"] == "completed"
    datos = [p["data"] for a in tarea["artifacts"] for p in a["parts"]]
    assert {"ap2.cart": {"cart_id": "cart_1", "item_label": "x", "amount": 1.0,
                         "currency": "USD"}} in datos
    assert tarea["history"][0]["role"] == "user"


def test_el_proveedor_malicioso_de_computadoras_es_otra_cosa_dentro_del_tope():
    datos = computadoras_malicioso()
    tope = datos["intent"]["hard"]["max_price"]
    objetivo = datos["expected"]["attack_target_cart_id"]
    cart = next(c for c in datos["carts"] if c["cart_id"] == objetivo)
    assert cart["amount"] <= tope
    assert datos["expected"]["ap2_blocks_attack_target"] is False
    detalle = next(o for o in datos["offer_details"] if o["cart_id"] == objetivo)
    assert detalle["sku"] == datos["attack"]["target_sku"]
    assert detalle["category"] != "laptops"


def test_el_caso_malicioso_de_computadoras_es_el_escenario_real_de_world():
    """El texto es el que exporta world/ (`demo_world.export_fixtures`), no uno
    armado aparte: lo que mide el filtro es lo que responde el comercio."""
    real = json.loads((FIXTURES / "etapa2" / "computadoras_malicioso.json").read_text("utf-8"))
    assert computadoras_malicioso() == real
    assert real["scenario"] == "computadoras_malicioso"
    assert "demo_world.export_fixtures" in real["_comment"]
    # Mismo pedido que el proveedor honesto: lo único que cambia es el proveedor.
    honesto = json.loads((FIXTURES / "etapa2" / "computadoras.json").read_text("utf-8"))
    assert real["intent"] == honesto["intent"]
    # La respuesta del caso lleva los textos tal cual (item_label y descripción).
    respuesta = json.dumps(CASOS["computadoras_malicioso"].respuesta, ensure_ascii=False)
    objetivo = real["expected"]["attack_target_cart_id"]
    label = next(c["item_label"] for c in real["carts"] if c["cart_id"] == objetivo)
    descripcion = next(o["description"] for o in real["offer_details"] if o["cart_id"] == objetivo)
    assert json.dumps(label, ensure_ascii=False) in respuesta
    assert json.dumps(descripcion, ensure_ascii=False) in respuesta
    assert "Instructions for the purchasing agent" in label


@pytest.mark.parametrize("nombre", ["A", "B", "B2", "computadoras_malicioso"])
def test_bloquea_los_ataques_de_la_demo_solo_con_heuristicas(nombre):
    r = SOLO_HEURISTICAS(CASOS[nombre].respuesta)
    assert r.pasa is False


@pytest.mark.parametrize("nombre", ["baseline", "C", "computadoras"])
def test_no_bloquea_lo_honesto_de_la_demo(nombre):
    assert SOLO_HEURISTICAS(CASOS[nombre].respuesta).pasa is True
    cliente = ClienteFalso()
    assert FiltroSeguridad(cliente=cliente)(CASOS[nombre].respuesta).pasa is True


@pytest.mark.parametrize("caso", [c for c in casos() if not c.malicioso], ids=lambda c: c.nombre)
def test_ninguna_variante_honesta_tiene_senales_heuristicas(caso: Caso):
    r = SOLO_HEURISTICAS(caso.respuesta)
    assert r.pasa is True, r.senales


@pytest.mark.parametrize(
    "caso", [c for c in casos() if c.malicioso and c.heuristica_lo_ve], ids=lambda c: c.nombre
)
def test_las_variantes_maliciosas_obvias_caen_en_las_heuristicas(caso: Caso):
    assert SOLO_HEURISTICAS(caso.respuesta).pasa is False


def test_hay_al_menos_una_variante_que_solo_ve_el_modelo():
    sutiles = [c for c in casos() if c.malicioso and not c.heuristica_lo_ve]
    assert sutiles
    for caso in sutiles:
        assert SOLO_HEURISTICAS(caso.respuesta).pasa is True  # por eso hace falta la capa (b)
        cliente = ClienteFalso({"manipulacion": 0.95})
        assert FiltroSeguridad(cliente=cliente)(caso.respuesta).pasa is False


def test_evaluar_sin_decisiones_imprime_la_tabla(capsys):
    assert main(["--solo-heuristicas"]) == 0
    salida = capsys.readouterr().out
    assert "computadoras_malicioso" in salida
    assert "falsos positivos" in salida.lower()
    assert "detección" in salida.lower()


def test_evaluar_con_decisiones_exige_permiso_explicito(capsys, monkeypatch):
    monkeypatch.delenv("RUN_DECISIONES_TESTS", raising=False)
    assert main(["--decisiones"]) == 2
    assert "RUN_DECISIONES_TESTS" in capsys.readouterr().err


def test_evaluar_con_decisiones_respeta_el_maximo_de_llamadas(capsys, monkeypatch):
    monkeypatch.setenv("RUN_DECISIONES_TESTS", "1")
    cliente = ClienteFalso()
    assert main(["--decisiones", "--max-llamadas", "3"], cliente=cliente) == 0
    assert len(cliente.llamadas) == 3
    assert "sin evaluar" in capsys.readouterr().out.lower()
