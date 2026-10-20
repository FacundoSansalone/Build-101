"""Dashboard detrás de un dominio público (deploy): `DASHBOARD_PUBLIC_ORIGINS`
suma ese origen y su host a los de localhost; todo lo demás sigue afuera.
Publicado, todo (páginas, `/api/*` y estáticos) pide HTTP Basic con
`DASHBOARD_ACCESS_TOKEN`: Host, Origin y Sec-Fetch-Site solo frenan
navegadores, y `curl` puede mandar cualquiera.

Datos sintéticos de test.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from starlette.testclient import TestClient

from dashboard.server import Config, config_desde_entorno, create_app, origenes_publicos

PUBLICO = "https://demo.ejemplo.test"
ESCENARIO = "/api/demo/escenario"
CLAVE = "clave-de-acceso-de-test-0123456789"


def _basic(usuario: str, clave: str) -> dict[str, str]:
    par = base64.b64encode(f"{usuario}:{clave}".encode()).decode()
    return {"Authorization": f"Basic {par}"}


def _app(tmp_path, **cfg):
    def no_llamar(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError(f"no debería salir: {request.url}")

    config = Config(
        repo_root=tmp_path, world_temp_dir=tmp_path / "world-temp",
        proxy_transport=httpx.MockTransport(no_llamar), adk_transport=httpx.MockTransport(no_llamar), **cfg,
    )
    return create_app(config)


def _cliente(tmp_path, base_url: str, **cfg) -> TestClient:
    """Publicado, con la contraseña puesta (como un navegador que ya entró)."""
    publicado = any(cfg.get(k) for k in ("origenes_publicos", "hosts_extra", "escucha_remota"))
    if publicado:
        cfg.setdefault("clave_acceso", CLAVE)
    headers = _basic("kaiai", CLAVE) if publicado else None
    return TestClient(_app(tmp_path, **cfg), base_url=base_url, headers=headers)


def _post(c: TestClient, ruta: str, cuerpo, origen: str, **headers):
    return c.post(
        ruta, content=json.dumps(cuerpo).encode(),
        headers={"Origin": origen, "content-type": "application/json", **headers},
    )


def test_sin_variables_solo_localhost(tmp_path):
    cfg = Config(repo_root=tmp_path)
    assert cfg.origenes == ("http://localhost:8020", "http://127.0.0.1:8020")
    assert cfg.hosts == frozenset({"localhost:8020", "127.0.0.1:8020"})
    c = _cliente(tmp_path, PUBLICO)
    assert c.get("/api/demo/escenarios").status_code == 403


def test_dominio_publico_acepta_su_host_y_su_origen(tmp_path):
    c = _cliente(tmp_path, PUBLICO, origenes_publicos=(PUBLICO,))
    assert c.get("/api/demo/escenarios").status_code == 200
    r = _post(c, ESCENARIO, {"escenario": "B"}, PUBLICO, **{"Sec-Fetch-Site": "same-origin"})
    assert r.status_code != 403
    # Otro origen sigue afuera.
    r = _post(c, ESCENARIO, {"escenario": "B"}, "https://otro.test")
    assert r.status_code == 403
    assert r.json()["error"] == "origen_no_permitido"


def test_localhost_sigue_funcionando_con_dominio_publico(tmp_path):
    c = _cliente(tmp_path, "http://localhost:8020", origenes_publicos=(PUBLICO,))
    assert c.get("/api/demo/escenarios").status_code == 200


def test_host_extra_para_tuneles(tmp_path):
    con = _cliente(tmp_path, "http://interno:8020", hosts_extra=("interno:8020",))
    assert con.get("/api/demo/escenarios").status_code == 200
    sin = _cliente(tmp_path, "http://interno:8020")
    assert sin.get("/api/demo/escenarios").status_code == 403


def test_origenes_publicos_desde_texto():
    assert origenes_publicos("") == ()
    assert origenes_publicos(" https://A.test/ , http://b.test:8443") == ("https://a.test", "http://b.test:8443")
    for malo in ("demo.test", "https://a.test/ruta", "ftp://a.test", "https://u@a.test", "https://"):
        with pytest.raises(SystemExit):
            origenes_publicos(malo)


def test_config_desde_entorno_lee_las_variables_del_deploy(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PUBLIC_ORIGINS", PUBLICO)
    monkeypatch.setenv("DASHBOARD_EXTRA_HOSTS", " Interno:8020 ,")
    cfg = config_desde_entorno()
    assert PUBLICO in cfg.origenes
    assert {"demo.ejemplo.test", "interno:8020"} <= cfg.hosts


def _evento_jsonl(i: int) -> str:
    from datetime import datetime, timezone

    from contracts import Evento

    return Evento(
        id=f"evento-test-{i}", ts=datetime(2026, 1, 1, 0, 0, i, tzinfo=timezone.utc),
        contexto_id="ctx-test", escenario="B", guard_activo=True, tipo="busqueda_enviada",
        datos={"catalog_search": "búsqueda de test"},
    ).model_dump_json()


def test_con_dominio_publico_los_replays_son_solo_los_de_dashboard_demo(tmp_path):
    """`.logs/guard_events.jsonl` es el registro entero del proxy (pedidos,
    contextos, mandatos): publicado el panel, no se sirve. Las grabaciones de
    `dashboard/demo/` (commiteadas para el plan B) sí."""
    (tmp_path / ".logs").mkdir()
    (tmp_path / ".logs" / "guard_events.jsonl").write_text(_evento_jsonl(1) + "\n", encoding="utf-8")
    (tmp_path / "dashboard" / "demo").mkdir(parents=True)
    (tmp_path / "dashboard" / "demo" / "ensayo-test.jsonl").write_text(_evento_jsonl(2) + "\n", encoding="utf-8")
    for base in (PUBLICO, "http://localhost:8020"):
        c = _cliente(tmp_path, base, origenes_publicos=(PUBLICO,))
        assert [r["nombre"] for r in c.get("/api/replay").json()] == ["ensayo-test.jsonl"]
        r = c.get("/api/replay/guard_events.jsonl")
        assert r.status_code == 404 and "evento-test-1" not in r.text
        assert [e["id"] for e in c.get("/api/replay/ensayo-test.jsonl").json()] == ["evento-test-2"]
    # En local, sin dominio público, como siempre.
    local = _cliente(tmp_path, "http://localhost:8020")
    assert {r["nombre"] for r in local.get("/api/replay").json()} == {"guard_events.jsonl", "ensayo-test.jsonl"}
    assert local.get("/api/replay/guard_events.jsonl").status_code == 200


def test_la_demo_publica_no_depende_del_replay(tmp_path):
    """/demo no tiene selector de grabaciones ni pide /api/replay: publicada,
    no pierde nada sin el registro del proxy."""
    c = _cliente(tmp_path, PUBLICO, origenes_publicos=(PUBLICO,))
    html = c.get("/demo").text
    assert "grabacion" not in html and "/api/replay" not in html
    js = c.get("/demo.js").text
    assert "reproducir(" not in js.replace("async function reproducir(", "")


# ---------------------------------------------------------------------------
# Publicado: contraseña (HTTP Basic) en todo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("publicado", [
    {"origenes_publicos": (PUBLICO,)},
    {"hosts_extra": ("interno:8020",)},
    {"escucha_remota": True},
])
def test_publicado_sin_contrasena_no_arranca(tmp_path, publicado):
    with pytest.raises(SystemExit) as e:
        _app(tmp_path, **publicado)
    assert "DASHBOARD_ACCESS_TOKEN" in str(e.value)
    # Una contraseña corta tampoco.
    with pytest.raises(SystemExit):
        _app(tmp_path, clave_acceso="corta", **publicado)


def test_sin_contrasena_solo_si_se_pide_explicito(tmp_path):
    app = _app(tmp_path, origenes_publicos=(PUBLICO,), permitir_sin_clave=True)
    assert TestClient(app, base_url=PUBLICO).get("/api/demo/escenarios").status_code == 200


def test_publicado_pide_la_contrasena_en_todo(tmp_path):
    """El ataque del hallazgo: curl con el Origin y el Host del dominio
    público. Sin la contraseña no ve nada ni cambia nada."""
    app = _app(tmp_path, origenes_publicos=(PUBLICO,), clave_acceso=CLAVE)
    curl = TestClient(app, base_url=PUBLICO)
    for ruta in ("/", "/demo", "/demo.js", "/index.html", "/api/demo/escenarios", "/api/proxy/guard/state",
                 "/api/proxy/guard/events", "/api/replay"):
        r = curl.get(ruta)
        assert r.status_code == 401, ruta
        assert r.headers["www-authenticate"].startswith('Basic realm="KaiAI"'), ruta
        assert r.json() == {"error": "clave_requerida"}
    for ruta, cuerpo in (("/api/proxy/guard/state", {"guard_activo": False}),
                         ("/api/proxy/guard/aprobaciones", {"contexto_id": "ctx-test", "cart_id": "cart-test"}),
                         ("/api/proxy/guard/compras/decision", {"compra_id": "cmp-test", "decision": "aceptar"}),
                         (ESCENARIO, {"escenario": "B"})):
        r = _post(curl, ruta, cuerpo, PUBLICO, **{"Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 401, ruta
    assert not (tmp_path / "world-temp" / "demo_scenario").exists()


@pytest.mark.parametrize("autorizacion", [
    _basic("kaiai", "otra-clave-de-test-0123456789")["Authorization"],
    _basic("kaiai", CLAVE + "x")["Authorization"],
    _basic("kaiai", CLAVE[:-1])["Authorization"],
    "Basic " + base64.b64encode(CLAVE.encode()).decode(),  # sin usuario ni ":"
    "Basic !!!no-es-base64",
    "Basic",
    f"Bearer {CLAVE}",
    CLAVE,
])
def test_una_contrasena_mal_puesta_no_entra(tmp_path, autorizacion):
    app = _app(tmp_path, origenes_publicos=(PUBLICO,), clave_acceso=CLAVE)
    r = TestClient(app, base_url=PUBLICO).get("/api/demo/escenarios", headers={"Authorization": autorizacion})
    assert r.status_code == 401


def test_con_la_contrasena_y_cualquier_usuario_entra(tmp_path):
    app = _app(tmp_path, origenes_publicos=(PUBLICO,), clave_acceso=CLAVE)
    for usuario in ("kaiai", "", "otro usuario"):
        c = TestClient(app, base_url=PUBLICO, headers=_basic(usuario, CLAVE))
        assert c.get("/demo").status_code == 200
        assert c.get("/demo.js").status_code == 200
        assert c.get("/api/demo/escenarios").status_code == 200
    # El esquema no distingue mayúsculas (RFC 7617).
    par = base64.b64encode(f"kaiai:{CLAVE}".encode()).decode()
    c = TestClient(app, base_url=PUBLICO, headers={"Authorization": f"basic {par}"})
    assert c.get("/api/demo/escenarios").status_code == 200
    # Con la contraseña siguen valiendo Host y Origin: es la misma puerta.
    c = TestClient(app, base_url=PUBLICO, headers=_basic("kaiai", CLAVE))
    assert _post(c, ESCENARIO, {"escenario": "B"}, "https://otro.test").status_code == 403
    assert TestClient(app, base_url="http://otro.test", headers=_basic("kaiai", CLAVE)).get(
        "/api/demo/escenarios").status_code == 403


def test_en_local_la_contrasena_no_cambia_nada(tmp_path):
    """Sin dominio público, Host extra ni red, todo como siempre (aunque el
    .env traiga DASHBOARD_ACCESS_TOKEN para el deploy)."""
    c = TestClient(_app(tmp_path, clave_acceso=CLAVE), base_url="http://localhost:8020")
    assert c.get("/demo").status_code == 200
    assert c.get("/api/demo/escenarios").status_code == 200


def test_config_desde_entorno_lee_la_contrasena(monkeypatch):
    monkeypatch.delenv("DASHBOARD_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("DASHBOARD_PERMITIR_SIN_CLAVE", raising=False)
    cfg = config_desde_entorno()
    assert cfg.clave_acceso is None and cfg.permitir_sin_clave is False
    monkeypatch.setenv("DASHBOARD_ACCESS_TOKEN", f"  {CLAVE}\n")
    monkeypatch.setenv("DASHBOARD_PERMITIR_SIN_CLAVE", "1")
    cfg = config_desde_entorno()
    assert cfg.clave_acceso == CLAVE and cfg.permitir_sin_clave is True
    assert CLAVE not in repr(cfg)
    monkeypatch.setenv("DASHBOARD_ACCESS_TOKEN", "  ")
    assert config_desde_entorno().clave_acceso is None


def test_main_escuchando_en_la_red_sin_contrasena_no_arranca(monkeypatch):
    from dashboard import __main__ as principal

    def no_arrancar(*a, **k):  # pragma: no cover
        raise AssertionError("no debería arrancar")

    monkeypatch.setattr(principal.uvicorn, "run", no_arrancar)
    for nombre in ("DASHBOARD_PUBLIC_ORIGINS", "DASHBOARD_EXTRA_HOSTS", "DASHBOARD_ACCESS_TOKEN",
                   "DASHBOARD_PERMITIR_SIN_CLAVE"):
        monkeypatch.delenv(nombre, raising=False)
    monkeypatch.setenv("DASHBOARD_HOST", "0.0.0.0")
    monkeypatch.setenv("DASHBOARD_ALLOW_REMOTE", "1")
    with pytest.raises(SystemExit) as e:
        principal.main()
    assert "DASHBOARD_ACCESS_TOKEN" in str(e.value)

    arrancado = {}
    monkeypatch.setattr(principal.uvicorn, "run", lambda app, **k: arrancado.update(app=app, **k))
    monkeypatch.setenv("DASHBOARD_ACCESS_TOKEN", CLAVE)
    principal.main()
    assert arrancado["host"] == "0.0.0.0"
    c = TestClient(arrancado["app"], base_url="http://localhost:8020")
    assert c.get("/api/demo/escenarios").status_code == 401
