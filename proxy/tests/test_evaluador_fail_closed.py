"""N1: el evaluador falla cerrado.

Sin un `guard.evaluar()` real que acepte `checkout=`, el proxy no arranca
(error claro), salvo `PROXY_ALLOW_STUB=1` explícito. Aun con el stub
permitido, `GET /guard/state` y cada evento `veredicto` lo marcan.
"""

from __future__ import annotations

import logging
import sys
import types
from dataclasses import replace

import pytest

from proxy import guard_stub
from proxy.app import create_app
from proxy.config import Config
from proxy.evaluador import EvaluadorCargado, EvaluadorNoDisponible, cargar_evaluador
from proxy.tests.ayudantes import buscar, checkout, cliente_de, eventos, evaluador_espia


def _sin_guard(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "guard", None)  # import guard -> ImportError


def _guard_sin_evaluar(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "guard", types.ModuleType("guard"))


def _evaluar_sin_checkout(ficha, ofertas_vistas, cart_id_elegido, catalogo,
                          politica=None, checkout_hash=None, ahora=None):  # pragma: no cover
    raise AssertionError("no se tiene que llamar")


def _guard_sin_checkout(monkeypatch) -> None:
    modulo = types.ModuleType("guard")
    modulo.evaluar = _evaluar_sin_checkout
    monkeypatch.setitem(sys.modules, "guard", modulo)


ROTOS = [_sin_guard, _guard_sin_evaluar, _guard_sin_checkout]
IDS = ["no-importa", "sin-evaluar", "sin-checkout"]


@pytest.mark.parametrize("romper", ROTOS, ids=IDS)
def test_sin_guard_real_no_hay_evaluador(monkeypatch, romper):
    romper(monkeypatch)
    with pytest.raises(EvaluadorNoDisponible, match="PROXY_ALLOW_STUB"):
        cargar_evaluador()


@pytest.mark.parametrize("romper", ROTOS, ids=IDS)
def test_con_permiso_explicito_usa_el_stub_y_avisa(monkeypatch, caplog, romper):
    romper(monkeypatch)
    with caplog.at_level(logging.WARNING):
        cargado = cargar_evaluador(permitir_stub=True)
    assert cargado.nombre == "stub"
    assert cargado.evaluar is guard_stub.evaluar
    assert "stub" in caplog.text.lower()


def test_guard_real_que_acepta_checkout_se_usa(monkeypatch):
    modulo = types.ModuleType("guard")

    def evaluar(ficha, ofertas_vistas, cart_id_elegido, catalogo, politica=None,
                checkout_hash=None, ahora=None, checkout=None):  # pragma: no cover
        raise AssertionError

    modulo.evaluar = evaluar
    monkeypatch.setitem(sys.modules, "guard", modulo)
    cargado = cargar_evaluador()
    assert cargado.nombre == "guard"
    assert cargado.evaluar is evaluar


def test_create_app_sin_guard_real_no_arranca(monkeypatch, config, transporte_mundo):
    _sin_guard(monkeypatch)
    with pytest.raises(EvaluadorNoDisponible):
        create_app(replace(config, permitir_stub=False), transport=transporte_mundo)


def test_create_app_no_acepta_el_stub_sin_permiso(config, transporte_mundo, stub):
    with pytest.raises(EvaluadorNoDisponible, match="PROXY_ALLOW_STUB"):
        create_app(
            replace(config, permitir_stub=False), transport=transporte_mundo, evaluador=stub
        )


def test_create_app_no_acepta_un_evaluador_sin_checkout(config, transporte_mundo):
    sin_checkout = EvaluadorCargado(evaluar=_evaluar_sin_checkout, nombre="guard")
    for permitir in (False, True):
        with pytest.raises(EvaluadorNoDisponible, match="checkout"):
            create_app(
                replace(config, permitir_stub=permitir),
                transport=transporte_mundo,
                evaluador=sin_checkout,
            )


def test_permitir_stub_desde_el_entorno():
    assert Config.desde_entorno({}).permitir_stub is False
    assert Config.desde_entorno({"PROXY_ALLOW_STUB": "1"}).permitir_stub is True
    assert Config.desde_entorno({"PROXY_ALLOW_STUB": "0"}).permitir_stub is False


def test_main_sale_con_un_error_claro(monkeypatch, tmp_path):
    import uvicorn

    import proxy.__main__ as principal

    _sin_guard(monkeypatch)
    monkeypatch.delenv("PROXY_ALLOW_STUB", raising=False)
    monkeypatch.setenv("GUARD_EVENTS_JSONL", str(tmp_path / "e.jsonl"))
    monkeypatch.setenv("GUARD_KEY_PATH", str(tmp_path / "k.pem"))
    monkeypatch.setenv("GUARD_API_TOKEN_PATH", str(tmp_path / "token"))

    def no_arrancar(*args, **kwargs):  # pragma: no cover
        pytest.fail("el proxy no tiene que arrancar sin el Guard real")

    monkeypatch.setattr(uvicorn, "run", no_arrancar)
    with pytest.raises(SystemExit) as salida:
        principal.main()
    assert "PROXY_ALLOW_STUB" in str(salida.value.code)


# ---------------------------------------------------------------------------
# Con el stub permitido, todo lo marca
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_stub_se_marca_en_state_y_en_cada_veredicto(cliente):
    assert (await cliente.get("/guard/state")).json()["evaluador"] == "stub"
    contexto = await buscar(cliente, "B")
    await checkout(cliente, contexto, "cart_1")
    await checkout(cliente, contexto, "cart_4")
    veredictos = [e for e in await eventos(cliente) if e["tipo"] == "veredicto"]

    assert len(veredictos) == 2
    assert all(e["datos"]["evaluador"] == "stub" for e in veredictos)


@pytest.mark.anyio
async def test_guard_real_se_marca_en_cada_veredicto(armar_proxy):
    evaluador, llamadas = evaluador_espia()
    async with cliente_de(armar_proxy(evaluador=evaluador)) as c:
        contexto = await buscar(c, "B")
        await checkout(c, contexto, "cart_1")
        (ev,) = [e for e in await eventos(c) if e["tipo"] == "veredicto"]

    assert ev["datos"]["evaluador"] == "guard"
    # Siempre se ata el checkout firmado: evaluar() lo recibe.
    assert llamadas[0]["checkout"] is not None
