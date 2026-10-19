"""Cargadores del arranque (`proxy.arranque`, lote D de Vercel).

- Integradores y comercios por variable (`GATEWAY_CLAVES_JSON`,
  `GATEWAY_COMERCIOS_JSON`), con el mismo JSON que los archivos: en Vercel no
  hay disco. Variable y archivo a la vez, JSON inválido o vacío: no arranca,
  y el mensaje no trae el contenido (puede tener secretos).
- La misma validación cruzada que con archivo (comercios que nombran las
  claves, webhooks).
- `python -m proxy` acepta las variables como alternativa al archivo y sigue
  re-exportando los cargadores con los mismos nombres.
- La clave del recibo (`cargar_clave_recibo`): la del entorno si está; si no,
  la del archivo de hoy, que con `crear_si_falta=False` no se crea. `python
  -m proxy` firma con la del entorno si está (si no sirve, no arranca) y si
  no, con el archivo como siempre.
- `proxy.arranque` se importa (y sus cargadores corren) sin uvicorn, que en
  Vercel no está instalado.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from proxy import arranque
from proxy.arranque import (
    VAR_CLAVES_JSON,
    VAR_COMERCIOS_JSON,
    cargar_clave_recibo,
    cargar_comercios,
    cargar_gateway,
    cargar_integradores,
    json_de_variable,
    validar_registros,
)
from proxy.claves import generar_clave
from proxy.config import Config
from proxy.evaluador import (
    KID_POR_DEFECTO,
    VAR_CLAVE_PEM,
    VAR_CLAVE_PEM_B64,
    VAR_KID,
    ClaveReciboInvalida,
)
from proxy.tipos_gateway import ConfigInvalida

RAIZ = Path(__file__).resolve().parents[2]
CLAVE, KID, SHA = generar_clave()
SECRETO = "SECRETO-NO-DEBE-SALIR-7731"


def claves_json(**integrador: Any) -> dict[str, Any]:
    return {
        "integradores": [
            {"id": "acme", "claves": [{"kid": KID, "sha256": SHA, "activa": True}], **integrador}
        ]
    }


def comercios_json(*extra: dict[str, Any]) -> dict[str, Any]:
    return {
        "por_defecto": "merchant_1",
        "comercios": [
            {"id": "merchant_1", "a2a_url": "https://comercio.example/a2a",
             "catalogo_url": "https://comercio.example/catalogo", "merchant_ids": ["merchant_1"]},
            *extra,
        ],
    }


def _sin_encadenar(e: pytest.ExceptionInfo) -> None:
    """`JSONDecodeError.doc` lleva el texto entero: no tiene que colgar del error."""
    assert e.value.__cause__ is None and e.value.__suppress_context__ is True


# -- json_de_variable -------------------------------------------------------------


def test_sin_la_variable_es_none():
    assert json_de_variable({}, VAR_CLAVES_JSON) is None


@pytest.mark.parametrize("valor", ["", "   ", "\n"])
def test_definida_pero_vacia_es_un_error(valor):
    with pytest.raises(ConfigInvalida, match=f"{VAR_CLAVES_JSON} está definida pero vacía"):
        json_de_variable({VAR_CLAVES_JSON: valor}, VAR_CLAVES_JSON)


def test_json_invalido_dice_donde_pero_no_que():
    texto = '{"integradores": [{"id": "acme", "claves": [{"sha256": "' + SECRETO + '" ]}'
    with pytest.raises(ConfigInvalida, match=r"no es JSON válido \(línea 1, columna \d+\)") as e:
        json_de_variable({VAR_CLAVES_JSON: texto}, VAR_CLAVES_JSON)
    assert SECRETO not in str(e.value) and SECRETO not in repr(e.value)
    _sin_encadenar(e)


# -- cargar_integradores -----------------------------------------------------------


def test_integradores_desde_la_variable():
    integradores = cargar_integradores(Config(), {VAR_CLAVES_JSON: json.dumps(claves_json())})
    assert integradores.hay_claves()
    assert integradores.autenticar(CLAVE).id == "acme"


def test_integradores_sin_nada_es_vacio():
    assert cargar_integradores(Config(), {}).hay_claves() is False


def test_integradores_variable_y_archivo_a_la_vez(tmp_path):
    ruta = tmp_path / "claves.json"
    ruta.write_text(json.dumps(claves_json()), "utf-8")
    for valor in (json.dumps(claves_json()), ""):  # aunque la variable esté vacía
        with pytest.raises(ConfigInvalida, match="GATEWAY_CLAVES_ARCHIVO están los dos"):
            cargar_integradores(Config(claves_archivo=ruta), {VAR_CLAVES_JSON: valor})


@pytest.mark.parametrize(
    "datos",
    [None, [], {}, {"integradores": "acme"}, {"integradores": [], "extra": 1},
     {"integradores": [{"id": "acme", "claves": [{"kid": KID, "sha256": SHA, "activa": "si"}]}]}],
    ids=["null", "lista", "objeto_vacio", "no_lista", "clave_desconocida", "activa_no_bool"],
)
def test_integradores_json_con_otra_forma(datos):
    with pytest.raises(ConfigInvalida, match=f"^{VAR_CLAVES_JSON}[: ]") as e:
        cargar_integradores(Config(), {VAR_CLAVES_JSON: json.dumps(datos)})
    assert SHA not in str(e.value)


def test_json_null_no_es_como_que_no_este():
    """`null` no puede volver en silencio al archivo o a "sin claves"."""
    with pytest.raises(ConfigInvalida, match="es null"):
        json_de_variable({VAR_CLAVES_JSON: "null"}, VAR_CLAVES_JSON)
    with pytest.raises(ConfigInvalida, match="es null"):
        cargar_comercios(Config(), {VAR_COMERCIOS_JSON: " null "})


def test_integradores_lista_vacia_es_valida_y_sin_gateway():
    """Como con el archivo: sin claves no hay oyente público (y la entrada de
    Vercel exige al menos una activa)."""
    integradores = cargar_integradores(Config(), {VAR_CLAVES_JSON: '{"integradores": []}'})
    assert integradores.hay_claves() is False


def test_el_secreto_del_webhook_no_sale_en_el_error():
    datos = claves_json(webhook={"url": "https://hooks.example/kaiai", "secreto_env": "WH"})
    with pytest.raises(ConfigInvalida, match="WH") as e:
        cargar_integradores(Config(), {VAR_CLAVES_JSON: json.dumps(datos), "WH": SECRETO})
    assert SECRETO not in str(e.value)  # el secreto es corto: el error nombra la variable


def test_el_secreto_del_webhook_se_lee_del_entorno():
    datos = claves_json(webhook={"url": "https://hooks.example/kaiai", "secreto_env": "WH"})
    integradores = cargar_integradores(
        Config(), {VAR_CLAVES_JSON: json.dumps(datos), "WH": "w" * 40}
    )
    assert integradores.get("acme").webhook.secreto == b"w" * 40


# -- cargar_comercios -------------------------------------------------------------


def test_comercios_desde_la_variable(caplog):
    with caplog.at_level("WARNING", logger="proxy"):
        comercios = cargar_comercios(Config(), {VAR_COMERCIOS_JSON: json.dumps(comercios_json())})
    assert [c.id for c in comercios.todos()] == ["merchant_1"]
    assert comercios.por_defecto.merchant_ids == ("merchant_1",)
    assert comercios.por_defecto.a2a_url == "https://comercio.example/a2a"
    assert f"Comercios desde {VAR_COMERCIOS_JSON}" in caplog.text


def test_comercios_sin_nada_es_el_de_siempre():
    comercios = cargar_comercios(Config(), {})
    assert [c.id for c in comercios.todos()] == ["merchant_1"]
    assert comercios.por_defecto.a2a_url == Config().merchant_upstream_url


def test_comercios_variable_y_archivo_a_la_vez(tmp_path):
    ruta = tmp_path / "comercios.json"
    ruta.write_text(json.dumps(comercios_json()), "utf-8")
    with pytest.raises(ConfigInvalida, match="GATEWAY_COMERCIOS_ARCHIVO están los dos"):
        cargar_comercios(
            Config(comercios_archivo=ruta), {VAR_COMERCIOS_JSON: json.dumps(comercios_json())}
        )


def test_comercios_json_invalido_o_vacio():
    for valor in ("", "{", "null", "[]", '{"por_defecto": "merchant_1", "comercios": []}'):
        with pytest.raises(ConfigInvalida, match=VAR_COMERCIOS_JSON):
            cargar_comercios(Config(), {VAR_COMERCIOS_JSON: valor})


@pytest.mark.parametrize(
    "url",
    [
        f"https://usuario:{SECRETO}@comercio.example/a2a",       # userinfo
        f"ftp://usuario:{SECRETO}@comercio.example/a2a",         # otro esquema
        f"https://usuario:{SECRETO}@comercio.example:abc/a2a",   # puerto inválido
        f"https://comercio.example/a2a?token={SECRETO}",         # query
        f"https://comercio.example/a2a#{SECRETO}",               # fragment
        f"http://usuario:{SECRETO}@comercio.example/a2a",        # http fuera de loopback
        f"usuario:{SECRETO}",                                    # sin host
    ],
)
def test_una_url_con_credenciales_no_sale_en_el_error(url):
    datos = comercios_json({"id": "tienda_b", "a2a_url": url, "merchant_ids": ["tienda-b"]})
    with pytest.raises(ConfigInvalida, match=VAR_COMERCIOS_JSON) as e:
        cargar_comercios(Config(), {VAR_COMERCIOS_JSON: json.dumps(datos)})
    assert SECRETO not in str(e.value) and SECRETO not in repr(e.value)


def test_comercio_por_http_fuera_de_loopback_no_arranca():
    """El registro exige https fuera de loopback (`comercios.validar_url_upstream`)."""
    datos = comercios_json({"id": "tienda_b", "a2a_url": "http://tienda-b.example/a2a",
                            "merchant_ids": ["tienda-b"]})
    with pytest.raises(ConfigInvalida, match="https"):
        cargar_comercios(Config(), {VAR_COMERCIOS_JSON: json.dumps(datos)})


# -- validación cruzada y cargar_gateway ------------------------------------------


def _env_gateway(claves: dict[str, Any], comercios: dict[str, Any], **extra: str) -> dict[str, str]:
    return {VAR_CLAVES_JSON: json.dumps(claves), VAR_COMERCIOS_JSON: json.dumps(comercios), **extra}


def test_cargar_gateway_con_las_dos_variables():
    env = _env_gateway(claves_json(comercios=["merchant_1"]), comercios_json())
    integradores, comercios, almacen = cargar_gateway(Config(), env)
    assert integradores.autenticar(CLAVE).id == "acme"
    assert [c.id for c in comercios.permitidos(integradores.get("acme"))] == ["merchant_1"]
    assert almacen.nombre == "memoria"


def test_un_comercio_que_no_esta_en_la_variable_no_arranca():
    env = _env_gateway(claves_json(comercios=["tienda_x"]), comercios_json())
    with pytest.raises(SystemExit, match="tienda_x"):
        cargar_gateway(Config(), env)
    with pytest.raises(ConfigInvalida, match="tienda_x"):
        validar_registros(
            cargar_integradores(Config(), env), cargar_comercios(Config(), env),
            webhooks_inseguros=False,
        )


def test_un_webhook_a_la_red_interna_no_arranca_con_la_variable():
    claves = claves_json(webhook={"url": "https://10.0.0.5/hook", "secreto_env": "WH"})
    env = _env_gateway(claves, comercios_json(), WH="s" * 40)
    with pytest.raises(SystemExit, match="webhook del integrador acme"):
        cargar_gateway(Config(), env)
    cargar_gateway(Config(webhooks_inseguros=True), env)  # en desarrollo, sí


def test_cargar_gateway_pasa_los_errores_a_system_exit_sin_el_contenido():
    with pytest.raises(SystemExit, match="El proxy del Guard no arranca") as e:
        cargar_gateway(Config(), {VAR_CLAVES_JSON: '{"integradores": [' + SECRETO})
    assert SECRETO not in str(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__ is True


def test_cargar_gateway_con_archivos_sigue_igual(tmp_path):
    ruta = tmp_path / "claves.json"
    ruta.write_text(json.dumps(claves_json()), "utf-8")
    integradores, comercios, _ = cargar_gateway(Config(claves_archivo=ruta), {})
    assert integradores.autenticar(CLAVE).id == "acme"
    assert [c.id for c in comercios.todos()] == ["merchant_1"]


# -- python -m proxy -----------------------------------------------------------------


def test_python_m_proxy_re_exporta_los_cargadores():
    import proxy.__main__ as principal

    for nombre in ("cargar_agente", "cargar_filtro", "modo_filtro", "cargar_fabricante",
                   "exigir_filtro", "exigir_filtro_gateway", "cargar_gateway",
                   "cargar_integradores", "cargar_comercios", "validar_registros",
                   "cargar_clave_recibo", "json_de_variable"):
        assert getattr(principal, nombre) is getattr(arranque, nombre), nombre
        assert nombre in principal.__all__
    # Lo del servidor local sigue en __main__.
    for nombre in ("abrir_socket", "sockets_de", "servir", "main"):
        assert callable(getattr(principal, nombre))
        assert not hasattr(arranque, nombre)


class _Servidor:
    corridas: list[list[Any]] = []

    def __init__(self, config: Any) -> None:
        pass

    def run(self, sockets: list[Any] | None = None) -> None:
        _Servidor.corridas.append(list(sockets or []))


@pytest.fixture
def entorno_main(monkeypatch, tmp_path):
    import proxy.__main__ as principal

    for variable in list(os.environ):
        if variable.startswith(("GATEWAY_", "SUPABASE_", "PROXY_", "GUARD_", "MERCHANT_")):
            monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("INTENCION_ACTIVA", "0")
    monkeypatch.setenv("FABRICANTE_FUENTE", "ninguna")
    monkeypatch.setenv("GUARD_EVENTS_JSONL", str(tmp_path / "eventos.jsonl"))
    monkeypatch.setenv("GUARD_KEY_PATH", str(tmp_path / "clave.pem"))
    monkeypatch.setenv("GUARD_API_TOKEN_PATH", str(tmp_path / "token"))
    monkeypatch.setattr(principal.uvicorn, "Server", _Servidor)
    monkeypatch.setattr(
        principal, "sockets_de",
        lambda app, config, gateway: ["local", "publico"] if gateway else ["local"],
    )
    monkeypatch.setattr(principal.logging, "basicConfig", lambda **kw: None)
    _Servidor.corridas = []
    return monkeypatch


def test_main_con_claves_por_variable_abre_los_dos_oyentes(entorno_main):
    import proxy.__main__ as principal

    entorno_main.setenv(VAR_CLAVES_JSON, json.dumps(claves_json(comercios=["merchant_1"])))
    entorno_main.setenv(VAR_COMERCIOS_JSON, json.dumps(comercios_json()))
    entorno_main.setenv("PROXY_PERMITIR_SIN_FILTRO", "1")
    principal.main()
    assert _Servidor.corridas == [["local", "publico"]]


def test_main_con_variable_y_archivo_no_arranca(entorno_main, tmp_path):
    import proxy.__main__ as principal

    ruta = tmp_path / "claves.json"
    ruta.write_text(json.dumps(claves_json()), "utf-8")
    entorno_main.setenv("GATEWAY_CLAVES_ARCHIVO", str(ruta))
    entorno_main.setenv(VAR_CLAVES_JSON, json.dumps(claves_json()))
    with pytest.raises(SystemExit, match="están los dos"):
        principal.main()
    assert _Servidor.corridas == []


def test_main_con_json_invalido_no_arranca(entorno_main):
    import proxy.__main__ as principal

    entorno_main.setenv(VAR_COMERCIOS_JSON, '{"por_defecto": "' + SECRETO)
    with pytest.raises(SystemExit, match=VAR_COMERCIOS_JSON) as e:
        principal.main()
    assert SECRETO not in str(e.value) and _Servidor.corridas == []


def test_main_sin_las_variables_nuevas_sigue_como_siempre(entorno_main):
    import proxy.__main__ as principal

    principal.main()
    assert _Servidor.corridas == [["local"]]


# -- la clave del recibo -------------------------------------------------------------


@pytest.fixture
def clave_p256() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
def pem(clave_p256) -> bytes:
    return clave_p256.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _misma(a: ec.EllipticCurvePrivateKey, b: ec.EllipticCurvePrivateKey) -> bool:
    return a.private_numbers() == b.private_numbers()


def _cuerpo(pem: bytes) -> str:
    """Una línea del medio del PEM: si aparece en un mensaje, se filtró la clave."""
    return pem.decode("ascii").splitlines()[1]


def test_cargar_clave_recibo_prefiere_el_entorno(tmp_path, clave_p256, pem):
    config = Config(guard_key_path=tmp_path / "guard_signing_key.pem")
    clave = cargar_clave_recibo(config, {VAR_CLAVE_PEM_B64: base64.b64encode(pem).decode()})
    assert _misma(clave.clave, clave_p256) and clave.origen == VAR_CLAVE_PEM_B64
    assert not config.guard_key_path.exists()  # el archivo ni se mira


def test_cargar_clave_recibo_sin_entorno_es_el_archivo_de_hoy(tmp_path):
    config = Config(guard_key_path=tmp_path / "guard_signing_key.pem")
    clave = cargar_clave_recibo(config, {})
    assert config.guard_key_path.is_file()
    assert clave.origen == str(config.guard_key_path) and clave.kid == KID_POR_DEFECTO
    assert cargar_clave_recibo(config, {VAR_KID: "k3"}).kid == "k3"


def test_cargar_clave_recibo_sin_crear(tmp_path):
    config = Config(guard_key_path=tmp_path / "sub" / "guard_signing_key.pem")
    with pytest.raises(ClaveReciboInvalida):
        cargar_clave_recibo(config, {}, crear_si_falta=False)
    assert list(tmp_path.iterdir()) == []


def test_cargar_clave_recibo_por_defecto_es_la_de_dot_temp_db():
    """Sin `GUARD_KEY_PATH`, la ruta es la de siempre (no se toca: es secreto)."""
    assert Config().guard_key_path.parts[-2:] == (".temp-db", "guard_signing_key.pem")


# -- la clave del recibo en python -m proxy -------------------------------------


def test_python_m_proxy_toma_la_clave_del_entorno_solo_si_esta(tmp_path, clave_p256, pem):
    import proxy.__main__ as principal

    config = Config(guard_key_path=tmp_path / "k.pem")
    assert principal.clave_recibo_del_entorno(config, {}) is None  # lo de siempre: el archivo
    assert principal.clave_recibo_del_entorno(config, {VAR_CLAVE_PEM: "  "}) is None
    clave = principal.clave_recibo_del_entorno(config, {VAR_CLAVE_PEM: pem.decode(), VAR_KID: "k7"})
    assert clave is not None and _misma(clave.clave, clave_p256) and clave.kid == "k7"
    with pytest.raises(SystemExit, match="no arranca") as e:
        principal.clave_recibo_del_entorno(config, {VAR_CLAVE_PEM_B64: pem.decode()})
    assert _cuerpo(pem) not in str(e.value)
    assert list(tmp_path.iterdir()) == []  # ni la crea ni la mira


def _entorno_main_clave(monkeypatch, tmp_path) -> list:
    import proxy.__main__ as principal

    for nombre in ("GUARD_FILTRO", "PROXY_PERMITIR_SIN_FILTRO", "PROXY_ALLOW_STUB",
                   "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SECRET_KEY",
                   "GATEWAY_CLAVES_ARCHIVO", "GATEWAY_COMERCIOS_ARCHIVO", "GATEWAY_CLAVES_JSON",
                   "GATEWAY_COMERCIOS_JSON", VAR_CLAVE_PEM, VAR_CLAVE_PEM_B64, VAR_KID):
        monkeypatch.delenv(nombre, raising=False)
    monkeypatch.setenv("INTENCION_ACTIVA", "0")
    monkeypatch.setenv("FABRICANTE_FUENTE", "ninguna")
    monkeypatch.setenv("GUARD_EVENTS_JSONL", str(tmp_path / "e.jsonl"))
    monkeypatch.setenv("GUARD_KEY_PATH", str(tmp_path / "k.pem"))
    monkeypatch.setenv("GUARD_API_TOKEN_PATH", str(tmp_path / "token"))
    apps: list = []
    monkeypatch.setattr(principal, "sockets_de", lambda app, config, gateway: ["local"])
    monkeypatch.setattr(principal, "servir", lambda app, sockets: apps.append(app))
    monkeypatch.setattr(principal.logging, "basicConfig", lambda **kw: None)
    return apps


def test_python_m_proxy_firma_con_la_clave_del_entorno(monkeypatch, tmp_path, pem):
    """Con la variable, `create_app` recibe la clave: `/guard/public-key`
    publica esa (con su kid) y no se crea el archivo de `GUARD_KEY_PATH`."""
    import httpx

    import proxy.__main__ as principal

    apps = _entorno_main_clave(monkeypatch, tmp_path)
    monkeypatch.setenv(VAR_CLAVE_PEM_B64, base64.b64encode(pem).decode())
    monkeypatch.setenv(VAR_KID, "kaiai-local-2")
    principal.main()
    (app,) = apps
    proxy = app.state.proxy
    assert proxy.clave_recibo is not None and proxy.clave_recibo.kid == "kaiai-local-2"
    assert proxy.datos_clave_publica() == proxy.clave_recibo.clave_publica()
    assert not (tmp_path / "k.pem").exists()

    async def pedir() -> httpx.Response:
        transporte = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transporte, base_url="http://localhost:8010") as c:
            return await c.get("/guard/public-key")

    import asyncio

    respuesta = asyncio.run(pedir())
    assert respuesta.status_code == 200 and respuesta.json()["kid"] == "kaiai-local-2"


def test_python_m_proxy_sin_la_variable_usa_el_archivo_como_siempre(monkeypatch, tmp_path):
    import proxy.__main__ as principal

    apps = _entorno_main_clave(monkeypatch, tmp_path)
    principal.main()
    (app,) = apps
    assert app.state.proxy.clave_recibo is None
    assert (tmp_path / "k.pem").is_file()  # `create_app` la crea al arrancar, como siempre


def test_python_m_proxy_con_clave_invalida_no_arranca(monkeypatch, tmp_path, pem):
    import proxy.__main__ as principal

    apps = _entorno_main_clave(monkeypatch, tmp_path)
    monkeypatch.setenv(VAR_CLAVE_PEM_B64, pem.decode())  # el PEM, no su base64
    with pytest.raises(SystemExit, match=VAR_CLAVE_PEM_B64) as e:
        principal.main()
    assert apps == [] and _cuerpo(pem) not in str(e.value)
    assert not Path(tmp_path / "k.pem").exists()


# -- sin uvicorn (en Vercel no está instalado) --------------------------------------

_SCRIPT = r'''
import base64, importlib, importlib.abc, json, sys, tempfile
from pathlib import Path

params = json.loads(sys.argv[1])
sys.path.insert(0, params["raiz"])
BLOQUEADOS = tuple(params["bloqueados"])
intentos = []


def bloqueado(nombre):
    return any(nombre == b or nombre.startswith(b + ".") for b in BLOQUEADOS)


class SinUvicorn(importlib.abc.MetaPathFinder):
    def find_spec(self, nombre, path=None, target=None):
        if bloqueado(nombre):
            intentos.append(nombre)
            raise ModuleNotFoundError(f"no instalado en la prueba: {nombre}", name=nombre)
        return None


sys.meta_path.insert(0, SinUvicorn())
resultado = {}
try:
    importlib.import_module("uvicorn")
    resultado["control"] = "importó"
except ModuleNotFoundError:
    resultado["control"] = "bloqueado"
intentos.clear()

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import proxy.arranque as arranque
from proxy.config import Config

pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
)
env = dict(params["env"])
env["GUARD_SIGNING_KEY_PEM_B64"] = base64.b64encode(pem).decode()
with tempfile.TemporaryDirectory() as tmp:
    integradores, comercios, almacen = arranque.cargar_gateway(Config(), env)
    resultado["gateway"] = [integradores.hay_claves(), [c.id for c in comercios.todos()],
                            almacen.nombre]
    clave = arranque.cargar_clave_recibo(
        Config(guard_key_path=Path(tmp) / "k.pem"), env, crear_si_falta=False
    )
    resultado["clave"] = [clave.origen, clave.kid, sorted(clave.jwk_publica())]
    resultado["archivos"] = sorted(p.name for p in Path(tmp).iterdir())
resultado["filtro"] = callable(arranque.cargar_filtro({"GUARD_FILTRO": "filtro:filtrar"}))
resultado["fabricante"] = repr(arranque.cargar_fabricante({"FABRICANTE_FUENTE": "ninguna"}))
resultado["agente"] = repr(arranque.cargar_agente({"INTENCION_ACTIVA": "0"}))
resultado["intentos"] = sorted(set(intentos))
resultado["en_sys_modules"] = sorted(m for m in sys.modules if bloqueado(m))
try:
    importlib.import_module("proxy.__main__")
    resultado["main"] = "importó"
except ModuleNotFoundError as e:
    resultado["main"] = f"sin {e.name}"
resultado["intentos_main"] = sorted(set(intentos))
print(json.dumps(resultado))
'''


@pytest.fixture(scope="module")
def sin_uvicorn() -> dict[str, Any]:
    prohibidos = tomllib.loads((RAIZ / "pyproject.toml").read_text("utf-8"))["tool"]["kaiai"][
        "motor"
    ]["prohibidos"]
    env = _env_gateway(claves_json(comercios=["merchant_1"]), comercios_json())
    params = {"raiz": str(RAIZ), "bloqueados": ["uvicorn", *prohibidos], "env": env}
    resultado = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", "-c", _SCRIPT, json.dumps(params)],
        cwd=RAIZ, capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    assert resultado.returncode == 0, resultado.stderr[-4000:]
    return json.loads(resultado.stdout.strip().splitlines()[-1])


def test_arranque_se_importa_y_carga_sin_uvicorn(sin_uvicorn):
    assert sin_uvicorn["control"] == "bloqueado"  # el bloqueo funciona
    assert sin_uvicorn["gateway"] == [True, ["merchant_1"], "memoria"]
    assert sin_uvicorn["clave"] == [
        "GUARD_SIGNING_KEY_PEM_B64", "guard-key-1", ["alg", "crv", "kid", "kty", "use", "x", "y"]
    ]
    assert sin_uvicorn["archivos"] == []  # crear_si_falta=False y del entorno: nada en disco
    assert sin_uvicorn["filtro"] is True
    assert sin_uvicorn["fabricante"] == "None" and sin_uvicorn["agente"] == "None"
    # Nadie intentó importar uvicorn ni la simulación (tampoco con try/except).
    assert sin_uvicorn["intentos"] == []
    assert sin_uvicorn["en_sys_modules"] == []


def test_el_servidor_local_si_necesita_uvicorn(sin_uvicorn):
    """Control del otro lado: `proxy.__main__` (el servidor local) importa
    uvicorn arriba; sin él no se importa. La entrada de Vercel no lo usa."""
    assert sin_uvicorn["main"] == "sin uvicorn"
    assert sin_uvicorn["intentos_main"] == ["uvicorn"]

