"""WP1: claves de API de los integradores y middleware de autenticación.

Cubre el formato `psg_<kid>_<secreto>` (con `_` dentro del secreto), que solo
se guarde el sha256, los rechazos (kid inexistente, clave inactiva, hash
distinto, `Bearer` ajeno), la validación del archivo de claves y las reglas
por ruta y oyente del middleware sobre una app Starlette mínima.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from proxy import claves
from proxy.claves import (
    AutenticacionGateway,
    RegistroIntegradores,
    extraer_bearer,
    generar_clave,
    hash_clave,
    oyente_de,
    parsear_kid,
)
from proxy.tipos_gateway import ConfigInvalida, Integrador

pytestmark = pytest.mark.anyio

PUERTO_PUBLICO = 8030
SECRETO_WEBHOOK = "whsec_" + "x" * 40


def _datos(*claves_acme: dict[str, Any], **extra_acme: Any) -> dict[str, Any]:
    return {
        "integradores": [
            {
                "id": "acme",
                "nombre": "ACME Compras",
                "claves": list(claves_acme),
                "comercios": ["merchant_1", "tienda_b"],
                **extra_acme,
            }
        ]
    }


def _entrada(kid: str, sha: str, activa: bool = True) -> dict[str, Any]:
    return {"kid": kid, "sha256": sha, "creada": "2026-10-18", "activa": activa}


@pytest.fixture
def clave_y_registro() -> tuple[str, RegistroIntegradores]:
    clave, kid, sha = generar_clave()
    return clave, RegistroIntegradores.desde_dict(_datos(_entrada(kid, sha)), {})


# -- formato y registro -------------------------------------------------------
def test_ida_y_vuelta_generar_hash_autenticar(clave_y_registro):
    clave, registro = clave_y_registro
    kid = parsear_kid(clave)
    assert clave.startswith("psg_") and kid is not None and len(kid) == 12
    assert hash_clave(clave) == registro._claves[kid].sha256
    integrador = registro.autenticar(clave)
    assert isinstance(integrador, Integrador)
    assert integrador.id == "acme"
    assert integrador.comercios == ("merchant_1", "tienda_b")
    assert registro.hay_claves() is True


def test_generar_clave_da_kid_y_hash_coherentes():
    clave, kid, sha = generar_clave()
    assert clave.startswith(f"psg_{kid}_")
    assert sha == hash_clave(clave) and len(sha) == 64
    assert clave not in sha


def test_secreto_con_guion_bajo_se_parsea_por_el_primer_separador():
    kid = "a1b2c3d4e5f6"
    clave = f"psg_{kid}_abc_def_ghi_jklmnopqrstuv"
    assert parsear_kid(clave) == kid
    registro = RegistroIntegradores.desde_dict(_datos(_entrada(kid, hash_clave(clave))), {})
    assert registro.autenticar(clave).id == "acme"


@pytest.mark.parametrize(
    "clave",
    [
        "",
        "psg_",
        "psg_a1b2c3d4e5f6",                    # sin secreto
        "psg_a1b2c3d4e5f6_",                   # secreto vacío
        "psg_A1B2C3D4E5F6_" + "s" * 30,        # kid en mayúscula
        "psg_a1b2c3d4e5_" + "s" * 30,          # kid corto
        "psx_a1b2c3d4e5f6_" + "s" * 30,        # otro prefijo
        "psg_a1b2c3d4e5f6_" + "s" * 30 + " ",  # espacio en el secreto
        "psg_a1b2c3d4e5f6_" + "s" * 300,       # secreto enorme
    ],
)
def test_parsear_kid_rechaza_formatos_invalidos(clave):
    assert parsear_kid(clave) is None


def test_kid_inexistente_no_autentica(clave_y_registro):
    _, registro = clave_y_registro
    otra, _, _ = generar_clave()
    assert registro.autenticar(otra) is None


def test_hash_distinto_no_autentica(clave_y_registro):
    clave, registro = clave_y_registro
    kid = parsear_kid(clave)
    falsa = f"psg_{kid}_" + "Z" * 43  # kid real, secreto inventado
    assert registro.autenticar(falsa) is None


def test_clave_inactiva_no_autentica_ni_cuenta_como_clave():
    clave, kid, sha = generar_clave()
    registro = RegistroIntegradores.desde_dict(_datos(_entrada(kid, sha, activa=False)), {})
    assert registro.autenticar(clave) is None
    assert registro.hay_claves() is False


def test_autenticar_none_o_basura(clave_y_registro):
    _, registro = clave_y_registro
    for valor in (None, "", "hola", "Bearer psg_x"):
        assert registro.autenticar(valor) is None


def test_registro_vacio():
    registro = RegistroIntegradores.vacio()
    assert registro.hay_claves() is False
    assert registro.todos() == []
    assert registro.autenticar(generar_clave()[0]) is None


def test_get_y_todos(clave_y_registro):
    _, registro = clave_y_registro
    assert registro.get("acme").nombre == "ACME Compras"
    assert registro.get("otro") is None
    assert [i.id for i in registro.todos()] == ["acme"]


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        (None, None),
        ("", None),
        ("Basic dXNlcjpwYXNz", None),
        ("Bearer abc.def", None),              # Bearer ajeno (no es nuestro)
        ("Bearer psg_abc", "psg_abc"),         # nuestro aunque esté mal formado
        ("bearer   psg_abc  ", "psg_abc"),     # el esquema no distingue mayúsculas
        ("Token psg_abc", None),
    ],
)
def test_extraer_bearer(valor, esperado):
    assert extraer_bearer(valor) == esperado


def test_webhook_carga_el_secreto_del_entorno():
    clave, kid, sha = generar_clave()
    datos = _datos(
        _entrada(kid, sha),
        webhook={"url": "https://acme.example/hooks", "secreto_env": "WEBHOOK_SECRETO_ACME"},
        max_compras_activas=5,
    )
    registro = RegistroIntegradores.desde_dict(datos, {"WEBHOOK_SECRETO_ACME": SECRETO_WEBHOOK})
    integrador = registro.autenticar(clave)
    assert integrador.webhook.url == "https://acme.example/hooks"
    assert integrador.webhook.secreto == SECRETO_WEBHOOK.encode()
    assert integrador.max_compras_activas == 5
    assert SECRETO_WEBHOOK not in repr(integrador)


def test_defaults_del_integrador():
    datos = {"integradores": [{"id": "solo", "claves": []}]}
    integrador = RegistroIntegradores.desde_dict(datos, {}).get("solo")
    assert integrador == Integrador(id="solo", nombre="solo")


# -- archivo inválido ---------------------------------------------------------
def _invalidos() -> list[tuple[str, dict[str, Any], dict[str, str]]]:
    kid, sha = "a1b2c3d4e5f6", "0" * 64
    ok = _entrada(kid, sha)
    webhook = {"url": "https://acme.example/h", "secreto_env": "WH"}
    return [
        ("no_es_objeto", [], {}),
        ("sin_integradores", {}, {}),
        ("clave_desconocida_raiz", {"integradores": [], "extra": 1}, {}),
        ("kid_duplicado_mismo", _datos(ok, _entrada(kid, "1" * 64)), {}),
        ("kid_duplicado_entre_integradores",
         {"integradores": [{"id": "a", "claves": [ok]}, {"id": "b", "claves": [ok]}]}, {}),
        ("id_invalido", {"integradores": [{"id": "ACME", "claves": []}]}, {}),
        ("id_vacio", {"integradores": [{"id": "", "claves": []}]}, {}),
        ("id_repetido", {"integradores": [{"id": "a"}, {"id": "a"}]}, {}),
        ("kid_invalido", _datos(_entrada("XYZ", sha)), {}),
        ("sha_invalido", _datos(_entrada(kid, "abc")), {}),
        ("activa_no_bool", _datos(_entrada(kid, sha) | {"activa": "si"}), {}),
        ("clave_desconocida_en_clave", _datos(ok | {"secreto": "x"}), {}),
        ("clave_desconocida_en_integrador", _datos(ok, activo=True), {}),
        ("comercio_invalido", _datos(ok) | {"integradores": [{"id": "a", "comercios": ["Tienda B"]}]}, {}),
        ("max_no_entero", _datos(ok, max_compras_activas="10"), {}),
        ("max_cero", _datos(ok, max_compras_activas=0), {}),
        ("max_bool", _datos(ok, max_compras_activas=True), {}),
        ("falta_secreto_env", _datos(ok, webhook={"url": "https://acme.example/h"}), {}),
        ("secreto_env_ausente_del_entorno", _datos(ok, webhook=webhook), {}),
        ("secreto_corto", _datos(ok, webhook=webhook), {"WH": "x" * 31}),
        ("webhook_clave_desconocida", _datos(ok, webhook=webhook | {"secreto": "x"}), {"WH": "x" * 32}),
        ("webhook_sin_url", _datos(ok, webhook={"secreto_env": "WH"}), {"WH": "x" * 32}),
    ]


@pytest.mark.parametrize(("caso", "datos", "env"), _invalidos(), ids=[c[0] for c in _invalidos()])
def test_archivo_invalido_da_config_invalida(caso, datos, env):
    with pytest.raises(ConfigInvalida):
        RegistroIntegradores.desde_dict(datos, env)


def test_secreto_corto_no_aparece_en_el_error():
    secreto = "corto-y-secreto"
    datos = _datos(webhook={"url": "https://a.example/h", "secreto_env": "WH"})
    with pytest.raises(ConfigInvalida) as e:
        RegistroIntegradores.desde_dict(datos, {"WH": secreto})
    assert secreto not in str(e.value) and "WH" in str(e.value)


def test_desde_archivo(tmp_path, caplog):
    clave, kid, sha = generar_clave()
    path = tmp_path / "claves.json"
    path.write_text(json.dumps(_datos(_entrada(kid, sha))), encoding="utf-8")
    os.chmod(path, 0o600)
    with caplog.at_level("WARNING", logger="proxy.claves"):
        registro = RegistroIntegradores.desde_archivo(path, {})
    assert registro.autenticar(clave).id == "acme"
    assert not caplog.records


@pytest.mark.skipif(os.name != "posix", reason="permisos POSIX")
def test_desde_archivo_legible_por_otros_avisa(tmp_path, caplog):
    path = tmp_path / "claves.json"
    path.write_text(json.dumps({"integradores": []}), encoding="utf-8")
    os.chmod(path, 0o644)
    with caplog.at_level("WARNING", logger="proxy.claves"):
        RegistroIntegradores.desde_archivo(path, {})
    assert any("chmod 600" in r.getMessage() for r in caplog.records)


def test_desde_archivo_inexistente_o_no_json(tmp_path):
    with pytest.raises(ConfigInvalida):
        RegistroIntegradores.desde_archivo(tmp_path / "no-existe.json", {})
    roto = tmp_path / "roto.json"
    roto.write_text("{no es json", encoding="utf-8")
    with pytest.raises(ConfigInvalida):
        RegistroIntegradores.desde_archivo(roto, {})


# -- oyente -------------------------------------------------------------------
def test_oyente_de():
    publicos = frozenset({PUERTO_PUBLICO})
    assert oyente_de({"server": ("gw.test", PUERTO_PUBLICO)}, publicos) == "publico"
    assert oyente_de({"server": ("127.0.0.1", 8010)}, publicos) == "local"
    assert oyente_de({"server": None}, publicos) == "local"
    assert oyente_de({}, publicos) == "local"
    assert oyente_de({"server": ("sock", None)}, publicos) == "local"
    assert oyente_de({"server": ("gw.test", PUERTO_PUBLICO)}, frozenset()) == "local"


# -- middleware ---------------------------------------------------------------
async def _eco(request: Request) -> JSONResponse:
    integrador = request.state.integrador
    return JSONResponse({
        "oyente": request.state.oyente,
        "integrador": integrador.id if integrador else None,
        "authorization": request.headers.get("authorization"),
    })


def _app(registro: RegistroIntegradores, puertos: set[int] | None = None) -> Starlette:
    rutas = [
        Route("/guard/{x:path}", _eco, methods=["GET", "POST"]),
        Route("/a2a/{x:path}", _eco, methods=["GET", "POST"]),
        Route("/v1/{x:path}", _eco, methods=["GET", "POST"]),
        Route("/otra", _eco),
    ]
    app = Starlette(routes=rutas)
    return AutenticacionGateway(
        app, registro=registro, puertos_publicos=puertos if puertos is not None else {PUERTO_PUBLICO}
    )


def _cliente(app, base: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base)


PUBLICO = f"http://gw.test:{PUERTO_PUBLICO}"
LOCAL = "http://localhost:8010"


async def test_publico_guard_da_404(clave_y_registro):
    clave, registro = clave_y_registro
    async with _cliente(_app(registro), PUBLICO) as c:
        for path in ("/guard/x", "/guard/state", "/guard"):
            resp = await c.get(path, headers={"Authorization": f"Bearer {clave}"})
            assert resp.status_code == 404, path
            assert resp.json()["error"]["codigo"] == "no_encontrado"


async def test_publico_a2a_sin_clave_da_401_con_www_authenticate(clave_y_registro):
    _, registro = clave_y_registro
    async with _cliente(_app(registro), PUBLICO) as c:
        for headers in ({}, {"Authorization": "Basic dXNlcjpwYXNz"}, {"Authorization": "Bearer otro"}):
            resp = await c.post("/a2a/m/merchant_1", json={}, headers=headers)
            assert resp.status_code == 401, headers
            assert resp.headers["www-authenticate"].startswith("Bearer")
            assert resp.json()["error"]["codigo"] == "no_autenticado"


async def test_publico_a2a_con_clave_invalida_da_401(clave_y_registro):
    clave, registro = clave_y_registro
    async with _cliente(_app(registro), PUBLICO) as c:
        resp = await c.get("/a2a/m/merchant_1/.well-known/agent-card.json",
                           headers={"Authorization": f"Bearer {clave}x"})
    assert resp.status_code == 401
    assert resp.json()["error"]["codigo"] == "clave_invalida"
    assert "www-authenticate" in resp.headers


async def test_publico_a2a_con_clave_pasa(clave_y_registro):
    clave, registro = clave_y_registro
    async with _cliente(_app(registro), PUBLICO) as c:
        resp = await c.post("/a2a/m/merchant_1", json={}, headers={"Authorization": f"Bearer {clave}"})
    assert resp.status_code == 200
    assert resp.json()["integrador"] == "acme" and resp.json()["oyente"] == "publico"


async def test_publico_rutas_v1_sin_auth_pasan(clave_y_registro):
    _, registro = clave_y_registro
    async with _cliente(_app(registro), PUBLICO) as c:
        for path in ("/v1/openapi.json", "/v1/public-key", "/v1/docs"):
            resp = await c.get(path)
            assert resp.status_code == 200, path
            assert resp.json() == {"oyente": "publico", "integrador": None, "authorization": None}


async def test_publico_v1_compras_con_clave_pasa_con_integrador(clave_y_registro):
    clave, registro = clave_y_registro
    async with _cliente(_app(registro), PUBLICO) as c:
        sin = await c.get("/v1/compras")
        con = await c.post("/v1/compras", json={}, headers={"Authorization": f"Bearer {clave}"})
    assert sin.status_code == 401 and sin.json()["error"]["codigo"] == "no_autenticado"
    assert con.status_code == 200
    assert con.json()["integrador"] == "acme"


async def test_publico_otra_ruta_da_404(clave_y_registro):
    clave, registro = clave_y_registro
    async with _cliente(_app(registro), PUBLICO) as c:
        for path in ("/otra", "/", "/v1x", "/a2ax/y"):
            resp = await c.get(path, headers={"Authorization": f"Bearer {clave}"})
            assert resp.status_code == 404, path


async def test_local_a2a_anonimo_pasa_con_integrador_none(clave_y_registro):
    _, registro = clave_y_registro
    async with _cliente(_app(registro), LOCAL) as c:
        resp = await c.post("/a2a/merchant_agent", json={})
    assert resp.status_code == 200
    assert resp.json() == {"oyente": "local", "integrador": None, "authorization": None}


async def test_local_authorization_ajeno_pasa_como_anonimo(clave_y_registro):
    _, registro = clave_y_registro
    async with _cliente(_app(registro), LOCAL) as c:
        for valor in ("Basic dXNlcjpwYXNz", "Bearer token-de-otro"):
            resp = await c.post("/a2a/merchant_agent", json={}, headers={"Authorization": valor})
            assert resp.status_code == 200
            # el header sigue ahí: sacarlo hacia el comercio lo decide la app
            assert resp.json() == {"oyente": "local", "integrador": None, "authorization": valor}


async def test_local_bearer_psg_invalido_da_401(clave_y_registro):
    clave, registro = clave_y_registro
    # El último carácter cambiado por otro (con "Q" fijo, 1 de cada 64 claves
    # ya terminaba en Q y la "inválida" era la válida).
    otra = clave[:-1] + ("R" if clave.endswith("Q") else "Q")
    async with _cliente(_app(registro), LOCAL) as c:
        for valor in ("Bearer psg_basura", f"Bearer {otra}"):
            resp = await c.post("/a2a/merchant_agent", json={}, headers={"Authorization": valor})
            assert resp.status_code == 401, valor
            assert resp.json()["error"]["codigo"] == "clave_invalida"


async def test_local_a2a_con_clave_valida_es_modo_integrador(clave_y_registro):
    clave, registro = clave_y_registro
    async with _cliente(_app(registro), LOCAL) as c:
        resp = await c.post("/a2a/m/merchant_1", json={}, headers={"Authorization": f"Bearer {clave}"})
    assert resp.json()["integrador"] == "acme" and resp.json()["oyente"] == "local"


async def test_local_v1_exige_clave_y_guard_sigue_igual(clave_y_registro):
    clave, registro = clave_y_registro
    async with _cliente(_app(registro), LOCAL) as c:
        v1 = await c.get("/v1/compras")
        abierta = await c.get("/v1/openapi.json")
        guard = await c.get("/guard/state")
        otra = await c.get("/otra")
    assert v1.status_code == 401
    assert abierta.status_code == 200
    assert guard.status_code == 200 and guard.json()["integrador"] is None
    assert otra.status_code == 200


async def test_varios_authorization_con_clave_nuestra_se_rechaza(clave_y_registro):
    """Si pasara como anónimo, el reenvío al comercio llevaría la clave."""
    clave, registro = clave_y_registro
    headers = [("Authorization", "Basic dXNlcjpwYXNz"), ("Authorization", f"Bearer {clave}")]
    for base in (LOCAL, PUBLICO):
        async with _cliente(_app(registro), base) as c:
            resp = await c.post("/a2a/m/merchant_1", content=b"{}", headers=headers)
        assert resp.status_code == 401, base
        assert resp.json()["error"]["codigo"] == "clave_invalida"


async def test_sin_server_en_el_scope_es_local(clave_y_registro):
    _, registro = clave_y_registro
    vistos: list[dict[str, Any]] = []

    async def app_interna(scope, receive, send):
        vistos.append(dict(scope["state"]))
        await JSONResponse({})(scope, receive, send)

    mw = AutenticacionGateway(app_interna, registro=registro, puertos_publicos={PUERTO_PUBLICO})
    enviados: list[dict[str, Any]] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(mensaje):
        enviados.append(mensaje)

    scope = {"type": "http", "method": "GET", "path": "/a2a/merchant_agent", "headers": [],
             "query_string": b"", "server": None}
    await mw(scope, receive, send)
    assert vistos == [{"oyente": "local", "integrador": None}]
    assert enviados[0]["status"] == 200


async def test_scope_no_http_pasa_sin_tocar(clave_y_registro):
    _, registro = clave_y_registro
    vistos: list[dict[str, Any]] = []

    async def app_interna(scope, receive, send):
        vistos.append(scope)

    mw = AutenticacionGateway(app_interna, registro=registro, puertos_publicos={PUERTO_PUBLICO})
    scope = {"type": "lifespan"}
    await mw(scope, None, None)
    assert vistos == [{"type": "lifespan"}]


async def test_sin_puertos_publicos_todo_es_local(clave_y_registro):
    _, registro = clave_y_registro
    async with _cliente(_app(registro, puertos=set()), PUBLICO) as c:
        resp = await c.post("/a2a/merchant_agent", json={})
    assert resp.json()["oyente"] == "local"


async def test_la_clave_no_se_loguea(clave_y_registro, caplog):
    clave, registro = clave_y_registro
    with caplog.at_level("DEBUG"):
        async with _cliente(_app(registro), PUBLICO) as c:
            await c.get("/v1/compras", headers={"Authorization": f"Bearer {clave}"})
            await c.get("/v1/compras", headers={"Authorization": f"Bearer {clave}x"})
    assert all(clave not in r.getMessage() for r in caplog.records)


# -- CLI ----------------------------------------------------------------------
def test_cli_nueva_imprime_una_clave_que_autentica(capsys):
    assert claves.main(["nueva", "--integrador", "acme"]) == 0
    salida = capsys.readouterr()
    clave, objeto_json = salida.out.strip().splitlines()
    objeto = json.loads(objeto_json)
    assert set(objeto) == {"kid", "sha256", "creada", "activa"} and objeto["activa"] is True
    assert clave not in salida.err
    registro = RegistroIntegradores.desde_dict({"integradores": [{"id": "acme", "claves": [objeto]}]}, {})
    assert registro.autenticar(clave).id == "acme"


def test_cli_nueva_con_integrador_invalido_falla(capsys):
    assert claves.main(["nueva", "--integrador", "No Vale"]) == 2


def test_cli_secreto_webhook(capsys):
    assert claves.main(["secreto-webhook"]) == 0
    secreto = capsys.readouterr().out.strip()
    assert secreto.startswith("whsec_") and len(secreto) >= 32
    # sirve como secreto de webhook del archivo de claves
    datos = {"integradores": [{"id": "a", "webhook": {"url": "https://a.example/h", "secreto_env": "S"}}]}
    assert RegistroIntegradores.desde_dict(datos, {"S": secreto}).get("a").webhook is not None
