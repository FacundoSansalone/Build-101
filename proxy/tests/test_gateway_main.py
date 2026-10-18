"""Arranque del gateway (`python -m proxy`, diseño del gateway §4.1 y §4.4).

La configuración nueva se lee en `Config.desde_entorno`; todo lo que está mal
configurado no arranca (`SystemExit`): un archivo de claves o de comercios
inválido, un webhook que apunta a la red interna, un almacén pedido sin
credenciales, el oyente público fuera de loopback sin claves, el stub o la
falta de filtro con el gateway activo.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

import pytest

from proxy import __main__ as main_mod
from proxy.claves import generar_clave
from proxy.config import Config, verificar_gateway
from proxy.estado import EstadoMemoria

CLAVE, KID, SHA = generar_clave()


def _claves(tmp_path: Path, **integrador: Any) -> Path:
    datos = {
        "integradores": [
            {"id": "acme", "claves": [{"kid": KID, "sha256": SHA, "activa": True}], **integrador}
        ]
    }
    ruta = tmp_path / "claves.json"
    ruta.write_text(json.dumps(datos), "utf-8")
    ruta.chmod(0o600)
    return ruta


# -- Config -------------------------------------------------------------------------


def test_config_desde_entorno_lee_las_variables_del_gateway(tmp_path):
    config = Config.desde_entorno({
        "GATEWAY_HOST": "0.0.0.0",
        "GATEWAY_PORT": "9030",
        "GATEWAY_PUBLIC_URL": "https://gw.example.com/",
        "GATEWAY_CLAVES_ARCHIVO": str(tmp_path / "claves.json"),
        "GATEWAY_COMERCIOS_ARCHIVO": ".temp-db/comercios.json",
        "GATEWAY_COMERCIO_POR_DEFECTO": "tienda",
        "GATEWAY_ESTADO": "supabase",
        "GATEWAY_WEBHOOKS_INSEGUROS": "1",
        "GATEWAY_MAX_CUERPO_BYTES": "2048",
        "GATEWAY_MAX_CUERPO_A2A_BYTES": "4096",
        "GATEWAY_COMPRA_TTL_S": "60",
    })
    assert config.gateway_host == "0.0.0.0"
    assert config.gateway_port == 9030
    assert config.gateway_public_url == "https://gw.example.com"
    assert config.claves_archivo == tmp_path / "claves.json"
    assert config.comercios_archivo is not None and config.comercios_archivo.is_absolute()
    assert config.comercios_archivo.parts[-2:] == (".temp-db", "comercios.json")
    assert config.comercio_por_defecto_id == "tienda"
    assert config.estado_almacen == "supabase"
    assert config.webhooks_inseguros is True
    assert (config.max_cuerpo_v1, config.max_cuerpo_a2a, config.compra_ttl_s) == (2048, 4096, 60)


def test_config_sin_variables_nuevas_es_la_de_siempre():
    config = Config.desde_entorno({})
    assert config.claves_archivo is None and config.comercios_archivo is None
    assert (config.gateway_host, config.gateway_port) == ("127.0.0.1", 8030)
    assert config.gateway_public_url == "http://localhost:8030"
    assert config.estado_almacen == "memoria" and config.webhooks_inseguros is False
    assert config.base_publica_local == "http://localhost:8010"


@pytest.mark.parametrize(
    "variable, valor",
    [("GATEWAY_PORT", "0"), ("GATEWAY_COMPRA_TTL_S", "-1"), ("GATEWAY_MAX_CUERPO_BYTES", "x"),
     ("GATEWAY_ESTADO", "redis"), ("GATEWAY_WEBHOOKS_INSEGUROS", "quizas")],
)
def test_config_con_valores_invalidos_no_arranca(variable, valor):
    with pytest.raises(ValueError):
        Config.desde_entorno({variable: valor})


def test_verificar_gateway():
    verificar_gateway(Config(), hay_claves=False)  # loopback sin claves: no hay público
    verificar_gateway(Config(gateway_host="0.0.0.0"), hay_claves=True)
    with pytest.raises(SystemExit, match="GATEWAY_PORT"):
        verificar_gateway(Config(gateway_port=8010), hay_claves=True)


def test_sin_claves_activas_un_gateway_host_afuera_solo_avisa(caplog):
    """Revisión de compatibilidad: sin claves activas (todas revocadas) el
    proxy no abre el oyente público, así que `GATEWAY_HOST=0.0.0.0` (lo
    exporta start_all.sh con una URL https) no expone nada. Cortar el
    arranque tiraba también la demo entera."""
    with caplog.at_level("WARNING", logger="proxy.config"):
        verificar_gateway(Config(gateway_host="0.0.0.0"), hay_claves=False)
    assert "GATEWAY_HOST" in caplog.text


def test_claves_todas_revocadas_no_cortan_el_arranque(tmp_path):
    ruta = tmp_path / "claves.json"
    ruta.write_text(json.dumps({"integradores": [
        {"id": "acme", "claves": [{"kid": KID, "sha256": SHA, "activa": False}]}
    ]}), "utf-8")
    ruta.chmod(0o600)
    config = Config.desde_entorno(
        {"GATEWAY_CLAVES_ARCHIVO": str(ruta), "GATEWAY_HOST": "0.0.0.0"}
    )
    integradores, _, _ = main_mod.cargar_gateway(config, {})
    assert integradores.hay_claves() is False
    verificar_gateway(config, integradores.hay_claves())  # no lanza


def test_la_url_publica_por_defecto_sigue_al_puerto():
    """Revisión de compatibilidad: con otro GATEWAY_PORT y sin
    GATEWAY_PUBLIC_URL, la Agent Card y la `a2a_url` apuntaban a :8030."""
    assert Config.desde_entorno({}).gateway_public_url == "http://localhost:8030"
    config = Config.desde_entorno({"GATEWAY_PORT": "9030"})
    assert config.gateway_port == 9030
    assert config.gateway_public_url == "http://localhost:9030"
    explicita = Config.desde_entorno(
        {"GATEWAY_PORT": "9030", "GATEWAY_PUBLIC_URL": "https://gw.example.com/"}
    )
    assert explicita.gateway_public_url == "https://gw.example.com"


# -- cargar_gateway --------------------------------------------------------------------


def test_cargar_gateway_sin_archivos_es_el_comercio_de_siempre():
    integradores, comercios, almacen = main_mod.cargar_gateway(Config(), {})
    assert integradores.hay_claves() is False
    assert [c.id for c in comercios.todos()] == ["merchant_1"]
    assert comercios.por_defecto.a2a_url == Config().merchant_upstream_url
    assert comercios.por_defecto.merchant_ids == ()
    assert isinstance(almacen, EstadoMemoria) and almacen.durable is False


def test_cargar_gateway_con_claves(tmp_path):
    config = Config(claves_archivo=_claves(tmp_path))
    integradores, _, _ = main_mod.cargar_gateway(config, {})
    assert integradores.hay_claves()
    assert integradores.autenticar(CLAVE).id == "acme"


def test_un_archivo_de_claves_que_no_existe_no_arranca(tmp_path):
    with pytest.raises(SystemExit, match="claves"):
        main_mod.cargar_gateway(Config(claves_archivo=tmp_path / "no.json"), {})


def test_un_comercio_inexistente_no_arranca(tmp_path):
    config = Config(claves_archivo=_claves(tmp_path, comercios=["tienda_x"]))
    with pytest.raises(SystemExit, match="tienda_x"):
        main_mod.cargar_gateway(config, {})


def test_un_webhook_a_la_red_interna_no_arranca(tmp_path):
    config = Config(claves_archivo=_claves(
        tmp_path, webhook={"url": "https://10.0.0.5/hook", "secreto_env": "SECRETO"}
    ))
    env = {"SECRETO": "s" * 40}
    with pytest.raises(SystemExit, match="webhook"):
        main_mod.cargar_gateway(config, env)
    # En desarrollo, con GATEWAY_WEBHOOKS_INSEGUROS=1, sí.
    main_mod.cargar_gateway(
        Config(claves_archivo=config.claves_archivo, webhooks_inseguros=True), env
    )


def test_supabase_sin_credenciales_no_arranca():
    with pytest.raises(SystemExit, match="SUPABASE"):
        main_mod.cargar_gateway(Config(), {"GATEWAY_ESTADO": "supabase"})


def test_un_archivo_de_comercios_valido(tmp_path):
    ruta = tmp_path / "comercios.json"
    ruta.write_text(json.dumps({
        "por_defecto": "merchant_1",
        "comercios": [
            {"id": "merchant_1", "a2a_url": "http://127.0.0.1:8001/a2a/merchant_agent",
             "merchant_ids": ["merchant_1"]},
            {"id": "tienda_b", "a2a_url": "https://b.example/a2a", "merchant_ids": ["tienda-b"]},
        ],
    }), "utf-8")
    config = Config(
        claves_archivo=_claves(tmp_path, comercios=["merchant_1", "tienda_b"]),
        comercios_archivo=ruta,
    )
    _, comercios, _ = main_mod.cargar_gateway(config, {})
    assert [c.id for c in comercios.todos()] == ["merchant_1", "tienda_b"]


# -- filtro y stub con el gateway ----------------------------------------------------------


def test_sin_filtro_con_gateway_no_arranca_salvo_permiso():
    with pytest.raises(SystemExit, match="filtro"):
        main_mod.exigir_filtro_gateway(None, {})
    assert main_mod.exigir_filtro_gateway(None, {"PROXY_PERMITIR_SIN_FILTRO": "1"}) is True
    assert main_mod.exigir_filtro_gateway(lambda m: m, {}) is False


class _ServidorFalso:
    """En lugar de uvicorn.Server: registra con qué sockets lo corrieron."""

    corridas: list[list[Any]] = []

    def __init__(self, config: Any) -> None:
        self.config = config

    def run(self, sockets: list[Any] | None = None) -> None:
        _ServidorFalso.corridas.append(list(sockets or []))


@pytest.fixture
def entorno_main(monkeypatch, tmp_path):
    """El entorno de `main()` sin red ni servicios: sin Supabase, sin LLM,
    sin fabricante y con los archivos en tmp_path."""
    for variable in list(os.environ):
        if variable.startswith(("GATEWAY_", "SUPABASE_", "PROXY_", "GUARD_", "MERCHANT_")):
            monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("INTENCION_ACTIVA", "0")
    monkeypatch.setenv("FABRICANTE_FUENTE", "ninguna")
    monkeypatch.setenv("GUARD_EVENTS_JSONL", str(tmp_path / "eventos.jsonl"))
    monkeypatch.setenv("GUARD_KEY_PATH", str(tmp_path / "clave.pem"))
    monkeypatch.setenv("GUARD_API_TOKEN_PATH", str(tmp_path / "token"))
    monkeypatch.setattr(main_mod.uvicorn, "Server", _ServidorFalso)
    monkeypatch.setattr(
        main_mod.uvicorn, "run", lambda app, **kw: _ServidorFalso.corridas.append(["run", kw["port"]])
    )
    monkeypatch.setattr(
        main_mod, "sockets_de",
        lambda app, config, gateway: ["local", "publico"] if gateway else ["local"],
    )
    monkeypatch.setattr(main_mod.logging, "basicConfig", lambda **kw: None)
    _ServidorFalso.corridas = []
    return monkeypatch


def test_main_sin_variables_nuevas_arranca_como_siempre(entorno_main):
    main_mod.main()
    # Un solo servidor sobre los sockets locales (127.0.0.1 y [::1]).
    assert _ServidorFalso.corridas == [["local"]]


def test_main_con_claves_abre_los_dos_oyentes(entorno_main, tmp_path):
    entorno_main.setenv("GATEWAY_CLAVES_ARCHIVO", str(_claves(tmp_path)))
    entorno_main.setenv("PROXY_PERMITIR_SIN_FILTRO", "1")
    main_mod.main()
    assert _ServidorFalso.corridas == [["local", "publico"]]


def test_main_con_claves_y_stub_no_arranca(entorno_main, tmp_path):
    entorno_main.setenv("GATEWAY_CLAVES_ARCHIVO", str(_claves(tmp_path)))
    entorno_main.setenv("PROXY_ALLOW_STUB", "1")
    with pytest.raises(SystemExit, match="stub"):
        main_mod.main()
    assert _ServidorFalso.corridas == []


def test_main_con_claves_y_sin_filtro_no_arranca(entorno_main, tmp_path):
    entorno_main.setenv("GATEWAY_CLAVES_ARCHIVO", str(_claves(tmp_path)))
    with pytest.raises(SystemExit, match="filtro"):
        main_mod.main()
    assert _ServidorFalso.corridas == []


def test_main_publico_fuera_de_loopback_sin_claves_arranca_solo_el_local(entorno_main):
    """Sin claves activas no hay oyente público: `GATEWAY_HOST=0.0.0.0` se
    ignora (con un aviso) y la demo arranca como siempre, con un oyente."""
    entorno_main.setenv("GATEWAY_HOST", "0.0.0.0")
    main_mod.main()
    assert _ServidorFalso.corridas == [["local"]]


# -- sockets ------------------------------------------------------------------------------


def _puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_sockets_de_abre_uno_o_dos_oyentes():
    config = Config(port=_puerto_libre(), gateway_port=_puerto_libre())
    solo = main_mod.sockets_de(object(), config, False)
    dos = main_mod.sockets_de(
        object(), Config(port=_puerto_libre(), gateway_port=config.gateway_port), True
    )
    try:
        # El local en 127.0.0.1 (y en [::1] al mismo puerto, si hay IPv6).
        assert solo[0].getsockname()[:2] == ("127.0.0.1", config.port)
        assert {s.getsockname()[1] for s in solo} == {config.port}
        assert [s.getsockname()[0] for s in solo[1:]] in ([], ["::1"])
        # Con gateway, el público al final (también en 127.0.0.1).
        assert dos[-1].getsockname()[:2] == ("127.0.0.1", config.gateway_port)
        assert len(dos) == len(solo) + 1
    finally:
        for s in [*solo, *dos]:
            s.close()


# -- deploy/ (revisión de compatibilidad) ------------------------------------------------
#
# Los scripts levantan servicios en puertos fijos y una VM paga: no se corren
# en los tests. Se fija lo que arregló la revisión y que sigan siendo bash válido.

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def _bash() -> str | None:
    """Un bash que funcione. En Windows, `bash` puede ser el de WSL sin
    distribución instalada (System32): se prueba también el de Git."""
    import shutil
    import subprocess

    candidatos = [shutil.which("bash")]
    git = shutil.which("git")
    if git:  # ...\Git\cmd\git.exe -> ...\Git\bin\bash.exe
        candidatos.append(str(Path(git).resolve().parents[1] / "bin" / "bash.exe"))
    for bash in candidatos:
        if not bash or not Path(bash).exists():
            continue
        try:
            if subprocess.run([bash, "-c", "exit 0"], capture_output=True, timeout=20).returncode == 0:
                return bash
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


@pytest.mark.parametrize("script", ["start_all.sh", "tenki.sh"])
def test_los_scripts_de_deploy_son_bash_valido(script):
    import subprocess

    bash = _bash()
    if bash is None:
        pytest.skip("no hay un bash que funcione en esta máquina")
    subprocess.run([bash, "-n", str(DEPLOY / script)], check=True)


def test_start_all_no_da_el_gateway_por_activo_solo_por_el_archivo():
    texto = (DEPLOY / "start_all.sh").read_text("utf-8")
    # Con un archivo de claves que no existe, el proxy no tiene que leerlo.
    assert "unset GATEWAY_CLAVES_ARCHIVO" in texto
    assert "unset GATEWAY_COMERCIOS_ARCHIVO" in texto
    # El puerto y la URL que se muestran son los que usa el proxy.
    assert "export GATEWAY_PORT GATEWAY_PUBLIC_URL" in texto
    # Solo se espera :8030 si el proxy abrió el oyente público.
    assert '"publico": ?true' in texto


def test_tenki_sin_claves_pisa_las_rutas_locales_con_variables_vacias():
    texto = (DEPLOY / "tenki.sh").read_text("utf-8")
    assert "'GATEWAY_CLAVES_ARCHIVO=' 'GATEWAY_COMERCIOS_ARCHIVO='" in texto
    assert "rm -f .temp-db/deploy.env" not in texto
