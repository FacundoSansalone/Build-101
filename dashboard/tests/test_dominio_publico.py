"""Dashboard detrás de un dominio público (deploy): `DASHBOARD_PUBLIC_ORIGINS`
suma ese origen y su host a los de localhost; todo lo demás sigue afuera.

Datos sintéticos de test.
"""

from __future__ import annotations

import json

import httpx
import pytest
from starlette.testclient import TestClient

from dashboard.server import Config, config_desde_entorno, create_app, origenes_publicos

PUBLICO = "https://demo.ejemplo.test"
ESCENARIO = "/api/demo/escenario"


def _cliente(tmp_path, base_url: str, **cfg) -> TestClient:
    def no_llamar(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError(f"no debería salir: {request.url}")

    config = Config(
        repo_root=tmp_path, world_temp_dir=tmp_path / "world-temp",
        proxy_transport=httpx.MockTransport(no_llamar), adk_transport=httpx.MockTransport(no_llamar), **cfg,
    )
    return TestClient(create_app(config), base_url=base_url)


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
