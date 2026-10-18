"""Fixtures del proxy: comercio y catálogo falsos, proxy con stub y cliente."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from proxy import guard_stub
from proxy.app import create_app
from proxy.config import Config
from proxy.evaluador import EvaluadorCargado
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
    ) as client:
        yield client
