"""Dashboard: reenvío de las aprobaciones humanas al proxy y protección de su
propia API (Host, Origin, Content-Type), que es la puerta al proxy.

Datos sintéticos de test.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from dashboard.server import Config, config_desde_entorno, create_app, verificar_host_local

PROXY = "http://localhost:8010"
BASE = "http://localhost:8020"


def _cliente(tmp_path: Path, handler, base_url: str = BASE) -> TestClient:
    cfg = Config(repo_root=tmp_path, proxy_url=PROXY, proxy_transport=httpx.MockTransport(handler))
    return TestClient(create_app(cfg), base_url=base_url)


def _no_llamar(request: httpx.Request) -> httpx.Response:  # pragma: no cover
    raise AssertionError(f"no debería llegar al proxy: {request.method} {request.url}")


# ---------------------------------------------------------------------------
# Reenvío de /guard/aprobaciones
# ---------------------------------------------------------------------------


def test_get_aprobaciones_se_reenvia(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path == "/guard/aprobaciones"
        return httpx.Response(200, json={"pendientes": [], "usadas": [{"id": "apr-test"}]})

    r = _cliente(tmp_path, handler).get("/api/proxy/guard/aprobaciones")
    assert r.status_code == 200
    assert r.json()["usadas"] == [{"id": "apr-test"}]


def test_post_aprobacion_valida_y_reenvia_como_json_sin_origin(tmp_path):
    _escribir_token(tmp_path)  # lo escribe el proxy al arrancar (N4)
    recibidos: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recibidos.append(request)
        return httpx.Response(201, json={"id": "apr-test", "estado": "pendiente"})

    c = _cliente(tmp_path, handler)
    for malo in ({}, {"contexto_id": "c"}, {"contexto_id": "c", "cart_id": 1},
                 {"contexto_id": "", "cart_id": "x"}, {"contexto_id": "c", "cart_id": "x", "y": 1}, []):
        assert c.post("/api/proxy/guard/aprobaciones", json=malo).status_code == 400, malo
    assert recibidos == []

    r = c.post("/api/proxy/guard/aprobaciones", json={"contexto_id": "ctx-test", "cart_id": "cart_1"},
               headers={"Origin": BASE})
    assert r.status_code == 201
    (pedido,) = recibidos
    assert json.loads(pedido.content) == {"contexto_id": "ctx-test", "cart_id": "cart_1"}
    assert pedido.headers["content-type"] == "application/json"
    assert pedido.headers["host"] == "localhost:8010"
    assert "origin" not in pedido.headers


def test_post_aprobacion_propaga_el_409_del_proxy(tmp_path):
    _escribir_token(tmp_path)  # lo escribe el proxy al arrancar (N4)
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": "sin_escalar"})

    r = _cliente(tmp_path, handler).post(
        "/api/proxy/guard/aprobaciones", json={"contexto_id": "c", "cart_id": "x"}
    )
    assert r.status_code == 409 and r.json()["error"] == "sin_escalar"


# ---------------------------------------------------------------------------
# CSRF y DNS rebinding en la API del panel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ruta", ["/api/proxy/guard/state", "/api/proxy/guard/aprobaciones"])
def test_post_exige_json(tmp_path, ruta):
    c = _cliente(tmp_path, _no_llamar)
    for tipo in ("text/plain", "application/x-www-form-urlencoded"):
        r = c.post(ruta, content=b'{"guard_activo": false}', headers={"content-type": tipo})
        assert r.status_code == 415, tipo


@pytest.mark.parametrize("ruta", ["/api/proxy/guard/state", "/api/proxy/guard/aprobaciones"])
def test_post_rechaza_otro_origen(tmp_path, ruta):
    c = _cliente(tmp_path, _no_llamar)
    for origen in ("http://evil.test", "null", "http://localhost:8010"):
        r = c.post(ruta, json={"guard_activo": False}, headers={"Origin": origen})
        assert r.status_code == 403, origen


@pytest.mark.parametrize(
    "ruta", ["/api/config", "/api/proxy/guard/events/history", "/api/proxy/guard/aprobaciones",
             "/api/replay"]
)
def test_host_ajeno_da_403(tmp_path, ruta):
    for base in ("http://evil.test:8020", "http://localhost:9999"):
        c = _cliente(tmp_path, _no_llamar, base_url=base)
        assert c.get(ruta).status_code == 403, base
    assert _cliente(tmp_path, _no_llamar, base_url="http://127.0.0.1:8020").get("/api/config").status_code == 200


def test_config_toma_el_puerto_del_entorno(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PORT", "8123")
    cfg = config_desde_entorno()
    assert cfg.port == 8123
    assert cfg.origenes == ("http://localhost:8123", "http://127.0.0.1:8123")


def test_host_remoto_no_arranca_sin_permiso():
    verificar_host_local("127.0.0.1", permitir_remoto=False)
    verificar_host_local("localhost", permitir_remoto=False)
    with pytest.raises(SystemExit):
        verificar_host_local("0.0.0.0", permitir_remoto=False)
    verificar_host_local("0.0.0.0", permitir_remoto=True)


# ---------------------------------------------------------------------------
# Cruzado: el reenvío del panel pasa los controles del proxy de verdad
# ---------------------------------------------------------------------------


def test_reenvio_del_panel_pasa_host_origin_y_content_type_del_proxy(tmp_path):
    from proxy.app import create_app as crear_proxy
    from proxy.config import Config as ConfigProxy
    from proxy.evaluador import EvaluadorCargado
    from proxy import guard_stub

    proxy_app = crear_proxy(
        ConfigProxy(events_jsonl=None, guard_key_path=tmp_path / "k.pem", permitir_stub=True,
                    api_token_path=tmp_path / ".temp-db" / "guard_api_token"),
        transport=httpx.MockTransport(_no_llamar),
        evaluador=EvaluadorCargado(evaluar=guard_stub.evaluar, nombre="stub"),
    )
    cfg = Config(repo_root=tmp_path, proxy_url=PROXY,
                 proxy_transport=httpx.ASGITransport(app=proxy_app))
    c = TestClient(create_app(cfg), base_url=BASE)

    r = c.post("/api/proxy/guard/state", json={"guard_activo": False}, headers={"Origin": BASE})
    assert r.status_code == 200 and r.json()["guard_activo"] is False
    # Sin escalar previo, el proxy contesta 409 (no 403 ni 415): pasó los controles.
    r = c.post("/api/proxy/guard/aprobaciones", json={"contexto_id": "c", "cart_id": "x"},
               headers={"Origin": BASE})
    assert r.status_code == 409
    assert c.get("/api/proxy/guard/aprobaciones").json() == {"pendientes": [], "usadas": []}


# ---------------------------------------------------------------------------
# N4: el panel agrega el token del proxy en su reenvío (el navegador no lo ve)
# ---------------------------------------------------------------------------

TOKEN = "token-de-test-0123456789abcdefghijklmnop"


def _escribir_token(repo: Path, token: str = TOKEN) -> Path:
    path = repo / ".temp-db" / "guard_api_token"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "ruta, cuerpo",
    [("/api/proxy/guard/state", {"guard_activo": False}),
     ("/api/proxy/guard/aprobaciones", {"contexto_id": "c", "cart_id": "x"})],
)
def test_post_reenviado_lleva_el_token_del_proxy(tmp_path, ruta, cuerpo):
    _escribir_token(tmp_path)
    recibidos: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recibidos.append(request)
        return httpx.Response(200, json={"ok": True})

    r = _cliente(tmp_path, handler).post(ruta, json=cuerpo, headers={"Origin": BASE})
    assert r.status_code == 200
    (pedido,) = recibidos
    assert pedido.headers["x-guard-token"] == TOKEN
    assert TOKEN not in r.text


@pytest.mark.parametrize(
    "ruta", ["/api/proxy/guard/state", "/api/proxy/guard/aprobaciones",
             "/api/proxy/guard/events/history"]
)
def test_get_reenviado_no_manda_el_token(tmp_path, ruta):
    _escribir_token(tmp_path)
    recibidos: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recibidos.append(request)
        return httpx.Response(200, json={})

    assert _cliente(tmp_path, handler).get(ruta).status_code == 200
    assert all("x-guard-token" not in p.headers for p in recibidos)


def test_sin_token_del_proxy_el_post_da_503_sin_llamar(tmp_path):
    c = _cliente(tmp_path, _no_llamar)
    r = c.post("/api/proxy/guard/state", json={"guard_activo": False}, headers={"Origin": BASE})
    assert r.status_code == 503
    assert r.json()["error"] == "sin_token_del_proxy"


@pytest.mark.parametrize("contenido", ["", "con espacios y  ", "x" * 5000, "corto"],
                         ids=["vacio", "espacios", "largo", "corto"])
def test_token_ilegible_no_se_manda(tmp_path, contenido):
    _escribir_token(tmp_path, contenido)
    c = _cliente(tmp_path, _no_llamar)
    r = c.post("/api/proxy/guard/state", json={"guard_activo": False}, headers={"Origin": BASE})
    assert r.status_code == 503


def test_el_token_no_aparece_en_la_api_del_panel(tmp_path):
    _escribir_token(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"guard_activo": True})

    c = _cliente(tmp_path, handler)
    for ruta in ("/api/config", "/api/proxy/guard/state", "/api/proxy/guard/aprobaciones"):
        assert TOKEN not in c.get(ruta).text, ruta


def test_ruta_del_token_desde_el_entorno(monkeypatch, tmp_path):
    monkeypatch.delenv("GUARD_API_TOKEN_PATH", raising=False)
    assert config_desde_entorno().token_path == config_desde_entorno().repo_root / ".temp-db" / "guard_api_token"
    monkeypatch.setenv("GUARD_API_TOKEN_PATH", str(tmp_path / "t"))
    assert config_desde_entorno().token_path == tmp_path / "t"


def test_cruzado_el_panel_pasa_el_token_y_sin_el_el_proxy_rechaza(tmp_path):
    from proxy import guard_stub
    from proxy.app import create_app as crear_proxy
    from proxy.config import Config as ConfigProxy
    from proxy.evaluador import EvaluadorCargado

    proxy_app = crear_proxy(
        ConfigProxy(events_jsonl=None, guard_key_path=tmp_path / "k.pem", permitir_stub=True,
                    api_token_path=tmp_path / ".temp-db" / "guard_api_token"),
        transport=httpx.MockTransport(_no_llamar),
        evaluador=EvaluadorCargado(evaluar=guard_stub.evaluar, nombre="stub"),
    )
    cfg = Config(repo_root=tmp_path, proxy_url=PROXY,
                 proxy_transport=httpx.ASGITransport(app=proxy_app))
    panel = TestClient(create_app(cfg), base_url=BASE)
    directo = TestClient(proxy_app, base_url=PROXY)

    # Sin token (un proceso local o el agente con una herramienta HTTP): 403.
    r = directo.post("/guard/state", json={"guard_activo": False})
    assert r.status_code == 403
    assert proxy_app.state.proxy.guard_activo is True
    # El panel lo lee de .temp-db/guard_api_token y lo agrega: pasa.
    r = panel.post("/api/proxy/guard/state", json={"guard_activo": False}, headers={"Origin": BASE})
    assert r.status_code == 200 and r.json()["guard_activo"] is False
    assert proxy_app.state.proxy.api_token not in r.text
