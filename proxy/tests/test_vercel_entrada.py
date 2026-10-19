"""La entrada serverless del motor (lote D de Vercel).

- `create_app(..., clave_recibo=, rutas_control=, rutas_heredadas=)`: con la
  clave inyectada firma y publica con ella y no toca el archivo; sin la API
  de control no hay `/guard/*`, ni `ControlLocal`, ni CORS, ni archivo de
  token, y todo pedido es del oyente público; sin las rutas heredadas no
  está la ruta vieja del comercio por defecto. Con los defaults, la app de
  siempre.
- `proxy.vercel.crear_app_serverless(env, almacen=, transport=)`: la app
  completa (solo `/a2a/m/{id}` y `/v1/*`, con lo que fuerza el despliegue)
  contra un PostgREST falso; 503 `motor_no_configurado` a todo si falta o
  sobra algo, con el motivo en el log y sin secretos; los topes de tiempo y
  de cuerpo; la frontera escrita en `vercel.py` igual a la del pyproject.
- `index.py` en un subproceso, con uvicorn y la simulación bloqueados, sin
  red ni escrituras al armar la app.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from starlette.middleware.cors import CORSMiddleware

from guard.recibo import verificar_recibo
from proxy import guard_stub, vercel
from proxy.estado import EstadoMemoria, EstadoSupabase
from proxy.evaluador import ClaveRecibo, EvaluadorCargado, EvaluadorNoDisponible
from proxy.seguridad import HEADER_TOKEN, ControlLocal
from proxy.tests.ayudantes import buscar, checkout, cliente_de, eventos
from proxy.tests.fake_world import (
    CATALOG_HOST,
    MERCHANT_HOST,
    FakeMerchant,
    RuteoPorHost,
    app_catalogo,
)
from proxy.tests.gateway_ayudantes import (
    CLAVE_ACME,
    MERCHANT_B_HOST,
    SHA_ACME,
    FakeMerchantB,
    armar_gateway,
    cliente_publico,
    crear_compra,
    datos_integradores,
    jws_valido,
)
from proxy.tests.gateway_ayudantes import buscar as buscar_compra
from proxy.tests.gateway_ayudantes import checkout as checkout_compra
from proxy.tests.postgrest_falso import BaseFalsa
from proxy.vercel import PROHIBIDAS

pytestmark = pytest.mark.anyio


def _rutas(app: Any) -> set[str]:
    return {getattr(r, "path", "") for r in app.routes}


def _middlewares(app: Any) -> list[Any]:
    return [m.cls for m in app.user_middleware]


@pytest.fixture
def clave() -> ClaveRecibo:
    return ClaveRecibo(clave=ec.generate_private_key(ec.SECP256R1()), kid="kaiai-prueba-1")


# -- create_app con los defaults: la app de siempre ------------------------------------


def test_los_defaults_dejan_la_app_de_siempre(armar_proxy, config):
    app = armar_proxy()
    rutas = _rutas(app)
    assert {"/guard/state", "/guard/public-key", config.rpc_path, "/a2a/m/{comercio}"} <= rutas
    assert {ControlLocal, CORSMiddleware} <= set(_middlewares(app))
    assert app.state.proxy.clave_recibo is None
    assert config.api_token_path.read_text("utf-8").strip() == app.state.proxy.api_token
    assert config.guard_key_path.exists()  # la del archivo, creada al arrancar


# -- clave_recibo ----------------------------------------------------------------------


async def test_con_clave_inyectada_firma_y_publica_con_ella(armar_proxy, config, clave):
    from proxy.evaluador import cargar_evaluador

    app = armar_proxy(
        clave_recibo=clave, evaluador=cargar_evaluador(False),
        config=replace(config, permitir_stub=False),
    )
    assert not config.guard_key_path.exists()  # no se crea ni se lee
    async with cliente_de(app) as c:
        publica = (await c.get("/guard/public-key")).json()
        contexto = await buscar(c, "B")
        await checkout(c, contexto, "cart_1")
        (veredicto,) = [e for e in await eventos(c) if e["tipo"] == "veredicto"]
    assert publica == {"kid": "kaiai-prueba-1", "jwk": clave.jwk_publica()}
    recibo = veredicto["datos"]["veredicto"]["recibo"]
    assert recibo["kid"] == "kaiai-prueba-1" and veredicto["datos"]["recibo_firmado"] is True
    from contracts import Recibo

    assert verificar_recibo(Recibo.model_validate(recibo), clave.clave.public_key())
    assert not config.guard_key_path.exists()


async def test_v1_public_key_y_evaluar_con_la_clave_inyectada(config, clave):
    """El gateway (`NucleoProxy`) usa la misma: `/v1/public-key` y el recibo
    de las compras de integrador."""
    from proxy.evaluador import cargar_evaluador

    mundo = armar_gateway(
        replace(config, permitir_stub=False), cargar_evaluador(False), clave_recibo=clave,
        demo=None,
    )
    async with cliente_publico(mundo.app) as c:
        publica = (await c.get("/v1/public-key")).json()
        compra = await crear_compra(c)
        busqueda = await buscar_compra(c, compra["compra_id"], escenario="baseline")
        assert busqueda.status_code == 200
        contexto = busqueda.json()["result"]["contextId"]
        await checkout_compra(c, compra["compra_id"], contexto, "cart_1")
        vista = (await c.get(f"/v1/compras/{compra['compra_id']}")).json()
    assert publica["kid"] == "kaiai-prueba-1" and publica["keys"] == [clave.jwk_publica()]
    (veredicto,) = vista["veredictos"]
    assert veredicto["recibo"]["kid"] == "kaiai-prueba-1"
    assert jws_valido(veredicto["recibo"]["jws"], publica["jwk"])
    assert not config.guard_key_path.exists()


# -- rutas_control=False ----------------------------------------------------------------


async def test_sin_rutas_de_control_no_hay_guard_ni_token_y_todo_es_publico(armar_proxy, config):
    app = armar_proxy(rutas_control=False)
    assert not any(r.startswith("/guard") for r in _rutas(app))
    assert not {ControlLocal, CORSMiddleware} & set(_middlewares(app))
    assert not config.api_token_path.exists()  # ningún archivo de token
    token = {HEADER_TOKEN: app.state.proxy.api_token}
    async with cliente_de(app) as c:  # por el "puerto local" 8010: igual es público
        guard = await c.get("/guard/state", headers=token)
        anonimo = await c.post(config.rpc_path, json={})
        v1 = await c.get("/v1/compras")
        abierta = await c.get("/v1/openapi.json")
        preflight = await c.options(
            "/v1/compras",
            headers={"Origin": "http://localhost:8020", "Access-Control-Request-Method": "POST"},
        )
    assert guard.status_code == 404
    assert anonimo.status_code == 401  # el A2A anónimo de la demo no existe
    assert v1.status_code == 401 and abierta.status_code == 200
    assert "access-control-allow-origin" not in preflight.headers


async def test_sin_rutas_de_control_la_clave_sigue_entrando(config):
    from proxy.tests.ayudantes import evaluador_espia

    mundo = armar_gateway(config, evaluador_espia()[0], rutas_control=False, demo=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mundo.app), base_url="https://kaiai.example.com",
        headers={"Authorization": f"Bearer {CLAVE_ACME}"},
    ) as c:
        compra = await crear_compra(c)
    assert compra["estado"]


# -- rutas_heredadas=False -----------------------------------------------------------


async def test_sin_rutas_heredadas_no_esta_la_ruta_vieja(armar_proxy, config):
    app = armar_proxy(rutas_heredadas=False)
    rutas = _rutas(app)
    assert config.rpc_path not in rutas and f"{config.rpc_path}/{{resto:path}}" not in rutas
    assert {"/a2a/m/{comercio}", "/a2a/m/{comercio}/{resto:path}"} <= rutas
    async with cliente_de(app) as c:
        vieja = await c.post(config.rpc_path, content=json.dumps({}).encode())
        card = await c.get(f"{config.rpc_path}/.well-known/agent-card.json")
        nueva = await c.get("/a2a/m/merchant_1/.well-known/agent-card.json")
    assert vieja.status_code == 404 and card.status_code == 404
    assert nueva.status_code == 200


# ===========================================================================
# proxy.vercel: crear_app_serverless
# ===========================================================================

RAIZ = Path(__file__).resolve().parents[2]
URL_PUBLICA = "https://kaiai.example.com"
URL_SUPABASE = "https://base.supabase.test"
SECRETO_SUPABASE = "sb_secret_" + "x" * 32  # de prueba: no tiene que salir en ningún log
KID = "kaiai-vercel-1"
_CLAVE_RECIBO = ec.generate_private_key(ec.SECP256R1())
PEM = _CLAVE_RECIBO.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
)
PEM_B64 = base64.b64encode(PEM).decode()
COMERCIOS = {
    "por_defecto": "merchant_1",
    "comercios": [
        {"id": "merchant_1", "a2a_url": f"https://{MERCHANT_HOST}/a2a/merchant_agent",
         "catalogo_url": f"https://{CATALOG_HOST}", "merchant_ids": ["merchant_1"]},
        {"id": "tienda_b", "a2a_url": f"https://{MERCHANT_B_HOST}/a2a/merchant_agent",
         "merchant_ids": ["tienda-b"]},
    ],
}


def entorno(**cambios: str | None) -> dict[str, str]:
    """Un entorno completo de Vercel (de prueba). `None` saca la variable."""
    env = {
        "GATEWAY_PUBLIC_URL": URL_PUBLICA,
        "GATEWAY_CLAVES_JSON": json.dumps(datos_integradores(webhook=False)),
        "GATEWAY_COMERCIOS_JSON": json.dumps(COMERCIOS),
        "GATEWAY_ESTADO": "supabase",
        "SUPABASE_URL": URL_SUPABASE,
        "SUPABASE_SERVICE_ROLE_KEY": SECRETO_SUPABASE,
        "GUARD_SIGNING_KEY_PEM_B64": PEM_B64,
        "GUARD_SIGNING_KID": KID,
        "GUARD_FILTRO": "filtro:filtrar",
        "FILTRO_SOLO_HEURISTICAS": "1",
        "FABRICANTE_FUENTE": "ninguna",
    }
    for nombre, valor in cambios.items():
        if valor is None:
            env.pop(nombre, None)
        else:
            env[nombre] = valor
    return env


def _transporte() -> RuteoPorHost:
    return RuteoPorHost({
        MERCHANT_HOST: FakeMerchant().app,
        CATALOG_HOST: app_catalogo(),
        MERCHANT_B_HOST: FakeMerchantB().app,
    })


def _serverless(env: dict[str, str] | None = None, base: BaseFalsa | None = None) -> Any:
    base = base or BaseFalsa()
    almacen = EstadoSupabase(URL_SUPABASE, SECRETO_SUPABASE, transport=base.transporte())
    return vercel.crear_app_serverless(
        entorno() if env is None else env, almacen=almacen, transport=_transporte()
    )


def _cliente(app: Any, clave: str | None = CLAVE_ACME) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {clave}"} if clave else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=URL_PUBLICA, timeout=20, headers=headers
    )


def _b64url(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode("ascii")


def _checkout(cart: dict[str, Any]) -> dict[str, Any]:
    """El data part `ap2.checkout` con un JWT ES256 de prueba (merchant_1)."""
    centavos = int(round(cart["amount"] * 100))
    iat = int(time.time())
    payload = {
        "id": cart["cart_id"], "merchant": {"id": "merchant_1", "name": "Comercio"},
        "line_items": [{"id": "li_1", "item": {"id": cart["cart_id"], "title": "x",
                                               "price": centavos}, "quantity": 1}],
        "currency": cart["currency"],
        "totals": [{"type": "subtotal", "amount": centavos}, {"type": "total", "amount": centavos}],
        "iat": iat, "exp": iat + 3600,
    }
    h = _b64url(json.dumps({"alg": "ES256", "typ": "JWT", "kid": "k1"}).encode())
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    der = ec.generate_private_key(ec.SECP256R1()).sign(f"{h}.{p}".encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    jwt = f"{h}.{p}.{_b64url(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    return {"cart_id": cart["cart_id"], "checkout_jwt": jwt,
            "checkout_hash": _b64url(hashlib.sha256(jwt.encode()).digest()),
            "amount": cart["amount"], "currency": cart["currency"]}


def _sin_secretos(texto: str) -> None:
    for secreto in (SECRETO_SUPABASE, PEM_B64, PEM.decode().splitlines()[1], SHA_ACME, CLAVE_ACME):
        assert secreto not in texto


# -- la app completa -------------------------------------------------------------------


def test_la_app_completa_con_lo_que_fuerza_el_despliegue():
    app = _serverless()
    assert not isinstance(app, vercel.AppNoConfigurada)
    proxy = app.state.proxy
    config = proxy.config
    assert config.instancias == "varias" and config.webhooks_perfil == "en_linea"
    assert config.events_jsonl is None and config.jsonl_privado is None
    assert config.api_token_path is None
    assert (config.claves_archivo, config.comercios_archivo) == (None, None)
    assert config.ficha_demo is False and config.catalogo_respaldo_fixture is False
    assert config.permitir_stub is False and config.webhooks_inseguros is False
    assert config.guard_activo is True and config.demo_hoy is None
    assert (config.presupuesto_llamada_s, config.espera_contexto_s, config.espera_decision_s) == (
        270.0, 60.0, 150.0
    )
    assert config.max_cuerpo_a2a == 4_000_000 and config.max_respuesta_bytes == 4_000_000
    assert config.max_cuerpo_v1 == 1_048_576  # el default ya entra
    assert proxy.clave_recibo.kid == KID and proxy.clave_recibo.origen == "GUARD_SIGNING_KEY_PEM_B64"
    assert proxy.demo is None and proxy.persistencia is None
    assert proxy.evaluador.nombre == "guard" and proxy.filtro is not None
    assert proxy.filtro_modo == "solo heurísticas"
    assert proxy.agente is None  # sin credenciales del LLM: intención apagada
    assert proxy.almacen.durable is True


def test_solo_a2a_m_v1_y_la_consola():
    rutas = _rutas(_serverless())
    assert not any(r.startswith("/guard") for r in rutas)
    assert "/a2a/merchant_agent" not in rutas
    assert {r for r in rutas if r.startswith("/a2a")} == {
        "/a2a/m/{comercio}", "/a2a/m/{comercio}/{resto:path}"
    }
    # Además, la consola web (estáticos sin clave, ver test_consola.py).
    consola = {r for r in rutas if r == "/" or r == "/consola" or r.startswith("/consola/")}
    assert consola == {
        "/", "/consola", "/consola/", "/consola/app.js", "/consola/logica.mjs",
        "/consola/estilo.css",
    }
    assert all(r == "/v1" or r.startswith(("/a2a/m/", "/v1/")) for r in rutas - consola)


async def test_openapi_public_key_y_las_rutas_que_no_estan():
    app = _serverless()
    async with _cliente(app, clave=None) as anonimo, _cliente(app) as c:
        openapi = await anonimo.get("/v1/openapi.json")
        publica = await anonimo.get("/v1/public-key")
        guard = await c.get("/guard/state")
        vieja = await c.post("/a2a/merchant_agent", json={})
        sin_clave = await anonimo.post("/a2a/m/merchant_1", json={})
        raiz = await c.get("/", follow_redirects=False)
        no_existe = await c.get("/no-existe")
    assert openapi.status_code == 200
    assert openapi.json()["servers"][0]["url"].startswith(URL_PUBLICA)
    assert publica.status_code == 200 and publica.json()["kid"] == KID
    assert publica.json()["jwk"] == ClaveRecibo.desde_entorno(entorno()).jwk_publica()
    assert guard.status_code == 404 and vieja.status_code == 404 and no_existe.status_code == 404
    assert raiz.status_code == 307 and raiz.headers["location"] == "/consola"
    assert sin_clave.status_code == 401


async def test_v1_evaluar_firma_con_la_clave_del_entorno():
    app = _serverless()
    cart = {"cart_id": "cart_1", "item_label": "Monitor", "amount": 120.0, "currency": "USD"}
    async with _cliente(app) as c:
        jwk = (await c.get("/v1/public-key")).json()["jwk"]
        r = await c.post("/v1/evaluar", json={
            "comercio": "merchant_1",
            "ficha": {"pedido": "un monitor", "requisitos": [
                {"campo": "precio", "operador": "maximo", "valor": 150, "obligatorio": True},
                {"campo": "moneda", "operador": "igual", "valor": "USD", "obligatorio": True},
            ]},
            "ofertas": {"carts": [cart], "offer_details": []},
            "checkout": _checkout(cart),
        })
    assert r.status_code == 200, r.text
    recibo = r.json()["recibo"]
    assert recibo["kid"] == KID and jws_valido(recibo["jws"], jwk)


async def test_compra_busqueda_y_checkout_por_a2a_m(tmp_path):
    base = BaseFalsa()
    env = entorno(
        GUARD_KEY_PATH=str(tmp_path / "k.pem"), GUARD_API_TOKEN_PATH=str(tmp_path / "token"),
        GUARD_EVENTS_JSONL=str(tmp_path / "e.jsonl"),
    )
    app = _serverless(env, base)
    async with _cliente(app) as c:
        compra = await crear_compra(c)
        busqueda = await buscar_compra(c, compra["compra_id"], escenario="baseline")
        assert busqueda.status_code == 200, busqueda.text
        resultado = busqueda.json()["result"]
        assert resultado["status"]["state"] == "completed", resultado["status"]
        respuesta = await checkout_compra(c, compra["compra_id"], resultado["contextId"], "cart_1")
        assert respuesta.status_code == 200, respuesta.text
        vista = (await c.get(f"/v1/compras/{compra['compra_id']}")).json()
        jwk = (await c.get("/v1/public-key")).json()["jwk"]
    (veredicto,) = vista["veredictos"]
    assert veredicto["recibo"]["kid"] == KID and jws_valido(veredicto["recibo"]["jws"], jwk)
    # El estado quedó en la base (otra instancia lo lee de ahí).
    (fila,) = base.filas("gw_compras")
    assert fila["id"] == compra["compra_id"]
    assert base.filas("gw_contextos")
    # Nada en disco: ni la clave, ni el token, ni el JSONL.
    assert list(tmp_path.iterdir()) == []


async def test_dos_instancias_sobre_la_misma_base():
    """La búsqueda en A y el checkout en B: B lee las ofertas de la base."""
    base = BaseFalsa()
    a, b = _serverless(base=base), _serverless(base=base)
    async with _cliente(a) as ca, _cliente(b) as cb:
        compra = await crear_compra(ca)
        busqueda = (await buscar_compra(ca, compra["compra_id"], escenario="baseline")).json()
        respuesta = await checkout_compra(
            cb, compra["compra_id"], busqueda["result"]["contextId"], "cart_1"
        )
        assert respuesta.status_code == 200, respuesta.text
        vista = (await ca.get(f"/v1/compras/{compra['compra_id']}")).json()
    assert len(vista["veredictos"]) == 1


# -- 503 motor_no_configurado -----------------------------------------------------------


async def _responde_503(app: Any) -> None:
    assert isinstance(app, vercel.AppNoConfigurada)
    async with _cliente(app) as c:
        for metodo, ruta in (("GET", "/v1/openapi.json"), ("GET", "/v1/public-key"),
                             ("POST", "/a2a/m/merchant_1"), ("GET", "/"), ("POST", "/v1/compras"),
                             ("GET", "/guard/state")):
            r = await c.request(metodo, ruta)
            assert r.status_code == 503, (metodo, ruta)
            assert r.json() == {"error": "motor_no_configurado"}


@pytest.mark.parametrize(
    "variable",
    ["GATEWAY_PUBLIC_URL", "GATEWAY_CLAVES_JSON", "GATEWAY_COMERCIOS_JSON", "GATEWAY_ESTADO",
     "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "GUARD_SIGNING_KEY_PEM_B64", "GUARD_FILTRO"],
)
async def test_sin_una_obligatoria_responde_503(variable, caplog):
    with caplog.at_level("ERROR", logger="proxy.vercel"):
        app = _serverless(entorno(**{variable: None}))
    await _responde_503(app)
    assert "El motor no arranca" in caplog.text
    _sin_secretos(caplog.text)


@pytest.mark.parametrize("variable", PROHIBIDAS)
@pytest.mark.parametrize("valor", ["1", "true", "quizás"])
async def test_una_prohibida_prendida_responde_503(variable, valor, caplog):
    with caplog.at_level("ERROR", logger="proxy.vercel"):
        app = _serverless(entorno(**{variable: valor}))
    await _responde_503(app)
    assert variable in caplog.text


@pytest.mark.parametrize("variable", PROHIBIDAS)
def test_una_prohibida_en_cero_no_cuenta(variable):
    assert not isinstance(_serverless(entorno(**{variable: "0"})), vercel.AppNoConfigurada)


@pytest.mark.parametrize(
    "cambios, motivo",
    [
        ({"GATEWAY_PUBLIC_URL": "http://kaiai.example.com"}, "GATEWAY_PUBLIC_URL"),
        ({"GATEWAY_PUBLIC_URL": "https://u:p@kaiai.example.com"}, "GATEWAY_PUBLIC_URL"),
        ({"SUPABASE_URL": "http://base.supabase.test"}, "SUPABASE_URL"),
        ({"GATEWAY_ESTADO": "memoria"}, "GATEWAY_ESTADO"),
        ({"GATEWAY_ESTADO": "redis"}, "GATEWAY_ESTADO"),
        ({"GATEWAY_CLAVES_JSON": json.dumps({"integradores": []})}, "clave activa"),
        ({"GATEWAY_CLAVES_JSON": ""}, "GATEWAY_CLAVES_JSON"),
        ({"GATEWAY_COMERCIOS_JSON": json.dumps({**COMERCIOS, "comercios": [
            {**COMERCIOS["comercios"][0], "a2a_url": "http://merchant.test/a2a/merchant_agent"},
            COMERCIOS["comercios"][1]]})}, "https"),
        ({"GUARD_FILTRO": "os:system"}, "GUARD_FILTRO"),
        ({"GUARD_FILTRO": "filtro.casos:filtrar"}, "GUARD_FILTRO"),
        ({"GUARD_FILTRO": "proxy.tests.ayudantes:buscar"}, "GUARD_FILTRO"),
        ({"GUARD_FILTRO": "world.ap2:x"}, "GUARD_FILTRO"),
        ({"GUARD_FILTRO": "filtro:no_existe"}, "GUARD_FILTRO"),
        ({"GUARD_SIGNING_KEY_PEM": PEM.decode()}, "una sola"),
        ({"GUARD_SIGNING_KID": "con espacio"}, "GUARD_SIGNING_KID"),
        ({"PROXY_PRESUPUESTO_LLAMADA_S": "300"}, "PROXY_PRESUPUESTO_LLAMADA_S"),
        ({"PROXY_ESPERA_CONTEXTO_S": "61"}, "PROXY_ESPERA_CONTEXTO_S"),
        ({"PROXY_ESPERA_DECISION_S": "400"}, "PROXY_ESPERA_DECISION_S"),
        ({"GATEWAY_MAX_CUERPO_A2A_BYTES": "5000000"}, "GATEWAY_MAX_CUERPO_A2A_BYTES"),
        ({"PROXY_MAX_RESPUESTA_BYTES": "4500000"}, "PROXY_MAX_RESPUESTA_BYTES"),
        ({"GATEWAY_BARRERA_S": "20"}, "supera los 300"),
        ({"INTENCION_PROVEEDOR": "otro"}, "INTENCION_PROVEEDOR"),
        ({"FABRICANTE_FUENTE": "icecat"}, None),
    ],
)
async def test_lo_que_no_sirve_responde_503(cambios, motivo, caplog):
    with caplog.at_level("ERROR", logger="proxy.vercel"):
        app = _serverless(entorno(**cambios))
    await _responde_503(app)
    if motivo:
        assert motivo in caplog.text
    _sin_secretos(caplog.text)


def test_la_clave_de_supabase_con_el_otro_nombre_sirve():
    env = entorno(SUPABASE_SERVICE_ROLE_KEY=None, SUPABASE_SECRET_KEY=SECRETO_SUPABASE)
    assert not isinstance(_serverless(env), vercel.AppNoConfigurada)


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8040", "http://localhost:8040", "http://[::1]:8040"]
)
def test_la_url_publica_por_http_solo_en_loopback(url):
    """La prueba local con `local_multi.py`."""
    assert not isinstance(_serverless(entorno(GATEWAY_PUBLIC_URL=url)), vercel.AppNoConfigurada)
    env = entorno(SUPABASE_URL="http://127.0.0.1:54321")
    assert not isinstance(_serverless(env), vercel.AppNoConfigurada)


def test_los_mensajes_no_traen_secretos(caplog):
    with caplog.at_level("ERROR", logger="proxy.vercel"):
        malos = [
            _serverless(entorno(GATEWAY_CLAVES_JSON='{"integradores": [' + SECRETO_SUPABASE)),
            _serverless(entorno(GUARD_SIGNING_KEY_PEM_B64=PEM.decode())),  # el PEM, no su base64
            _serverless(entorno(GATEWAY_COMERCIOS_JSON=json.dumps({**COMERCIOS, "comercios": [
                {**COMERCIOS["comercios"][0],
                 "a2a_url": f"https://u:{SECRETO_SUPABASE}@merchant.test/a2a"}]}))),
        ]
    assert all(isinstance(app, vercel.AppNoConfigurada) for app in malos)
    assert caplog.text.count("El motor no arranca") == 3
    _sin_secretos(caplog.text)
    for app in malos:
        _sin_secretos(app.motivo)


def test_el_evaluador_tiene_que_ser_el_real(monkeypatch):
    def sin_guard(permitir_stub: bool = False):
        raise EvaluadorNoDisponible("guard.evaluar() no está disponible")

    monkeypatch.setattr(vercel, "cargar_evaluador", sin_guard)
    assert isinstance(_serverless(), vercel.AppNoConfigurada)
    monkeypatch.setattr(
        vercel, "cargar_evaluador",
        lambda permitir_stub=False: EvaluadorCargado(evaluar=guard_stub.evaluar, nombre="stub"),
    )
    assert isinstance(_serverless(), vercel.AppNoConfigurada)


def test_un_almacen_que_no_es_durable_no_sirve():
    app = vercel.crear_app_serverless(entorno(), almacen=EstadoMemoria(), transport=_transporte())
    assert isinstance(app, vercel.AppNoConfigurada)


def test_un_error_inesperado_tambien_es_503_sin_el_texto(monkeypatch, caplog):
    def explota(*a: Any, **k: Any) -> Any:
        raise RuntimeError(f"algo con {SECRETO_SUPABASE}")

    monkeypatch.setattr(vercel, "create_app", explota)
    with caplog.at_level("ERROR", logger="proxy.vercel"):
        app = _serverless()
    assert isinstance(app, vercel.AppNoConfigurada)
    assert "RuntimeError" in caplog.text
    _sin_secretos(caplog.text)


async def test_la_app_minima_maneja_el_lifespan():
    app = vercel.AppNoConfigurada("x")
    mensajes = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    enviados: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return mensajes.pop(0)

    async def send(mensaje: dict[str, Any]) -> None:
        enviados.append(mensaje)

    await app({"type": "lifespan"}, receive, send)
    assert [m["type"] for m in enviados] == [
        "lifespan.startup.complete", "lifespan.shutdown.complete"
    ]
    assert "x" not in repr(app)


# -- los tiempos ------------------------------------------------------------------------


def test_sin_variables_los_topes_de_hobby():
    config = _serverless().state.proxy.config
    assert config.presupuesto_llamada_s + config.barrera_s + vercel.MARGEN_S <= 300
    assert (config.presupuesto_llamada_s, config.espera_contexto_s, config.espera_decision_s) == (
        vercel.PRESUPUESTO_LLAMADA_S, vercel.ESPERA_CONTEXTO_S, vercel.ESPERA_DECISION_S
    )


def test_los_tiempos_se_pueden_bajar():
    config = _serverless(entorno(
        PROXY_PRESUPUESTO_LLAMADA_S="200", PROXY_ESPERA_CONTEXTO_S="30",
        PROXY_ESPERA_DECISION_S="90", GATEWAY_BARRERA_S="5", GATEWAY_MAX_CUERPO_A2A_BYTES="2000000",
    )).state.proxy.config
    assert (config.presupuesto_llamada_s, config.espera_contexto_s, config.espera_decision_s) == (
        200.0, 30.0, 90.0
    )
    assert config.barrera_s == 5.0 and config.max_cuerpo_a2a == 2_000_000


def test_los_topes_exactos_valen():
    config = _serverless(entorno(
        PROXY_PRESUPUESTO_LLAMADA_S="270", PROXY_ESPERA_CONTEXTO_S="60",
        PROXY_ESPERA_DECISION_S="150", GATEWAY_BARRERA_S="15",
    )).state.proxy.config
    assert config.presupuesto_llamada_s + config.barrera_s + vercel.MARGEN_S == 300


def test_el_despliegue_fuerza_el_latido():
    """Sin bytes, Vercel corta a los 60 s: un pedido retenido late."""
    assert vercel.LATIDO_S == 15.0 and vercel.MAX_LATIDO_S == 30.0
    assert _serverless().state.proxy.config.latido_s == vercel.LATIDO_S


@pytest.mark.parametrize(("valor", "esperado"), [("5", 5.0), ("0.5", 0.5), ("30", 30.0)])
def test_el_latido_se_puede_bajar_hasta_el_tope(valor, esperado):
    config = _serverless(entorno(GATEWAY_LATIDO_S=valor)).state.proxy.config
    assert config.latido_s == esperado


@pytest.mark.parametrize("valor", ["0", "off", "no", "31", "61", "-1", "nan", "inf", "quince"])
async def test_el_latido_no_se_apaga_ni_pasa_del_tope(valor, caplog):
    with caplog.at_level("ERROR", logger="proxy.vercel"):
        app = _serverless(entorno(GATEWAY_LATIDO_S=valor))
    await _responde_503(app)
    assert "GATEWAY_LATIDO_S" in caplog.text


# -- la frontera ------------------------------------------------------------------------


def _motor_pyproject() -> dict[str, list[str]]:
    return tomllib.loads((RAIZ / "pyproject.toml").read_text("utf-8"))["tool"]["kaiai"]["motor"]


def test_la_frontera_escrita_en_vercel_py_es_la_del_pyproject():
    from proxy.tests.test_frontera_motor import excluido, modulo_de

    motor = _motor_pyproject()
    assert vercel.PAQUETES_MOTOR == tuple(motor["paquetes"])
    for paquete in motor["paquetes"]:
        for archivo in (RAIZ / paquete).rglob("*.py"):
            ruta = archivo.relative_to(RAIZ).as_posix()
            if "__pycache__" in ruta:
                continue
            modulo = modulo_de(ruta)
            esperado = not excluido(ruta, motor["excluidos"]) and modulo not in (
                "proxy.__main__", "proxy.vercel"
            )
            assert vercel.modulo_del_motor(modulo) is esperado, ruta
    assert not vercel.modulo_del_motor("os") and not vercel.modulo_del_motor("filtro..x")


def test_pyproject_fija_la_entrada_de_vercel():
    datos = tomllib.loads((RAIZ / "pyproject.toml").read_text("utf-8"))
    assert datos["tool"]["vercel"]["entrypoint"] == "index:app"
    assert "index.py" in _motor_pyproject()["entradas"]


def test_index_y_vercel_py_no_importan_el_servidor_local_ni_la_demo():
    from proxy.tests.test_frontera_motor import importaciones

    for ruta in ("index.py", "proxy/vercel.py", "proxy/arranque.py"):
        modulos = {m for _, m in importaciones((RAIZ / ruta).read_text("utf-8"), ruta)}
        assert not any(
            m == p or m.startswith(p + ".")
            for m in modulos
            for p in ("uvicorn", "proxy.__main__", "proxy.demo_local")
        ), ruta
    arbol = ast.parse((RAIZ / "index.py").read_text("utf-8"))
    assert [ast.unparse(n) for n in arbol.body if isinstance(n, (ast.Import, ast.ImportFrom))] == [
        "from proxy.vercel import app"
    ]


# -- en un subproceso: index sin uvicorn ni la simulación, sin red ni disco ---------------

_SCRIPT = r"""
import builtins, importlib, importlib.abc, io, json, os, socket, sys
params = json.loads(sys.argv[1])
sys.path.insert(0, params["raiz"])
BLOQUEADOS = tuple(params["bloqueados"])
intentos, red, escrituras = [], [], []


def bloqueado(nombre):
    return any(nombre == b or nombre.startswith(b + ".") for b in BLOQUEADOS)


class Bloqueo(importlib.abc.MetaPathFinder):
    def find_spec(self, nombre, path=None, target=None):
        if bloqueado(nombre):
            intentos.append(nombre)
            raise ModuleNotFoundError(f"no instalado en la prueba: {nombre}", name=nombre)
        return None


sys.meta_path.insert(0, Bloqueo())

LOCALES = {"127.0.0.1", "::1", "localhost", None}
_getaddrinfo, _connect = socket.getaddrinfo, socket.socket.connect


def getaddrinfo(host, *a, **k):
    if host not in LOCALES:
        red.append(str(host))
        raise socket.gaierror("sin red en la prueba")
    return _getaddrinfo(host, *a, **k)


def connect(self, direccion):
    if isinstance(direccion, tuple) and direccion[0] not in LOCALES:
        red.append(str(direccion[0]))
        raise OSError("sin red en la prueba")
    return _connect(self, direccion)


socket.getaddrinfo = getaddrinfo
socket.socket.connect = connect
_open, _os_open, _mkdir = builtins.open, os.open, os.mkdir


def abrir(archivo, modo="r", *a, **k):
    if any(c in modo for c in "wax+"):
        escrituras.append(str(archivo))
    return _open(archivo, modo, *a, **k)


def os_abrir(ruta, flags, *a, **k):
    if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
        escrituras.append(str(ruta))
    return _os_open(ruta, flags, *a, **k)


def crear_carpeta(ruta, *a, **k):
    escrituras.append(str(ruta))
    return _mkdir(ruta, *a, **k)


builtins.open = io.open = abrir
os.open = os_abrir
os.mkdir = crear_carpeta

# Controles: los detectores andan (si no, la prueba no prueba nada).
with open(os.devnull, "w") as nulo:
    nulo.write("")
try:
    socket.getaddrinfo("control.invalid", 443)
except socket.gaierror:
    pass
controles = [list(escrituras), list(red)]
escrituras.clear()
red.clear()

import index  # arma la app con os.environ (el entorno de la prueba)

resultado = {
    "controles": controles,
    "tipo": type(index.app).__name__,
    "escrituras_al_armar": list(escrituras),
    "red_al_armar": list(red),
}
from starlette.testclient import TestClient

with TestClient(index.app, base_url=params["url"]) as c:
    resultado["openapi"] = c.get("/v1/openapi.json").status_code
    publica = c.get("/v1/public-key")
    resultado["public_key"] = [publica.status_code, publica.json().get("kid")]
    resultado["guard"] = c.get("/guard/state").status_code
    resultado["compras_sin_clave"] = c.get("/v1/compras").status_code
resultado["intentos"] = sorted(set(intentos))
resultado["en_sys_modules"] = sorted(m for m in sys.modules if bloqueado(m))
resultado["red"] = sorted(set(red))
resultado["escrituras"] = sorted(set(escrituras))
print(json.dumps(resultado))
"""

# Lo mínimo del sistema para que Python y los sockets anden (en Windows, sin
# SYSTEMROOT no hay sockets). El resto del entorno es el de la prueba.
_ESENCIALES = {"PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "TMPDIR", "HOME",
               "USERPROFILE", "LANG", "LC_ALL"}


def _correr_index(env: dict[str, str]) -> dict[str, Any]:
    prohibidos = _motor_pyproject()["prohibidos"]
    params = {"raiz": str(RAIZ), "bloqueados": ["uvicorn", *prohibidos], "url": URL_PUBLICA}
    esenciales = {k: v for k, v in os.environ.items() if k.upper() in _ESENCIALES}
    resultado = subprocess.run(
        [sys.executable, "-I", "-B", "-X", "utf8", "-c", _SCRIPT, json.dumps(params)],
        cwd=RAIZ, capture_output=True, text=True, encoding="utf-8", timeout=180,
        env={**esenciales, **env},
    )
    assert resultado.returncode == 0, resultado.stderr[-4000:]
    _sin_secretos(resultado.stderr)
    return json.loads(resultado.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def index_completo() -> dict[str, Any]:
    return _correr_index(entorno())


def test_index_arma_la_app_sin_uvicorn_ni_la_simulacion(index_completo):
    assert index_completo["tipo"] == "Starlette"
    assert index_completo["intentos"] == [] and index_completo["en_sys_modules"] == []


def test_index_no_hace_red_ni_escribe_al_armar(index_completo):
    escritura, red = index_completo["controles"]
    assert len(escritura) == 1 and red == ["control.invalid"]  # los detectores andan
    assert index_completo["red_al_armar"] == [] and index_completo["escrituras_al_armar"] == []
    # Ni al atender lo que no toca la base (openapi, la clave pública).
    assert index_completo["red"] == [] and index_completo["escrituras"] == []


def test_index_atiende_v1(index_completo):
    assert index_completo["openapi"] == 200
    assert index_completo["public_key"] == [200, KID]
    assert index_completo["guard"] == 404 and index_completo["compras_sin_clave"] == 401


def test_index_sin_configurar_responde_503():
    resultado = _correr_index(entorno(GUARD_SIGNING_KEY_PEM_B64=None))
    assert resultado["tipo"] == "AppNoConfigurada"
    assert resultado["openapi"] == 503 and resultado["public_key"][0] == 503
    assert resultado["intentos"] == [] and resultado["red"] == [] and resultado["escrituras"] == []


# -- la prueba local documentada (deploy/vercel) ------------------------------------------


def _script_deploy(nombre: str) -> Any:
    import importlib.util

    ruta = RAIZ / "deploy" / "vercel" / f"{nombre}.py"
    spec = importlib.util.spec_from_file_location(f"_d2_deploy_{nombre}", ruta)
    assert spec and spec.loader
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modulo  # dataclasses lo busca ahí
    spec.loader.exec_module(modulo)
    return modulo


def test_la_prueba_local_sin_supabase_arma_el_motor(tmp_path, capsys):
    """`generar_secretos.py` + `local_sin_supabase.env.ejemplo` (lo que dice
    el README) pasan todos los chequeos de `crear_app_serverless`."""
    assert _script_deploy("generar_secretos").main(["--carpeta", str(tmp_path)]) == 0
    capsys.readouterr()
    leer_env = _script_deploy("local_multi").leer_env
    env = {
        **leer_env(tmp_path / "vercel.env"),
        **leer_env(RAIZ / "deploy" / "vercel" / "local_sin_supabase.env.ejemplo"),
    }
    assert env["SUPABASE_URL"] == "http://127.0.0.1:54321"
    app = vercel.crear_app_serverless(
        env, almacen=EstadoSupabase(env["SUPABASE_URL"], "x", transport=BaseFalsa().transporte())
    )
    assert not isinstance(app, vercel.AppNoConfigurada), getattr(app, "motivo", "")
    assert app.state.proxy.config.presupuesto_llamada_s == vercel.PRESUPUESTO_LLAMADA_S
    _sin_secretos(capsys.readouterr().out)
