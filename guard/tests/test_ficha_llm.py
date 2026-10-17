"""Tests de la ficha con LLM (único uso de LLM del Guard).

Los tests usan un cliente falso. El smoke test real corre solo con
RUN_LLM_TESTS=1 (necesita GOOGLE_API_KEY o GUARD_ENV_FILE apuntando a un
.env; la clave nunca se imprime).
"""

import json
import os
from pathlib import Path

import pytest

from contracts import Ficha
from guard.ficha_llm import ficha_desde_pedido, vocabulario_desde_catalogo


class _Respuesta:
    def __init__(self, texto: str):
        self.text = texto
        self.parsed = None


class _ClienteFalso:
    """Imita genai.Client().models.generate_content(...)."""

    def __init__(self, respuesta: dict | str):
        self.llamadas: list[dict] = []
        self._texto = respuesta if isinstance(respuesta, str) else json.dumps(respuesta)
        self.models = self

    def generate_content(self, *, model, contents, config):
        self.llamadas.append({"model": model, "contents": contents, "config": config})
        return _Respuesta(self._texto)


def _requisitos(ficha: Ficha) -> set:
    return {(r.campo, r.operador, json.dumps(r.valor), r.obligatorio) for r in ficha.requisitos}


def _respuesta_de_intent(intent: dict) -> dict:
    vacio = dict.fromkeys(
        ["max_price", "currency", "category", "use", "size", "brand", "merchant"]
    )
    return {
        "hard": {**vacio, **intent["hard"]},
        "preferred": {**vacio, **intent["preferred"]},
    }


def test_ficha_equivalente_al_intent_y_sin_confirmar(escenario):
    intent = escenario("baseline")["intent"]
    cliente = _ClienteFalso(_respuesta_de_intent(intent))
    ficha = ficha_desde_pedido(intent["user_request"], cliente=cliente)
    assert ficha.confirmada is False
    assert ficha.pedido_original == intent["user_request"]
    assert _requisitos(ficha) == _requisitos(Ficha.desde_intent(intent))


def test_pide_salida_estructurada_al_modelo(escenario):
    intent = escenario("baseline")["intent"]
    cliente = _ClienteFalso(_respuesta_de_intent(intent))
    ficha_desde_pedido(intent["user_request"], cliente=cliente, modelo="modelo-x")
    (llamada,) = cliente.llamadas
    assert llamada["model"] == "modelo-x"
    assert intent["user_request"] in llamada["contents"]
    config = llamada["config"]
    assert config["response_mime_type"] == "application/json"
    esquema = config["response_schema"]
    assert set(esquema["properties"]) == {"hard", "preferred"}
    assert "max_price" in esquema["properties"]["hard"]["properties"]


def test_modelo_por_defecto():
    cliente = _ClienteFalso({"hard": {}, "preferred": {}})
    ficha_desde_pedido("algo", cliente=cliente)
    assert cliente.llamadas[0]["model"] == "gemini-3.1-flash-lite-preview"


def test_vocabulario_del_catalogo_va_en_la_instruccion(catalogo):
    vocabulario = vocabulario_desde_catalogo(catalogo)
    assert "basketball_shoes" in vocabulario["category"]
    assert "street" in vocabulario["use"]
    cliente = _ClienteFalso({"hard": {}, "preferred": {}})
    ficha_desde_pedido("algo", cliente=cliente, vocabulario=vocabulario)
    instruccion = cliente.llamadas[0]["config"]["system_instruction"]
    assert "basketball_shoes" in instruccion and "lifestyle_sneakers" in instruccion


def test_limpia_nulos_duplicados_y_normaliza():
    cliente = _ClienteFalso(
        {
            "hard": {"max_price": "150", "currency": "usd", "size": 42.0, "brand": None},
            "preferred": {"currency": "EUR", "brand": "Nike", "use": None},
        }
    )
    ficha = ficha_desde_pedido("pedido", cliente=cliente)
    por_campo = {(r.campo, r.obligatorio): r.valor for r in ficha.requisitos}
    assert por_campo == {
        ("precio", True): 150.0,
        ("moneda", True): "USD",
        ("talle", True): 42,
        ("marca", False): "Nike",
    }


@pytest.mark.parametrize("respuesta", ["no es json", "[]", '{"hard": "x", "preferred": {}}'])
def test_respuesta_invalida_no_da_una_ficha(respuesta):
    with pytest.raises(ValueError):
        ficha_desde_pedido("pedido", cliente=_ClienteFalso(respuesta))


def test_pedido_vacio_es_error():
    with pytest.raises(ValueError):
        ficha_desde_pedido("   ", cliente=_ClienteFalso({"hard": {}, "preferred": {}}))


def test_sin_cliente_crea_uno_de_google_genai(monkeypatch):
    from google import genai

    creados = []

    class _ClienteGenai(_ClienteFalso):
        def __init__(self, **kwargs):
            super().__init__({"hard": {"size": 42}, "preferred": {}})
            creados.append(kwargs)

    monkeypatch.setattr(genai, "Client", _ClienteGenai)
    ficha = ficha_desde_pedido("talle 42")
    assert len(creados) == 1
    assert [r.campo for r in ficha.requisitos] == ["talle"]


# --- smoke test real (opcional) --------------------------------------------------


def _cargar_clave(monkeypatch) -> None:
    """Pone GOOGLE_API_KEY en el entorno desde un .env, sin imprimirla."""
    if os.environ.get("GOOGLE_API_KEY"):
        return
    raiz = Path(__file__).resolve().parents[2]
    candidatos = [os.environ.get("GUARD_ENV_FILE"), raiz / ".env"]
    for candidato in filter(None, candidatos):
        ruta = Path(candidato)
        if not ruta.is_file():
            continue
        for linea in ruta.read_text(encoding="utf-8").splitlines():
            clave, _, valor = linea.strip().partition("=")
            if clave == "GOOGLE_API_KEY" and valor:
                monkeypatch.setenv("GOOGLE_API_KEY", valor.strip().strip('"').strip("'"))
                return
    pytest.skip("Sin GOOGLE_API_KEY (definila o usá GUARD_ENV_FILE)")


@pytest.mark.skipif(os.environ.get("RUN_LLM_TESTS") != "1", reason="RUN_LLM_TESTS=1 para correrlo")
def test_smoke_llm_real(escenario, catalogo, monkeypatch):
    _cargar_clave(monkeypatch)
    intent = escenario("baseline")["intent"]
    ficha = ficha_desde_pedido(
        intent["user_request"], vocabulario=vocabulario_desde_catalogo(catalogo)
    )
    assert ficha.confirmada is False
    assert _requisitos(ficha) == _requisitos(Ficha.desde_intent(intent))
