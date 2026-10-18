"""Adaptador de decisiones: formato de pedido y respuesta, fallas y entorno."""

import json

import httpx
import pytest

from decisiones import (
    DecisionesError,
    Eleccion,
    JevDecisiones,
    OpenAIDecisiones,
    Predicado,
    Puntaje,
    Rechazo,
    RespuestaEleccion,
    RespuestaPredicado,
    RespuestaPuntaje,
    cliente_desde_entorno,
)

PREGUNTAS = [
    Predicado("dio_tope", "¿El usuario indicó un presupuesto máximo?"),
    Eleccion("uso", "¿Dónde lo va a usar?", {"outdoor": "Afuera", "no_dicho": None}),
    Puntaje("urgencia", "¿Qué tan urgente es?", ("baja", "media", "alta")),
]


def _transporte(respuesta: dict | None = None, status: int = 200, pedidos: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if pedidos is not None:
            pedidos.append(json.loads(request.content))
        return httpx.Response(status, json=respuesta if respuesta is not None else {})
    return httpx.MockTransport(handler)


def test_openai_arma_el_pedido_y_lee_las_respuestas():
    pedidos: list = []
    respuesta = {"answers": [
        {"type": "predicate", "name": "dio_tope", "probability": 0.97},
        {"type": "choice", "name": "uso", "choice": "outdoor", "confidence": 0.9,
         "probabilities": [{"value": "outdoor", "probability": 0.95},
                           {"value": "no_dicho", "probability": 0.05}]},
        {"type": "score", "name": "urgencia", "score": 1.8, "confidence": 0.7,
         "probabilities": [{"value": 0, "label": "nivel_0", "probability": 0.05},
                           {"value": 1, "label": "nivel_1", "probability": 0.1},
                           {"value": 2, "label": "nivel_2", "probability": 0.85}]},
    ]}
    cliente = OpenAIDecisiones("sk-test", transport=_transporte(respuesta, pedidos=pedidos))
    r = cliente.decidir({"pedido": "hasta 150 USD"}, PREGUNTAS)

    (pedido,) = pedidos
    assert pedido["model"] == "gpt-6-luna"
    assert json.loads(pedido["input"]) == {"pedido": "hasta 150 USD"}
    tipos = [(q["type"], q["name"]) for q in pedido["questions"]]
    assert tipos == [("predicate", "dio_tope"), ("choice", "uso"), ("score", "urgencia")]
    assert pedido["questions"][1]["choices"][1] == {"value": "no_dicho", "description": "no_dicho"}

    assert r["dio_tope"] == RespuestaPredicado("dio_tope", 0.97)
    assert isinstance(r["uso"], RespuestaEleccion) and r["uso"].eleccion == "outdoor"
    assert isinstance(r["urgencia"], RespuestaPuntaje) and r["urgencia"].probabilidades[2] == 0.85


def test_openai_rechazo_y_pregunta_sin_respuesta_son_no_se():
    respuesta = {"answers": [{"type": "refusal", "name": "dio_tope"}]}
    r = OpenAIDecisiones("sk-test", transport=_transporte(respuesta)).decidir("x", PREGUNTAS)
    assert r["dio_tope"] == Rechazo("dio_tope")
    assert r["uso"] == Rechazo("uso") and r["urgencia"] == Rechazo("urgencia")


def test_jev_arma_el_pedido_y_lee_las_respuestas():
    pedidos: list = []
    respuesta = {"answers": {
        "dio_tope": {"type": "noul", "noul": 0.02},
        "uso": {"type": "choice", "choice": "no_dicho", "confidence": 0.8,
                "probabilities": {"outdoor": 0.1, "no_dicho": 0.9}},
        "urgencia": {"type": "score", "score": 0.4, "confidence": 0.6,
                     "probabilities": {"0": 0.6, "1": 0.4, "2": 0.0}},
    }}
    r = JevDecisiones("ts-test", transport=_transporte(respuesta, pedidos=pedidos)).decidir(
        "zapatillas", PREGUNTAS
    )
    (pedido,) = pedidos
    assert pedido["model"] == "jev-latest" and pedido["state"] == "zapatillas"
    assert pedido["questions"]["dio_tope"]["type"] == "noul"
    assert pedido["questions"]["urgencia"]["criteria"] == ["baja", "media", "alta"]
    assert r["dio_tope"].probabilidad == 0.02
    assert r["uso"].eleccion == "no_dicho"
    assert r["urgencia"].probabilidades == {0: 0.6, 1: 0.4, 2: 0.0}


@pytest.mark.parametrize("respuesta", [
    {"answers": [{"type": "predicate", "name": "dio_tope", "probability": 1.7}]},
    {"answers": [{"type": "predicate", "name": "dio_tope", "probability": True}]},
    {"answers": [{"type": "choice", "name": "uso", "choice": "x"}]},
])
def test_respuestas_mal_formadas_son_error(respuesta):
    with pytest.raises(DecisionesError):
        OpenAIDecisiones("sk-test", transport=_transporte(respuesta)).decidir("x", PREGUNTAS)


def test_http_de_error_es_error_sin_reintentar_los_400():
    pedidos: list = []
    cliente = OpenAIDecisiones("sk-test", transport=_transporte({"error": "x"}, 400, pedidos))
    with pytest.raises(DecisionesError):
        cliente.decidir("x", PREGUNTAS)
    assert len(pedidos) == 1


def test_nombres_duplicados_se_rechazan():
    with pytest.raises(ValueError):
        OpenAIDecisiones("sk-test").decidir("x", [Predicado("a", "?"), Predicado("a", "?")])


def test_cliente_desde_entorno():
    assert cliente_desde_entorno({}) is None
    assert isinstance(cliente_desde_entorno({"OPENAI_API_KEY": "sk"}), OpenAIDecisiones)
    assert isinstance(cliente_desde_entorno({"TYPESAFE_API_KEY": "ts"}), JevDecisiones)
    ambos = {"OPENAI_API_KEY": "sk", "TYPESAFE_API_KEY": "ts", "DECISIONES_PROVEEDOR": "jev"}
    assert isinstance(cliente_desde_entorno(ambos), JevDecisiones)
    assert cliente_desde_entorno({"DECISIONES_PROVEEDOR": "jev", "OPENAI_API_KEY": "sk"}) is None
    with pytest.raises(ValueError):
        cliente_desde_entorno({"DECISIONES_PROVEEDOR": "otro"})
