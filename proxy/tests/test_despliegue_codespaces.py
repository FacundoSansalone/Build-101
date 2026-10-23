"""La demo a pedido en GitHub Codespaces (`.devcontainer/`, `deploy/codespaces/`).

Un Codespace corre lo mismo que el contenedor de Railway: `prender.sh` lanza
`deploy/railway/arrancar.sh` con el origen que sale del nombre del Codespace
y deja público el 8080. Sin un Codespace eso no se levanta acá; se prueba:

- `devcontainer.json`: un solo puerto, los secretos declarados (sin valores),
  el script de cada etapa y el mismo uv que el Dockerfile de Railway.
- `prender.sh --solo-validar`: el origen público y los secretos, sin
  mostrarlos; y `arrancar.sh` con `KAIAI_PLATAFORMA=Codespace`.
- `cargar_secretos.sh` y `apuntar_vercel.sh`, con `gh`, `curl` y `npx` falsos.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from proxy.tests.test_despliegue_railway import (  # noqa: F401 (bash y npx_falso son fixtures)
    CLAVE_GEMINI,
    CLAVE_PANEL,
    CURL_FALSO,
    PREFIJO,
    _carpeta_secretos,
    _correr,
    _registro,
    bash,
    npx_falso,
)

RAIZ = Path(__file__).resolve().parents[2]
DEPLOY = RAIZ / "deploy"
CODESPACES = DEPLOY / "codespaces"
DEVCONTAINER = RAIZ / ".devcontainer" / "devcontainer.json"
FEATURE = RAIZ / ".devcontainer" / "kaiai-arranque"
NOMBRE = "cuddly-space-umbrella-x7v9q4w5j3"
ORIGEN = f"https://{NOMBRE}-8080.app.github.dev"
REPO = "equipo/kaiai"
SECRETOS = {"GOOGLE_API_KEY": CLAVE_GEMINI, "DASHBOARD_ACCESS_TOKEN": CLAVE_PANEL,
            "TUNEL_PREFIJO": PREFIJO}


def _devcontainer() -> dict:
    return json.loads(DEVCONTAINER.read_text(encoding="utf-8"))


def _puertos_internos() -> set[int]:
    texto = (DEPLOY / "railway" / "arrancar.sh").read_text(encoding="utf-8")
    encontrado = re.search(r'^PUERTOS_INTERNOS="([0-9 ]+)"', texto, re.M)
    assert encontrado
    return {int(p) for p in encontrado.group(1).split()}


# ---------------------------------------------------------------------------
# devcontainer.json
# ---------------------------------------------------------------------------


def test_devcontainer_reenvia_solo_el_8080_y_ninguno_de_adentro():
    dc = _devcontainer()
    assert dc["forwardPorts"] == [8080]
    assert 8080 not in _puertos_internos()
    assert set(dc["portsAttributes"]) == {"8080"}
    # ADK, el proxy y el panel escuchan en 127.0.0.1: GitHub no los ofrece.
    assert dc["otherPortsAttributes"]["onAutoForward"] == "ignore"


def test_devcontainer_declara_los_secretos_de_arrancar_sin_valores():
    dc = _devcontainer()
    assert set(dc["secrets"]) == set(SECRETOS)
    for declarado in dc["secrets"].values():
        assert set(declarado) == {"description"}
    entorno = {**dc.get("containerEnv", {}), **dc.get("remoteEnv", {})}
    assert not set(entorno) & set(SECRETOS)
    assert not any(n in entorno for n in ("DASHBOARD_PUBLIC_ORIGINS", "PORT"))  # los pone prender.sh


def test_devcontainer_instala_al_crear_y_prende_en_cada_arranque():
    dc = _devcontainer()
    assert dc["postCreateCommand"] == "bash deploy/codespaces/instalar.sh"
    assert dc["postStartCommand"] == "bash deploy/codespaces/prender.sh --avisar"
    assert (CODESPACES / "instalar.sh").is_file()
    assert (CODESPACES / "prender.sh").is_file()
    # Python 3.12 sobre trixie, como el Dockerfile de Railway, con la versión
    # mayor de la imagen fija.
    assert re.fullmatch(r"mcr\.microsoft\.com/devcontainers/python:\d+-3\.12-trixie", dc["image"])
    assert dc["containerEnv"]["UV_FROZEN"] == "1"
    assert dc["hostRequirements"] == {"cpus": 2, "memory": "8gb"}


def test_instalar_usa_el_mismo_uv_que_el_dockerfile():
    dockerfile = (DEPLOY / "railway" / "Dockerfile").read_text(encoding="utf-8")
    instalar = (CODESPACES / "instalar.sh").read_text(encoding="utf-8")
    del_dockerfile = re.search(r"ghcr\.io/astral-sh/uv:([0-9.]+)", dockerfile)
    de_instalar = re.search(r'^UV_VERSION="([0-9.]+)"', instalar, re.M)
    assert del_dockerfile and de_instalar
    assert de_instalar.group(1) == del_dockerfile.group(1)
    assert "uv sync --package ap2-samples" in instalar


@pytest.mark.parametrize("ruta", [
    *(CODESPACES / s for s in ("instalar.sh", "prender.sh", "cargar_secretos.sh", "apuntar_vercel.sh")),
    *(FEATURE / s for s in ("install.sh", "entrypoint.sh", "vigia.sh")),
], ids=lambda r: f"{r.parent.name}/{r.name}")
def test_los_scripts_son_bash_valido_y_con_lf(bash, ruta):
    assert b"\r" not in ruta.read_bytes()
    subprocess.run([bash, "-n", str(ruta)], check=True, timeout=30)


def test_la_feature_deja_el_vigia_con_el_contenedor_y_prende_con_prender():
    """Lo que lanza el postStartCommand, GitHub lo mata al terminar esa etapa
    (medido en un Codespace real, aun con setsid y nohup): la demo la prende
    un vigía que arranca con el contenedor, como el sshd de su feature."""
    dc = _devcontainer()
    assert "./kaiai-arranque" in dc["features"]
    feature = json.loads((FEATURE / "devcontainer-feature.json").read_text(encoding="utf-8"))
    assert feature["id"] == "kaiai-arranque"
    install = (FEATURE / "install.sh").read_text(encoding="utf-8")
    assert feature["entrypoint"] == "/usr/local/share/kaiai-arranque/entrypoint.sh"
    assert "DIR=/usr/local/share/kaiai-arranque" in install
    assert "cp entrypoint.sh vigia.sh" in install
    entrypoint = (FEATURE / "entrypoint.sh").read_text(encoding="utf-8")
    # No bloquea el arranque del contenedor: el vigía va en segundo plano.
    assert 'nohup setsid sh "$DIR/vigia.sh" "$USUARIO" > /tmp/kaiai-vigia.log 2>&1 < /dev/null &' in entrypoint
    assert entrypoint.rstrip().endswith('exec "$@"')
    vigia = (FEATURE / "vigia.sh").read_text(encoding="utf-8")
    assert "/workspaces/*/.temp-db/codespaces.aviso" in vigia
    assert "echo \"$$\" > /tmp/kaiai-vigia.pid" in vigia
    # Como el usuario del Codespace, con su HOME (ahí está uv) y shell de login
    # (CODESPACE_NAME, GITHUB_TOKEN y los secretos de este arranque).
    assert 'runuser -u "$USUARIO" -- env HOME=' in vigia
    assert "bash -lc \"cd '$raiz' && bash deploy/codespaces/prender.sh\"" in vigia
    # Un aviso de antes de este arranque no cuenta.
    assert '[ "$cuando" -lt "$inicio" ]' in vigia
    prender = (CODESPACES / "prender.sh").read_text(encoding="utf-8")
    assert "touch .temp-db/codespaces.aviso" in prender
    assert "/tmp/kaiai-vigia.pid" in prender


# ---------------------------------------------------------------------------
# prender.sh y arrancar.sh en un Codespace
# ---------------------------------------------------------------------------

EN_CODESPACE = {"CODESPACE_NAME": NOMBRE, "GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN": "app.github.dev"}


def test_prender_valida_con_el_origen_del_codespace_sin_mostrar_secretos(bash):
    r = _correr(bash, "codespaces/prender.sh", "--solo-validar", entorno={**EN_CODESPACE, **SECRETOS})
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert f"{ORIGEN}/demo" in r.stdout
    assert "enrutador en :8080" in r.stdout
    for secreto in SECRETOS.values():
        assert secreto not in r.stdout + r.stderr


@pytest.mark.parametrize("falta", sorted(SECRETOS))
def test_prender_sin_un_secreto_no_arranca_y_dice_como_cargarlo(bash, falta):
    entorno = {**EN_CODESPACE, **SECRETOS, falta: ""}
    r = _correr(bash, "codespaces/prender.sh", entorno=entorno)  # el modo de verdad
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert falta in r.stderr
    assert "secretos de Codespaces" in r.stderr
    assert "cargar_secretos.sh" in r.stderr
    assert "Railway" not in r.stderr
    assert "Prendiendo" not in r.stdout  # no lanzó nada
    for secreto in SECRETOS.values():
        assert secreto not in r.stdout + r.stderr


@pytest.mark.parametrize("entorno", [
    {},
    {"CODESPACE_NAME": "no/es un nombre"},
    {**EN_CODESPACE, "GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN": "x/y"},
])
def test_prender_fuera_de_un_codespace_no_hace_nada(bash, entorno):
    r = _correr(bash, "codespaces/prender.sh", "--solo-validar", entorno={**SECRETOS, **entorno})
    assert r.returncode == 2
    assert "Variables en orden" not in r.stdout


def test_prender_lanza_arrancar_en_segundo_plano_con_la_salida_en_el_log():
    texto = (CODESPACES / "prender.sh").read_text(encoding="utf-8")
    # Nada en la terminal: si no, el Codespace no se apagaría por inactividad.
    assert 'setsid nohup bash deploy/railway/arrancar.sh >> "$LOG" 2>&1 < /dev/null 9>&- &' in texto
    assert "LOG=.logs/codespaces.log" in texto
    for opcion in ("KAIAI_PLATAFORMA=Codespace", "ENRUTADOR_HOST=127.0.0.1", 'PORT="$PUERTO"',
                   'DASHBOARD_PUBLIC_ORIGINS="$ORIGEN,https://localhost:$PUERTO"'):
        assert opcion in texto
    assert 'gh codespace ports visibility "$PUERTO:public" -c "$NOMBRE"' in texto


def test_el_panel_acepta_lo_que_reescribe_github_y_nada_mas(tmp_path):
    """GitHub manda `Host: localhost:8080` y, si el Origin es el del propio
    Codespace, `Origin: https://localhost:8080`; los de otros sitios llegan tal
    cual (medido en un Codespace real). Con los orígenes de prender.sh el
    panel acepta eso y sigue rechazando otros sitios."""
    from dashboard.server import origenes_publicos
    from dashboard.tests.test_dominio_publico import ESCENARIO, _cliente, _post

    origenes = origenes_publicos(f"{ORIGEN},https://localhost:8080")
    c = _cliente(tmp_path, "https://localhost:8080", origenes_publicos=origenes)
    assert c.get("/api/demo/escenarios").status_code == 200
    propio = _post(c, ESCENARIO, {"escenario": "B"}, "https://localhost:8080",
                   **{"Sec-Fetch-Site": "same-origin"})
    assert propio.status_code != 403
    for ajeno in ("https://evil.example", "https://otro-8080.app.github.dev", "null"):
        r = _post(c, ESCENARIO, {"escenario": "B"}, ajeno)
        assert r.status_code == 403, ajeno
        assert r.json()["error"] == "origen_no_permitido"
    otro_host = _cliente(tmp_path, "https://evil.example", origenes_publicos=origenes)
    assert otro_host.get("/api/demo/escenarios").json()["error"] == "host_no_permitido"


def test_arrancar_en_un_codespace_habla_de_sus_secretos_y_no_de_railway(bash):
    r = _correr(bash, "railway/arrancar.sh", entorno={"KAIAI_PLATAFORMA": "Codespace"})
    assert r.returncode == 2
    assert "secretos de Codespaces" in r.stderr
    assert "Railway" not in r.stderr
    por_defecto = _correr(bash, "railway/arrancar.sh", entorno={})
    assert "Railway → Variables" in por_defecto.stderr
    otra = _correr(bash, "railway/arrancar.sh", "--solo-validar",
                   entorno={**SECRETOS, "DASHBOARD_PUBLIC_ORIGINS": ORIGEN, "KAIAI_PLATAFORMA": "Tenki"})
    assert otra.returncode == 2
    assert "KAIAI_PLATAFORMA" in otra.stderr


# ---------------------------------------------------------------------------
# cargar_secretos.sh y apuntar_vercel.sh (con gh, curl y npx falsos)
# ---------------------------------------------------------------------------

GH_FALSO = """#!/bin/bash
# Registra cada llamada (y lo que llega por stdin a `secret set`), sin red.
printf '%s\\n' "$*" >> "$GH_REGISTRO"
case "$*" in
  "secret set "*)
    cat > "$GH_STDIN/$3"
    if [ "${GH_FALLA:-}" = secret ]; then echo "HTTP 403: Resource not accessible" >&2; exit 1; fi
    echo "Set Codespaces secret $3 for your user" ;;
  "codespace list"*)
    if [ "${GH_FALLA:-}" = list ]; then echo "error: not logged in" >&2; exit 1; fi
    printf '%s' "${GH_LISTA:-}" | tr ',' '\\n' ;;
  "codespace ports visibility"*)
    if [ "${GH_FALLA:-}" = ports ]; then echo "error: codespace is not running" >&2; exit 1; fi ;;
esac
"""


def _con_falsos(bash: str, tmp_path: Path, script: str, *args: str,
                entorno: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """El script con gh, curl y npx falsos adelante en el PATH (se ponen adentro
    de bash: el bash de Git pone su curl antes que el PATH heredado)."""
    falsos = tmp_path / "falsos"
    falsos.mkdir(exist_ok=True)
    stdin = tmp_path / "gh_stdin"
    stdin.mkdir(exist_ok=True)
    for nombre, texto in (("gh", GH_FALSO), ("curl", CURL_FALSO)):
        (falsos / nombre).write_bytes(texto.encode())
        (falsos / nombre).chmod(0o755)
    lanzador = ('d="$FALSOS"; command -v cygpath >/dev/null && d="$(cygpath -u "$d")"; '
                'PATH="$d:$PATH" exec bash "$0" "$@"')
    resultado = subprocess.run(
        [bash, "-c", lanzador, (DEPLOY / script).as_posix(), *args], cwd=RAIZ,
        capture_output=True, timeout=60,
        env={**os.environ, "PYTHONUTF8": "1", "REPO": REPO, **entorno,
             "FALSOS": falsos.as_posix(), "GH_REGISTRO": (tmp_path / "gh.txt").as_posix(),
             "GH_STDIN": stdin.as_posix(), "CURL_REGISTRO": (tmp_path / "curl.txt").as_posix()},
    )
    return subprocess.CompletedProcess(resultado.args, resultado.returncode,
                                       resultado.stdout.decode("utf-8", "replace"),
                                       resultado.stderr.decode("utf-8", "replace"))


def _lineas(ruta: Path) -> list[str]:
    return ruta.read_text(encoding="utf-8").splitlines() if ruta.exists() else []


@pytest.fixture
def para_cargar(tmp_path) -> dict[str, str]:
    env = tmp_path / ".env"
    env.write_bytes(f'OTRA=1\r\nGOOGLE_API_KEY="{CLAVE_GEMINI}"\r\n'.encode())
    return {
        "ENV_ARCHIVO": env.as_posix(),
        "SECRETOS": _carpeta_secretos(tmp_path).as_posix(),
        "CLAVE_PANEL": (tmp_path / "temp" / "codespaces_dashboard_token").as_posix(),
        "PY": Path(sys.executable).as_posix(),
    }


def test_cargar_secretos_los_manda_por_stdin_y_nunca_los_muestra(bash, tmp_path, para_cargar):
    r = _con_falsos(bash, tmp_path, "codespaces/cargar_secretos.sh", entorno=para_cargar)
    assert r.returncode == 0, (r.stdout, r.stderr)
    llamadas = _lineas(tmp_path / "gh.txt")
    assert llamadas == [f"secret set {n} --user --app codespaces --repos {REPO}"
                        for n in ("GOOGLE_API_KEY", "DASHBOARD_ACCESS_TOKEN", "TUNEL_PREFIJO")]
    recibido = {p.name: p.read_text(encoding="utf-8") for p in (tmp_path / "gh_stdin").iterdir()}
    clave_panel = Path(para_cargar["CLAVE_PANEL"]).read_text(encoding="utf-8").strip()
    assert recibido == {"GOOGLE_API_KEY": CLAVE_GEMINI, "DASHBOARD_ACCESS_TOKEN": clave_panel,
                        "TUNEL_PREFIJO": PREFIJO}
    assert len(clave_panel) >= 16
    for secreto in (CLAVE_GEMINI, clave_panel, PREFIJO):
        assert secreto not in r.stdout + r.stderr
        assert not any(secreto in llamada for llamada in llamadas)  # nunca en la línea de comandos
    assert "Listo" in r.stdout


def test_cargar_secretos_reusa_la_contrasena_del_panel(bash, tmp_path, para_cargar):
    primera = _con_falsos(bash, tmp_path, "codespaces/cargar_secretos.sh", entorno=para_cargar)
    clave = Path(para_cargar["CLAVE_PANEL"]).read_text(encoding="utf-8")
    segunda = _con_falsos(bash, tmp_path, "codespaces/cargar_secretos.sh", entorno=para_cargar)
    assert primera.returncode == segunda.returncode == 0
    assert "Generé" in primera.stdout and "Generé" not in segunda.stdout
    assert Path(para_cargar["CLAVE_PANEL"]).read_text(encoding="utf-8") == clave
    assert (tmp_path / "gh_stdin" / "DASHBOARD_ACCESS_TOKEN").read_text(encoding="utf-8") == clave.strip()


@pytest.mark.parametrize(("romper", "dice"), [
    ("env", "GOOGLE_API_KEY"),
    ("prefijo", "TUNEL_PREFIJO"),
    ("repo", "REPO="),
])
def test_cargar_secretos_sin_lo_necesario_no_toca_github(bash, tmp_path, para_cargar, romper, dice):
    entorno = dict(para_cargar)
    if romper == "env":
        Path(entorno["ENV_ARCHIVO"]).write_text("OTRA=1\n", encoding="utf-8")
    elif romper == "prefijo":
        (Path(entorno["SECRETOS"]) / "TUNEL_PREFIJO.txt").write_text("corto", encoding="utf-8")
    else:
        entorno["REPO"] = "no es un repo"
    r = _con_falsos(bash, tmp_path, "codespaces/cargar_secretos.sh", entorno=entorno)
    assert r.returncode == 2
    assert dice in r.stderr
    assert _lineas(tmp_path / "gh.txt") == []
    assert not Path(entorno["CLAVE_PANEL"]).exists()


def test_cargar_secretos_corta_si_gh_falla(bash, tmp_path, para_cargar):
    r = _con_falsos(bash, tmp_path, "codespaces/cargar_secretos.sh",
                    entorno={**para_cargar, "GH_FALLA": "secret"})
    assert r.returncode == 1
    assert len(_lineas(tmp_path / "gh.txt")) == 1  # no sigue con los otros
    assert "gh auth login -s codespace" in r.stderr
    assert "Listo" not in r.stdout + r.stderr


def test_apuntar_desde_codespaces_publica_comprueba_y_redespliega(bash, tmp_path, npx_falso):
    r = _con_falsos(bash, tmp_path, "codespaces/apuntar_vercel.sh", NOMBRE, entorno=npx_falso)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert _lineas(tmp_path / "gh.txt") == [f"codespace ports visibility 8080:public -c {NOMBRE}"]
    assert _lineas(tmp_path / "curl.txt") == [f"{ORIGEN}/salud", f"{ORIGEN}/{PREFIJO}/products"]
    assert _registro(npx_falso) == ["vercel env add GATEWAY_COMERCIOS_JSON production --force",
                                    "vercel deploy --prod --yes"]
    comercios = Path(npx_falso["NPX_STDIN"]).read_text(encoding="utf-8")
    assert f"{ORIGEN}/{PREFIJO}/a2a/" in comercios
    assert PREFIJO not in r.stdout + r.stderr
    assert "Listo" in r.stdout


def test_apuntar_desde_codespaces_sin_nombre_usa_el_unico(bash, tmp_path, npx_falso):
    r = _con_falsos(bash, tmp_path, "codespaces/apuntar_vercel.sh",
                    entorno={**npx_falso, "GH_LISTA": NOMBRE})
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert _lineas(tmp_path / "gh.txt")[0] == f"codespace list --repo {REPO} --json name --jq .[].name"
    assert f"{ORIGEN}/salud" in _lineas(tmp_path / "curl.txt")


@pytest.mark.parametrize(("variables", "codigo", "dice"), [
    ({"GH_LISTA": f"{NOMBRE},otro-codespace-123"}, 2, "Tenés 2 Codespaces"),
    ({"GH_LISTA": ""}, 1, "No tenés ningún Codespace"),
    ({"GH_FALLA": "list"}, 1, "gh auth login -s codespace"),
])
def test_apuntar_desde_codespaces_sin_nombre_y_sin_uno_solo_no_adivina(bash, tmp_path, npx_falso,
                                                                       variables, codigo, dice):
    r = _con_falsos(bash, tmp_path, "codespaces/apuntar_vercel.sh", entorno={**npx_falso, **variables})
    assert r.returncode == codigo
    assert dice in r.stderr
    assert _lineas(tmp_path / "curl.txt") == []
    assert _registro(npx_falso) == []


@pytest.mark.parametrize(("variables", "dice"), [
    ({"GH_FALLA": "ports"}, "no pude dejar público"),
    ({"CURL_SALUD": "503"}, "salud contestó 503"),
    ({"CURL_CATALOGO": "401"}, "cargar_secretos.sh"),  # lo atendió el panel: otro prefijo
    ({"CURL_CATALOGO": "502"}, "contestó 502"),
])
def test_apuntar_desde_codespaces_no_toca_vercel_si_algo_falla(bash, tmp_path, npx_falso,
                                                               variables, dice):
    r = _con_falsos(bash, tmp_path, "codespaces/apuntar_vercel.sh", NOMBRE,
                    entorno={**npx_falso, **variables})
    assert r.returncode == 1
    assert dice.lower() in r.stderr.lower()
    assert "Listo" not in r.stdout + r.stderr
    assert PREFIJO not in r.stdout + r.stderr
    assert _registro(npx_falso) == []


@pytest.mark.parametrize("args", [
    [f"https://{NOMBRE}-8080.app.github.dev"], ["a b"], ["--sin-salud"], [NOMBRE, "otro-mas"],
])
def test_apuntar_desde_codespaces_valida_el_nombre(bash, tmp_path, npx_falso, args):
    r = _con_falsos(bash, tmp_path, "codespaces/apuntar_vercel.sh", *args, entorno=npx_falso)
    assert r.returncode == 2
    assert _lineas(tmp_path / "gh.txt") == []
    assert _registro(npx_falso) == []


def test_apuntar_solo_publicar_deja_el_puerto_y_comprueba_sin_tocar_vercel(bash, tmp_path, npx_falso):
    r = _con_falsos(bash, tmp_path, "codespaces/apuntar_vercel.sh", "--solo-publicar", NOMBRE,
                    entorno=npx_falso)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert _lineas(tmp_path / "gh.txt") == [f"codespace ports visibility 8080:public -c {NOMBRE}"]
    assert _lineas(tmp_path / "curl.txt") == [f"{ORIGEN}/salud", f"{ORIGEN}/{PREFIJO}/products"]
    assert _registro(npx_falso) == []
    assert "no se tocó" in r.stdout
    caida = _con_falsos(bash, tmp_path, "codespaces/apuntar_vercel.sh", NOMBRE, "--solo-publicar",
                        entorno={**npx_falso, "CURL_SALUD": "502"})
    assert caida.returncode == 1
    assert "Listo" not in caida.stdout + caida.stderr
