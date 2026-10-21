"""Retención de las tablas `gw_*` (`proxy/retencion.py`): la configuración
(`GATEWAY_RETENCION_DIAS`, el secreto de la purga), la ruta de la purga para
Vercel Cron (fuera de `/v1`, solo con su secreto y en el oyente público) y
su tope en Vercel. La purga en cada almacén está en `test_historial_almacen`.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from proxy import vercel
from proxy.config import RUTA_PURGA, Config
from proxy.estado import EstadoMemoria, EstadoSupabase
from proxy.tests.ayudantes import evaluador_espia
from proxy.tests.gateway_ayudantes import (
    BASE_LOCAL,
    BASE_PUBLICA,
    CLAVE_ACME,
    GATEWAY_PUBLIC_URL,
    armar_gateway,
)
from proxy.tests.postgrest_falso import BaseFalsa
from proxy.tests.test_claves_lectura import CLAVE_LECTURA, registro_con_lectura
from proxy.tests.test_estado import fila_compra
from proxy.tipos_gateway import ErrorAlmacen

pytestmark = pytest.mark.anyio

SECRETO = "s" * 40
VIEJA, NUEVA = "cmp-000000000000000a", "cmp-000000000000000b"


# -- configuración ------------------------------------------------------------------


def test_por_defecto_90_dias_y_sin_purga():
    config = Config.desde_entorno({})
    assert config.retencion_dias == 90 and config.purga_secreto is None


def test_la_retencion_desde_el_entorno():
    assert Config.desde_entorno({"GATEWAY_RETENCION_DIAS": "30"}).retencion_dias == 30


@pytest.mark.parametrize("valor", ["0", "-3", "6", "3651", "diez", "1.5"])
def test_una_retencion_fuera_de_rango_no_arranca(valor):
    with pytest.raises(ValueError):
        Config.desde_entorno({"GATEWAY_RETENCION_DIAS": valor})


def test_la_retencion_es_mas_larga_que_la_vida_de_una_compra():
    with pytest.raises(ValueError, match="vida de una compra"):
        Config.desde_entorno({"GATEWAY_RETENCION_DIAS": "7",
                              "GATEWAY_COMPRA_TTL_S": str(8 * 86400)})
    with pytest.raises(ValueError):
        replace(Config(), retencion_dias=2)


@pytest.mark.parametrize("env, esperado", [
    ({"GATEWAY_PURGA_SECRETO": SECRETO}, SECRETO),
    ({"CRON_SECRET": SECRETO}, SECRETO),
    ({"GATEWAY_PURGA_SECRETO": SECRETO, "CRON_SECRET": SECRETO}, SECRETO),
    ({"GATEWAY_PURGA_SECRETO": "  "}, None),
])
def test_el_secreto_de_la_purga(env, esperado):
    assert Config.desde_entorno(env).purga_secreto == esperado


@pytest.mark.parametrize("env", [
    {"GATEWAY_PURGA_SECRETO": "corto"},
    {"CRON_SECRET": "x" * 31},
    {"GATEWAY_PURGA_SECRETO": "con espacio " + "x" * 40},
    {"GATEWAY_PURGA_SECRETO": "ñ" * 40},
    {"GATEWAY_PURGA_SECRETO": SECRETO, "CRON_SECRET": "t" * 40},
])
def test_un_secreto_que_no_sirve_no_arranca(env):
    with pytest.raises(ValueError) as error:
        Config.desde_entorno(env)
    for valor in env.values():
        assert valor.strip() not in str(error.value) or len(valor.strip()) < 8


def test_el_secreto_no_sale_en_el_repr():
    assert SECRETO not in repr(Config.desde_entorno({"GATEWAY_PURGA_SECRETO": SECRETO}))


# -- la ruta de la purga --------------------------------------------------------------


@pytest.fixture
def almacen() -> EstadoMemoria:
    return EstadoMemoria(durable=True)


async def _compras(almacen: EstadoMemoria) -> None:
    ahora = datetime.now(timezone.utc)
    await almacen.guardar_compra(fila_compra(VIEJA, creada=ahora - timedelta(days=91)))
    await almacen.guardar_compra(fila_compra(NUEVA, creada=ahora - timedelta(days=89)))


def _mundo(config, almacen, *, secreto: str | None = SECRETO):
    config = replace(config, gateway_public_url=GATEWAY_PUBLIC_URL, purga_secreto=secreto)
    return armar_gateway(config, evaluador_espia()[0], almacen=almacen,
                         integradores=registro_con_lectura())


def _cliente(app, base: str = BASE_PUBLICA, **headers: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base,
                             headers=headers, timeout=10)


@pytest.mark.parametrize("metodo", ["GET", "POST"])
async def test_la_purga_borra_lo_viejo_con_el_secreto(config, almacen, metodo):
    await _compras(almacen)
    mundo = _mundo(config, almacen)
    async with _cliente(mundo.app, Authorization=f"Bearer {SECRETO}") as c:
        r = await c.request(metodo, RUTA_PURGA)
    assert r.status_code == 200, r.text
    datos = r.json()
    assert datos["retencion_dias"] == 90 and datos["borradas"]["gw_compras"] == 1
    corte = datetime.fromisoformat(datos["antes_de"])
    assert abs((datetime.now(timezone.utc) - corte) - timedelta(days=90)) < timedelta(minutes=1)
    assert r.headers["cache-control"] == "no-store"
    assert await almacen.cargar_compra(VIEJA) is None
    assert await almacen.cargar_compra(NUEVA) is not None


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer otro-secreto-de-cuarenta-caracteres-xxxxx"},
    {"Authorization": f"Basic {SECRETO}"},
    {"Authorization": f"Bearer {CLAVE_ACME}"},
    {"Authorization": f"Bearer {CLAVE_LECTURA}"},
    {"Authorization": f"Bearer {SECRETO[:-1]}"},
])
async def test_sin_el_secreto_no_purga_nada(config, almacen, headers):
    """Ninguna clave de API (completa ni de solo lectura) sirve para purgar."""
    await _compras(almacen)
    mundo = _mundo(config, almacen)
    async with _cliente(mundo.app, **headers) as c:
        r = await c.get(RUTA_PURGA)
    assert r.status_code == 401 and r.json()["error"]["codigo"] == "no_autorizado"
    assert SECRETO not in r.text
    assert await almacen.cargar_compra(VIEJA) is not None


async def test_dos_authorization_no_valen(config, almacen):
    await _compras(almacen)
    mundo = _mundo(config, almacen)
    async with _cliente(mundo.app) as c:
        r = await c.get(RUTA_PURGA, headers=[("Authorization", f"Bearer {SECRETO}"),
                                             ("Authorization", f"Bearer {SECRETO}")])
    assert r.status_code == 401
    assert await almacen.cargar_compra(VIEJA) is not None


async def test_en_el_oyente_local_no_existe(config, almacen):
    await _compras(almacen)
    mundo = _mundo(config, almacen)
    async with _cliente(mundo.app, BASE_LOCAL, Authorization=f"Bearer {SECRETO}") as c:
        r = await c.get(RUTA_PURGA)
    assert r.status_code == 404
    assert await almacen.cargar_compra(VIEJA) is not None


async def test_sin_secreto_la_ruta_no_existe(config, almacen):
    mundo = _mundo(config, almacen, secreto=None)
    async with _cliente(mundo.app, Authorization=f"Bearer {SECRETO}") as c:
        r = await c.get(RUTA_PURGA)
    assert r.status_code == 404
    assert not any(getattr(ruta, "path", "") == RUTA_PURGA for ruta in mundo.app.routes)


async def test_si_el_almacen_falla_es_503(config):
    class Caido(EstadoMemoria):
        async def purgar(self, antes_de):
            raise ErrorAlmacen("no responde")

    mundo = _mundo(config, Caido())
    async with _cliente(mundo.app, Authorization=f"Bearer {SECRETO}") as c:
        r = await c.post(RUTA_PURGA)
    assert r.status_code == 503 and r.json()["error"]["codigo"] == "estado_no_disponible"


async def test_no_esta_en_v1_ni_en_la_openapi(config, almacen):
    mundo = _mundo(config, almacen)
    assert not RUTA_PURGA.startswith("/v1")
    async with _cliente(mundo.app, Authorization=f"Bearer {CLAVE_ACME}") as c:
        spec = (await c.get("/v1/openapi.json")).json()
    assert not any("purg" in ruta for ruta in spec["paths"])


# -- en Vercel ------------------------------------------------------------------------


def _app_vercel(**cambios: Any):
    from proxy.tests import test_vercel_entrada as tv

    base = BaseFalsa()
    almacen = EstadoSupabase(tv.URL_SUPABASE, tv.SECRETO_SUPABASE, transport=base.transporte())
    return vercel.crear_app_serverless(tv.entorno(**cambios), almacen=almacen), base, tv.URL_PUBLICA


@pytest.mark.parametrize("valor", ["366", "3650"])
def test_en_vercel_la_retencion_tiene_tope(valor):
    app, _, _ = _app_vercel(GATEWAY_RETENCION_DIAS=valor)
    assert isinstance(app, vercel.AppNoConfigurada)
    assert "GATEWAY_RETENCION_DIAS" in app.motivo


def test_en_vercel_un_secreto_corto_no_arranca():
    app, _, _ = _app_vercel(CRON_SECRET="corto")
    assert isinstance(app, vercel.AppNoConfigurada) and "corto" not in app.motivo


async def test_el_cron_de_vercel_purga_con_cron_secret():
    app, base, url = _app_vercel(CRON_SECRET=SECRETO, GATEWAY_RETENCION_DIAS="30")
    assert not isinstance(app, vercel.AppNoConfigurada)
    vieja = fila_compra(VIEJA, creada=datetime.now(timezone.utc) - timedelta(days=31))
    base.tablas["gw_compras"].filas.append(json.loads(json.dumps(vieja)))
    async with _cliente(app, url, Authorization=f"Bearer {SECRETO}",
                        **{"User-Agent": "vercel-cron/1.0"}) as c:
        r = await c.get(RUTA_PURGA)
    assert r.status_code == 200, r.text
    assert r.json()["retencion_dias"] == 30 and r.json()["borradas"]["gw_compras"] == 1
    assert base.filas("gw_compras") == []


async def test_en_vercel_sin_secreto_no_hay_purga():
    app, _, url = _app_vercel()
    async with _cliente(app, url, Authorization=f"Bearer {SECRETO}") as c:
        r = await c.get(RUTA_PURGA)
    assert r.status_code == 404


def test_vercel_json_tiene_el_cron_de_la_purga():
    from proxy.tests.test_despliegue_vercel import VERCEL_JSON

    datos = json.loads(VERCEL_JSON.read_text(encoding="utf-8"))
    (cron,) = datos["crons"]
    assert cron["path"] == RUTA_PURGA
    # Una vez por día (lo que permite Vercel Hobby).
    minuto, hora, *resto = cron["schedule"].split()
    assert minuto.isdigit() and hora.isdigit() and resto == ["*", "*", "*"]
