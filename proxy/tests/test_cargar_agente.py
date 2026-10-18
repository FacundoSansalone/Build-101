"""`python -m proxy`: qué agente de intención se carga según el entorno.

INTENCION_PROVEEDOR=decisiones|gemini. Sin la variable: decisiones si hay
cliente de decisiones (OPENAI_API_KEY o TYPESAFE_API_KEY), si no Gemini. No
hay llamadas a la red: solo se arman los clientes.
"""

from __future__ import annotations

import pytest

from intencion.agente import AgenteGemini
from intencion.agente_decisiones import AgenteDecisiones
from proxy.__main__ import cargar_agente

OPENAI = {"OPENAI_API_KEY": "sk-test"}
GOOGLE = {"GOOGLE_API_KEY": "g-test"}


def test_apagada_con_intencion_activa_0():
    assert cargar_agente({**OPENAI, **GOOGLE, "INTENCION_ACTIVA": "0"}) is None


def test_sin_claves_no_hay_agente():
    assert cargar_agente({}) is None


def test_con_clave_de_decisiones_por_defecto_es_decisiones_sin_detector():
    agente = cargar_agente(dict(OPENAI))
    assert isinstance(agente, AgenteDecisiones)
    assert agente.detector is None  # sin Gemini no hay oportunidades
    assert agente.cliente.nombre == "openai-decisions"


def test_decisiones_con_gemini_como_detector():
    agente = cargar_agente({**OPENAI, **GOOGLE})
    assert isinstance(agente, AgenteDecisiones)
    assert isinstance(agente.detector, AgenteGemini)


def test_jev_tambien_es_decisiones():
    agente = cargar_agente({"TYPESAFE_API_KEY": "ts-test"})
    assert isinstance(agente, AgenteDecisiones) and agente.cliente.nombre == "jev"


def test_solo_gemini_por_defecto_es_gemini():
    assert isinstance(cargar_agente(dict(GOOGLE)), AgenteGemini)


def test_gemini_pedido_explicito():
    agente = cargar_agente({**OPENAI, **GOOGLE, "INTENCION_PROVEEDOR": "gemini"})
    assert isinstance(agente, AgenteGemini)


def test_gemini_pedido_sin_clave_queda_apagada():
    assert cargar_agente({**OPENAI, "INTENCION_PROVEEDOR": "gemini"}) is None


def test_decisiones_pedido_sin_clave_queda_apagada():
    assert cargar_agente({**GOOGLE, "INTENCION_PROVEEDOR": "decisiones"}) is None


@pytest.mark.parametrize(
    "env",
    [
        {**OPENAI, "INTENCION_PROVEEDOR": "claude"},
        {**OPENAI, "DECISIONES_PROVEEDOR": "otro"},
    ],
)
def test_proveedor_desconocido_no_arranca(env):
    with pytest.raises(SystemExit):
        cargar_agente(env)
