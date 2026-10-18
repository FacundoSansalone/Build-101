"""`filtro.describir()`: el modo efectivo del filtro en un texto corto.

Lo muestra quien arranca el proxy o el panel (se consume con
`getattr(filtro, "describir")`). Sale del mismo entorno que lee
`desde_entorno` y no llama a la red.
"""

from __future__ import annotations

import logging

import httpx
import pytest

import filtro
from filtro import FiltroSeguridad, describir
from filtro.tests.falsos import ClienteFalso

SIN_CLIENTE = "sin cliente: bloquea todo"


@pytest.fixture(autouse=True)
def sin_filtro_configurado(monkeypatch):
    for clave in (
        "FILTRO_SOLO_HEURISTICAS",
        "OPENAI_API_KEY",
        "TYPESAFE_API_KEY",
        "DECISIONES_PROVEEDOR",
        "DECISIONES_MODELO_OPENAI",
        "DECISIONES_MODELO_JEV",
    ):
        monkeypatch.delenv(clave, raising=False)
    filtro.configurar(None)
    yield
    filtro.configurar(None)


@pytest.fixture
def sin_red(monkeypatch):
    def prohibido(*args, **kwargs):
        raise AssertionError("describir() no puede llamar a la red")

    monkeypatch.setattr(httpx.Client, "send", prohibido)
    monkeypatch.setattr(httpx.AsyncClient, "send", prohibido)


def test_se_consume_por_getattr_sin_argumentos():
    funcion = getattr(filtro, "describir")
    assert callable(funcion)
    assert isinstance(funcion(), str)
    assert "describir" in filtro.__all__


@pytest.mark.parametrize(
    ("env", "esperado"),
    [
        ({"FILTRO_SOLO_HEURISTICAS": "1"}, "solo heurísticas"),
        ({"FILTRO_SOLO_HEURISTICAS": "true", "OPENAI_API_KEY": "sk-x"}, "solo heurísticas"),
        ({}, SIN_CLIENTE),
        ({"FILTRO_SOLO_HEURISTICAS": "0"}, SIN_CLIENTE),
        ({"OPENAI_API_KEY": "sk-x"}, "heurísticas + modelo openai/gpt-6-luna"),
        (
            {"OPENAI_API_KEY": "sk-x", "DECISIONES_MODELO_OPENAI": "gpt-6-sol"},
            "heurísticas + modelo openai/gpt-6-sol",
        ),
        ({"TYPESAFE_API_KEY": "ts-x"}, "heurísticas + modelo jev/jev-latest"),
        (
            {"OPENAI_API_KEY": "sk-x", "TYPESAFE_API_KEY": "ts-x", "DECISIONES_PROVEEDOR": "jev",
             "DECISIONES_MODELO_JEV": "jev-2"},
            "heurísticas + modelo jev/jev-2",
        ),
    ],
)
def test_describe_el_modo_que_arma_el_entorno(sin_red, env, esperado):
    assert describir(env) == esperado


def test_con_un_proveedor_invalido_dice_que_bloquea_todo(sin_red):
    texto = describir({"DECISIONES_PROVEEDOR": "otro", "OPENAI_API_KEY": "sk-x"})
    assert texto.startswith(SIN_CLIENTE)
    assert "DECISIONES_PROVEEDOR" in texto


def test_nunca_muestra_la_clave(sin_red):
    assert "sk-secreta" not in describir({"OPENAI_API_KEY": "sk-secreta"})
    assert "ts-secreta" not in describir({"TYPESAFE_API_KEY": "ts-secreta"})


def test_sin_argumentos_lee_el_entorno_del_proceso_y_no_fija_el_filtro(sin_red, monkeypatch):
    monkeypatch.setenv("FILTRO_SOLO_HEURISTICAS", "1")
    assert describir() == "solo heurísticas"
    # No arma ni guarda el filtro de filtrar(): si cambia el entorno, cambia.
    monkeypatch.delenv("FILTRO_SOLO_HEURISTICAS")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    assert describir() == "heurísticas + modelo openai/gpt-6-luna"


def test_sin_argumentos_describe_el_filtro_que_usa_filtrar(sin_red, monkeypatch):
    """Si el filtro ya está armado (configurar() o la primera llamada a
    filtrar()), ese es el modo efectivo, aunque el entorno diga otra cosa."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    filtro.configurar(FiltroSeguridad(solo_heuristicas=True))
    assert describir() == "solo heurísticas"
    # Con un entorno explícito, describe ese entorno.
    assert describir({}) == SIN_CLIENTE


def test_describe_tambien_un_filtro_armado_a_mano(sin_red):
    assert FiltroSeguridad(cliente=ClienteFalso()).describir() == "heurísticas + modelo falso"
    assert FiltroSeguridad().describir() == SIN_CLIENTE
    assert FiltroSeguridad(config_invalida=True).describir().startswith(SIN_CLIENTE)


def test_no_ensucia_el_log_cada_vez_que_se_consulta(sin_red, caplog):
    with caplog.at_level(logging.WARNING, logger="filtro"):
        describir({})
        describir({"FILTRO_SOLO_HEURISTICAS": "1"})
        describir({"DECISIONES_PROVEEDOR": "otro"})
        assert caplog.records == []
        filtro.desde_entorno({})  # armar el filtro de verdad sí avisa
    assert caplog.records
