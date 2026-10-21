"""El panel del cliente (`/panel`, `proxy/panel/`), fase 2.

- Con `create_app(..., panel=True)`: la página y sus estáticos, sin clave,
  en los dos oyentes, con la CSP estricta, `nosniff`, `no-referrer` y
  `no-store`; `/panel/` → 307 a `/panel`. Sin el panel, nada de eso existe.
  El panel no abre nada de la API: `/v1` y `/a2a` siguen pidiendo clave.
- `/panel/recibo.mjs` es el mismo archivo que `/consola/recibo.mjs` (una sola
  verificación del recibo).
- La app de Vercel lo sirve, junto con la consola; en `python -m proxy`,
  `GATEWAY_PANEL=1`.
- Un chequeo estático del JS y el HTML: nada de `innerHTML` y parecidos, ni
  scripts o estilos en línea, ni URLs externas; cada `$("#id")` existe en el
  HTML; la clave solo viaja en `Authorization` y nunca se escribe en la
  página; los `compra_id` se validan antes de ir a una URL.
- Los estáticos entran en el bundle de Vercel; los tests de JS no.
- Un recibo firmado por el motor se verifica con la lógica del panel
  (`node`, WebCrypto), leyendo la compra con una clave de solo lectura.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from starlette.applications import Starlette

from proxy import consola, panel
from proxy.claves import AutenticacionGateway, RegistroIntegradores
from proxy.tests.gateway_ayudantes import armar_gateway, cliente_local, cliente_publico
from proxy.tests.gateway_ayudantes import buscar as buscar_compra
from proxy.tests.gateway_ayudantes import checkout as checkout_compra
from proxy.tests.gateway_ayudantes import crear_compra
from proxy.tests.test_despliegue_vercel import _excluido, se_sube
from proxy.tests.test_vercel_entrada import _cliente, _serverless

pytestmark = pytest.mark.anyio

RAIZ = Path(__file__).resolve().parents[2]
CARPETA = RAIZ / "proxy" / "panel"
RECIBO = RAIZ / "proxy" / "consola" / "recibo.mjs"
TIPOS = {
    "/panel": ("text/html", CARPETA / "index.html"),
    "/panel/app.js": ("text/javascript", CARPETA / "app.js"),
    "/panel/logica.mjs": ("text/javascript", CARPETA / "logica.mjs"),
    "/panel/vista.mjs": ("text/javascript", CARPETA / "vista.mjs"),
    "/panel/estilo.css": ("text/css", CARPETA / "estilo.css"),
    "/panel/recibo.mjs": ("text/javascript", RECIBO),
}
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
# Identificadores con forma de URL que no se piden nunca: el namespace del SVG
# (el favicon y los íconos que arma vista.mjs).
URLS_PERMITIDAS = {"http://www.w3.org/2000/svg"}
JS = ("app.js", "logica.mjs", "vista.mjs")


def _cabeceras_ok(respuesta) -> None:
    assert respuesta.headers["content-security-policy"] == CSP
    assert respuesta.headers["x-content-type-options"] == "nosniff"
    assert respuesta.headers["referrer-policy"] == "no-referrer"
    assert respuesta.headers["cache-control"] == "no-store"


@pytest.fixture
def gateway(config, stub):
    return armar_gateway(config, stub, panel=True)


# -- rutas y cabeceras -------------------------------------------------------------


async def test_sirve_la_pagina_y_los_estaticos_sin_clave(gateway):
    async with cliente_publico(gateway.app, clave=None) as c:
        for ruta, (tipo, archivo) in TIPOS.items():
            r = await c.get(ruta)
            assert r.status_code == 200, ruta
            assert r.headers["content-type"].split(";")[0] == tipo, ruta
            assert r.headers["content-type"].endswith("charset=utf-8"), ruta
            assert r.content == archivo.read_bytes(), ruta
            _cabeceras_ok(r)
            assert "set-cookie" not in r.headers
        head = await c.head("/panel")
    assert head.status_code == 200


async def test_el_recibo_es_el_mismo_archivo_que_el_de_la_consola(config, stub):
    mundo = armar_gateway(config, stub, panel=True, consola=True)
    async with cliente_publico(mundo.app, clave=None) as c:
        del_panel = await c.get("/panel/recibo.mjs")
        de_la_consola = await c.get("/consola/recibo.mjs")
    assert del_panel.status_code == de_la_consola.status_code == 200
    assert del_panel.content == de_la_consola.content == RECIBO.read_bytes()


async def test_tambien_en_el_oyente_local_y_con_cualquier_authorization(gateway):
    async with cliente_local(gateway.app) as c:
        local = await c.get("/panel")
    async with cliente_publico(gateway.app, clave="psg_000000000000_no-existe") as c:
        clave_mala = await c.get("/panel/app.js")
    assert local.status_code == 200
    _cabeceras_ok(local)
    # El panel no mira la clave: los estáticos son iguales para todos.
    assert clave_mala.status_code == 200


async def test_la_barra_final_redirige(gateway):
    async with cliente_publico(gateway.app, clave=None) as c:
        r = await c.get("/panel/", follow_redirects=False)
        raiz = await c.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/panel"
    _cabeceras_ok(r)
    # La raíz es de la consola: con el panel solo, no existe.
    assert raiz.status_code == 404


async def test_solo_get_y_las_rutas_exactas(gateway):
    async with cliente_publico(gateway.app, clave=None) as c:
        post = await c.post("/panel", json={})
        put = await c.put("/panel/app.js", json={})
        otra = await c.get("/panel/otra.js")
        arriba = await c.get("/panel/../v1/compras")
        indice = await c.get("/panel/index.html")
        compra = await c.get("/panel/compra/cmp-0123456789abcdef")
    assert post.status_code == 405 and put.status_code == 405
    assert otra.status_code == 404 and otra.json()["error"]["codigo"] == "no_encontrado"
    assert indice.status_code == 404 and compra.status_code == 404
    assert arriba.status_code in (401, 404)


async def test_el_panel_no_abre_la_api(gateway):
    async with cliente_publico(gateway.app, clave=None) as c:
        compras = await c.get("/v1/compras")
        resumen = await c.get("/v1/resumen")
        a2a = await c.post("/a2a/m/merchant_1", json={})
        guard = await c.get("/guard/state")
        publica = await c.get("/v1/public-key")
    assert compras.status_code == resumen.status_code == a2a.status_code == 401
    assert guard.status_code == 404
    # La clave pública no pide clave (con el stub no hay clave de recibos: 503).
    assert publica.status_code in (200, 503)
    async with cliente_publico(gateway.app) as c:
        con_clave = await c.get("/v1/resumen")
    assert con_clave.status_code == 200


async def test_con_el_panel_apagado_no_existe(config, stub):
    mundo = armar_gateway(config, stub)
    async with cliente_publico(mundo.app, clave=None) as c:
        respuestas = {ruta: await c.get(ruta, follow_redirects=False) for ruta in [*TIPOS, "/panel/"]}
    for ruta, r in respuestas.items():
        assert r.status_code == 404, ruta
        assert r.json()["error"]["codigo"] == "no_encontrado"
    async with cliente_local(mundo.app) as c:
        local = await c.get("/panel")
    assert local.status_code == 404
    assert not any(getattr(r, "path", "").startswith("/panel") for r in mundo.app.routes)


async def test_panel_y_consola_juntos(config, stub):
    mundo = armar_gateway(config, stub, panel=True, consola=True)
    async with cliente_publico(mundo.app, clave=None) as c:
        pagina = await c.get("/panel")
        la_consola = await c.get("/consola")
        raiz = await c.get("/", follow_redirects=False)
    assert pagina.status_code == la_consola.status_code == 200
    assert raiz.status_code == 307 and raiz.headers["location"] == "/consola"


def test_rutas_abiertas_son_las_del_panel_y_no_abren_la_api():
    assert panel.RUTAS_ABIERTAS == {"/panel/", *TIPOS}
    assert not panel.RUTAS_ABIERTAS & consola.RUTAS_ABIERTAS
    # El middleware no deja abrir nada protegido (ni con el panel).
    registro = RegistroIntegradores.vacio()
    AutenticacionGateway(Starlette(), registro=registro, rutas_abiertas=panel.RUTAS_ABIERTAS)
    for ruta in ("/v1/compras", "/a2a/m/merchant_1", "/guard/state"):
        with pytest.raises(ValueError, match="rutas protegidas"):
            AutenticacionGateway(
                Starlette(), registro=registro, rutas_abiertas={*panel.RUTAS_ABIERTAS, ruta},
            )


async def test_sin_los_archivos_el_panel_da_503(tmp_path, caplog):
    import httpx

    app = Starlette(routes=panel.rutas_panel(tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x") as c:
        r = await c.get("/panel")
        redirige = await c.get("/panel/", follow_redirects=False)
    assert r.status_code == 503 and r.json() == {"error": "panel_no_disponible"}
    _cabeceras_ok(r)
    assert redirige.status_code == 307
    assert "panel del cliente no está disponible" in caplog.text


def test_los_archivos_que_sirve_son_los_de_la_carpeta():
    estaticos = {
        p.name for p in CARPETA.iterdir() if p.is_file() and p.suffix in (".html", ".js", ".mjs", ".css")
    }
    assert estaticos == {a.name for _t, a in TIPOS.values() if a.parent == CARPETA}
    servidos = {ruta: (CARPETA / archivo).resolve() for ruta, (archivo, _t) in panel.ARCHIVOS.items()}
    assert servidos == {ruta: archivo.resolve() for ruta, (_t, archivo) in TIPOS.items()}


def test_python_m_proxy_lo_prende_con_gateway_panel():
    from proxy.__main__ import panel_del_entorno

    assert panel_del_entorno({}) is False
    assert panel_del_entorno({"GATEWAY_PANEL": ""}) is False
    assert panel_del_entorno({"GATEWAY_PANEL": "1"}) is True
    assert panel_del_entorno({"GATEWAY_PANEL": "false"}) is False
    with pytest.raises(ValueError, match="GATEWAY_PANEL"):
        panel_del_entorno({"GATEWAY_PANEL": "quizás"})


# -- Vercel --------------------------------------------------------------------------


async def test_la_app_de_vercel_sirve_el_panel():
    app = _serverless()
    rutas = {getattr(r, "path", "") for r in app.routes}
    assert {"/panel/", *TIPOS} <= rutas
    async with _cliente(app, clave=None) as c:
        pagina = await c.get("/panel")
        script = await c.get("/panel/logica.mjs")
        recibo = await c.get("/panel/recibo.mjs")
        compras = await c.get("/v1/compras")
    assert pagina.status_code == 200 and b"KaiAI" in pagina.content
    _cabeceras_ok(pagina)
    assert script.headers["content-type"].startswith("text/javascript")
    assert recibo.content == RECIBO.read_bytes()
    assert compras.status_code == 401


# -- chequeo estático ------------------------------------------------------------------


def _texto(nombre: str) -> str:
    return (CARPETA / nombre).read_text(encoding="utf-8")


@pytest.mark.parametrize("archivo", [*JS, "index.html"])
def test_el_js_no_arma_html_con_texto(archivo):
    texto = _texto(archivo)
    for prohibido in (
        "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "createContextualFragment",
        "DOMParser", "srcdoc", "eval(", "new Function", "setTimeout(\"", "setInterval(\"",
        "console.", "localStorage", ".style", "setAttribute(\"style", "setAttribute(\"on",
        "setAttribute(\"href", "javascript:",
    ):
        assert prohibido not in texto, f"{archivo} usa {prohibido}"


@pytest.mark.parametrize("archivo", [*JS, "index.html", "estilo.css"])
def test_sin_urls_externas(archivo):
    urls = set(re.findall(r"""(?i)\b(?:https?:)?//[a-z0-9.-]+\.[a-z]{2,}[^\s"'`<>)]*""", _texto(archivo)))
    assert urls <= URLS_PERMITIDAS, f"{archivo}: {sorted(urls - URLS_PERMITIDAS)}"
    if archivo == "estilo.css":
        assert "@import" not in _texto(archivo)
        assert "url(" not in _texto(archivo)


def test_el_html_no_tiene_scripts_ni_estilos_en_linea():
    html = _texto("index.html")
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", html, re.S | re.I)
    assert scripts == [(' type="module" src="/panel/app.js"', "")]
    assert "<style" not in html.lower()
    assert not re.search(r"\sstyle\s*=", html, re.I)
    assert not re.search(r"\son[a-z]+\s*=", html, re.I)
    for ref in re.findall(r"""(?:src|href)="([^"]+)\"""", html):
        assert ref.startswith(("/panel/", "/consola", "/v1/docs", "#", "data:image/svg+xml,")), ref
    assert '<html lang="es">' in html
    assert '<a href="/consola">Consola</a>' in html
    # El campo de la clave: password y sin name (un formulario nunca la manda).
    (campo,) = re.findall(r"<input id=\"clave\"[^>]*>", html, re.S)
    assert 'type="password"' in campo and "name=" not in campo and 'autocomplete="off"' in campo
    # Las regiones que anuncian cargas y errores.
    for region in ("entrada-estado", "resumen-estado", "lista-estado", "detalle-estado", "verificacion"):
        (etiqueta,) = re.findall(rf"<[a-z]+ id=\"{region}\"[^>]*>", html)
        assert 'aria-live="polite"' in etiqueta or 'role="status"' in etiqueta, region
    for region in ("lista-error", "detalle-error"):
        (etiqueta,) = re.findall(rf"<[a-z]+ id=\"{region}\"[^>]*>", html)
        assert 'role="alert"' in etiqueta, region


def test_cada_id_que_usa_el_js_esta_en_el_html():
    html = _texto("index.html")
    ids = set()
    for archivo in JS:
        ids |= set(re.findall(r"""\$\("#([\w-]+)"\)""", _texto(archivo)))
    assert {"clave", "salir", "filas", "cargar-mas", "verificar", "volver"} <= ids, sorted(ids)
    faltan = sorted(i for i in ids if f'id="{i}"' not in html)
    assert faltan == []
    # Ningún id repetido en el HTML.
    todos = re.findall(r'\sid="([^"]+)"', html)
    assert len(todos) == len(set(todos))


def test_la_clave_solo_viaja_en_authorization():
    app = _texto("app.js")
    usos = [linea.strip() for linea in app.splitlines() if "estado.clave" in linea]
    assert usos, "app.js no usa estado.clave"
    permitidos = (
        "estado.clave = valor;", "estado.clave = null;",
        "if (conClave && estado.clave) headers.Authorization = `Bearer ${estado.clave}`;",
        "if (!estado.clave) return;",
    )
    assert all(u.startswith(permitidos) for u in usos), usos
    # Un solo fetch, a rutas de /v1 de este origen, sin cookies ni redirecciones.
    assert app.count("fetch(") == 1
    assert 'credentials: "omit"' in app and 'redirect: "error"' in app
    assert 'if (!ruta.startsWith("/v1/")) throw' in app
    # La clave nunca se escribe en la página ni en un atributo.
    for linea in app.splitlines():
        if "textContent" in linea or "setAttribute" in linea or ".value =" in linea:
            assert "estado.clave" not in linea, linea
    # sessionStorage solo con "recordar en esta pestaña" (y el tema).
    assert app.count("sessionStorage.setItem(") == 2


def test_el_panel_usa_lo_que_da_la_api():
    """La API filtra por resultado y por intento de estafa, dice de quién es
    la clave (`GET /v1/clave`) y trae los datos de la compra en la
    explicación: el panel no filtra en el navegador ni manda el HEAD, y el
    detalle es un solo pedido (la compra entera, solo para verificar la firma)."""
    app = _texto("app.js")
    logica = _texto("logica.mjs")
    assert '"/v1/clave"' in app and "HEAD" not in app
    assert "filtrosDeLaApi(estado.filtros)" in app
    for viejo in ("planResultado", "filtrarPorResultado", "seguirCargando", "MAX_PAGINAS_POR_CARGA"):
        assert viejo not in app and viejo not in logica, viejo
    (abrir,) = re.findall(r"async function abrirCompra\(id\) \{.*?\n\}", app, re.S)
    assert abrir.count("pedirApi(") == 1 and "/explicacion`" in abrir
    assert 'id="filtro-nota"' not in _texto("index.html")


def test_los_ids_de_compra_se_validan_antes_de_la_url():
    app = _texto("app.js")
    rutas = re.findall(r"`/v1/compras/\$\{([^}]+)\}", app)
    assert rutas, "app.js no arma rutas de compra"
    # Siempre con un id que pasó por `esIdCompra` (lo dice el nombre).
    assert set(rutas) == {"id"}, rutas
    for bloque in re.findall(r"async function [a-zA-Z]+\(id[^)]*\) \{.*?\n\}", app, re.S):
        if "/v1/compras/${id}" in bloque:
            assert "if (!esIdCompra(id))" in bloque, bloque[:80]
    assert "ID_COMPRA_RE = /^cmp-[0-9a-f]{16}$/" in _texto("logica.mjs")


# -- bundle ------------------------------------------------------------------------------


def test_los_estaticos_entran_en_el_bundle():
    for _tipo, archivo in TIPOS.values():
        ruta = archivo.relative_to(RAIZ).as_posix()
        assert se_sube(ruta), f"{ruta} no se subiría a Vercel"
        assert not _excluido(ruta), f"excludeFiles saca {ruta}"
    assert se_sube("proxy/panel/__init__.py") and not _excluido("proxy/panel/__init__.py")
    for ruta in ("proxy/tests/js/panel.test.mjs", "proxy/tests/js/panel_vista.test.mjs",
                 "proxy/tests/js/verificar_recibo_panel_cli.mjs"):
        assert not se_sube(ruta) or _excluido(ruta), f"{ruta} llega al bundle"


# -- el recibo del motor, verificado con la lógica del panel ------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
@pytest.mark.parametrize("alterar", [False, True])
async def test_un_recibo_del_motor_se_verifica_con_la_logica_del_panel(tmp_path, alterar):
    """Como el panel: la explicación da el id del recibo, la compra (leída con
    la misma clave) trae el JWS y se verifica contra `GET /v1/public-key`."""
    app = _serverless()
    async with _cliente(app) as c:
        compra = await crear_compra(c)
        busqueda = await buscar_compra(c, compra["compra_id"], escenario="baseline")
        contexto = busqueda.json()["result"]["contextId"]
        await checkout_compra(c, compra["compra_id"], contexto, "cart_1")
        explicacion = (await c.get(f"/v1/compras/{compra['compra_id']}/explicacion")).json()
        vista = (await c.get(f"/v1/compras/{compra['compra_id']}")).json()
        publica = (await c.get("/v1/public-key")).json()
    assert explicacion["recibo"]["firmado"] is True
    if alterar:
        (veredicto,) = vista["veredictos"]
        h, p, f = veredicto["recibo"]["jws"].split(".")
        veredicto["recibo"]["jws"] = f"{h}.{p[:-2]}{'A' if p[-2] != 'A' else 'B'}{p[-1]}.{f}"
    entrada = tmp_path / "recibo.json"
    entrada.write_text(
        json.dumps({"explicacion": explicacion, "compra": vista, "publica": publica}), encoding="utf-8",
    )
    salida = subprocess.run(
        ["node", str(RAIZ / "proxy" / "tests" / "js" / "verificar_recibo_panel_cli.mjs"), str(entrada)],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=True,
    )
    resultado = json.loads(salida.stdout)
    assert resultado["ok"] is (not alterar), resultado
    assert resultado["id"] == explicacion["recibo"]["id"]
