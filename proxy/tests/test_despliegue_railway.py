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
