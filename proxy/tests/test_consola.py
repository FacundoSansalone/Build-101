"""La consola web del motor (`/consola`, `proxy/consola/`).

- Con `create_app(..., consola=True)`: la página y sus estáticos, sin clave,
  en los dos oyentes, con la CSP estricta, `nosniff`, `no-referrer` y
  `no-store`; `/` y `/consola/` → 307 a `/consola`. Sin la consola, nada de
  eso existe. La consola no abre nada de la API: `/v1` y `/a2a` siguen
  pidiendo clave.
- La app de Vercel la sirve.
- Un chequeo estático del JS y el HTML: nada de `innerHTML` y parecidos, ni
  scripts o estilos en línea, ni URLs externas; la clave nunca va al registro.
- Los estáticos entran en el bundle de Vercel.
- La lista de comportamientos del JS es la de `scenarios.json` (`"tipo":
  "general"`).
- Un recibo firmado por el motor se verifica con la lógica del navegador
  (`node`, WebCrypto).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from starlette.applications import Starlette

from proxy import consola
from proxy.claves import AutenticacionGateway, RegistroIntegradores
from proxy.tests.gateway_ayudantes import (
    armar_gateway,
    cliente_local,
    cliente_publico,
)
from proxy.tests.gateway_ayudantes import buscar as buscar_compra
from proxy.tests.gateway_ayudantes import checkout as checkout_compra
from proxy.tests.gateway_ayudantes import crear_compra
from proxy.tests.test_despliegue_vercel import _excluido, se_sube
from proxy.tests.test_vercel_entrada import _cliente, _serverless

pytestmark = pytest.mark.anyio

RAIZ = Path(__file__).resolve().parents[2]
CARPETA = RAIZ / "proxy" / "consola"
ESCENARIOS_JSON = (
    RAIZ / "world" / "ap2" / "code" / "samples" / "python" / "src" / "demo_world" / "data"
    / "scenarios.json"
)
TIPOS = {
    "/consola": ("text/html", "index.html"),
    "/consola/app.js": ("text/javascript", "app.js"),
    "/consola/logica.mjs": ("text/javascript", "logica.mjs"),
    "/consola/recibo.mjs": ("text/javascript", "recibo.mjs"),
    "/consola/estilo.css": ("text/css", "estilo.css"),
}
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
# Identificadores con forma de URL que no se piden nunca: el namespace del SVG
# del favicon y la extensión AP2 (un header).
URLS_PERMITIDAS = {
    "http://www.w3.org/2000/svg",
    "https://github.com/google-agentic-commerce/ap2/v1",
}


def _cabeceras_ok(respuesta) -> None:
    assert respuesta.headers["content-security-policy"] == CSP
    assert respuesta.headers["x-content-type-options"] == "nosniff"
    assert respuesta.headers["referrer-policy"] == "no-referrer"
    assert respuesta.headers["cache-control"] == "no-store"


@pytest.fixture
def gateway(config, stub):
    return armar_gateway(config, stub, consola=True)


# -- rutas y cabeceras -------------------------------------------------------------


async def test_sirve_la_pagina_y_los_estaticos_sin_clave(gateway):
    async with cliente_publico(gateway.app, clave=None) as c:
        for ruta, (tipo, archivo) in TIPOS.items():
            r = await c.get(ruta)
            assert r.status_code == 200, ruta
            assert r.headers["content-type"].split(";")[0] == tipo, ruta
            assert r.content == (CARPETA / archivo).read_bytes(), ruta
            _cabeceras_ok(r)
            assert "set-cookie" not in r.headers
        head = await c.head("/consola")
    assert head.status_code == 200


async def test_tambien_en_el_oyente_local_y_con_cualquier_authorization(gateway):
    async with cliente_local(gateway.app) as c:
        local = await c.get("/consola")
    async with cliente_publico(gateway.app, clave="psg_000000000000_no-existe") as c:
        clave_mala = await c.get("/consola/app.js")
    assert local.status_code == 200
    _cabeceras_ok(local)
    # La consola no mira la clave: los estáticos son iguales para todos.
    assert clave_mala.status_code == 200


async def test_la_raiz_y_la_barra_final_redirigen(gateway):
    async with cliente_publico(gateway.app, clave=None) as c:
        for ruta in ("/", "/consola/"):
            r = await c.get(ruta, follow_redirects=False)
            assert r.status_code == 307, ruta
            assert r.headers["location"] == "/consola"
            _cabeceras_ok(r)


async def test_solo_get_y_las_rutas_exactas(gateway):
    async with cliente_publico(gateway.app, clave=None) as c:
        post = await c.post("/consola", json={})
        otra = await c.get("/consola/otra.js")
        arriba = await c.get("/consola/../v1/compras")
        indice = await c.get("/consola/index.html")
    assert post.status_code == 405
    assert otra.status_code == 404 and otra.json()["error"]["codigo"] == "no_encontrado"
    assert indice.status_code == 404
    assert arriba.status_code in (401, 404)


async def test_la_consola_no_abre_la_api(gateway):
    async with cliente_publico(gateway.app, clave=None) as c:
        compras = await c.get("/v1/compras")
        a2a = await c.post("/a2a/m/merchant_1", json={})
        guard = await c.get("/guard/state")
        docs = await c.get("/v1/docs")
    assert compras.status_code == 401
    assert a2a.status_code == 401
    assert guard.status_code == 404
    assert docs.status_code == 200
    async with cliente_publico(gateway.app) as c:
        con_clave = await c.get("/v1/comercios")
    assert con_clave.status_code == 200


async def test_con_la_consola_apagada_no_existe(config, stub):
    mundo = armar_gateway(config, stub)
    async with cliente_publico(mundo.app, clave=None) as c:
        respuestas = {ruta: await c.get(ruta, follow_redirects=False) for ruta in [*TIPOS, "/"]}
    for ruta, r in respuestas.items():
        assert r.status_code == 404, ruta
        assert r.json()["error"]["codigo"] == "no_encontrado"
    async with cliente_local(mundo.app) as c:
        local = await c.get("/consola")
    assert local.status_code == 404
    assert not any(getattr(r, "path", "").startswith("/consola") for r in mundo.app.routes)


def test_rutas_abiertas_no_puede_abrir_rutas_protegidas():
    registro = RegistroIntegradores.vacio()
    for ruta in ("/v1", "/v1/compras", "/guard/state", "/a2a/m/merchant_1", "/a2a"):
        with pytest.raises(ValueError, match="rutas protegidas"):
            AutenticacionGateway(Starlette(), registro=registro, rutas_abiertas={ruta})
    assert consola.RUTAS_ABIERTAS == {"/", "/consola", "/consola/", *TIPOS}


async def test_sin_los_archivos_la_consola_da_503(tmp_path, caplog):
    import httpx

    app = Starlette(routes=consola.rutas_consola(tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x") as c:
        r = await c.get("/consola")
        redirige = await c.get("/", follow_redirects=False)
    assert r.status_code == 503 and r.json() == {"error": "consola_no_disponible"}
    _cabeceras_ok(r)
    assert redirige.status_code == 307
    assert "consola web no está disponible" in caplog.text


def test_los_archivos_que_sirve_son_los_de_la_carpeta():
    estaticos = {
        p.name for p in CARPETA.iterdir() if p.is_file() and p.suffix in (".html", ".js", ".mjs", ".css")
    }
    assert estaticos == {archivo for _tipo, archivo in TIPOS.values()}
    assert {r: a for r, (a, _t) in consola.ARCHIVOS.items()} == {r: a for r, (_t, a) in TIPOS.items()}


def test_python_m_proxy_la_prende_con_gateway_consola():
    from proxy.__main__ import consola_del_entorno

    assert consola_del_entorno({}) is False
    assert consola_del_entorno({"GATEWAY_CONSOLA": ""}) is False
    assert consola_del_entorno({"GATEWAY_CONSOLA": "1"}) is True
    assert consola_del_entorno({"GATEWAY_CONSOLA": "false"}) is False
    with pytest.raises(ValueError, match="GATEWAY_CONSOLA"):
        consola_del_entorno({"GATEWAY_CONSOLA": "quizás"})


# -- Vercel --------------------------------------------------------------------------


async def test_la_app_de_vercel_sirve_la_consola():
    app = _serverless()
    rutas = {getattr(r, "path", "") for r in app.routes}
    assert {"/", "/consola", "/consola/", *TIPOS} <= rutas
    async with _cliente(app, clave=None) as c:
        pagina = await c.get("/consola")
        raiz = await c.get("/", follow_redirects=False)
        script = await c.get("/consola/logica.mjs")
        compras = await c.get("/v1/compras")
    assert pagina.status_code == 200 and b"KaiAI" in pagina.content
    _cabeceras_ok(pagina)
    assert raiz.status_code == 307 and raiz.headers["location"] == "/consola"
    assert script.headers["content-type"].startswith("text/javascript")
    assert compras.status_code == 401


# -- chequeo estático ------------------------------------------------------------------


def _texto(nombre: str) -> str:
    return (CARPETA / nombre).read_text(encoding="utf-8")


@pytest.mark.parametrize("archivo", ["app.js", "logica.mjs", "recibo.mjs", "index.html"])
def test_el_js_no_arma_html_con_texto(archivo):
    texto = _texto(archivo)
    for prohibido in (
        "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "createContextualFragment",
        "DOMParser", "srcdoc", "eval(", "new Function", "setTimeout(\"", "setInterval(\"",
        "console.", "localStorage", ".style", "setAttribute(\"style", "setAttribute(\"on",
    ):
        assert prohibido not in texto, f"{archivo} usa {prohibido}"


@pytest.mark.parametrize("archivo", ["app.js", "logica.mjs", "recibo.mjs", "index.html", "estilo.css"])
def test_sin_urls_externas(archivo):
    urls = set(re.findall(r"""(?i)\b(?:https?:)?//[a-z0-9.-]+\.[a-z]{2,}[^\s"'`<>)]*""", _texto(archivo)))
    assert urls <= URLS_PERMITIDAS, f"{archivo}: {sorted(urls - URLS_PERMITIDAS)}"
    if archivo == "estilo.css":
        assert "@import" not in _texto(archivo)
        assert "url(" not in _texto(archivo)


def test_el_html_no_tiene_scripts_ni_estilos_en_linea():
    html = _texto("index.html")
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", html, re.S | re.I)
    assert scripts == [(' type="module" src="/consola/app.js"', "")]
    assert "<style" not in html.lower()
    assert not re.search(r"\sstyle\s*=", html, re.I)
    assert not re.search(r"\son[a-z]+\s*=", html, re.I)
    for ref in re.findall(r"""(?:src|href)="([^"]+)\"""", html):
        assert ref.startswith(("/consola/", "/v1/", "#", "data:image/svg+xml,")) or ref == "/panel", ref
    # El link al panel del cliente (y el panel tiene uno a la consola).
    assert '<a href="/panel">Panel</a>' in html
    assert '<html lang="es">' in html
    # El campo de la clave: password y sin name (un formulario nunca la manda).
    (campo,) = re.findall(r"<input id=\"clave\"[^>]*>", html, re.S)
    assert 'type="password"' in campo and "name=" not in campo and 'autocomplete="off"' in campo


def test_la_clave_nunca_va_al_registro():
    app = _texto("app.js")
    for linea in app.splitlines():
        if "registrar(" in linea:
            assert "estado.clave" not in linea and '$("clave")' not in linea, linea
    # Solo viaja en el header Authorization, a rutas de este origen.
    usos = [linea.strip() for linea in app.splitlines() if "estado.clave" in linea]
    assert all(
        u.startswith(("clave: null", "estado.clave =", "if (conClave && estado.clave)",
                      "headers.Authorization = `Bearer ${estado.clave}`",
                      "if (!estado.rutaA2a || !esIdCompra(compraId) || !estado.clave)",
                      "if (!estado.clave)", '$("recordar").addEventListener'))
        for u in usos
    ), usos
    assert 'fetch(ruta' in app and 'fetch(estado.rutaA2a' in app
    assert app.count("fetch(") == 2
    assert app.count('redirect: "error"') == 2 and app.count('credentials: "omit"') == 2


def test_los_comportamientos_son_los_de_scenarios_json():
    datos = json.loads(ESCENARIOS_JSON.read_text(encoding="utf-8"))
    generales = sorted(k for k, v in datos["scenarios"].items() if v.get("tipo") == "general")
    bloque = _texto("logica.mjs").split("export const ESCENARIOS", 1)[1].split("]);", 1)[0]
    en_js = sorted(re.findall(r'id: "([a-z_]+)"', bloque))
    assert en_js == generales
    assert "general_entrega_partes" in generales


# -- bundle ------------------------------------------------------------------------------


def test_los_estaticos_entran_en_el_bundle():
    for _tipo, archivo in TIPOS.values():
        ruta = f"proxy/consola/{archivo}"
        assert se_sube(ruta), f"{ruta} no se subiría a Vercel"
        assert not _excluido(ruta), f"excludeFiles saca {ruta}"
    assert se_sube("proxy/consola/__init__.py") and not _excluido("proxy/consola/__init__.py")
    # Los tests de JS no.
    assert not se_sube("proxy/tests/js/consola.test.mjs")


# -- el recibo del motor, verificado con la lógica del navegador --------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
@pytest.mark.parametrize("alterar", [False, True])
async def test_un_recibo_del_motor_se_verifica_con_la_logica_de_la_consola(tmp_path, alterar):
    app = _serverless()
    async with _cliente(app) as c:
        compra = await crear_compra(c)
        busqueda = await buscar_compra(c, compra["compra_id"], escenario="baseline")
        contexto = busqueda.json()["result"]["contextId"]
        await checkout_compra(c, compra["compra_id"], contexto, "cart_1")
        vista = (await c.get(f"/v1/compras/{compra['compra_id']}")).json()
        publica = (await c.get("/v1/public-key")).json()
    (veredicto,) = vista["veredictos"]
    recibo = dict(veredicto["recibo"])
    if alterar:
        h, p, f = recibo["jws"].split(".")
        recibo["jws"] = f"{h}.{p[:-2]}{'A' if p[-2] != 'A' else 'B'}{p[-1]}.{f}"
    entrada = tmp_path / "recibo.json"
    entrada.write_text(json.dumps({"recibo": recibo, "publica": publica}), encoding="utf-8")
    salida = subprocess.run(
        ["node", str(RAIZ / "proxy" / "tests" / "js" / "verificar_recibo_cli.mjs"), str(entrada)],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=True,
    )
    resultado = json.loads(salida.stdout)
    assert resultado["ok"] is (not alterar), resultado
    if not alterar:
        assert resultado["id"] == recibo["id"]

