"""Fixtures del proxy: comercio y catálogo falsos, proxy con stub y cliente."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from proxy import guard_stub
from proxy.app import create_app
from proxy.config import Config
from proxy.demo_local import FuentesDemo
from proxy.evaluador import EvaluadorCargado
from proxy.seguridad import HEADER_TOKEN
from proxy.tests.fake_world import (
    CATALOG_HOST,
    CATALOG_URL,
    MERCHANT_HOST,
    MERCHANT_URL,
    PROXY_PUBLIC_URL,
    FakeMerchant,
    RuteoPorHost,
    app_catalogo,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def merchant() -> FakeMerchant:
    return FakeMerchant()


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        merchant_upstream_url=MERCHANT_URL,
        proxy_public_url=PROXY_PUBLIC_URL,
        catalogo_url=CATALOG_URL,
        guard_activo=True,
        events_jsonl=tmp_path / "guard_events.jsonl",
        guard_key_path=tmp_path / "guard_signing_key.pem",
        permitir_stub=True,  # los tests usan el stub explícitamente (N1)
        api_token_path=tmp_path / "guard_api_token",
        # El default es 0 (manda la ficha del usuario, etapa 3). Los tests de
        # siempre prueban con la ficha del escenario de la demo: la piden; los
        # del comportamiento por defecto usan `replace(config, ficha_demo=False)`.
        ficha_demo=True,
    )


@pytest.fixture
def stub() -> EvaluadorCargado:
    return EvaluadorCargado(evaluar=guard_stub.evaluar, nombre="stub")


@pytest.fixture
def transporte_mundo(merchant: FakeMerchant) -> RuteoPorHost:
    return RuteoPorHost({MERCHANT_HOST: merchant.app, CATALOG_HOST: app_catalogo()})


@pytest.fixture
def armar_proxy(
    config: Config, transporte_mundo: RuteoPorHost, stub: EvaluadorCargado
) -> Callable[..., Any]:
    def _armar(**kwargs: Any):
        kwargs.setdefault("config", config)
        kwargs.setdefault("transport", transporte_mundo)
        kwargs.setdefault("evaluador", stub)
        # La demo local, como `python -m proxy`: las fichas de los escenarios
        # fijos y el chequeo de AP2. Sin demo, `demo=None` explícito.
        kwargs.setdefault("demo", FuentesDemo())
        return create_app(**kwargs)

    return _armar


@pytest.fixture
def proxy_app(armar_proxy):
    return armar_proxy()


@pytest.fixture
async def cliente(proxy_app) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy_app),
        base_url="http://localhost:8010",
        timeout=10,
        # Como el reenvío del dashboard: con el token de la API de control (N4).
        headers={HEADER_TOKEN: proxy_app.state.proxy.api_token},
    ) as client:
        yield client
