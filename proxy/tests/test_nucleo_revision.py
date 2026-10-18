"""Revisión del núcleo del proxy (2026-10-18): fecha de la demo, oyentes,
marcas de tiempo, eventos privados, seguridad y robustez."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date

import pytest

from proxy.config import Config
from proxy.tests.ayudantes import RPC
from proxy.tests.fake_world import HEADERS_A2A, cuerpo, pedido_find_products

# ---------------------------------------------------------------------------
# DEMO_HOY: el "hoy" de la demo (las ofertas tienen fechas fijas)
# ---------------------------------------------------------------------------


def test_demo_hoy_desde_el_entorno():
    assert Config.desde_entorno({}).demo_hoy is None
    assert Config.desde_entorno({"DEMO_HOY": ""}).demo_hoy is None
    assert Config.desde_entorno({"DEMO_HOY": " 2026-10-18 "}).demo_hoy == date(2026, 10, 18)


@pytest.mark.parametrize("valor", ["18/10/2026", "2026-10-32", "20261018", "2026-W42-7", "hoy"])
def test_demo_hoy_invalido_no_arranca(valor):
    with pytest.raises(ValueError, match="DEMO_HOY"):
        Config.desde_entorno({"DEMO_HOY": valor})


def test_hoy_del_proxy_es_demo_hoy_si_esta_fijado(armar_proxy, config, caplog):
    assert armar_proxy().state.proxy.hoy() == date.today()
    with caplog.at_level(logging.WARNING, logger="proxy"):
        fijado = armar_proxy(config=replace(config, demo_hoy=date(2026, 10, 18)))
    assert fijado.state.proxy.hoy() == date(2026, 10, 18)
    assert any("DEMO_HOY" in r.getMessage() for r in caplog.records)


class _AgenteQueAnotaHoy:
    """Agente de intención que solo anota qué "hoy" recibe (y no pregunta nada:
    sin pregunta, el proxy usa la de siempre)."""

    nombre = "anota_hoy"

    def __init__(self) -> None:
        self.hoy: list[date] = []

    def preguntar(self, pedido, hoy, categorias=None):
        self.hoy.append(hoy)
        raise RuntimeError("sin pregunta")


@pytest.mark.anyio
async def test_el_agente_de_intencion_recibe_demo_hoy(armar_proxy, config):
    import httpx

    agente = _AgenteQueAnotaHoy()
    app = armar_proxy(
        config=replace(config, demo_hoy=date(2026, 10, 18), espera_contexto_s=0.05),
        agente=agente,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8010", timeout=10
    ) as c:
        resp = await c.post(RPC, content=cuerpo(pedido_find_products("B")), headers=HEADERS_A2A)
    assert resp.status_code == 200
    assert agente.hoy == [date(2026, 10, 18)]
