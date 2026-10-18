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
