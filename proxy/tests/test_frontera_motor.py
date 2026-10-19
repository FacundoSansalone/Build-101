"""La frontera del motor: lo que se despliega no importa la simulación.

El motor es lo que declara `[tool.kaiai.motor]` en `pyproject.toml`
(`paquetes` menos `excluidos`). Nunca importa los módulos `prohibidos`
(demo_world, world, dashboard, el sample de AP2 y google.adk): en Vercel no
están, y lo que viene de la demo entra por inyección (`demo=`, ver
`proxy.demo_local`).

Dos chequeos:

(a) estático, con `ast`, sobre cada archivo del motor: ningún import de una
    raíz prohibida, tampoco dentro de una función, en un `try` o con
    `importlib.import_module("...")` / `__import__("...")` literal; y ningún
    import de un módulo excluido (salvo la entrada local, `python -m proxy`,
    que inyecta la demo).
(b) en un subproceso aislado, con un `MetaPathFinder` que bloquea las raíces
    prohibidas: se importan todos los módulos del motor, se arma
    `create_app(..., demo=None)` con almacén en memoria y clave de API en
    memoria, y se piden `/v1/openapi.json`, `/v1/public-key`, `/v1/evaluar` y
    `/v1/filtrar`. Al final ningún módulo prohibido quedó en `sys.modules` y
    nadie intentó importar uno (un `try/except ImportError` no lo esconde).
"""

from __future__ import annotations

import ast
import fnmatch
import json
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from contracts import Ficha, Requisito

RAIZ = Path(__file__).resolve().parents[2]

# La única entrada del motor que puede importar lo excluido: `python -m proxy`
# (servidor local) le pasa `FuentesDemo()` a `create_app` (se importa dentro
# de `main()`, no al importar el módulo). En Vercel la entrada es otra.
IMPORTACIONES_LOCALES = frozenset({("proxy/__main__.py", "proxy.demo_local")})


def frontera() -> dict[str, list[str]]:
    datos = tomllib.loads((RAIZ / "pyproject.toml").read_text("utf-8"))
    return datos["tool"]["kaiai"]["motor"]


def excluido(ruta: str, patrones: list[str]) -> bool:
    """¿La ruta (relativa a la raíz) o alguna de sus carpetas cae en un patrón?"""
    partes = PurePosixPath(ruta).parts
    prefijos = ["/".join(partes[: i + 1]) for i in range(len(partes))]
    return any(fnmatch.fnmatchcase(p, patron) for p in prefijos for patron in patrones)


def archivos_del_motor(datos: dict[str, list[str]] | None = None) -> list[str]:
    """Los `.py` de los paquetes del motor (sin lo excluido) y sus `entradas`
    fuera de los paquetes (`index.py`, la de Vercel)."""
    datos = datos or frontera()
    archivos = []
    for paquete in datos["paquetes"]:
        for archivo in sorted((RAIZ / paquete).rglob("*.py")):
            ruta = archivo.relative_to(RAIZ).as_posix()
            if "__pycache__" not in ruta and not excluido(ruta, datos["excluidos"]):
                archivos.append(ruta)
    archivos += [e for e in datos.get("entradas", []) if (RAIZ / e).is_file()]
    return archivos


def modulo_de(ruta: str) -> str:
    partes = list(PurePosixPath(ruta).with_suffix("").parts)
    if partes[-1] == "__init__":
        partes.pop()
    return ".".join(partes)


def _base_relativa(ruta: str, nivel: int) -> str:
    """El paquete desde el que se resuelve un import relativo de `nivel` puntos."""
    paquete = modulo_de(ruta).split(".")
    if not ruta.endswith("__init__.py"):
        paquete.pop()
    return ".".join(paquete[: len(paquete) - (nivel - 1)] if nivel > 1 else paquete)


def _unir(*partes: str | None) -> str:
    return ".".join(p for p in partes if p)


def _es_import_dinamico(funcion: ast.expr) -> bool:
    """`importlib.import_module(...)`, `import_module(...)` o `__import__(...)`."""
    if isinstance(funcion, ast.Attribute):
        return funcion.attr == "import_module"
    return isinstance(funcion, ast.Name) and funcion.id in {"import_module", "__import__"}


def importaciones(fuente: str, ruta: str) -> list[tuple[int, str]]:
    """Todo lo que importa un archivo: (línea, módulo), en cualquier lugar.

    `from a import b` cuenta como `a` y como `a.b` (puede ser un submódulo:
    `from google import adk`); los relativos se resuelven contra el paquete.
    """
    encontrados: list[tuple[int, str]] = []
    for nodo in ast.walk(ast.parse(fuente, filename=ruta)):
        if isinstance(nodo, ast.Import):
            encontrados += [(nodo.lineno, alias.name) for alias in nodo.names]
        elif isinstance(nodo, ast.ImportFrom):
            base = _base_relativa(ruta, nodo.level) if nodo.level else ""
            modulo = _unir(base, nodo.module)
            if modulo:
                encontrados.append((nodo.lineno, modulo))
            encontrados += [
                (nodo.lineno, _unir(modulo, alias.name)) for alias in nodo.names if alias.name != "*"
            ]
        elif (
            isinstance(nodo, ast.Call)
            and _es_import_dinamico(nodo.func)
            and nodo.args
            and isinstance(nodo.args[0], ast.Constant)
            and isinstance(nodo.args[0].value, str)
        ):
            encontrados.append((nodo.lineno, nodo.args[0].value))
    return encontrados


def prohibido(modulo: str, prohibidos: list[str]) -> bool:
    return any(modulo == p or modulo.startswith(p + ".") for p in prohibidos)


def ruta_de_modulo(modulo: str) -> str | None:
    """El archivo del repo de un módulo (`proxy.demo_local` → `proxy/demo_local.py`)."""
    base = modulo.replace(".", "/")
    for candidata in (f"{base}.py", f"{base}/__init__.py"):
        if (RAIZ / candidata).is_file():
            return candidata
    return None


# -- (a) estático ------------------------------------------------------------------


def test_la_frontera_esta_declarada_en_el_pyproject():
    datos = tomllib.loads((RAIZ / "pyproject.toml").read_text("utf-8"))
    assert datos["project"]["name"] == "kaiai-motor"
    assert datos["project"]["requires-python"] == ">=3.12,<3.13"
    assert datos["tool"]["uv"]["package"] is False
    assert (RAIZ / ".python-version").read_text("utf-8").strip() == "3.12"
    motor = frontera()
    for paquete in motor["paquetes"]:
        assert (RAIZ / paquete / "__init__.py").is_file(), paquete
    for entrada in motor.get("entradas", []):
        assert (RAIZ / entrada).is_file(), entrada
    # Cada exclusión apunta a algo que existe: si se renombra un archivo, la
    # lista (y la del despliegue) se actualiza a la vez.
    todos = [p.relative_to(RAIZ).as_posix() for q in motor["paquetes"] for p in (RAIZ / q).rglob("*")]
    for patron in motor["excluidos"]:
        assert any(excluido(ruta, [patron]) for ruta in todos), f"{patron} no excluye nada"
    assert {"demo_world", "world", "dashboard", "google.adk", "a2a", "ap2", "roles",
            "common"} <= set(motor["prohibidos"])


def test_lo_excluido_no_es_del_motor():
    archivos = set(archivos_del_motor())
    assert "proxy/app.py" in archivos and "guard/__init__.py" in archivos
    # La entrada de Vercel y su módulo, también.
    assert {"index.py", "proxy/vercel.py", "proxy/arranque.py"} <= archivos
    for fuera in ("proxy/demo_local.py", "proxy/tests/conftest.py", "guard/benchmark_hook.py",
                  "filtro/casos.py", "filtro/evaluar.py", "intencion/evaluar_intake.py",
                  "proxy/scripts/smoke_v1_general.py", "guard/tests/test_fuentes.py"):
        assert fuera not in archivos, fuera


def test_el_detector_ve_los_imports_escondidos():
    fuente = textwrap.dedent('''
        import importlib
        from . import a2a
        from .. import contracts
        from google import genai

        def perezoso():
            try:
                from demo_world.ap2_check import ap2_violations
            except ImportError:
                pass
            from google import adk
            importlib.import_module("roles.merchant_agent")
            __import__("dashboard.server")
            import a2a.types as tipos
    ''')
    vistos = importaciones(fuente, "proxy/x.py")
    prohibidos = frontera()["prohibidos"]
    malos = {modulo for _, modulo in vistos if prohibido(modulo, prohibidos)}
    assert malos == {
        "demo_world.ap2_check", "demo_world.ap2_check.ap2_violations", "google.adk",
        "roles.merchant_agent", "dashboard.server", "a2a.types",
    }
    # `from . import a2a` en proxy/ es `proxy.a2a` (nuestro), no el SDK de A2A.
    assert (3, "proxy.a2a") in vistos and (4, "contracts") in vistos
    assert not prohibido("google.genai", prohibidos) and not prohibido("proxy.a2a", prohibidos)


def test_el_motor_no_importa_la_simulacion():
    motor = frontera()
    violaciones = [
        f"{ruta}:{linea} importa {modulo}"
        for ruta in archivos_del_motor(motor)
        for linea, modulo in importaciones((RAIZ / ruta).read_text("utf-8"), ruta)
        if prohibido(modulo, motor["prohibidos"])
    ]
    assert violaciones == []


def test_el_motor_no_importa_lo_excluido():
    """En el despliegue lo excluido no está: un import (aunque sea perezoso)
    fallaría ahí. Solo la entrada local inyecta la demo."""
    motor = frontera()
    violaciones = []
    for ruta in archivos_del_motor(motor):
        for linea, modulo in importaciones((RAIZ / ruta).read_text("utf-8"), ruta):
            destino = ruta_de_modulo(modulo)
            if (
                destino is not None
                and excluido(destino, motor["excluidos"])
                and (ruta, modulo) not in IMPORTACIONES_LOCALES
            ):
                violaciones.append(f"{ruta}:{linea} importa {modulo} ({destino})")
    assert violaciones == []


def test_proxy_main_importa_la_demo_solo_al_arrancar():
    """`proxy.__main__` se importa en los tests y desde otras entradas: la
    demo local entra en `main()`, no al importar el módulo."""
    arbol = ast.parse((RAIZ / "proxy/__main__.py").read_text("utf-8"))
    arriba = [
        nodo for nodo in arbol.body
        if isinstance(nodo, (ast.Import, ast.ImportFrom))
        and "demo_local" in ast.unparse(nodo)
    ]
    assert arriba == []


# -- (b) en un subproceso, con las raíces prohibidas bloqueadas -----------------------

_SCRIPT = r'''
import base64, hashlib, importlib, importlib.abc, json, sys, tempfile, time
from pathlib import Path

params = json.loads(sys.argv[1])
sys.path.insert(0, params["raiz"])
PROHIBIDOS = tuple(params["prohibidos"])
intentos = []


def prohibido(nombre):
    return any(nombre == p or nombre.startswith(p + ".") for p in PROHIBIDOS)


class Frontera(importlib.abc.MetaPathFinder):
    """Nada de la simulación: cada intento queda registrado, aunque quien
    importa atrape el error."""

    def find_spec(self, nombre, path=None, target=None):
        if prohibido(nombre):
            intentos.append(nombre)
            raise ModuleNotFoundError(f"frontera del motor: {nombre}", name=nombre)
        return None


antes = sorted(m for m in sys.modules if prohibido(m))
sys.meta_path.insert(0, Frontera())
# Control: el venv de la demo trae el sample instalado; el bloqueo tiene que
# frenarlo (si no, la prueba no prueba nada).
try:
    importlib.import_module(PROHIBIDOS[0])
    control = "importó"
except ModuleNotFoundError:
    control = "bloqueado"
intentos.clear()

errores_import = {}
for modulo in params["modulos"]:
    try:
        importlib.import_module(modulo)
    except Exception as e:
        errores_import[modulo] = f"{type(e).__name__}: {e}"

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from starlette.testclient import TestClient

from filtro.nucleo import FiltroSeguridad
from proxy.app import create_app
from proxy.claves import RegistroIntegradores, generar_clave
from proxy.comercios import RegistroComercios
from proxy.config import Config
from proxy.estado import EstadoMemoria
from proxy.evaluador import cargar_evaluador
from proxy.tipos_gateway import Comercio


def b64url(datos):
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode("ascii")


def checkout(cart):
    """El data part `ap2.checkout` con un JWT ES256 de prueba (merchant_1)."""
    centavos = int(round(cart["amount"] * 100))
    iat = int(time.time())
    payload = {
        "id": cart["cart_id"],
        "merchant": {"id": "merchant_1", "name": "Comercio"},
        "line_items": [{"id": "li_1", "item": {"id": cart["cart_id"], "title": "x",
                                               "price": centavos}, "quantity": 1}],
        "currency": cart["currency"],
        "totals": [{"type": "subtotal", "amount": centavos}, {"type": "total", "amount": centavos}],
        "iat": iat,
        "exp": iat + 3600,
    }
    h = b64url(json.dumps({"alg": "ES256", "typ": "JWT", "kid": "k1"}).encode())
    p = b64url(json.dumps(payload, separators=(",", ":")).encode())
    der = ec.generate_private_key(ec.SECP256R1()).sign(f"{h}.{p}".encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    jwt = f"{h}.{p}.{b64url(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    return {
        "cart_id": cart["cart_id"], "checkout_jwt": jwt,
        "checkout_hash": b64url(hashlib.sha256(jwt.encode()).digest()),
        "amount": cart["amount"], "currency": cart["currency"],
    }


def sin_red(request):
    raise httpx.ConnectError(f"sin red en la prueba de frontera: {request.url}")


respuestas = {}
with tempfile.TemporaryDirectory() as tmp:
    clave, kid, sha = generar_clave()
    integradores = RegistroIntegradores.desde_dict(
        {"integradores": [{"id": "acme", "claves": [{"kid": kid, "sha256": sha, "activa": True}],
                           "comercios": ["merchant_1"]}]},
        {},
    )
    comercios = RegistroComercios(
        [Comercio(id="merchant_1", nombre="Comercio", a2a_url="https://comercio.example/a2a",
                  catalogo_url=None, merchant_ids=("merchant_1",))],
        "merchant_1",
    )
    config = Config(
        events_jsonl=None, api_token_path=None, guard_key_path=Path(tmp) / "guard.pem",
        permitir_stub=False, guard_activo=True, ficha_demo=False,
        gateway_public_url="https://gw.example.com", catalogo_url=None,
    )
    app = create_app(
        config, transport=httpx.MockTransport(sin_red), evaluador=cargar_evaluador(False),
        filtro=FiltroSeguridad(solo_heuristicas=True), filtro_modo="solo heurísticas",
        integradores=integradores, comercios=comercios, almacen=EstadoMemoria(), demo=None,
    )
    respuestas["demo"] = repr(app.state.proxy.demo)
    respuestas["ficha_demo"] = app.state.proxy.estado(con_token=True)["ficha_demo"]
    cart = {"cart_id": "cart_1", "item_label": "Monitor", "amount": 120.0, "currency": "USD"}
    with TestClient(app, base_url="http://gw.test:8030",
                    headers={"Authorization": f"Bearer {clave}"}) as c:
        r = c.get("/v1/openapi.json")
        respuestas["openapi"] = [r.status_code, "openapi" in r.json()]
        r = c.get("/v1/public-key")
        respuestas["public_key"] = [r.status_code, "jwk" in r.json()]
        r = c.post("/v1/evaluar", json={
            "comercio": "merchant_1",
            "ficha": {"pedido": "un monitor", "requisitos": [
                {"campo": "precio", "operador": "maximo", "valor": 150, "obligatorio": True},
                {"campo": "moneda", "operador": "igual", "valor": "USD", "obligatorio": True},
            ]},
            "ofertas": {"carts": [cart], "offer_details": []},
            "checkout": checkout(cart),
        })
        respuestas["evaluar"] = [r.status_code, r.json().get("status")]
        r = c.post("/v1/filtrar", json={"contenido": "un monitor de 27 pulgadas", "origen": "otro"})
        respuestas["filtrar"] = [r.status_code, r.json().get("status")]

print(json.dumps({
    "control": control,
    "antes": antes,
    "intentos": sorted(set(intentos)),
    "en_sys_modules": sorted(m for m in sys.modules if prohibido(m)),
    "errores_import": errores_import,
    "respuestas": respuestas,
}))
'''


@pytest.fixture(scope="module")
def corrida_aislada() -> dict[str, Any]:
    motor = frontera()
    modulos = sorted({modulo_de(ruta) for ruta in archivos_del_motor(motor)})
    params = {"raiz": str(RAIZ), "prohibidos": motor["prohibidos"], "modulos": modulos}
    # -I: sin PYTHONPATH ni el directorio actual (la demo local pone world/ en
    # el path); el venv sí trae el sample instalado, por eso el bloqueo.
    resultado = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", "-c", _SCRIPT, json.dumps(params)],
        cwd=RAIZ, capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    assert resultado.returncode == 0, resultado.stderr[-4000:]
    return json.loads(resultado.stdout.strip().splitlines()[-1])


def test_el_motor_se_importa_entero_sin_la_simulacion(corrida_aislada):
    assert corrida_aislada["control"] == "bloqueado"  # el bloqueo funciona
    assert corrida_aislada["antes"] == []  # el arranque de Python no trajo nada
    assert corrida_aislada["errores_import"] == {}
    assert corrida_aislada["intentos"] == []
    assert corrida_aislada["en_sys_modules"] == []


def test_la_app_sin_demo_atiende_v1(corrida_aislada):
    respuestas = corrida_aislada["respuestas"]
    assert respuestas["demo"] == "None"
    assert respuestas["ficha_demo"] is False
    assert respuestas["openapi"] == [200, True]
    assert respuestas["public_key"] == [200, True]
    codigo, status = respuestas["evaluar"]
    assert codigo == 200 and status in {"aprobado", "con_avisos", "escalar", "bloqueado"}
    assert respuestas["filtrar"][0] == 200


# -- sin demo, dentro del proceso ---------------------------------------------------


@pytest.mark.anyio
async def test_sin_demo_no_hay_ficha_del_escenario_ni_ap2(armar_proxy, config, caplog):
    """`demo=None` con PROXY_FICHA_DEMO=1 (el `config` de los tests): como con
    `PROXY_FICHA_DEMO=0`, y `ap2_violaciones` en null. Nada falla."""
    from proxy.tests.ayudantes import buscar, checkout, cliente_de, eventos

    app = armar_proxy(demo=None)
    proxy = app.state.proxy
    assert proxy.config.ficha_demo is True and proxy.ficha_demo is False
    assert "PROXY_FICHA_DEMO=1 sin las fuentes de la demo" in caplog.text
    assert proxy.estado(con_token=False)["ficha_demo"] is False
    async with cliente_de(app) as c:
        contexto = await buscar(c, "B")
        assert proxy._ficha_confirmada("B") is None
        assert proxy.ficha_de(contexto).confirmada is False  # la vacía: el Guard escala
        await checkout(c, contexto, "cart_1")
        recibido = [e for e in await eventos(c) if e["tipo"] == "checkout_recibido"]
    assert recibido and recibido[-1]["datos"]["ap2_violaciones"] is None


@pytest.mark.anyio
async def test_con_la_demo_queda_como_antes(armar_proxy):
    """`armar_proxy` pasa `FuentesDemo()` (como `python -m proxy`): la ficha del
    escenario B y el chequeo de AP2 (120 USD <= 150: OK)."""
    from proxy.demo_local import FuentesDemo
    from proxy.tests.ayudantes import buscar, checkout, cliente_de, eventos

    app = armar_proxy()
    proxy = app.state.proxy
    assert isinstance(proxy.demo, FuentesDemo) and proxy.ficha_demo is True
    async with cliente_de(app) as c:
        contexto = await buscar(c, "B")
        assert proxy.ficha_de(contexto).confirmada is True
        await checkout(c, contexto, "cart_1")
        recibido = [e for e in await eventos(c) if e["tipo"] == "checkout_recibido"]
    assert recibido[-1]["datos"]["ap2_violaciones"] == []


def test_una_demo_que_falla_no_rompe_el_checkout(armar_proxy):
    """El chequeo de AP2 es solo para mostrar: si la demo inyectada falla, va
    en null."""

    class DemoRota:
        def intent_del_escenario(self, nombre):
            return None

        def violaciones_ap2(self, cart, intent):
            raise RuntimeError("explotó")

    proxy = armar_proxy(demo=DemoRota()).state.proxy
    ficha = Ficha(pedido_original="x", requisitos=[
        Requisito(campo="precio", operador="maximo", valor=150, obligatorio=True),
        Requisito(campo="moneda", operador="igual", valor="USD", obligatorio=True),
    ], confirmada=True)
    cart = {"cart_id": "c", "amount": 10.0, "currency": "USD"}
    assert proxy._violaciones_ap2(cart, ficha) is None


def test_python_m_proxy_pasa_la_demo_local(monkeypatch, tmp_path):
    """La demo local queda como antes: `python -m proxy` inyecta `FuentesDemo()`."""
    import proxy.__main__ as principal
    from proxy.demo_local import FuentesDemo

    for nombre in ("GUARD_FILTRO", "PROXY_PERMITIR_SIN_FILTRO", "PROXY_ALLOW_STUB",
                   "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SECRET_KEY",
                   "GATEWAY_CLAVES_ARCHIVO", "GATEWAY_COMERCIOS_ARCHIVO"):
        monkeypatch.delenv(nombre, raising=False)
    monkeypatch.setenv("INTENCION_ACTIVA", "0")
    monkeypatch.setenv("GUARD_EVENTS_JSONL", str(tmp_path / "e.jsonl"))
    monkeypatch.setenv("GUARD_KEY_PATH", str(tmp_path / "k.pem"))
    monkeypatch.setenv("GUARD_API_TOKEN_PATH", str(tmp_path / "token"))
    apps: list[Any] = []
    monkeypatch.setattr(principal, "sockets_de", lambda app, config, gateway: ["local"])
    monkeypatch.setattr(principal, "servir", lambda app, sockets: apps.append(app))
    principal.main()
    (app,) = apps
    assert isinstance(app.state.proxy.demo, FuentesDemo)
