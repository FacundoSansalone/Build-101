"""Con la capa de intención activa, el filtro de seguridad es obligatorio.

Nuestro agente de intención mira lo que ofrece el proveedor para buscar
oportunidades. Sin filtro (`GUARD_FILTRO`), esas respuestas le llegan sin
revisar: `python -m proxy` no arranca, salvo `PROXY_PERMITIR_SIN_FILTRO=1`
explícito (tests y demo), y en ese caso lo avisa en el log.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
import uvicorn

import proxy.__main__ as principal
from contracts import ResultadoFiltro


class AgenteCualquiera:
    nombre = "falso"
    modelo = "modelo-falso"


def _filtro(respuesta: dict[str, Any]) -> ResultadoFiltro:  # pragma: no cover
    return ResultadoFiltro(pasa=True)


# -- exigir_filtro() -----------------------------------------------------------------


def test_con_intencion_y_sin_filtro_no_arranca():
    with pytest.raises(SystemExit) as salida:
        principal.exigir_filtro(AgenteCualquiera(), None, {})
    mensaje = str(salida.value.code)
    assert "GUARD_FILTRO" in mensaje and "PROXY_PERMITIR_SIN_FILTRO" in mensaje


@pytest.mark.parametrize("valor", ["0", "false", "no", ""])
def test_un_permiso_apagado_no_alcanza(valor):
    with pytest.raises(SystemExit):
        principal.exigir_filtro(AgenteCualquiera(), None, {"PROXY_PERMITIR_SIN_FILTRO": valor})


def test_un_permiso_ilegible_no_arranca():
    with pytest.raises(SystemExit) as salida:
        principal.exigir_filtro(AgenteCualquiera(), None, {"PROXY_PERMITIR_SIN_FILTRO": "quizás"})
    assert "PROXY_PERMITIR_SIN_FILTRO" in str(salida.value.code)


def test_con_permiso_explicito_arranca_y_lo_avisa(caplog):
    with caplog.at_level(logging.WARNING, logger="proxy"):
        sin_filtro = principal.exigir_filtro(
            AgenteCualquiera(), None, {"PROXY_PERMITIR_SIN_FILTRO": "1"}
        )
    assert sin_filtro is True
    assert any(
        r.levelno == logging.WARNING and "PROXY_PERMITIR_SIN_FILTRO" in r.getMessage()
        for r in caplog.records
    )


def test_sin_intencion_no_hace_falta_filtro():
    # Etapa 1, benchmark y smoke (INTENCION_ACTIVA=0): sigue como antes.
    assert principal.exigir_filtro(None, None, {}) is False


def test_con_filtro_no_hace_falta_permiso():
    assert principal.exigir_filtro(AgenteCualquiera(), _filtro, {}) is False


# -- python -m proxy -----------------------------------------------------------------


@pytest.fixture
def entorno(monkeypatch, tmp_path):
    """Entorno mínimo para `main()`: nada de Supabase, filtro ni permisos."""
    for nombre in (
        "GUARD_FILTRO",
        "PROXY_PERMITIR_SIN_FILTRO",
        "PROXY_ALLOW_STUB",
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_SECRET_KEY",
    ):
        monkeypatch.delenv(nombre, raising=False)
    monkeypatch.setenv("GUARD_EVENTS_JSONL", str(tmp_path / "e.jsonl"))
    monkeypatch.setenv("GUARD_KEY_PATH", str(tmp_path / "k.pem"))
    monkeypatch.setenv("GUARD_API_TOKEN_PATH", str(tmp_path / "token"))
    arranques: list[Any] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: arranques.append(app))
    return arranques


def _con_agente(monkeypatch, agente: Any) -> None:
    monkeypatch.setattr(principal, "cargar_agente", lambda env=None: agente)


def test_main_con_intencion_y_sin_filtro_no_arranca(monkeypatch, entorno):
    _con_agente(monkeypatch, AgenteCualquiera())
    with pytest.raises(SystemExit) as salida:
        principal.main()
    assert "GUARD_FILTRO" in str(salida.value.code)
    assert entorno == []  # uvicorn.run no se llamó


def test_main_con_permiso_arranca_sin_filtro_y_lo_avisa(monkeypatch, entorno, caplog):
    _con_agente(monkeypatch, AgenteCualquiera())
    monkeypatch.setenv("PROXY_PERMITIR_SIN_FILTRO", "1")
    with caplog.at_level(logging.INFO, logger="proxy"):
        principal.main()
    assert len(entorno) == 1
    avisos = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("PROXY_PERMITIR_SIN_FILTRO" in m for m in avisos)
    # La línea de arranque también lo dice.
    assert any("filtro: NO" in r.getMessage() for r in caplog.records)


def test_main_sin_intencion_arranca_sin_filtro(monkeypatch, entorno):
    _con_agente(monkeypatch, None)
    principal.main()
    assert len(entorno) == 1
