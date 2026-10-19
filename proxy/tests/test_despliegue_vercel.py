"""Preparación del deploy del motor en Vercel (lote F).

- `.vercelignore`: que `vercel deploy` no suba ningún secreto ni la
  simulación, y que sí suba el motor y sus datos de runtime. Se emula el
  filtro (semántica de .gitignore, más lo que Vercel ignora solo) sobre
  `git ls-files` y sobre lo que hay en disco: el deploy sube la carpeta, no
  git.
- `vercel.json`: JSON válido, `maxDuration` ≤ 300 (Hobby), sin `regions`
  fijas, y que `excludeFiles` no saque nada que el motor use en runtime.
- Los scripts de `deploy/vercel/` y `proxy/scripts/smoke_v1_pausas.py`:
  `--help`, y las piezas puras (entorno, balanceador, túnel, secretos,
  verificación de Supabase) sin red.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import fnmatch
import importlib.util
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from types import ModuleType

import httpx
import pytest

RAIZ = Path(__file__).resolve().parents[2]
VERCELIGNORE = RAIZ / ".vercelignore"
VERCEL_JSON = RAIZ / "vercel.json"
DEPLOY_VERCEL = RAIZ / "deploy" / "vercel"
MOTOR = ("contracts", "guard", "filtro", "intencion", "decisiones", "taxonomia", "fabricante", "proxy")
# Módulos del motor que no se usan en runtime (el plan los saca del bundle).
NO_RUNTIME = (
    "proxy/demo_local.py", "proxy/subir_benchmark.py", "guard/benchmark_hook.py",
    "filtro/casos.py", "filtro/evaluar.py", "intencion/evaluar_*.py",
)
DATOS_RUNTIME = ("taxonomia/datos/taxonomy-with-ids.en-US.subconjunto.txt",)
# Lo que lee el build en la raíz (lotes A y D).
RAIZ_DEL_BUILD = ("index.py", "vercel.json", "pyproject.toml", ".python-version", "uv.lock")
# Lo que Vercel no sube nunca por su cuenta (docs: "Ignored files and folders").
IGNORADOS_POR_VERCEL = (
    ".hg", ".git", ".gitmodules", ".svn", ".cache", ".next", ".now", ".vercel", ".npmignore",
    ".dockerignore", ".gitignore", ".*.swp", ".DS_Store", ".wafpicke-*", ".lock-wscript",
    ".env.local", ".env.*.local", ".venv", ".yarn/cache", "npm-debug.log", "config.gypi",
    "node_modules", "__pycache__", "venv", "CVS",
)
SECRETOS = (
    # (descripción, regex sobre el nombre del archivo)
    (".env", re.compile(r"^\.env($|\.)")),
    ("*.pem", re.compile(r"\.pem$", re.I)),
    ("*.key", re.compile(r"\.(key|p12|pfx)$", re.I)),
    ("*_key*", re.compile(r"_key", re.I)),
    ("guard_api_token", re.compile(r"guard_api_token")),
)


# ---------------------------------------------------------------------------
# emulación de .vercelignore (semántica de .gitignore)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Patron:
    regex: re.Pattern[str]
    negado: bool
    solo_dir: bool
    texto: str


def _glob_a_regex(glob: str) -> str:
    """`*` y `?` no cruzan `/`; `**/`, `/**` y `/**/` cruzan niveles."""
    salida: list[str] = []
    i, n = 0, len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            if glob.startswith("**", i) and (i == 0 or glob[i - 1] == "/"):
                fin = i + 2
                if fin == n:
                    salida.append(".*")
                    i = fin
                    continue
                if glob[fin] == "/":
                    salida.append("(?:.*/)?")
                    i = fin + 1
                    continue
            while i < n and glob[i] == "*":
                i += 1
            salida.append("[^/]*")
            continue
        if c == "?":
            salida.append("[^/]")
        elif c == "[":
            cierre = glob.find("]", i + 1)
            if cierre == -1:
                salida.append(re.escape(c))
            else:
                clase = glob[i + 1:cierre]
                if clase.startswith("!"):
                    clase = "^" + clase[1:]
                salida.append(f"[{clase}]")
                i = cierre + 1
                continue
        elif c == "\\" and i + 1 < n:
            salida.append(re.escape(glob[i + 1]))
            i += 2
            continue
        else:
            salida.append(re.escape(c))
        i += 1
    return "".join(salida)


def leer_patrones(texto: str) -> list[Patron]:
    patrones = []
    for linea in texto.splitlines():
        linea = linea.rstrip()
        if not linea or linea.startswith("#"):
            continue
        original = linea
        negado = linea.startswith("!")
        if negado:
            linea = linea[1:]
        solo_dir = linea.endswith("/")
        linea = linea.rstrip("/")
        anclado = "/" in linea
        linea = linea.lstrip("/")
        regex = _glob_a_regex(linea)
        if not anclado:
            regex = "(?:.*/)?" + regex
        patrones.append(Patron(re.compile(regex + r"\Z"), negado, solo_dir, original))
    return patrones


class Ignorados:
    """Como git: gana el último patrón que coincide, y un archivo cuya carpeta
    está ignorada no se puede volver a sumar."""

    def __init__(self, patrones: list[Patron]) -> None:
        self.patrones = patrones

    def _ultimo(self, ruta: str, es_dir: bool) -> bool | None:
        resultado = None
        for p in self.patrones:
            if p.solo_dir and not es_dir:
                continue
            if p.regex.match(ruta):
                resultado = not p.negado
        return resultado

    def dir_ignorado(self, ruta: str) -> bool:
        partes = ruta.split("/")
        return any(self._ultimo("/".join(partes[:i]), True) for i in range(1, len(partes) + 1))

    def ignorado(self, ruta: str) -> bool:
        partes = ruta.split("/")
        if any(self._ultimo("/".join(partes[:i]), True) for i in range(1, len(partes))):
            return True
        return bool(self._ultimo(ruta, False))


@lru_cache(maxsize=1)
def _vercelignore() -> Ignorados:
    return Ignorados(leer_patrones(VERCELIGNORE.read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def _por_vercel() -> Ignorados:
    return Ignorados(leer_patrones("\n".join(IGNORADOS_POR_VERCEL)))


def se_sube(ruta: str) -> bool:
    return not (_por_vercel().ignorado(ruta) or _vercelignore().ignorado(ruta))


def _archivos_en_disco(raiz: Path = RAIZ) -> set[str]:
    """Lo que subiría `vercel deploy` desde `raiz` (podando lo ignorado)."""
    subidos: set[str] = set()
    for actual, dirs, archivos in os.walk(raiz):
        base = Path(actual).relative_to(raiz).as_posix()
        base = "" if base == "." else base + "/"
        dirs[:] = [
            d for d in dirs
            if not _por_vercel().dir_ignorado(base + d) and not _vercelignore().dir_ignorado(base + d)
        ]
        subidos.update(base + a for a in archivos if se_sube(base + a))
    return subidos


def _git_ls_files() -> list[str]:
    try:
        salida = subprocess.run(
            ["git", "ls-files", "-z"], cwd=RAIZ, capture_output=True, check=True, timeout=30
        ).stdout
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git no está disponible")
    return [r for r in salida.decode("utf-8").split("\0") if r]


# -- el emulador ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("reglas", "ruta", "ignorado"),
    [
        ("*.pem", "a/b/clave.pem", True),
        ("/x.txt", "x.txt", True),
        ("/x.txt", "a/x.txt", False),
        ("docs/", "docs/a.md", True),
        ("docs/", "docs", False),  # solo carpetas; un archivo "docs" no
        ("**/tests/", "proxy/tests/test_a.py", True),
        ("**/tests/", "tests/test_a.py", True),
        ("/*\n!/proxy", "proxy/app.py", False),
        ("/*\n!/proxy", "otro/app.py", True),
        ("/*\n!/proxy", "secreto.json", True),
        ("/a/*\n!/a/b", "a/b/c.json", False),
        ("/a/*\n!/a/b", "a/c.json", True),
        ("/a/\n!/a/b/c.json", "a/b/c.json", True),  # carpeta ignorada: no se re-incluye
        (".env.*", "proxy/.env.production", True),
        ("intencion/evaluar_*.py", "intencion/evaluar_casos.py", True),
        ("intencion/evaluar_*.py", "intencion/intake.py", False),
    ],
)
def test_el_emulador_sigue_la_semantica_de_gitignore(reglas, ruta, ignorado):
    assert Ignorados(leer_patrones(reglas)).ignorado(ruta) is ignorado


# ---------------------------------------------------------------------------
# .vercelignore
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ruta", [
    ".env", ".env.example", ".env.production", ".env.local", "proxy/.env", "guard/.env.vercel",
    ".temp-db/guard_signing_key.pem", ".temp-db/guard_api_token", ".temp-db/claves.json",
    ".temp-db/vercel/GATEWAY_CLAVES_JSON.json", ".temp-db/vercel/vercel.env",
    ".temp-db/vercel/clave_integrador_acme.txt", ".logs/guard_events.jsonl",
    ".logs/guard_events_privados.jsonl", "proxy/clave.pem", "guard/firma.key",
    "guard_api_token", "proxy/guard_api_token", ".claude/settings.local.json", ".mcp.json",
    ".vercel/project.json", "world/ap2/code/samples/certs/ds_cert_sdjwt.pem",
    # Cualquier archivo nuevo en la raíz (un volcado, un JSON de claves) no se sube.
    "secretos.json", "gateway_claves.json", "dump.sql",
])
def test_vercelignore_excluye_los_secretos(ruta):
    assert not se_sube(ruta), f"{ruta} se subiría a Vercel"


@pytest.mark.parametrize("ruta", [
    "world/run_world.sh", "world/ap2/pyproject.toml", "world/ap2/uv.lock",
    "world/ap2/.venv/Lib/site-packages/x.py", "dashboard/app.py", "deploy/start_all.sh",
    "deploy/vercel/local_multi.py", "docs/FLUJO.md", "results/benchmark.json",
    "fixtures/scenario_A.json", "fixtures/scenario_baseline.json", "fixtures/etapa2/computadoras.json",
    "fixtures/general/general.json", "fixtures/public_catalog.json", "fixtures/otra_cosa.json",
    "proxy/tests/test_app.py", "guard/tests/test_veredicto.py", "proxy/scripts/smoke_v1_pausas.py",
    "proxy/__pycache__/app.cpython-312.pyc", "proxy/app.pyc", ".pytest_cache/v/cache",
    ".ruff_cache/x", "node_modules/a/index.js", "README.md", "CLAUDE.md", "pytest.ini",
])
def test_vercelignore_excluye_la_simulacion_y_lo_de_desarrollo(ruta):
    assert not se_sube(ruta), f"{ruta} se subiría a Vercel"


def _python_del_motor() -> list[str]:
    rutas = []
    for paquete in MOTOR:
        for archivo in (RAIZ / paquete).rglob("*.py"):
            relativa = archivo.relative_to(RAIZ).as_posix()
            if "/tests/" in relativa or relativa.startswith("proxy/scripts/") or "__pycache__" in relativa:
                continue
            rutas.append(relativa)
    return sorted(rutas)


def test_vercelignore_deja_el_motor_entero():
    rutas = _python_del_motor()
    assert len(rutas) > 50
    faltan = [r for r in rutas if not se_sube(r)]
    assert not faltan, f"el deploy no subiría estos módulos del motor: {faltan}"
    for paquete in MOTOR:
        assert se_sube(f"{paquete}/__init__.py")


def test_vercelignore_deja_los_datos_de_runtime():
    for ruta in DATOS_RUNTIME:
        assert (RAIZ / ruta).exists(), ruta
        assert se_sube(ruta), f"{ruta} no se subiría"
    fabricante = sorted((RAIZ / "fixtures" / "fabricante").glob("*.json"))
    assert fabricante, "no hay fichas del fabricante"
    assert all(se_sube(p.relative_to(RAIZ).as_posix()) for p in fabricante)


@pytest.mark.parametrize("ruta", RAIZ_DEL_BUILD)
def test_vercelignore_deja_lo_que_lee_el_build(ruta):
    assert se_sube(ruta), f"{ruta} no se subiría y el build lo necesita"


def _secreto(ruta: str) -> str | None:
    nombre = ruta.rsplit("/", 1)[-1]
    return next((d for d, rx in SECRETOS if rx.search(nombre)), None)


def test_lo_que_se_sube_de_git_no_tiene_secretos():
    subidos = [r for r in _git_ls_files() if se_sube(r)]
    assert subidos
    con_secreto = [(r, _secreto(r)) for r in subidos if _secreto(r)]
    assert not con_secreto, f"se subirían archivos con forma de secreto: {con_secreto}"
    # Nada de la simulación entre lo que se sube.
    simulacion = [r for r in subidos if r.split("/")[0] not in (*MOTOR, "fixtures") and
                  r not in RAIZ_DEL_BUILD and r != "requirements.txt"]
    assert not simulacion, f"se subiría algo que no es el motor: {simulacion}"
    fixtures = [r for r in subidos if r.startswith("fixtures/")]
    assert all(r.startswith("fixtures/fabricante/") for r in fixtures)


def test_lo_que_se_sube_desde_el_disco_no_tiene_secretos():
    """Lo que de verdad subiría `vercel deploy` desde esta carpeta, con el
    `.env`, `.temp-db/` y `.logs/` que haya en disco."""
    subidos = _archivos_en_disco()
    assert any(r.startswith("proxy/") for r in subidos)
    con_secreto = sorted((r, _secreto(r)) for r in subidos if _secreto(r))
    assert not con_secreto, f"se subirían archivos con forma de secreto: {con_secreto}"
    fuera = sorted(r for r in subidos if r.split("/")[0] not in (*MOTOR, "fixtures")
                   and r not in RAIZ_DEL_BUILD and r != "requirements.txt")
    assert not fuera, f"se subiría algo que no es el motor: {fuera}"


def test_desde_una_carpeta_con_secretos_solo_sube_el_motor(tmp_path):
    """Una copia de trabajo como la del equipo (con `.env`, la clave del
    recibo, el token y los logs en disco): solo sube el motor."""
    for ruta in (
        ".env", ".env.example", ".temp-db/guard_signing_key.pem", ".temp-db/guard_api_token",
        ".temp-db/vercel/vercel.env", ".logs/guard_events.jsonl", ".claude/settings.local.json",
        ".mcp.json", "README.md", "secretos.json", "proxy/.env", "proxy/app.py",
        "proxy/__pycache__/app.cpython-312.pyc", "proxy/tests/test_app.py",
        "proxy/scripts/smoke_v1_pausas.py", "world/ap2/.venv/Lib/x.py", "world/run_world.sh",
        "dashboard/app.py", "fixtures/fabricante/2001234000017.json", "fixtures/scenario_A.json",
        "fixtures/general/general.json", "taxonomia/datos/t.txt", "index.py", "vercel.json",
        "pyproject.toml", ".python-version", ".git",
    ):
        archivo = tmp_path / ruta
        archivo.parent.mkdir(parents=True, exist_ok=True)
        archivo.write_text("x", encoding="utf-8")
    assert _archivos_en_disco(tmp_path) == {
        "proxy/app.py", "fixtures/fabricante/2001234000017.json", "taxonomia/datos/t.txt",
        "index.py", "vercel.json", "pyproject.toml", ".python-version",
    }


# ---------------------------------------------------------------------------
# vercel.json
# ---------------------------------------------------------------------------


def _vercel_json() -> dict:
    return json.loads(VERCEL_JSON.read_text(encoding="utf-8"))


def _expandir_llaves(glob: str) -> list[str]:
    """`{a,b}` de un nivel (lo que usa excludeFiles), como node-glob."""
    m = re.fullmatch(r"\{(.*)\}", glob)
    return [p.strip() for p in m.group(1).split(",")] if m else [glob]


def _excluidos_por_funcion() -> list[re.Pattern[str]]:
    funcion = _vercel_json()["functions"]["index.py"]
    return [re.compile(_glob_a_regex(g) + r"\Z") for g in _expandir_llaves(funcion["excludeFiles"])]


def _excluido(ruta: str) -> bool:
    return any(rx.match(ruta) for rx in _excluidos_por_funcion())


def test_vercel_json_es_valido_y_respeta_hobby():
    datos = _vercel_json()
    assert datos.get("$schema") == "https://openapi.vercel.sh/vercel.json"
    assert set(datos["functions"]) == {"index.py"}
    funcion = datos["functions"]["index.py"]
    duracion = funcion["maxDuration"]
    assert isinstance(duracion, int) and not isinstance(duracion, bool)
    assert 1 <= duracion <= 300
    assert isinstance(funcion["excludeFiles"], str)
    # Sin región fija (se elige según Supabase, ver deploy/vercel/README.md) y
    # sin la configuración vieja, que no convive con `functions`.
    texto = VERCEL_JSON.read_text(encoding="utf-8")
    assert "regions" not in texto
    assert not {"builds", "routes"} & set(datos)


def test_exclude_files_entra_en_el_limite_de_vercel():
    # El esquema de vercel.json rechaza un excludeFiles de más de 256
    # caracteres ("Schema verification failed" en el deploy).
    assert len(_vercel_json()["functions"]["index.py"]["excludeFiles"]) <= 256


def test_ni_la_simulacion_ni_lo_que_no_es_runtime_llegan_al_bundle():
    """Lo saca `.vercelignore` (no se sube) o, si se sube, `excludeFiles`."""
    for ruta in ("world/x.py", "dashboard/app.py", "deploy/start_all.sh", "docs/FLUJO.md",
                 "results/a.json", "fixtures/scenario_B.json", "fixtures/etapa2/a.json",
                 "fixtures/public_catalog.json", "proxy/tests/test_app.py",
                 "proxy/scripts/smoke_v1_general.py", "proxy/demo_local.py",
                 "proxy/subir_benchmark.py", "guard/benchmark_hook.py", "filtro/casos.py",
                 "filtro/evaluar.py", "intencion/evaluar_casos.py", ".temp-db/x.pem",
                 ".logs/a.jsonl", "proxy/__pycache__/app.cpython-312.pyc",
                 "proxy/sql/003_gateway.sql", "proxy/README.md"):
        assert not se_sube(ruta) or _excluido(ruta), f"{ruta} llega al bundle"


def test_exclude_files_no_saca_el_motor_ni_sus_datos():
    for ruta in DATOS_RUNTIME:
        assert not _excluido(ruta), ruta
    assert not _excluido("fixtures/fabricante/2001234000017.json")
    excluidos = [r for r in _python_del_motor() if _excluido(r)]
    no_runtime = [r for r in excluidos if not any(fnmatch.fnmatch(r, p) for p in NO_RUNTIME)]
    assert not no_runtime, f"excludeFiles saca módulos del motor: {no_runtime}"
    for ruta in ("index.py", "proxy/vercel.py", "proxy/app.py", "proxy/arranque.py",
                 "guard/__init__.py", "taxonomia/cargar.py", "fabricante/fixture.py"):
        assert not _excluido(ruta), ruta


def test_los_estaticos_de_la_consola_entran_en_el_bundle():
    """La consola web (`proxy/consola/`, `/consola` en el motor): el HTML, el
    JS y el CSS se suben y `excludeFiles` no los saca; sus tests no."""
    carpeta = RAIZ / "proxy" / "consola"
    estaticos = sorted(
        p.relative_to(RAIZ).as_posix() for p in carpeta.iterdir()
        if p.is_file() and p.suffix in (".html", ".js", ".mjs", ".css")
    )
    assert set(estaticos) == {
        "proxy/consola/index.html", "proxy/consola/app.js", "proxy/consola/logica.mjs",
        "proxy/consola/estilo.css",
    }
    for ruta in estaticos:
        assert se_sube(ruta), f"{ruta} no se subiría"
        assert not _excluido(ruta), f"excludeFiles saca {ruta}"
    for ruta in ("proxy/tests/js/consola.test.mjs", "proxy/tests/js/verificar_recibo_cli.mjs"):
        assert not se_sube(ruta) or _excluido(ruta), f"{ruta} llega al bundle"


def _motor_de_pyproject() -> dict | None:
    pyproject = RAIZ / "pyproject.toml"
    if not pyproject.exists():
        return None
    datos = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return datos.get("tool", {}).get("kaiai", {}).get("motor")


def test_la_frontera_del_motor_de_pyproject_entra_en_el_bundle():
    """Con `[tool.kaiai.motor]` (lote A): todo lo de `paquetes` que no está en
    `excluidos` se sube y no lo saca `excludeFiles`."""
    motor = _motor_de_pyproject()
    if motor is None:
        pytest.skip("todavía no hay [tool.kaiai.motor] en pyproject.toml (lote A)")
    paquetes = motor.get("paquetes")
    assert isinstance(paquetes, list) and paquetes, "[tool.kaiai.motor] sin 'paquetes'"
    excluidos = [str(e) for e in motor.get("excluidos", [])]
    faltan = []
    for paquete in paquetes:
        for archivo in (RAIZ / paquete).rglob("*.py"):
            ruta = archivo.relative_to(RAIZ).as_posix()
            if "/tests/" in ruta or "__pycache__" in ruta:
                continue
            if any(fnmatch.fnmatch(ruta, e) or ruta.startswith(e.rstrip("/") + "/")
                   or ruta == e for e in excluidos):
                continue
            if not se_sube(ruta) or _excluido(ruta):
                faltan.append(ruta)
    assert not faltan, f"el bundle no tendría estos módulos del motor: {faltan}"


def _modulo_a_archivo(nombre: str) -> Path | None:
    base = RAIZ.joinpath(*nombre.split("."))
    for candidato in (base.with_suffix(".py"), base / "__init__.py"):
        if candidato.exists():
            return candidato
    return None


def _imports_en_runtime(archivo: Path, nombre: str) -> set[str]:
    """Módulos que importa (también dentro de funciones), sin los bloques
    `if TYPE_CHECKING:`."""
    arbol = ast.parse(archivo.read_text(encoding="utf-8"))
    paquete = nombre if archivo.name == "__init__.py" else nombre.rpartition(".")[0]
    nombres: set[str] = set()

    def visitar(nodo: ast.AST) -> None:
        for hijo in ast.iter_child_nodes(nodo):
            if isinstance(hijo, ast.If) and "TYPE_CHECKING" in ast.unparse(hijo.test):
                for sub in hijo.orelse:
                    visitar(sub)
                continue
            if isinstance(hijo, ast.Import):
                nombres.update(a.name for a in hijo.names)
            elif isinstance(hijo, ast.ImportFrom):
                if hijo.level:
                    base = paquete.split(".")
                    base = base[: len(base) - hijo.level + 1]
                    modulo = ".".join([*base, *([hijo.module] if hijo.module else [])])
                else:
                    modulo = hijo.module or ""
                nombres.add(modulo)
                nombres.update(f"{modulo}.{a.name}" for a in hijo.names)
            visitar(hijo)

    visitar(arbol)
    return nombres


def test_lo_que_importa_index_py_entra_en_el_bundle():
    """Desde `index.py` (lote D), todo módulo del repo que se importa en
    runtime (transitivo, también dentro de funciones) se sube y no lo saca
    `excludeFiles`."""
    entrada = RAIZ / "index.py"
    if not entrada.exists():
        pytest.skip("todavía no hay index.py (lote D)")
    vistos: dict[str, Path] = {}
    pendientes = [("index", entrada)]
    while pendientes:
        nombre, archivo = pendientes.pop()
        if nombre in vistos:
            continue
        vistos[nombre] = archivo
        for importado in _imports_en_runtime(archivo, nombre):
            partes = importado.split(".")
            for i in range(1, len(partes) + 1):  # también los paquetes padre
                sub = ".".join(partes[:i])
                destino = _modulo_a_archivo(sub)
                if destino is not None and sub not in vistos:
                    pendientes.append((sub, destino))
    rutas = sorted({p.relative_to(RAIZ).as_posix() for p in vistos.values()})
    afuera = [r for r in rutas if not se_sube(r) or _excluido(r)]
    assert not afuera, f"index.py importa módulos que no estarían en el bundle: {afuera}"


# ---------------------------------------------------------------------------
# los scripts
# ---------------------------------------------------------------------------


@lru_cache(maxsize=None)
def _script(nombre: str) -> ModuleType:
    ruta = DEPLOY_VERCEL / f"{nombre}.py"
    spec = importlib.util.spec_from_file_location(f"_deploy_vercel_{nombre}", ruta)
    assert spec and spec.loader
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modulo  # dataclasses lo busca ahí
    spec.loader.exec_module(modulo)
    return modulo


@pytest.mark.parametrize("ruta", [
    "deploy/vercel/local_multi.py", "deploy/vercel/tunel_router.py",
    "deploy/vercel/verificar_supabase.py", "deploy/vercel/generar_secretos.py",
    "deploy/vercel/postgrest_local.py", "proxy/scripts/smoke_v1_pausas.py",
])
def test_cada_script_tiene_ayuda(ruta):
    """`python <ruta> --help` desde la raíz, como dice el README."""
    resultado = subprocess.run(
        [sys.executable, ruta, "--help"], cwd=RAIZ, capture_output=True, text=True,
        encoding="utf-8", timeout=60, env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert resultado.returncode == 0, resultado.stderr
    assert "usage" in resultado.stdout


# -- postgrest_local: la base compartida sin Supabase ---------------------------------


@pytest.mark.anyio
async def test_postgrest_local_comparte_la_base_entre_instancias():
    """Dos `EstadoSupabase` (dos instancias) contra el mismo PostgREST falso
    por HTTP: lo que escribe una lo lee la otra, y los índices únicos deciden
    igual que en la base (gana el primero que ata el contexto)."""
    from proxy.estado import EstadoSupabase
    from proxy.tests.postgrest_falso import BaseFalsa
    from proxy.tests.test_estado import CMP1, fila_compra

    servidor = _script("postgrest_local").PostgrestLocal(BaseFalsa())

    def instancia() -> EstadoSupabase:
        return EstadoSupabase(
            "http://127.0.0.1:54321", "local-sin-supabase",
            transport=httpx.ASGITransport(app=servidor),
        )

    a, b = instancia(), instancia()
    otra = "cmp-00000000000000ff"
    await a.guardar_compra(fila_compra(CMP1))
    await b.guardar_compra(fila_compra(otra))
    assert (await b.cargar_compra(CMP1))["id"] == CMP1
    assert await a.atar_contexto("ctx-1", CMP1, "merchant_1") == CMP1
    assert await b.atar_contexto("ctx-1", otra, "merchant_1") == CMP1  # gana el primero
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=servidor), base_url="http://127.0.0.1:54321"
    ) as c:
        tablas = (await c.get("/__tablas")).json()
        otra_ruta = await c.get("/no-existe")
    assert tablas["gw_compras"] == 2 and tablas["gw_contextos"] == 1
    assert otra_ruta.status_code == 404
    await a.cerrar()
    await b.cerrar()


def test_postgrest_local_solo_escucha_en_loopback(capsys):
    assert _script("postgrest_local").main(["--host", "0.0.0.0"]) == 2
    assert "loopback" in capsys.readouterr().err


# -- local_multi: entorno -----------------------------------------------------------


def test_leer_env_entiende_el_formato_y_los_vacios(tmp_path):
    archivo = tmp_path / "a.env"
    archivo.write_text(
        "# comentario\nA=1\nexport B = dos \nC='con espacios'\nD=\"x=y\"\nE=\n\n"
        'J={"integradores":[{"id":"acme"}]}\n',
        encoding="utf-8",
    )
    leidas = _script("local_multi").leer_env(archivo)
    assert leidas == {"A": "1", "B": "dos", "C": "con espacios", "D": "x=y",
                      "J": '{"integradores":[{"id":"acme"}]}'}


def test_leer_env_no_muestra_el_valor_de_una_linea_mala(tmp_path):
    archivo = tmp_path / "a.env"
    archivo.write_text("sb_secret_no_tiene_igual\n", encoding="utf-8")
    with pytest.raises(ValueError) as error:
        _script("local_multi").leer_env(archivo)
    assert "sb_secret" not in str(error.value) and ":1:" in str(error.value)


def test_el_entorno_de_las_instancias_es_limpio_salvo_que_se_pida(tmp_path):
    lm = _script("local_multi")
    archivo = tmp_path / "a.env"
    archivo.write_text("GATEWAY_ESTADO=supabase\n", encoding="utf-8")
    base = {"PATH": "/bin", "SYSTEMROOT": "C:\\Windows", "GOOGLE_API_KEY": "de-mi-shell"}
    limpio = lm.armar_entorno([archivo], heredar=False, base=base)
    assert "GOOGLE_API_KEY" not in limpio
    assert limpio["PATH"] == "/bin" and limpio["GATEWAY_ESTADO"] == "supabase"
    assert limpio["PYTHONPATH"].split(os.pathsep)[0] == str(RAIZ)
    heredado = lm.armar_entorno([archivo], heredar=True, base=base)
    assert heredado["GOOGLE_API_KEY"] == "de-mi-shell"


def test_local_multi_busca_la_app_en_la_raiz_y_en_el_pythonpath(tmp_path):
    lm = _script("local_multi")
    (tmp_path / "otra_app.py").write_text("app = None\n", encoding="utf-8")
    entorno = {"PYTHONPATH": os.pathsep.join([str(RAIZ), str(tmp_path)])}
    assert lm.encontrar_app("otra_app:app", entorno)
    assert lm.encontrar_app("proxy.app:app", entorno)  # un módulo dentro de un paquete
    assert not lm.encontrar_app("no_existe:app", entorno)
    # Las instancias salen si se cierra su stdin (el balanceador murió).
    assert "sys.stdin.read()" in lm._ARRANQUE and "os._exit" in lm._ARRANQUE


def test_env_ejemplo_sirve_como_env_y_no_tiene_secretos():
    texto = (DEPLOY_VERCEL / "env.ejemplo").read_text(encoding="utf-8")
    variables = _script("local_multi").leer_env(DEPLOY_VERCEL / "env.ejemplo")
    assert variables["GATEWAY_ESTADO"] == "supabase"
    assert variables["GUARD_FILTRO"] == "filtro:filtrar"
    for obligatoria in ("GATEWAY_PUBLIC_URL", "GATEWAY_CLAVES_JSON", "GATEWAY_COMERCIOS_JSON",
                        "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "GUARD_SIGNING_KEY_PEM_B64"):
        assert re.search(rf"^{obligatoria}=", texto, re.M), obligatoria
    for prohibida in ("PROXY_ALLOW_STUB", "PROXY_PERMITIR_SIN_FILTRO", "PROXY_FICHA_DEMO",
                      "CATALOGO_RESPALDO_FIXTURE", "GATEWAY_WEBHOOKS_INSEGUROS"):
        lineas = [linea for linea in texto.splitlines() if prohibida in linea]
        assert lineas, f"{prohibida} no está marcada"
        assert all(linea.startswith("#") and "PROHIBIDA" in linea for linea in lineas), prohibida
        assert prohibida not in variables
    for patron in (r"psg_[0-9a-f]{12}_\S+", r"whsec_\S{16,}", r"-----BEGIN", r"eyJ[\w-]{10,}\.",
                   r"sb_secret_\w{8,}", r"AIza[\w-]{20,}", r"sk-[\w-]{20,}"):
        assert not re.search(patron, texto), patron


def test_comercios_ejemplo_es_un_registro_valido():
    from proxy.comercios import RegistroComercios

    texto = (DEPLOY_VERCEL / "comercios.ejemplo.json").read_text(encoding="utf-8")
    registro = RegistroComercios.desde_dict(json.loads(texto.replace("<tu-dominio>", "ejemplo")))
    comercio = registro.get("merchant_1")
    assert comercio is not None
    assert comercio.a2a_url == "https://ejemplo.ngrok-free.app/a2a/merchant_agent"
    assert comercio.catalogo_url == "https://ejemplo.ngrok-free.app"
    assert tuple(comercio.merchant_ids) == ("merchant_1",)


# -- local_multi: balanceador ----------------------------------------------------------


class _Flujo(httpx.AsyncByteStream):
    """Un cuerpo que llega en streaming, como el de un servidor de verdad."""

    def __init__(self, *partes: bytes) -> None:
        self.partes = partes

    async def __aiter__(self):
        for parte in self.partes:
            yield parte


def _respuesta(datos: dict) -> httpx.Response:
    cuerpo = json.dumps(datos).encode()
    return httpx.Response(200, headers={"content-type": "application/json"},
                          stream=_Flujo(cuerpo[:5], cuerpo[5:]))


class _Instancias:
    """Instancias falsas: contestan su número y anotan lo que reciben."""

    def __init__(self, *, lentas: frozenset[int] = frozenset(), caidas: frozenset[int] = frozenset()):
        self.lentas = lentas
        self.caidas = caidas
        self.recibidos: list[httpx.Request] = []

    async def __call__(self, pedido: httpx.Request) -> httpx.Response:
        numero = pedido.url.port - 8040
        if numero in self.caidas:
            raise httpx.ConnectError("conexión rechazada", request=pedido)
        self.recibidos.append(pedido)
        if numero in self.lentas:
            await asyncio.sleep(2)
        return _respuesta({"instancia": numero, "ruta": pedido.url.path})


def _balanceador(instancias: _Instancias, n: int = 2, **kwargs):
    lm = _script("local_multi")
    lista = [lm.Instancia(numero=i, puerto=8040 + i) for i in range(1, n + 1)]
    return lm.Balanceador(lista, transporte=httpx.MockTransport(instancias), log=lambda _m: None,
                          **kwargs)


def _cliente(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8040")


@pytest.mark.anyio
async def test_el_balanceador_reparte_por_pedido_aun_en_la_misma_conexion():
    instancias = _Instancias()
    app = _balanceador(instancias, n=3)
    async with _cliente(app) as http:
        vistos = [int((await http.get("/v1/compras")).headers["x-balanceador-instancia"])
                  for _ in range(6)]
    assert vistos == [1, 2, 3, 1, 2, 3]


@pytest.mark.anyio
async def test_el_balanceador_no_reenvia_sus_headers_y_pone_los_de_vercel():
    instancias = _Instancias()
    app = _balanceador(instancias)
    async with _cliente(app) as http:
        r = await http.post("/a2a/m/merchant_1", content=b"{}", headers={
            "X-Balanceador-Pedido": "p-1", "X-Balanceador-Evitar": "1",
            "Authorization": "Bearer psg_x", "X-Forwarded-For": "6.6.6.6",
        })
        assert r.headers["x-balanceador-instancia"] == "2"  # evitó la 1
        estado = (await http.get("/__balanceador/pedido/p-1")).json()
    assert estado["instancia"] == 2 and estado["en_curso"] is False
    recibido = instancias.recibidos[-1]
    assert "x-balanceador-pedido" not in recibido.headers
    assert "x-balanceador-evitar" not in recibido.headers
    assert recibido.headers["authorization"] == "Bearer psg_x"  # la clave sí llega al motor
    assert recibido.headers["x-forwarded-for"] == "127.0.0.1"  # no el que mandó el cliente
    assert recibido.headers["host"] == "127.0.0.1:8040"  # el Host público, como en Vercel


@pytest.mark.anyio
async def test_el_balanceador_corta_a_los_300_s_como_vercel():
    app = _balanceador(_Instancias(lentas=frozenset({1})), limite_s=0.2)
    async with _cliente(app) as http:
        r = await http.get("/v1/compras/cmp-1")
    assert r.status_code == 504
    assert r.headers["x-vercel-error"] == "FUNCTION_INVOCATION_TIMEOUT"
    assert r.headers["x-balanceador-instancia"] == "1"


@pytest.mark.anyio
async def test_el_balanceador_rechaza_cuerpos_de_mas_de_4_5_mb():
    app = _balanceador(_Instancias(), max_cuerpo=10)
    async with _cliente(app) as http:
        r = await http.post("/v1/compras", content=b"x" * 11)
    assert r.status_code == 413
    assert r.headers["x-vercel-error"] == "FUNCTION_PAYLOAD_TOO_LARGE"


@pytest.mark.anyio
async def test_el_balanceador_saltea_instancias_muertas_o_que_no_atienden():
    instancias = _Instancias(caidas=frozenset({1}))
    app = _balanceador(instancias, n=3)
    async with _cliente(app) as http:
        assert (await http.get("/x")).headers["x-balanceador-instancia"] == "2"  # la 1 no atiende
        assert (await http.post("/__balanceador/matar/3")).json() == {"matada": 3}
        vistos = {(await http.get("/x")).headers["x-balanceador-instancia"] for _ in range(4)}
        estado = (await http.get("/__balanceador/estado")).json()
    assert vistos == {"2"}
    assert [i["viva"] for i in estado["instancias"]] == [True, True, False]


@pytest.mark.anyio
async def test_sin_instancias_vivas_el_balanceador_da_503():
    app = _balanceador(_Instancias(caidas=frozenset({1, 2})))
    async with _cliente(app) as http:
        r = await http.get("/v1/compras")
    assert r.status_code == 503


# -- tunel_router ------------------------------------------------------------------------


def test_el_tunel_manda_a2a_al_comercio_y_el_resto_al_catalogo():
    tr = _script("tunel_router")
    kw = {"comercio": "C", "catalogo": "K"}
    assert tr.destino("/a2a/merchant_agent", prefijo=None, **kw) == ("C", "/a2a/merchant_agent")
    assert tr.destino("/a2a/merchant_agent/.well-known/agent-card.json", prefijo=None, **kw)[0] == "C"
    assert tr.destino("/products/SKU-1", prefijo=None, **kw) == ("K", "/products/SKU-1")
    assert tr.destino("/a2ax", prefijo=None, **kw) == ("K", "/a2ax")
    assert tr.destino("/s3cr3t-pref/a2a/x", prefijo="s3cr3t-pref", **kw) == ("C", "/a2a/x")
    assert tr.destino("/s3cr3t-pref/products", prefijo="s3cr3t-pref", **kw) == ("K", "/products")
    assert tr.destino("/a2a/x", prefijo="s3cr3t-pref", **kw) is None
    assert tr.destino("/s3cr3t-prefijo-otro/a2a/x", prefijo="s3cr3t-pref", **kw) is None


@pytest.mark.anyio
async def test_el_tunel_reenvia_con_el_host_del_upstream_y_filtra_metodos():
    tr = _script("tunel_router")
    recibidos: list[httpx.Request] = []

    def upstream(pedido: httpx.Request) -> httpx.Response:
        recibidos.append(pedido)
        return _respuesta({"ok": True})

    app = tr.Enrutador(prefijo="s3cr3t-pref", transporte=httpx.MockTransport(upstream))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="https://mi.ngrok-free.app") as http:
        assert (await http.post("/s3cr3t-pref/a2a/merchant_agent", content=b"{}")).status_code == 200
        assert (await http.get("/s3cr3t-pref/products/A?x=1")).status_code == 200
        assert (await http.get("/products/A")).status_code == 404  # sin el prefijo
        assert (await http.delete("/s3cr3t-pref/products/A")).status_code == 405
    assert str(recibidos[0].url) == "http://127.0.0.1:8001/a2a/merchant_agent"
    assert recibidos[0].headers["host"] == "127.0.0.1:8001"
    assert recibidos[0].headers["x-forwarded-host"] == "mi.ngrok-free.app"
    assert str(recibidos[1].url) == "http://127.0.0.1:8011/products/A?x=1"


# -- generar_secretos ------------------------------------------------------------------


def test_generar_secretos_escribe_todo_y_no_imprime_nada_secreto(tmp_path, capsys):
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    from proxy.claves import RegistroIntegradores
    from proxy.comercios import RegistroComercios

    gs = _script("generar_secretos")
    carpeta = tmp_path / "vercel"
    codigo = gs.main(["--carpeta", str(carpeta), "--integrador", "acme",
                      "--dominio", "mi-kaiai.ngrok-free.app",
                      "--webhook-url", "https://acme.example/hook"])
    salida = capsys.readouterr()
    assert codigo == 0

    leer = lambda nombre: (carpeta / nombre).read_text(encoding="utf-8")  # noqa: E731
    pem = (carpeta / "guard_signing_key.pem").read_bytes()
    clave = load_pem_private_key(pem, password=None)
    assert isinstance(clave, ec.EllipticCurvePrivateKey) and isinstance(clave.curve, ec.SECP256R1)
    b64 = leer("GUARD_SIGNING_KEY_PEM_B64.txt")
    assert base64.b64decode(b64) == pem and "\n" not in b64
    assert leer("GUARD_SIGNING_KID.txt") == "guard-key-1"
    jwk = json.loads(leer("guard_public_jwk.json"))
    assert jwk["kid"] == "guard-key-1" and "d" not in jwk

    clave_api = leer("clave_integrador_acme.txt")
    secreto_webhook = leer("WEBHOOK_SECRETO_ACME.txt")
    integradores = RegistroIntegradores.desde_dict(
        json.loads(leer("GATEWAY_CLAVES_JSON.json")), {"WEBHOOK_SECRETO_ACME": secreto_webhook}
    )
    integrador = integradores.autenticar(clave_api)
    assert integrador is not None and integrador.id == "acme"
    assert integrador.webhook is not None

    prefijo = leer("TUNEL_PREFIJO.txt")
    comercios = RegistroComercios.desde_dict(json.loads(leer("GATEWAY_COMERCIOS_JSON.json")))
    comercio = comercios.get("merchant_1")
    assert comercio.a2a_url == f"https://mi-kaiai.ngrok-free.app/{prefijo}/a2a/merchant_agent"
    assert comercio.catalogo_url == f"https://mi-kaiai.ngrok-free.app/{prefijo}"

    entorno = _script("local_multi").leer_env(carpeta / "vercel.env")
    assert entorno["GUARD_SIGNING_KEY_PEM_B64"] == b64
    assert json.loads(entorno["GATEWAY_CLAVES_JSON"]) == json.loads(leer("GATEWAY_CLAVES_JSON.json"))
    assert entorno["WEBHOOK_SECRETO_ACME"] == secreto_webhook
    assert clave_api not in (carpeta / "vercel.env").read_text(encoding="utf-8")  # es del cliente

    impreso = salida.out + salida.err
    # psg_<kid>_<secreto>: el kid es hex, pero el secreto (token_urlsafe) puede
    # traer "_": se parte por el kid, no por el último "_" (que a veces dejaba
    # un pedacito de 1-3 caracteres que aparecía por casualidad en la salida).
    secreto_api = clave_api.split("_", 2)[2]
    for secreto in (clave_api, secreto_api, b64[:40], secreto_webhook, prefijo,
                    "BEGIN PRIVATE KEY"):
        assert secreto not in impreso


def test_generar_secretos_no_pisa_lo_que_ya_existe(tmp_path, capsys):
    gs = _script("generar_secretos")
    carpeta = tmp_path / "vercel"
    assert gs.main(["--carpeta", str(carpeta)]) == 0
    antes = (carpeta / "guard_signing_key.pem").read_bytes()
    assert gs.main(["--carpeta", str(carpeta)]) == 1
    assert (carpeta / "guard_signing_key.pem").read_bytes() == antes
    assert gs.main(["--carpeta", str(carpeta), "--forzar"]) == 0
    assert (carpeta / "guard_signing_key.pem").read_bytes() != antes
    capsys.readouterr()


# -- verificar_supabase ------------------------------------------------------------------


def test_las_migraciones_se_leen_bien():
    vs = _script("verificar_supabase")
    esperado = vs.esperado()
    assert esperado.tablas["gw_aprobaciones"][:3] == ["id", "contexto_id", "cart_id"]
    assert "primary" not in esperado.tablas["gw_ultimos_veredictos"]
    assert "espera_id" in esperado.tablas["gw_compras"]  # 004
    assert {"gw_esperas", "gw_eventos"} <= set(esperado.tablas)
    sql = "create table if not exists public.t (a text check (a in ('x,y')), b int, unique (a, b));" \
          "alter table public.t add column if not exists c jsonb, add column d text;"
    assert vs.columnas_de_sql(sql) == {"t": ["a", "b", "c", "d"]}


def _postgrest(tablas: dict[str, set[str]], *, anon_lee: bool = False):
    """PostgREST falso: 404 sin la tabla, 400 42703 si falta una columna."""
    def manejar(pedido: httpx.Request) -> httpx.Response:
        tabla = pedido.url.path.rsplit("/", 1)[-1]
        if pedido.headers["apikey"] == "anon":
            return httpx.Response(200 if anon_lee else 401, json=[] if anon_lee else {"code": "42501"})
        assert pedido.method == "GET"  # solo lectura
        if tabla not in tablas:
            return httpx.Response(404, json={"code": "PGRST205"})
        pedidas = set(pedido.url.params["select"].split(","))
        if pedidas - tablas[tabla]:
            return httpx.Response(400, json={"code": "42703"})
        return httpx.Response(200, json=[])
    return httpx.MockTransport(manejar)


def test_verificar_supabase_dice_que_falta_sin_mostrar_la_clave():
    vs = _script("verificar_supabase")
    completas = {t: set(c) for t, c in vs.esperado().tablas.items()}
    env = {"SUPABASE_URL": "https://abc.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": "sb_secret_NO_MOSTRAR"}

    lineas: list[str] = []
    assert vs.verificar(env, salida=lineas.append, transporte=_postgrest(completas)) == 0
    assert any("Todo en orden" in linea for linea in lineas)

    incompletas = {t: c for t, c in completas.items() if t != "gw_esperas"}
    incompletas["gw_compras"] = completas["gw_compras"] - {"espera_id"}
    lineas = []
    assert vs.verificar(env, salida=lineas.append, transporte=_postgrest(incompletas)) == 1
    texto = "\n".join(lineas)
    assert "FALTA la tabla gw_esperas" in texto
    assert "FALTAN columnas en gw_compras" in texto and "espera_id" in texto
    assert "sb_secret_NO_MOSTRAR" not in texto

    lineas = []
    con_anon = {**env, "SUPABASE_ANON_KEY": "anon"}
    assert vs.verificar(con_anon, salida=lineas.append, transporte=_postgrest(completas, anon_lee=True)) == 1
    assert any("clave pública puede leerla" in linea for linea in lineas)
    assert vs.verificar({}, salida=lambda _l: None) == 2


# -- smoke_v1_pausas ---------------------------------------------------------------------


def test_smoke_pausas_arma_la_ficha_con_el_proximo_lunes():
    from proxy.scripts.smoke_v1_pausas import ficha_de, leer_args, proximo_lunes

    assert proximo_lunes(date(2026, 10, 19)) == date(2026, 10, 26)  # dicho un lunes: el siguiente
    assert proximo_lunes(date(2026, 10, 18)) == date(2026, 10, 19)
    args = leer_args([], hoy=date(2026, 10, 18))
    assert args.caso == "aceptar" and args.esperado == "firma"
    requisitos = {r["campo"]: r for r in ficha_de(args)["requisitos"]}
    assert requisitos["entregas"]["valor"] == [{"cantidad": 20, "fecha": "2026-10-19"}]
    assert requisitos["precio"]["operador"] == "maximo" and requisitos["moneda"]["valor"] == "USD"
    assert "entregas" not in {r["campo"] for r in ficha_de(leer_args(["--plazo", ""]))["requisitos"]}
    with pytest.raises(SystemExit):
        leer_args(["--caso", "no-existe"])


@pytest.mark.anyio
async def test_smoke_pausas_sin_clave_no_arranca(monkeypatch):
    from proxy.scripts.smoke_v1_pausas import correr, leer_args

    monkeypatch.delenv("KAIAI_CLAVE", raising=False)
    lineas: list[str] = []
    assert await correr(leer_args([]), salida=lineas.append) == 1
    assert any("KAIAI_CLAVE" in linea for linea in lineas)
