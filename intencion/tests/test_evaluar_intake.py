"""El set de pedidos y `python -m intencion.evaluar_intake`, sin red.

- El set es variado y sus valores esperados están entre los candidatos que
  arma el código: el modelo no puede elegir un valor que no está.
- La simulación y el puntaje se prueban con respuestas grabadas a mano (el
  mismo camino que la repetición offline y la calibración).
- Contra decisiones real solo con RUN_DECISIONES_TESTS=1.
"""

from __future__ import annotations

import json
import os
from datetime import date

import pytest

from intencion import evaluar_intake as ev
from intencion.intake import Umbrales

SET = ev.cargar_set()
HOY = date.fromisoformat(SET["hoy"])
CATEGORIAS = SET["categorias"]
CASOS = {c["id"]: c for c in SET["casos"]}


def test_el_set_es_variado():
    casos = SET["casos"]
    assert len(casos) >= 10
    assert {c["tipo"] for c in casos} >= {"completo", "incompleto", "ambiguo", "fuera_de_catalogo"}
    idiomas = {ev.intake.extraer([c["pedido"]], HOY).idioma for c in casos}
    assert idiomas == {"es", "en"}
    categorias = {c["final"]["categoria"] for c in casos if isinstance(c["final"]["categoria"], str)}
    assert {"basketball_shoes", "laptops", "headphones", "fuera_de_catalogo"} <= categorias
    # Lo que se paga: unas 2 a 3 llamadas por caso, dentro de ~30.
    assert sum(1 + len(c["respuestas"]) for c in casos) <= 30


@pytest.mark.parametrize("caso_id", sorted(CASOS))
def test_lo_esperado_esta_entre_los_candidatos(caso_id):
    assert ev.cobertura(CASOS[caso_id], HOY, CATEGORIAS) == []


# -- simulación con respuestas grabadas a mano ----------------------------------------------


def _e(opcion: str, p: float) -> dict:
    return {"tipo": "eleccion", "eleccion": opcion, "probabilidades": {opcion: p}, "confianza": p}


SIN_DATOS = {
    "categoria": _e("laptops", 0.97),
    "cantidad": _e("una", 0.9),
    "precio": _e("no_dicho", 0.95),
    "moneda": _e("no_dicha", 0.9),
    "plazo": _e("no_dicho", 0.9),
}
GRABADAS = {
    "notebook_incompleto_es#0": {"latencia_s": 1.0, "respuestas": SIN_DATOS},
    "notebook_incompleto_es#1": {"latencia_s": 2.0, "respuestas": SIN_DATOS},
    "notebook_incompleto_es#2": {
        "latencia_s": 3.0,
        "respuestas": {
            "categoria": _e("laptops", 0.97),
            "cantidad": _e("una", 0.9),
            "precio": _e("m0", 0.96),
            "precio_alcance": _e("total", 0.9),
            "moneda": _e("USD", 0.97),
            "plazo": _e("f0", 0.92),
            "plazo_firmeza": _e("exige", 0.85),
        },
    },
}


def test_simula_la_conversacion_y_puntua():
    cliente = ev.ClienteGrabado(None, dict(GRABADAS))
    caso = CASOS["notebook_incompleto_es"]
    resultado = ev.correr_caso(caso, cliente, HOY, CATEGORIAS)
    assert resultado.error is None and not resultado.sin_cache
    assert resultado.repreguntas == 1
    assert resultado.final == {
        "categoria": "laptops", "cantidad": 1, "precio": 1200.0, "moneda": "USD",
        "plazo": "2026-10-23",
    }
    assert len(resultado.preguntas) == 3  # porqué, repregunta, confirmación
    puntaje = ev.puntuar(caso, resultado)
    assert puntaje["turno0"] == (5, 5) and puntaje["final"] == (5, 5)
    assert puntaje["repreguntas_ok"] and puntaje["perfecto"]
    assert puntaje["resuelto_mal"] == 0
    assert cliente.latencias == [1.0, 2.0, 3.0]


def test_sin_respuesta_grabada_corta_el_caso():
    cliente = ev.ClienteGrabado(None, {"notebook_incompleto_es#0": GRABADAS["notebook_incompleto_es#0"]})
    resultado = ev.correr_caso(CASOS["notebook_incompleto_es"], cliente, HOY, CATEGORIAS)
    assert resultado.sin_cache is True
    assert resultado.final["precio"] == "faltante"


def test_puntua_los_errores_peligrosos():
    caso = CASOS["notebook_incompleto_es"]
    resultado = ev.correr_caso(caso, ev.ClienteGrabado(None, dict(GRABADAS)), HOY, CATEGORIAS)
    resultado.final["precio"] = 999.0
    resultado.estados_finales["precio"] = "resuelto"
    puntaje = ev.puntuar(caso, resultado)
    assert puntaje["final"] == (4, 5) and puntaje["resuelto_mal"] == 1 and not puntaje["perfecto"]


def test_resumen_y_calibracion_sin_llamadas():
    casos = [CASOS["notebook_incompleto_es"]]
    filas = ev.calibrar(casos, dict(GRABADAS), HOY, CATEGORIAS,
                        grilla=[Umbrales(0.8, 0.5), Umbrales(0.98, 0.5), Umbrales(0.6, 0.4)])
    assert [f["umbrales"] for f in filas] == [(0.8, 0.5), (0.98, 0.5), (0.6, 0.4)]
    base = filas[0]
    assert base["final_pct"] == 100.0 and base["turno0_pct"] == 100.0
    # Con 0,98 nada queda resuelto: lo dicho pasa a "a confirmar" (no es faltante).
    assert filas[1]["turno0_pct"] < 100.0


def test_serializar_ida_y_vuelta():
    from decisiones import Rechazo, RespuestaEleccion, RespuestaPredicado, RespuestaPuntaje

    original = {
        "a": RespuestaEleccion("a", "x", {"x": 0.9, "y": 0.1}, 0.8),
        "b": RespuestaPredicado("b", 0.3),
        "c": RespuestaPuntaje("c", 1.5, {0: 0.5, 1: 0.5}, 0.4),
        "d": Rechazo("d"),
    }
    assert ev.deserializar(json.loads(json.dumps(ev.serializar(original)))) == original


def test_leer_env_solo_toma_las_claves_de_decisiones(tmp_path):
    archivo = tmp_path / ".env"
    archivo.write_text(
        'OPENAI_API_KEY="sk-x"\nSUPABASE_SECRET_KEY=nada\n# comentario\nDECISIONES_PROVEEDOR=openai\n',
        "utf-8",
    )
    assert ev.leer_env(archivo) == {"OPENAI_API_KEY": "sk-x", "DECISIONES_PROVEEDOR": "openai"}


def test_main_sin_red_ni_grabaciones_solo_mide_la_cobertura(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("RUN_DECISIONES_TESTS", raising=False)
    codigo = ev.main(["--cache", str(tmp_path / "no_existe.json")])
    salida = capsys.readouterr().out
    assert codigo == 0
    assert "Cobertura de candidatos" in salida and "RUN_DECISIONES_TESTS=1" in salida


def test_main_repite_desde_las_grabaciones(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("RUN_DECISIONES_TESTS", raising=False)
    cache = tmp_path / "grabadas.json"
    cache.write_text(json.dumps(GRABADAS), "utf-8")
    codigo = ev.main(["--cache", str(cache), "--casos", "notebook_incompleto_es", "--calibrar"])
    salida = capsys.readouterr().out
    assert codigo == 0
    assert "notebook_incompleto_es" in salida and "Calibración" in salida


# -- contra decisiones real (solo con RUN_DECISIONES_TESTS=1) --------------------------------


@pytest.mark.skipif(os.environ.get("RUN_DECISIONES_TESTS") != "1", reason="sin RUN_DECISIONES_TESTS=1")
def test_demo_contra_decisiones_real():
    from decisiones import cliente_desde_entorno

    real = cliente_desde_entorno()
    if real is None:
        pytest.skip("sin OPENAI_API_KEY ni TYPESAFE_API_KEY")
    caso = CASOS["demo_computadoras_es"]
    resultado = ev.correr_caso(caso, ev.ClienteGrabado(real, {}, max_llamadas=3), HOY, CATEGORIAS)
    puntaje = ev.puntuar(caso, resultado)
    assert puntaje["resuelto_mal"] == 0
    assert resultado.final["precio"] == 20000.0 and resultado.final["moneda"] == "USD"
