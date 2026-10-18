"""Revisión de seguridad del gateway, de punta a punta (la app entera).

Cada test demuestra un hallazgo de la revisión adversarial y fija el arreglo.
Los de una sola pieza (claves, webhooks, validación) están en su archivo.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from proxy.estado import EstadoMemoria
from proxy.tests.ayudantes import evaluador_espia
from proxy.tests.fake_world import HEADERS_A2A, cuerpo, pedido_find_products
from proxy.tests.gateway_ayudantes import (
    CLAVE_ACME,
    FICHA,
    GATEWAY_PUBLIC_URL,
    armar_gateway,
    cliente_local,
    cliente_publico,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def config_gw(config):
    return replace(config, gateway_public_url=GATEWAY_PUBLIC_URL)


def _anidado(niveles: int) -> Any:
    valor: Any = 1
    for _ in range(niveles):
        valor = [valor]
    return valor


# -- una compra que no se puede guardar no queda huérfana en memoria -----------------


async def test_un_cuerpo_anidado_de_mas_no_deja_una_compra_huerfana(config_gw):
    """Un `valor` anidado ~100 niveles validaba como requisito, pero pydantic no
    lo puede volver a serializar: guardar la compra lanzaba (500) y la compra
    quedaba en memoria, fuera del almacén y del límite de compras activas
    (cada una con hasta 1 MB). Ahora el cuerpo se corta al leerlo."""
    mundo = armar_gateway(config_gw, evaluador_espia()[0])
    ficha = {"requisitos": [
        *FICHA["requisitos"],
        {"campo": "marca", "operador": "incluye", "valor": _anidado(120), "obligatorio": False},
    ]}
    async with cliente_publico(mundo.app) as c:
        resp = await c.post("/v1/compras", json={"pedido": "x", "ficha": ficha})
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["codigo"] == "json_invalido"
        assert (await c.get("/v1/compras")).json()["compras"] == []
    assert mundo.proxy.compras.de_integrador("acme") == []


class AlmacenQueRompe(EstadoMemoria):
    """Falla al guardar con algo que no es `ErrorAlmacen` (un bug, una fila
    que no se puede escribir)."""

    async def guardar_compra(self, fila: dict[str, Any]) -> None:
        raise ValueError("fila que no se puede escribir")


async def test_si_guardar_la_compra_falla_por_otra_cosa_tampoco_queda_en_memoria(config_gw):
    mundo = armar_gateway(config_gw, evaluador_espia()[0], almacen=AlmacenQueRompe())
    async with cliente_publico(mundo.app) as c:
        resp = await c.post("/v1/compras", json={"pedido": "x", "ficha": FICHA})
    assert resp.status_code == 500
    assert resp.json()["error"] == {"codigo": "interno", "mensaje": "Error interno del gateway."}
    assert mundo.proxy.compras.de_integrador("acme") == []


# -- la clave del integrador nunca llega al comercio -----------------------------------


async def test_una_clave_mal_presentada_no_se_reenvia_al_comercio(config_gw):
    """En el oyente local, `Authorization: Token psg_…` (o sin esquema) pasaba
    como tráfico anónimo de la demo y el proxy lo reenviaba al comercio con la
    clave del integrador adentro."""
    mundo = armar_gateway(config_gw, evaluador_espia()[0])
    async with cliente_local(mundo.app) as c:
        for valor in (f"Token {CLAVE_ACME}", CLAVE_ACME):
            resp = await c.post(
                "/a2a/m/merchant_1", content=cuerpo(pedido_find_products("B")),
                headers={**HEADERS_A2A, "Authorization": valor},
            )
            assert resp.status_code == 401, valor
            assert resp.json()["error"]["codigo"] == "clave_invalida"
    assert mundo.merchant.recibidos == []
