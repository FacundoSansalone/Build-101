"""La demo siempre prendida en Railway (`deploy/railway/`).

Un solo contenedor con todo adentro (`deploy/start_all.sh`) y una sola puerta
pública: el enrutador de `deploy/vercel/tunel_router.py` en `$PORT`.

- El enrutador: el respaldo (`--resto`) para lo que no lleva el prefijo
  secreto (el panel), con el Host público y en streaming (SSE); la ruta de
  salud; el puerto de `PORT`. Sin `--resto`, igual que antes.
- `.dockerignore`: emulado con la semántica de Docker (no la de .gitignore:
  un patrón sin `**/` no baja a las subcarpetas) sobre casos fijos, sobre
  `git ls-files` y sobre lo que hay en disco.
- `generar_secretos.py --apuntar` y los scripts que apuntan el motor de
  Vercel al comercio fijo, con un `npx` falso.
- Lo que no se puede probar sin Docker ni Railway (Dockerfile, railway.json,
  arrancar.sh): chequeos estáticos, `bash -n` y la validación de variables.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import ModuleType

import httpx
import pytest

RAIZ = Path(__file__).resolve().parents[2]
DEPLOY = RAIZ / "deploy"
RAILWAY = DEPLOY / "railway"
DOCKERIGNORE = RAIZ / ".dockerignore"
PREFIJO = "s3cr3t-prefijo-123"
PUBLICO = "demo-kaiai.up.railway.app"


@lru_cache(maxsize=None)
def _script(ruta: str) -> ModuleType:
    archivo = RAIZ / ruta
    nombre = "_railway_" + re.sub(r"\W", "_", ruta)
    spec = importlib.util.spec_from_file_location(nombre, archivo)
    assert spec and spec.loader
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modulo  # dataclasses lo busca ahí
    spec.loader.exec_module(modulo)
    return modulo


def _router() -> ModuleType:
    return _script("deploy/vercel/tunel_router.py")


# ---------------------------------------------------------------------------
# el enrutador: respaldo, salud y puerto
# ---------------------------------------------------------------------------


def test_con_resto_lo_que_no_lleva_el_prefijo_va_al_panel_y_nunca_al_comercio():
    tr = _router()
    kw = {"comercio": "C", "catalogo": "K", "prefijo": PREFIJO, "resto": "R"}
    assert tr.destino("/demo", **kw) == ("R", "/demo")
    assert tr.destino("/", **kw) == ("R", "/")
    assert tr.destino("/api/proxy/guard/events", **kw) == ("R", "/api/proxy/guard/events")
    # Sin el prefijo, ni el comercio ni el catálogo: al panel (que pide contraseña).
    assert tr.destino("/a2a/merchant_agent", **kw) == ("R", "/a2a/merchant_agent")
    assert tr.destino("/products", **kw) == ("R", "/products")
    assert tr.destino(f"/{PREFIJO}-otro/a2a/x", **kw) == ("R", f"/{PREFIJO}-otro/a2a/x")
    # Con el prefijo, como siempre.
    assert tr.destino(f"/{PREFIJO}/a2a/merchant_agent", **kw) == ("C", "/a2a/merchant_agent")
    assert tr.destino(f"/{PREFIJO}/products/SKU-1", **kw) == ("K", "/products/SKU-1")
    assert tr.destino(f"/{PREFIJO}", **kw) == ("K", "/")
    # Sin resto, lo de siempre: 404.
    assert tr.destino("/demo", **{**kw, "resto": None}) is None


def test_el_resto_exige_el_prefijo():
    """Sin prefijo todo es del comercio y del catálogo: el panel no tendría
    lugar, y el comercio quedaría sin el secreto de ruta."""
    tr = _router()
    with pytest.raises(ValueError):
        tr.Enrutador(resto="http://127.0.0.1:8020")


class _Bytes(httpx.AsyncByteStream):
    def __init__(self, cuerpo: bytes) -> None:
        self.cuerpo = cuerpo

    async def __aiter__(self):
        yield self.cuerpo


def _json(status: int, datos: dict, **headers: str) -> httpx.Response:
    """Una respuesta en streaming, como las de un upstream de verdad (el
    enrutador la lee con `aiter_raw`)."""
    return httpx.Response(status, headers={"content-type": "application/json", **headers},
                          stream=_Bytes(json.dumps(datos).encode()))


def _upstream(recibidos: list[httpx.Request], *, panel_status: int = 401):
    def responder(pedido: httpx.Request) -> httpx.Response:
        recibidos.append(pedido)
        if pedido.url.port == 8020:
            return _json(panel_status, {"error": "clave_requerida"},
                         **{"www-authenticate": 'Basic realm="KaiAI"'})
        return _json(200, {"ok": True})
    return responder


@pytest.mark.anyio
async def test_el_panel_recibe_el_host_publico_y_el_pedido_tal_cual():
    tr = _router()
    recibidos: list[httpx.Request] = []
    app = tr.Enrutador(prefijo=PREFIJO, resto="http://127.0.0.1:8020",
                       transporte=httpx.MockTransport(_upstream(recibidos)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url=f"https://{PUBLICO}") as http:
        r = await http.get("/demo?x=1", headers={
            "authorization": "Basic abc", "origin": f"https://{PUBLICO}",
            "x-forwarded-host": "malo.example",
        })
        assert r.status_code == 401
        assert r.headers["www-authenticate"].startswith("Basic")
        r = await http.post("/api/demo/mensaje", json={"texto": "hola"},
                            headers={"origin": f"https://{PUBLICO}", "sec-fetch-site": "same-origin"})
        assert r.status_code == 401
        await http.get("/a2a/merchant_agent")
        await http.get(f"/{PREFIJO}/products")

    panel, post, sin_prefijo, catalogo = recibidos
    assert str(panel.url) == "http://127.0.0.1:8020/demo?x=1"
    # El Host público: el panel lo chequea contra DASHBOARD_PUBLIC_ORIGINS.
    assert panel.headers["host"] == PUBLICO
    assert panel.headers["authorization"] == "Basic abc"
    assert panel.headers["origin"] == f"https://{PUBLICO}"
    assert panel.headers.get_list("x-forwarded-host") == [PUBLICO]  # el nuestro, no el del cliente
    assert panel.headers["x-forwarded-proto"] == "https"
    assert post.method == "POST" and json.loads(post.content) == {"texto": "hola"}
    assert post.headers["sec-fetch-site"] == "same-origin"
    assert str(sin_prefijo.url) == "http://127.0.0.1:8020/a2a/merchant_agent"
    # El comercio y el catálogo, como antes: con el Host del upstream.
    assert str(catalogo.url) == "http://127.0.0.1:8011/products"
    assert catalogo.headers["host"] == "127.0.0.1:8011"


class _SSE(httpx.AsyncByteStream):
    """Un SSE que manda un evento y espera a que el test le dé permiso para el
    segundo (si el enrutador juntara la respuesta entera, nunca llegaría el
    primero)."""

    def __init__(self) -> None:
        self.segundo = asyncio.Event()
        self.cerrado = False

    async def __aiter__(self):
        yield b"data: uno\n\n"
        await self.segundo.wait()
        yield b"data: dos\n\n"

    async def aclose(self) -> None:
        self.cerrado = True


def _scope(ruta: str, host: str = PUBLICO) -> dict:
    return {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": ruta, "raw_path": ruta.encode(), "query_string": b"",
        "root_path": "", "headers": [(b"host", host.encode()), (b"accept", b"text/event-stream")],
        "client": ("10.0.0.1", 5000), "server": ("0.0.0.0", 8080),
    }


class _Conexion:
    """receive/send de una conexión ASGI, con un corte a pedido."""

    def __init__(self) -> None:
        self.enviados: list[dict] = []
        self.primer_fragmento = asyncio.Event()
        self.corte = asyncio.Event()
        self._pedido = [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive(self) -> dict:
        if self._pedido:
            return self._pedido.pop()
        await self.corte.wait()
        return {"type": "http.disconnect"}

    async def send(self, mensaje: dict) -> None:
        self.enviados.append(mensaje)
        if mensaje["type"] == "http.response.body" and mensaje.get("body"):
            self.primer_fragmento.set()

    def cuerpo(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.enviados if m["type"] == "http.response.body")


def _app_sse(flujo: _SSE):
    tr = _router()

    def upstream(pedido: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=flujo)

    return tr.Enrutador(prefijo=PREFIJO, resto="http://127.0.0.1:8020",
                        transporte=httpx.MockTransport(upstream))


@pytest.mark.anyio
async def test_el_sse_del_panel_pasa_en_streaming():
    flujo = _SSE()
    conexion = _Conexion()
    tarea = asyncio.create_task(_app_sse(flujo)(_scope("/api/proxy/guard/events"),
                                                conexion.receive, conexion.send))
    try:
        await asyncio.wait_for(conexion.primer_fragmento.wait(), 5)
    finally:
        flujo.segundo.set()
    await asyncio.wait_for(tarea, 5)
    inicio = conexion.enviados[0]
    assert inicio["type"] == "http.response.start" and inicio["status"] == 200
    assert (b"content-type", b"text/event-stream") in inicio["headers"]
    assert conexion.cuerpo() == b"data: uno\n\ndata: dos\n\n"
    assert flujo.cerrado


@pytest.mark.anyio
async def test_si_el_navegador_corta_el_sse_el_enrutador_suelta_el_upstream():
    """Sin esto, cada pestaña cerrada dejaba una suscripción abierta al proxy
    (que tiene un tope de suscriptores)."""
    flujo = _SSE()  # el segundo evento no llega nunca
    conexion = _Conexion()
    tarea = asyncio.create_task(_app_sse(flujo)(_scope("/api/proxy/guard/events"),
                                                conexion.receive, conexion.send))
    await asyncio.wait_for(conexion.primer_fragmento.wait(), 5)
    conexion.corte.set()
    try:
        await asyncio.wait_for(tarea, 5)
    finally:
        flujo.segundo.set()
    assert flujo.cerrado


# -- salud -------------------------------------------------------------------------


def _upstream_salud(recibidos: list[httpx.Request], caidos: dict[int, object]):
    def responder(pedido: httpx.Request) -> httpx.Response:
        recibidos.append(pedido)
        falla = caidos.get(pedido.url.port)
        if falla == "conexion":
            raise httpx.ConnectError("conexión rechazada", request=pedido)
        if falla == "lento":
            raise httpx.ReadTimeout("lento", request=pedido)
        if isinstance(falla, int):
            return httpx.Response(falla, text="error interno de 127.0.0.1:8011")
        if pedido.url.port == 8020:
            return httpx.Response(401, json={"error": "clave_requerida"})
        if pedido.url.port == 8001:
            return httpx.Response(404, text="Not Found")
        return httpx.Response(200, json={"status": "ok"})
    return responder


def _app_salud(recibidos: list[httpx.Request], caidos: dict[int, object] | None = None, **kw):
    tr = _router()
    return tr.Enrutador(prefijo=PREFIJO, resto="http://127.0.0.1:8020", salud=True,
                        transporte=httpx.MockTransport(_upstream_salud(recibidos, caidos or {})), **kw)


@pytest.mark.anyio
async def test_salud_da_200_si_comercio_catalogo_y_panel_contestan():
    recibidos: list[httpx.Request] = []
    app = _app_salud(recibidos)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://healthcheck.railway.app") as http:
        r = await http.get("/salud", headers={"authorization": "Basic abc"})
        cabeza = await http.head("/salud")
    assert r.status_code == 200
    assert r.json() == {"estado": "ok"}
    assert r.headers["cache-control"] == "no-store"
    assert cabeza.status_code == 200 and cabeza.content == b""
    # El 401 del panel cuenta como "contesta"; el 404 del comercio en "/", también.
    puertos = sorted(p.url.port for p in recibidos)
    assert puertos == [8001, 8001, 8011, 8011, 8020, 8020]
    assert all("authorization" not in p.headers for p in recibidos)  # no reenvía nada del cliente
    assert {p.url.path for p in recibidos if p.url.port == 8011} == {"/health"}


@pytest.mark.anyio
@pytest.mark.parametrize("caidos", [
    {8011: "conexion"}, {8001: "lento"}, {8020: 502}, {8011: 500, 8020: "conexion"},
])
async def test_salud_da_503_si_algo_no_contesta_y_no_dice_que(caidos):
    app = _app_salud([], caidos)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://healthcheck.railway.app") as http:
        r = await http.get("/salud")
    assert r.status_code == 503
    assert r.json() == {"estado": "no_disponible"}
    assert "127.0.0.1" not in r.text and "80" not in r.text


@pytest.mark.anyio
async def test_salud_solo_acepta_get_y_head():
    app = _app_salud([])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://healthcheck.railway.app") as http:
        assert (await http.post("/salud", content=b"{}")).status_code == 405


@pytest.mark.anyio
async def test_sin_salud_la_ruta_es_una_mas():
    tr = _router()
    recibidos: list[httpx.Request] = []
    app = tr.Enrutador(prefijo=PREFIJO, resto="http://127.0.0.1:8020",
                       transporte=httpx.MockTransport(_upstream(recibidos)))
    sin_resto = tr.Enrutador(prefijo=PREFIJO, transporte=httpx.MockTransport(_upstream(recibidos)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"https://{PUBLICO}") as http:
        assert (await http.get("/salud")).status_code == 401  # la contesta el panel
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=sin_resto),
                                 base_url=f"https://{PUBLICO}") as http:
        assert (await http.get("/salud")).status_code == 404  # como antes
    assert [str(p.url) for p in recibidos] == ["http://127.0.0.1:8020/salud"]


# -- opciones: puerto, prefijo y resto -----------------------------------------------


def test_el_puerto_sale_de_PORT_si_no_viene_puerto():
    tr = _router()
    assert tr.opciones([], {"PORT": "8080"}).puerto == 8080
    assert tr.opciones(["--puerto", "9000"], {"PORT": "8080"}).puerto == 9000
    assert tr.opciones([], {}).puerto == tr.PUERTO
    for malo in ("abc", "0", "70000", "-1"):
        with pytest.raises(tr.OpcionInvalida):
            tr.opciones([], {"PORT": malo})


def test_opciones_del_prefijo_y_el_resto():
    tr = _router()
    op = tr.opciones(["--resto", "http://127.0.0.1:8020/", "--salud", "--host", "0.0.0.0"],
                     {"TUNEL_PREFIJO": f" {PREFIJO}\n"})
    assert (op.prefijo, op.resto, op.salud, op.host) == (PREFIJO, "http://127.0.0.1:8020", True, "0.0.0.0")
    # Lo que usa tunel_demo.sh: sin resto ni salud.
    op = tr.opciones(["--puerto", "8090"], {"TUNEL_PREFIJO": PREFIJO})
    assert (op.resto, op.salud, op.host) == (None, False, "127.0.0.1")
    with pytest.raises(tr.OpcionInvalida):
        tr.opciones(["--resto", "http://127.0.0.1:8020"], {})  # sin prefijo
    with pytest.raises(tr.OpcionInvalida):
        tr.opciones(["--resto", "ftp://127.0.0.1:8020"], {"TUNEL_PREFIJO": PREFIJO})
    with pytest.raises(tr.OpcionInvalida):
        tr.opciones([], {"TUNEL_PREFIJO": "corto"})
    # El resto solo con la opción, nunca del entorno: con una variable
    # olvidada, tunel_demo.sh publicaría el panel local (sin contraseña).
    assert tr.opciones([], {"TUNEL_PREFIJO": PREFIJO, "TUNEL_RESTO": "http://127.0.0.1:8020"}).resto is None


def test_main_rechaza_opciones_invalidas_sin_arrancar(capsys):
    tr = _router()
    assert tr.main(["--resto", "http://127.0.0.1:8020"]) == 2
    assert "prefijo" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# generar_secretos.py --apuntar (el dominio nuevo con el prefijo de siempre)
# ---------------------------------------------------------------------------


def _carpeta_secretos(tmp_path: Path, *, con_env: bool = True) -> Path:
    carpeta = tmp_path / "vercel"
    carpeta.mkdir()
    (carpeta / "TUNEL_PREFIJO.txt").write_text(PREFIJO, encoding="utf-8")
    if con_env:
        (carpeta / "vercel.env").write_text(
            "# secreto\nGUARD_SIGNING_KID=guard-key-1\nGATEWAY_COMERCIOS_JSON={\"viejo\":1}\n", encoding="utf-8")
    return carpeta


def test_apuntar_reescribe_los_comercios_con_el_prefijo_que_ya_estaba(tmp_path, capsys):
    from proxy.comercios import RegistroComercios

    gs = _script("deploy/vercel/generar_secretos.py")
    carpeta = _carpeta_secretos(tmp_path)
    assert gs.main(["--carpeta", str(carpeta), "--apuntar", f"https://{PUBLICO.upper()}/"]) == 0
    salida = capsys.readouterr()
    assert PREFIJO not in salida.out + salida.err
    assert (carpeta / "TUNEL_PREFIJO.txt").read_text(encoding="utf-8") == PREFIJO  # no cambia
    datos = json.loads((carpeta / "GATEWAY_COMERCIOS_JSON.json").read_text(encoding="utf-8"))
    comercio = RegistroComercios.desde_dict(datos).get("merchant_1")
    assert comercio.a2a_url == f"https://{PUBLICO}/{PREFIJO}/a2a/merchant_agent"
    assert comercio.catalogo_url == f"https://{PUBLICO}/{PREFIJO}"
    entorno = _script("deploy/vercel/local_multi.py").leer_env(carpeta / "vercel.env")
    assert json.loads(entorno["GATEWAY_COMERCIOS_JSON"]) == datos
    assert entorno["GUARD_SIGNING_KID"] == "guard-key-1"  # lo demás, igual
    assert sorted(p.name for p in carpeta.iterdir()) == [
        "GATEWAY_COMERCIOS_JSON.json", "TUNEL_PREFIJO.txt", "vercel.env"]


def test_apuntar_sin_vercel_env_solo_escribe_el_registro(tmp_path):
    gs = _script("deploy/vercel/generar_secretos.py")
    carpeta = _carpeta_secretos(tmp_path, con_env=False)
    assert gs.main(["--carpeta", str(carpeta), "--apuntar", PUBLICO]) == 0
    assert not (carpeta / "vercel.env").exists()


@pytest.mark.parametrize("argv", [
    ["--apuntar", "https://demo.up.railway.app/ruta"],
    ["--apuntar", "no es un dominio"],
    ["--apuntar", PUBLICO, "--dominio", "otro.ngrok-free.app"],
    ["--apuntar", PUBLICO, "--sin-prefijo"],
    ["--apuntar", PUBLICO, "--clave-lectura", "demo"],
])
def test_apuntar_rechaza_lo_que_no_sirve(tmp_path, argv, capsys):
    gs = _script("deploy/vercel/generar_secretos.py")
    carpeta = _carpeta_secretos(tmp_path)
    assert gs.main(["--carpeta", str(carpeta), *argv]) == 2
    assert not (carpeta / "GATEWAY_COMERCIOS_JSON.json").exists()
    assert PREFIJO not in capsys.readouterr().err


def test_apuntar_sin_prefijo_generado_avisa(tmp_path, capsys):
    gs = _script("deploy/vercel/generar_secretos.py")
    carpeta = tmp_path / "vacia"
    carpeta.mkdir()
    assert gs.main(["--carpeta", str(carpeta), "--apuntar", PUBLICO]) == 2
    assert "TUNEL_PREFIJO.txt" in capsys.readouterr().err
    assert not list(carpeta.iterdir())


# ---------------------------------------------------------------------------
# los scripts de bash
# ---------------------------------------------------------------------------


def _bash() -> str | None:
    """Un bash que funcione. En Windows, primero el de Git (el `bash` del PATH
    puede ser el de WSL, que no entiende rutas C:/…)."""
    candidatos = []
    git = shutil.which("git")
    if git and os.name == "nt":  # ...\Git\cmd\git.exe -> ...\Git\bin\bash.exe
        candidatos.append(str(Path(git).resolve().parents[1] / "bin" / "bash.exe"))
    candidatos.append(shutil.which("bash"))
    for bash in candidatos:
        if not bash or not Path(bash).exists():
            continue
        try:
            if subprocess.run([bash, "-c", "tr --version >/dev/null && exit 0"],
                              capture_output=True, timeout=20).returncode == 0:
                return bash
        except (OSError, subprocess.SubprocessError):
            continue
    return None


@pytest.fixture
def bash() -> str:
    encontrado = _bash()
    if encontrado is None:
        pytest.skip("no hay un bash que funcione en esta máquina")
    return encontrado


@pytest.mark.parametrize("script", [
    "railway/arrancar.sh", "railway/apuntar_vercel.sh", "vercel/apuntar_vercel.sh",
    "vercel/tunel_demo.sh", "start_all.sh",
])
def test_los_scripts_son_bash_valido_y_con_lf(bash, script):
    ruta = DEPLOY / script
    assert b"\r" not in ruta.read_bytes()
    subprocess.run([bash, "-n", str(ruta)], check=True, timeout=30)


def _correr(bash: str, script: str, *args: str, entorno: dict[str, str],
            timeout: float = 60) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items()
            if k.upper() in ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "HOME",
                             "USERPROFILE", "COMSPEC", "PATHEXT", "WINDIR")}
    resultado = subprocess.run(
        [bash, str(DEPLOY / script), *args], cwd=RAIZ, capture_output=True, timeout=timeout,
        env={**base, "PYTHONUTF8": "1", **entorno},
    )
    return subprocess.CompletedProcess(
        resultado.args, resultado.returncode,
        resultado.stdout.decode("utf-8", "replace"), resultado.stderr.decode("utf-8", "replace"),
    )

# -- apuntar el motor de Vercel (con un npx falso) ---------------------------------


NPX_FALSO = """#!/bin/bash
printf '%s\\n' "$*" >> "$NPX_REGISTRO"
case "$*" in
  *"env add"*)
    cat > "$NPX_STDIN"
    if [ "${NPX_FALLA:-}" = env ]; then echo "Error: proyecto sin link" >&2; exit 1; fi
    echo "Added Environment Variable" ;;
  *"deploy"*)
    if [ "${NPX_FALLA:-}" = deploy ]; then echo "Error: el build falló" >&2; exit 1; fi
    echo "Production: https://kaiai-motor-abc123-equipo.vercel.app [12s]" ;;
esac
"""


@pytest.fixture
def npx_falso(tmp_path, bash) -> dict[str, str]:
    carpeta_bin = tmp_path / "bin"
    carpeta_bin.mkdir()
    npx = carpeta_bin / "npx"
    npx.write_bytes(NPX_FALSO.encode())
    npx.chmod(0o755)
    secretos = _carpeta_secretos(tmp_path)
    return {
        "PATH": str(carpeta_bin) + os.pathsep + os.environ.get("PATH", ""),
        "PY": Path(sys.executable).as_posix(),
        "SECRETOS": secretos.as_posix(),
        "NPX_REGISTRO": (tmp_path / "registro.txt").as_posix(),
        "NPX_STDIN": (tmp_path / "stdin.txt").as_posix(),
    }


def _registro(entorno: dict[str, str]) -> list[str]:
    ruta = Path(entorno["NPX_REGISTRO"])
    return ruta.read_text(encoding="utf-8").splitlines() if ruta.exists() else []


def test_apuntar_vercel_carga_el_registro_y_redespliega(bash, npx_falso):
    r = _correr(bash, "vercel/apuntar_vercel.sh", f"https://{PUBLICO}", entorno=npx_falso)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert _registro(npx_falso) == ["vercel env add GATEWAY_COMERCIOS_JSON production --force",
                                    "vercel deploy --prod --yes"]
    archivo = Path(npx_falso["SECRETOS"]) / "GATEWAY_COMERCIOS_JSON.json"
    cargado = Path(npx_falso["NPX_STDIN"]).read_text(encoding="utf-8")
    assert cargado == archivo.read_text(encoding="utf-8").strip()
    assert json.loads(cargado)["comercios"][0]["a2a_url"] == f"https://{PUBLICO}/{PREFIJO}/a2a/merchant_agent"
    assert "redesplegada" in r.stdout
    assert PREFIJO not in r.stdout + r.stderr


@pytest.mark.parametrize(("falla", "llamadas"), [("env", 1), ("deploy", 2)])
def test_apuntar_vercel_corta_y_muestra_el_error(bash, npx_falso, falla, llamadas):
    r = _correr(bash, "vercel/apuntar_vercel.sh", f"https://{PUBLICO}",
                entorno={**npx_falso, "NPX_FALLA": falla})
    assert r.returncode != 0
    assert len(_registro(npx_falso)) == llamadas  # no sigue después de la falla
    assert "Error:" in r.stderr  # el de la CLI
    assert "redesplegada" not in r.stdout


def test_apuntar_vercel_sin_dominio_no_toca_nada(bash, npx_falso):
    r = _correr(bash, "vercel/apuntar_vercel.sh", entorno=npx_falso)
    assert r.returncode != 0
    assert _registro(npx_falso) == []


def test_tunel_demo_usa_el_mismo_apuntar_vercel():
    texto = (DEPLOY / "vercel" / "tunel_demo.sh").read_text(encoding="utf-8")
    assert "deploy/vercel/apuntar_vercel.sh" in texto
    assert "vercel env add" not in texto  # una sola copia de esa lógica


# ---------------------------------------------------------------------------
# .dockerignore (semántica de Docker)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PatronDocker:
    regex: re.Pattern[str]
    negado: bool
    texto: str


def _regex_docker(patron: str) -> re.Pattern[str]:
    """Como `patternmatcher` de moby: `*` y `?` no cruzan `/`; `**` cruza
    niveles (también ninguno); anclado a la raíz del contexto."""
    salida = ["^"]
    i, n = 0, len(patron)
    while i < n:
        c = patron[i]
        if c == "*":
            if patron.startswith("**", i):
                i += 2
                if i < n and patron[i] == "/":
                    i += 1
                salida.append(".*" if i >= n else "(?:.*/)?")
                continue
            salida.append("[^/]*")
        elif c == "?":
            salida.append("[^/]")
        elif c == "\\" and i + 1 < n:
            salida.append(re.escape(patron[i + 1]))
            i += 2
            continue
        else:
            salida.append(re.escape(c))
        i += 1
    return re.compile("".join(salida) + "$")


def leer_dockerignore(texto: str) -> list[PatronDocker]:
    """Como `ignorefile.ReadAll`: `#` en la columna 0 es comentario, se
    recorta, `!` niega, y el patrón se limpia (`filepath.Clean`, sin la `/`
    inicial)."""
    patrones = []
    for linea in texto.splitlines():
        if linea.startswith("#"):
            continue
        linea = linea.strip()
        if not linea:
            continue
        negado = linea.startswith("!")
        if negado:
            linea = linea[1:].strip()
        assert "[" not in linea, "el emulador no soporta clases de caracteres"
        limpio = posixpath.normpath(linea)
        if len(limpio) > 1 and limpio.startswith("/"):
            limpio = limpio[1:]
        patrones.append(PatronDocker(_regex_docker(limpio), negado, linea))
    return patrones


class ExcluidosDocker:
    """`MatchesOrParentMatches`: un patrón vale si coincide con la ruta o con
    alguna de sus carpetas; gana el último que coincide."""

    def __init__(self, patrones: list[PatronDocker]) -> None:
        self.patrones = patrones

    def excluido(self, ruta: str) -> bool:
        partes = ruta.split("/")
        padres = ["/".join(partes[:i]) for i in range(1, len(partes))]
        excluido = False
        for p in self.patrones:
            if p.regex.match(ruta) or any(p.regex.match(d) for d in padres):
                excluido = not p.negado
        return excluido

    def podar(self, carpeta: str) -> bool:
        """¿Se puede no bajar a `carpeta`? Si está excluida y ninguna
        negación puede volver a sumar algo de adentro."""
        if not self.excluido(carpeta):
            return False
        partes = carpeta.split("/")
        rutas = ["/".join(partes[:i]) for i in range(1, len(partes) + 1)]
        ultimo = max(n for n, p in enumerate(self.patrones) if any(p.regex.match(r) for r in rutas))
        for p in self.patrones[ultimo + 1:]:  # solo una negación posterior puede volver a sumar
            if not p.negado:
                continue
            # Lo literal antes del primer comodín: si cae adentro de la
            # carpeta (o la contiene), la negación puede sumar algo de ahí.
            literal = re.split(r"[*?]", p.texto.lstrip("/"), maxsplit=1)[0]
            if literal.startswith(carpeta + "/") or (carpeta + "/").startswith(literal):
                return False
        return True


@lru_cache(maxsize=1)
def _dockerignore() -> ExcluidosDocker:
    return ExcluidosDocker(leer_dockerignore(DOCKERIGNORE.read_text(encoding="utf-8")))


def entra(ruta: str) -> bool:
    return not _dockerignore().excluido(ruta)


@pytest.mark.parametrize(
    ("reglas", "ruta", "excluido"),
    [
        (".env", ".env", True),
        (".env", "world/.env", False),  # sin **/ no baja: no es .gitignore
        ("**/.env", "world/ap2/.env", True),
        ("**/.env", ".env", True),
        ("**/.env.*", "proxy/.env.production", True),
        ("results", "results/a.json", True),
        ("results", "world/results/a.json", False),
        ("/docs/", "docs/a.md", True),
        ("**/.venv", "world/ap2/.venv/bin/python", True),
        ("*\n!world", "world/run_world.sh", False),
        ("*\n!world", "secretos.json", True),
        ("*\n!world\n**/.temp-db", "world/ap2/.temp-db/clave.pem", True),
        ("**/*.pem\n!world/certs/*.pem", "world/certs/a.pem", False),
        ("**/*.pem\n!world/certs/*.pem", ".temp-db/a.pem", True),
        ("# comentario\n  x.txt  ", "x.txt", True),
    ],
)
def test_el_emulador_sigue_la_semantica_de_docker(reglas, ruta, excluido):
    assert ExcluidosDocker(leer_dockerignore(reglas)).excluido(ruta) is excluido


@pytest.mark.parametrize("ruta", [
    ".env", ".env.local", ".env.example", "world/ap2/.env", "proxy/.env.production",
    ".temp-db/guard_signing_key.pem", ".temp-db/guard_api_token", ".temp-db/deploy.env",
    ".temp-db/vercel/TUNEL_PREFIJO.txt", ".temp-db/vercel/vercel.env",
    ".temp-db/vercel/clave_integrador_demo.txt", "world/ap2/.temp-db/merchant_signing_key.pem",
    "world/ap2/.temp-db/scenario.json", ".logs/proxy.log", ".logs/guard_events.jsonl",
    "world/ap2/.logs/merchant_agent.log", "results/benchmark-20261017-185835-4f5d16.json",
    "world/ap2/results/benchmark.json", ".venv/bin/python", "world/ap2/.venv/bin/python",
    "world/ap2/.venv/Scripts/python.exe", "venv/lib/x.py", "world/ap2/venv/x.py",
    ".vercel/project.json", ".git", ".git/HEAD", "world/ap2/.git/config",
    "proxy/__pycache__/app.cpython-312.pyc", "world/ap2/code/samples/python/src/roles/__pycache__/x.pyc",
    "guard_api_token", "proxy/guard_api_token", "proxy/clave.pem", "guard/firma.key",
    # Cualquier archivo nuevo en la raíz (un volcado, un JSON de claves) no entra.
    "secretos.json", "gateway_claves.json", "dump.sql", ".claude/settings.local.json", ".mcp.json",
    "node_modules/x/index.js",
])
def test_dockerignore_deja_afuera_los_secretos_y_lo_local(ruta):
    assert not entra(ruta), f"{ruta} entraría a la imagen"


# Lo que usa el contenedor en runtime (start_all.sh, run_world.sh, el proxy,
# el panel y el enrutador).
CARPETAS_RUNTIME = ("contracts", "guard", "filtro", "intencion", "decisiones", "taxonomia",
                    "fabricante", "proxy", "dashboard", "world", "deploy", "fixtures")


@pytest.mark.parametrize("ruta", [
    "deploy/start_all.sh", "deploy/railway/arrancar.sh", "deploy/vercel/tunel_router.py",
    "world/run_world.sh", "world/ap2/pyproject.toml", "world/ap2/uv.lock", "world/ap2/README.md",
    "world/ap2/code/samples/python/pyproject.toml", "world/ap2/code/samples/python/README.md",
    # Los certificados públicos del sample (la raíz de confianza de las SD-JWT).
    "world/ap2/code/samples/certs/issuer_cert_sdjwt.pem", "world/ap2/code/samples/certs/ds_cert_sdjwt.pem",
    "proxy/__main__.py", "dashboard/server.py", "fixtures/public_catalog.json",
    "taxonomia/datos/taxonomy-with-ids.en-US.subconjunto.txt",
])
def test_dockerignore_deja_lo_que_usa_el_contenedor(ruta):
    assert (RAIZ / ruta).exists(), ruta
    assert entra(ruta), f"{ruta} no entraría a la imagen"


def _git_ls_files() -> list[str]:
    try:
        salida = subprocess.run(["git", "ls-files", "-z"], cwd=RAIZ, capture_output=True,
                                check=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git no está disponible")
    return [r for r in salida.decode("utf-8").split("\0") if r]


SECRETO = re.compile(r"(^|/)(\.env($|\.)|\.temp-db/|\.logs/|guard_api_token)|\.(pem|key|p12|pfx)$|_key", re.I)
CERTS_DEL_SAMPLE = frozenset({
    "world/ap2/code/samples/certs/issuer_cert_sdjwt.pem", "world/ap2/code/samples/certs/ds_cert_sdjwt.pem",
})


def test_entra_todo_el_codigo_de_runtime_de_git():
    archivos = _git_ls_files()
    faltan = [r for r in archivos if r.split("/")[0] in CARPETAS_RUNTIME and not entra(r)
              and "__pycache__" not in r and not r.startswith("world/ap2/results/")]
    assert not faltan, f"no entrarían a la imagen: {faltan[:20]}"


def test_lo_que_entra_de_git_no_tiene_secretos():
    entran = [r for r in _git_ls_files() if entra(r)]
    assert any(r.startswith("proxy/") for r in entran)
    con_secreto = [r for r in entran if SECRETO.search(r) and r not in CERTS_DEL_SAMPLE]
    assert not con_secreto, f"entrarían archivos con forma de secreto: {con_secreto}"
    assert not [r for r in entran if r.startswith("results/")]


def test_lo_que_entra_desde_el_disco_no_tiene_secretos():
    """El contexto del build es la carpeta tal cual (`railway up` y
    `docker build`), con el `.env`, `.temp-db/` y `.logs/` que haya."""
    entran: list[str] = []
    for actual, dirs, archivos in os.walk(RAIZ):
        base = Path(actual).relative_to(RAIZ).as_posix()
        base = "" if base == "." else base + "/"
        dirs[:] = [d for d in dirs if not _dockerignore().podar(base + d)]
        entran += [base + a for a in archivos if entra(base + a)]
    assert any(r.startswith("proxy/") for r in entran)
    malos = [r for r in entran
             if (SECRETO.search(r) and r not in CERTS_DEL_SAMPLE)
             or re.search(r"(^|/)(\.venv|venv|__pycache__|\.vercel|\.git|results)(/|$)", r)]
    assert not malos, f"entrarían a la imagen: {malos[:20]}"


# ---------------------------------------------------------------------------
# Dockerfile y railway.json (sin Docker ni Railway: lo que se puede fijar)
# ---------------------------------------------------------------------------


def _instrucciones_dockerfile() -> list[tuple[str, str]]:
    """(INSTRUCCIÓN, resto) con las líneas continuadas unidas."""
    texto = (RAILWAY / "Dockerfile").read_text(encoding="utf-8")
    assert "\r" not in texto
    unidas, actual = [], ""
    for linea in texto.splitlines():
        if not actual and (not linea.strip() or linea.lstrip().startswith("#")):
            continue
        if linea.rstrip().endswith("\\"):
            actual += linea.rstrip()[:-1] + " "
            continue
        actual += linea
        unidas.append(actual.strip())
        actual = ""
    return [(l.split(None, 1)[0].upper(), l.split(None, 1)[1] if " " in l else "") for l in unidas]


def test_el_dockerfile_usa_python_y_uv_fijos_y_el_venv_de_world():
    inst = _instrucciones_dockerfile()
    froms = [r for i, r in inst if i == "FROM"]
    assert froms and re.match(r"python:3\.12(\.\d+)?-slim", froms[0]), froms
    copias = " ".join(r for i, r in inst if i == "COPY")
    uv = re.search(r"--from=ghcr\.io/astral-sh/uv:(\S+)", copias)
    assert uv and re.fullmatch(r"\d+\.\d+\.\d+", uv.group(1)), "uv con versión exacta"
    runs = " ".join(r for i, r in inst if i == "RUN")
    assert "uv sync --package ap2-samples" in runs  # como run_world.sh
    paquetes = ["bash", "curl", "tini"]
    # uv clona con el git del sistema las dependencias que vienen de un repo.
    if "git+https://" in (RAIZ / "world" / "ap2" / "uv.lock").read_text(encoding="utf-8"):
        paquetes.append("git")
    for paquete in paquetes:
        assert re.search(rf"apt-get install[^&]*\b{paquete}\b", runs), paquete
    # Los .sh con LF aunque el contexto venga de un checkout con CRLF.
    assert "sed -i 's/\\r$//'" in runs


def test_el_dockerfile_corre_sin_root_y_sin_secretos_en_la_imagen():
    inst = _instrucciones_dockerfile()
    usuarios = [r.strip() for i, r in inst if i == "USER"]
    assert usuarios and usuarios[-1] not in ("root", "0", "0:0")
    ultimo_user = max(n for n, (i, _) in enumerate(inst) if i == "USER")
    entrada = [n for n, (i, _) in enumerate(inst) if i in ("ENTRYPOINT", "CMD")]
    assert entrada and min(entrada) > ultimo_user
    assert any("arrancar.sh" in r for i, r in inst if i in ("ENTRYPOINT", "CMD"))
    assert any("tini" in r for i, r in inst if i == "ENTRYPOINT")
    declarados = " ".join(r for i, r in inst if i in ("ENV", "ARG"))
    for secreto in ("GOOGLE_API_KEY", "DASHBOARD_ACCESS_TOKEN", "TUNEL_PREFIJO", "OPENAI_API_KEY"):
        assert secreto not in declarados, f"{secreto} va en las variables de Railway, no en la imagen"
    assert not [r for i, r in inst if i in ("COPY", "ADD") and re.search(r"\.env|\.temp-db", r)]


def test_railway_json_build_salud_reinicio_y_una_sola_replica():
    datos = json.loads((RAILWAY / "railway.json").read_text(encoding="utf-8"))
    build, deploy = datos["build"], datos["deploy"]
    assert build["builder"] == "DOCKERFILE"
    assert build["dockerfilePath"] == "deploy/railway/Dockerfile"
    assert (RAIZ / build["dockerfilePath"]).is_file()
    assert deploy["healthcheckPath"] == _router().RUTA_SALUD
    assert deploy["healthcheckTimeout"] >= 300  # ADK tarda en arrancar
    assert deploy["restartPolicyType"] == "ON_FAILURE"
    assert deploy.get("sleepApplication") is False  # siempre prendida
    # El estado vive en memoria (sesiones de ADK, compras): una sola réplica.
    regiones = deploy["multiRegionConfig"]
    assert len(regiones) == 1 and next(iter(regiones.values())) == {"numReplicas": 1}
    assert next(iter(regiones)).startswith("us-east")  # cerca de iad1 (Vercel)
    assert "startCommand" not in deploy  # lo fija el Dockerfile


# ---------------------------------------------------------------------------
# el contenedor de Railway
# ---------------------------------------------------------------------------



CLAVE_PANEL = "clave-del-panel-de-prueba-0001"
CLAVE_GEMINI = "AIza-clave-de-gemini-de-prueba"
VALIDAS = {
    "GOOGLE_API_KEY": CLAVE_GEMINI,
    "DASHBOARD_ACCESS_TOKEN": CLAVE_PANEL,
    "TUNEL_PREFIJO": PREFIJO,
    "DASHBOARD_PUBLIC_ORIGINS": f"https://{PUBLICO}",
}


def test_arrancar_con_todo_valido_pasa_la_validacion(bash):
    r = _correr(bash, "railway/arrancar.sh", "--solo-validar", entorno={**VALIDAS, "PORT": "8080"})
    assert r.returncode == 0, r.stderr
    for secreto in (CLAVE_PANEL, CLAVE_GEMINI, PREFIJO):
        assert secreto not in r.stdout + r.stderr


@pytest.mark.parametrize(("cambios", "nombre"), [
    ({"GOOGLE_API_KEY": ""}, "GOOGLE_API_KEY"),
    ({"DASHBOARD_ACCESS_TOKEN": "corta-123"}, "DASHBOARD_ACCESS_TOKEN"),
    ({"DASHBOARD_ACCESS_TOKEN": "   "}, "DASHBOARD_ACCESS_TOKEN"),
    ({"TUNEL_PREFIJO": "abc"}, "TUNEL_PREFIJO"),
    ({"TUNEL_PREFIJO": "prefijo con espacios"}, "TUNEL_PREFIJO"),
    ({"DASHBOARD_PUBLIC_ORIGINS": ""}, "DASHBOARD_PUBLIC_ORIGINS"),
    ({"DASHBOARD_PUBLIC_ORIGINS": f"http://{PUBLICO}"}, "DASHBOARD_PUBLIC_ORIGINS"),
    ({"DASHBOARD_PUBLIC_ORIGINS": f"https://{PUBLICO}/demo"}, "DASHBOARD_PUBLIC_ORIGINS"),
    ({"PORT": "8020"}, "PORT"),
    ({"PORT": "8001"}, "PORT"),
    ({"PORT": "ochenta"}, "PORT"),
    ({"DASHBOARD_EXTRA_HOSTS": "a b"}, "DASHBOARD_EXTRA_HOSTS"),
    ({"ESCENARIO": "../x"}, "ESCENARIO"),
])
def test_arrancar_sin_lo_obligatorio_no_arranca_ni_muestra_secretos(bash, cambios, nombre):
    r = _correr(bash, "railway/arrancar.sh", entorno={**VALIDAS, **cambios})
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert nombre in r.stderr
    assert "start_all" not in r.stdout  # no arrancó nada
    for secreto in (CLAVE_PANEL, CLAVE_GEMINI, PREFIJO, "corta-123", "prefijo con espacios"):
        assert secreto not in r.stdout + r.stderr


def test_arrancar_sin_nada_nombra_todas_las_obligatorias(bash):
    r = _correr(bash, "railway/arrancar.sh", entorno={})
    assert r.returncode == 2
    for nombre in VALIDAS:
        assert nombre in r.stderr


def test_arrancar_levanta_la_demo_y_el_enrutador_con_respaldo_y_salud():
    texto = (RAILWAY / "arrancar.sh").read_text(encoding="utf-8")
    assert "deploy/start_all.sh" in texto
    assert "deploy/vercel/tunel_router.py" in texto
    for opcion in ("--resto http://127.0.0.1:8020", "--salud", '--host "${ENRUTADOR_HOST:-0.0.0.0}"'):
        assert opcion in texto
    assert "DASHBOARD_SOLO_LOOPBACK=1" in texto  # el panel, solo detrás del enrutador
    assert "unset GATEWAY_CLAVES_ARCHIVO" in texto
    # Si un proceso muere, el contenedor termina (Railway lo reinicia).
    assert "wait -n" in texto


def test_start_all_deja_el_panel_en_loopback_si_se_pide():
    texto = (DEPLOY / "start_all.sh").read_text(encoding="utf-8")
    assert 'DASHBOARD_SOLO_LOOPBACK:-0}" = 1' in texto
    # Sin la variable, como antes (Tenki publica el :8020 directo).
    assert "export DASHBOARD_HOST=0.0.0.0 DASHBOARD_ALLOW_REMOTE=1" in texto


def test_apuntar_desde_railway_dice_listo_solo_si_salio_bien(bash, npx_falso):
    bien = _correr(bash, "railway/apuntar_vercel.sh", "--sin-salud", PUBLICO, entorno=npx_falso)
    assert bien.returncode == 0, (bien.stdout, bien.stderr)
    assert "Listo" in bien.stdout
    mal = _correr(bash, "railway/apuntar_vercel.sh", "--sin-salud", PUBLICO,
                  entorno={**npx_falso, "NPX_FALLA": "deploy"})
    assert mal.returncode != 0
    assert "Listo" not in mal.stdout + mal.stderr


CURL_FALSO = """#!/bin/bash
# Contesta el status que piden CURL_SALUD y CURL_CATALOGO (sin red).
for url in "$@"; do :; done
printf '%s\\n' "$url" >> "$CURL_REGISTRO"
case "$url" in
  */salud) printf '%s' "${CURL_SALUD:-200}" ;;
  */products) printf '%s' "${CURL_CATALOGO:-200}" ;;
  *) printf '000'; exit 7 ;;
esac
"""


def _con_curl_falso(bash: str, tmp_path: Path, entorno: dict[str, str], *args: str,
                    **variables: str) -> subprocess.CompletedProcess[str]:
    """railway/apuntar_vercel.sh con un curl falso adelante en el PATH (se pone
    adentro de bash: el bash de Git pone su curl antes que el PATH heredado)."""
    falsos = tmp_path / "falsos"
    falsos.mkdir(exist_ok=True)
    (falsos / "curl").write_bytes(CURL_FALSO.encode())
    (falsos / "curl").chmod(0o755)
    script = (DEPLOY / "railway" / "apuntar_vercel.sh").as_posix()
    lanzador = ('d="$FALSOS"; command -v cygpath >/dev/null && d="$(cygpath -u "$d")"; '
                'PATH="$d:$PATH" exec bash "$0" "$@"')
    resultado = subprocess.run(
        [bash, "-c", lanzador, script, *args], cwd=RAIZ, capture_output=True, timeout=60,
        env={**os.environ, "PYTHONUTF8": "1", **entorno, **variables, "FALSOS": falsos.as_posix(),
             "CURL_REGISTRO": (tmp_path / "curl.txt").as_posix()},
    )
    return subprocess.CompletedProcess(resultado.args, resultado.returncode,
                                       resultado.stdout.decode("utf-8", "replace"),
                                       resultado.stderr.decode("utf-8", "replace"))


def test_apuntar_desde_railway_primero_comprueba_salud_y_prefijo(bash, npx_falso, tmp_path):
    r = _con_curl_falso(bash, tmp_path, npx_falso, PUBLICO)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "Listo" in r.stdout
    pedidos = (tmp_path / "curl.txt").read_text(encoding="utf-8").splitlines()
    assert pedidos == [f"https://{PUBLICO}/salud", f"https://{PUBLICO}/{PREFIJO}/products"]
    assert PREFIJO not in r.stdout + r.stderr
    assert len(_registro(npx_falso)) == 2


@pytest.mark.parametrize(("variables", "dice"), [
    ({"CURL_SALUD": "503"}, "salud contestó 503"),
    ({"CURL_CATALOGO": "401"}, "TUNEL_PREFIJO"),  # lo atendió el panel: otro prefijo
    ({"CURL_CATALOGO": "502"}, "contestó 502"),
])
def test_apuntar_desde_railway_no_toca_vercel_si_la_demo_no_responde(bash, npx_falso, tmp_path,
                                                                      variables, dice):
    r = _con_curl_falso(bash, tmp_path, npx_falso, PUBLICO, **variables)
    assert r.returncode != 0
    assert dice in r.stderr
    assert "Listo" not in r.stdout + r.stderr
    assert PREFIJO not in r.stdout + r.stderr
    assert _registro(npx_falso) == []


@pytest.mark.parametrize("dominio", ["", "https://x.up.railway.app/ruta", "no es un dominio"])
def test_apuntar_desde_railway_valida_el_dominio(bash, npx_falso, dominio):
    r = _correr(bash, "railway/apuntar_vercel.sh", "--sin-salud", *([dominio] if dominio else []),
                entorno=npx_falso)
    assert r.returncode != 0
    assert _registro(npx_falso) == []
