"""Dashboard: reenvío al proxy de lo que el usuario le contesta a nuestro
agente (el porqué del pedido, aceptar o rechazar una oportunidad) y la misma
protección de la API que el resto de los POST (Host, Origin, JSON, token).

Datos sintéticos de test.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from dashboard.server import RUTAS_POST, Config, create_app

PROXY = "http://localhost:8010"
BASE = "http://localhost:8020"
TOKEN = "token-de-test-0123456789abcdefghijklmnop"

CONTEXTO = "/api/proxy/guard/compras/contexto"
DECISION = "/api/proxy/guard/compras/decision"

COMPRA = {
    "compra_id": "cmp-test01",
    "estado": "negociando",
    "creada": "2026-10-18T12:00:00+00:00",
    "contexto_id": None,
    "pedido": "pedido de test",
    "pregunta": "pregunta de test",
    "requisitos": [],
    "contexto": None,
    "oportunidad": None,
    "vence": None,
    "decision": None,
    "requisitos_autorizados": None,
}


def _escribir_token(repo: Path, token: str = TOKEN) -> None:
    path = repo / ".temp-db" / "guard_api_token"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")


def _cliente(tmp_path: Path, handler, base_url: str = BASE) -> TestClient:
    cfg = Config(repo_root=tmp_path, proxy_url=PROXY, proxy_transport=httpx.MockTransport(handler))
    return TestClient(create_app(cfg), base_url=base_url)


def _no_llamar(request: httpx.Request) -> httpx.Response:  # pragma: no cover
    raise AssertionError(f"no debería llegar al proxy: {request.method} {request.url}")


def _post(c: TestClient, ruta: str, cuerpo, **headers):
    # content= y no json=: con json=None httpx no manda cuerpo.
    return c.post(
        ruta,
        content=json.dumps(cuerpo).encode(),
        headers={"Origin": BASE, "content-type": "application/json", **headers},
    )


# ---------------------------------------------------------------------------
# GET /guard/compras
# ---------------------------------------------------------------------------


def test_get_compras_se_reenvia_sin_token(tmp_path):
    _escribir_token(tmp_path)
    recibidos: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recibidos.append(request)
        return httpx.Response(200, json={"compras": [COMPRA]})

    r = _cliente(tmp_path, handler).get("/api/proxy/guard/compras")
    assert r.status_code == 200
    assert r.json() == {"compras": [COMPRA]}
    (pedido,) = recibidos
    assert pedido.method == "GET" and pedido.url.path == "/guard/compras"
    assert "x-guard-token" not in pedido.headers
    assert TOKEN not in r.text


def test_get_compras_con_proxy_caido_da_502(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("caído", request=request)

    r = _cliente(tmp_path, handler).get("/api/proxy/guard/compras")
    assert r.status_code == 502
    assert r.json()["error"] == "proxy_desconectado"


# ---------------------------------------------------------------------------
# POST /guard/compras/contexto
# ---------------------------------------------------------------------------

CONTEXTOS_INVALIDOS = [
    {},
    [],
    "texto",
    None,
    {"compra_id": "cmp-1"},
    {"respuesta": "algo"},
    {"compra_id": "cmp-1", "respuesta": ""},
    {"compra_id": "cmp-1", "respuesta": "   \n\t "},
    {"compra_id": "cmp-1", "respuesta": "x" * 1001},
    {"compra_id": "cmp-1", "respuesta": 12},
    {"compra_id": "cmp-1", "respuesta": None},
    {"compra_id": "cmp-1", "respuesta": "algo", "omitir": True},
    {"compra_id": "cmp-1", "respuesta": "algo", "extra": 1},
    {"compra_id": "cmp-1", "omitir": False},
    {"compra_id": "cmp-1", "omitir": "true"},
    {"compra_id": "cmp-1", "omitir": 1},
    {"compra_id": "", "respuesta": "algo"},
    {"compra_id": "x" * 201, "respuesta": "algo"},
    {"compra_id": 7, "respuesta": "algo"},
    {"compra_id": None, "omitir": True},
]


@pytest.mark.parametrize("cuerpo", CONTEXTOS_INVALIDOS, ids=range(len(CONTEXTOS_INVALIDOS)))
def test_contexto_valida_antes_de_reenviar(tmp_path, cuerpo):
    _escribir_token(tmp_path)
    r = _post(_cliente(tmp_path, _no_llamar), CONTEXTO, cuerpo)
    assert r.status_code == 400
    assert r.json()["error"] == "se_espera"


def test_contexto_json_invalido_da_400(tmp_path):
    _escribir_token(tmp_path)
    r = _cliente(tmp_path, _no_llamar).post(
        CONTEXTO, content=b"{no es json", headers={"Origin": BASE, "content-type": "application/json"}
    )
    assert r.status_code == 400
    assert r.json()["error"] == "json_invalido"


@pytest.mark.parametrize(
    "cuerpo",
    [
        {"compra_id": "cmp-test01", "respuesta": "entran 10 el lunes y 10 el jueves"},
        # 1000 caracteres sin los espacios de los bordes: vale.
        {"compra_id": "cmp-test01", "respuesta": "  " + "x" * 1000 + "\n"},
        {"compra_id": "c" * 200, "respuesta": "y"},
        {"compra_id": "cmp-test01", "omitir": True},
    ],
    ids=["respuesta", "borde_1000", "id_200", "omitir"],
)
def test_contexto_valido_se_reenvia_tal_cual_con_token(tmp_path, cuerpo):
    _escribir_token(tmp_path)
    recibidos: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recibidos.append(request)
        return httpx.Response(200, json=COMPRA)

    r = _post(_cliente(tmp_path, handler), CONTEXTO, cuerpo)
    assert r.status_code == 200
    assert r.json() == COMPRA
    (pedido,) = recibidos
    assert pedido.method == "POST" and pedido.url.path == "/guard/compras/contexto"
    assert json.loads(pedido.content) == cuerpo
    assert pedido.headers["content-type"] == "application/json"
    assert pedido.headers["x-guard-token"] == TOKEN
    assert pedido.headers["host"] == "localhost:8010"
    assert "origin" not in pedido.headers
    assert TOKEN not in r.text


# ---------------------------------------------------------------------------
# POST /guard/compras/decision
# ---------------------------------------------------------------------------

DECISIONES_INVALIDAS = [
    {},
    [],
    {"compra_id": "cmp-1"},
    {"decision": "aceptar"},
    {"compra_id": "cmp-1", "decision": "aceptada"},
    {"compra_id": "cmp-1", "decision": "Aceptar"},
    {"compra_id": "cmp-1", "decision": "vencida"},
    {"compra_id": "cmp-1", "decision": True},
    {"compra_id": "cmp-1", "decision": ["aceptar"]},
    {"compra_id": "cmp-1", "decision": "aceptar", "oportunidad_id": "op-1"},
    {"compra_id": "", "decision": "rechazar"},
    {"compra_id": "x" * 201, "decision": "rechazar"},
    {"compra_id": 3, "decision": "rechazar"},
]


@pytest.mark.parametrize("cuerpo", DECISIONES_INVALIDAS, ids=range(len(DECISIONES_INVALIDAS)))
def test_decision_valida_antes_de_reenviar(tmp_path, cuerpo):
    _escribir_token(tmp_path)
    r = _post(_cliente(tmp_path, _no_llamar), DECISION, cuerpo)
    assert r.status_code == 400
    assert r.json()["error"] == "se_espera"


@pytest.mark.parametrize("decision", ["aceptar", "rechazar"])
def test_decision_valida_se_reenvia_tal_cual_con_token(tmp_path, decision):
    _escribir_token(tmp_path)
    recibidos: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recibidos.append(request)
        return httpx.Response(200, json={**COMPRA, "estado": "aceptada" if decision == "aceptar" else "rechazada"})

    cuerpo = {"compra_id": "cmp-test01", "decision": decision}
    r = _post(_cliente(tmp_path, handler), DECISION, cuerpo)
    assert r.status_code == 200
    (pedido,) = recibidos
    assert pedido.url.path == "/guard/compras/decision"
    assert json.loads(pedido.content) == cuerpo
    assert pedido.headers["x-guard-token"] == TOKEN
    assert "origin" not in pedido.headers


# ---------------------------------------------------------------------------
# Respuestas del proxy: se propagan con su status y su cuerpo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ruta, cuerpo",
    [(CONTEXTO, {"compra_id": "cmp-1", "omitir": True}),
     (DECISION, {"compra_id": "cmp-1", "decision": "rechazar"})],
)
@pytest.mark.parametrize(
    "status, respuesta",
    [(404, {"error": "compra_desconocida"}),
     (409, {"error": "estado", "estado": "vencida"}),
     (400, {"error": "se_espera", "formato": {}})],
)
def test_propaga_los_errores_del_proxy(tmp_path, ruta, cuerpo, status, respuesta):
    _escribir_token(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=respuesta)

    r = _post(_cliente(tmp_path, handler), ruta, cuerpo)
    assert r.status_code == status
    assert r.json() == respuesta


@pytest.mark.parametrize(
    "ruta, cuerpo",
    [(CONTEXTO, {"compra_id": "cmp-1", "respuesta": "algo"}),
     (DECISION, {"compra_id": "cmp-1", "decision": "aceptar"})],
)
def test_sin_token_del_proxy_da_503_sin_llamar(tmp_path, ruta, cuerpo):
    r = _post(_cliente(tmp_path, _no_llamar), ruta, cuerpo)
    assert r.status_code == 503
    assert r.json()["error"] == "sin_token_del_proxy"


def test_proxy_lento_o_caido_da_502(tmp_path):
    _escribir_token(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lento", request=request)

    r = _post(_cliente(tmp_path, handler), CONTEXTO, {"compra_id": "cmp-1", "respuesta": "algo"})
    assert r.status_code == 502
    assert r.json()["error"] == "proxy_desconectado"
    assert r.json()["detalle"] == "ReadTimeout"


# ---------------------------------------------------------------------------
# Seguridad: misma protección que el interruptor y las aprobaciones
# ---------------------------------------------------------------------------

POSTS = [(CONTEXTO, {"compra_id": "cmp-1", "omitir": True}),
         (DECISION, {"compra_id": "cmp-1", "decision": "aceptar"})]


def test_las_rutas_post_estan_protegidas_por_el_middleware():
    assert {CONTEXTO, DECISION} <= RUTAS_POST


@pytest.mark.parametrize("ruta, cuerpo", POSTS)
def test_post_exige_json(tmp_path, ruta, cuerpo):
    _escribir_token(tmp_path)
    c = _cliente(tmp_path, _no_llamar)
    for tipo in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
        r = c.post(ruta, content=json.dumps(cuerpo).encode(), headers={"content-type": tipo, "Origin": BASE})
        assert r.status_code == 415, tipo


@pytest.mark.parametrize("ruta, cuerpo", POSTS)
def test_post_rechaza_otro_origen_o_sin_origin(tmp_path, ruta, cuerpo):
    _escribir_token(tmp_path)
    c = _cliente(tmp_path, _no_llamar)
    for origen in ("http://evil.test", "null", "http://localhost:8010", "http://localhost:8000"):
        r = c.post(ruta, json=cuerpo, headers={"Origin": origen})
        assert r.status_code == 403, origen
        assert r.json()["error"] == "origen_no_permitido"
    r = c.post(ruta, json=cuerpo)
    assert r.status_code == 403
    assert r.json()["error"] == "origen_requerido"
    r = c.post(ruta, json=cuerpo, headers={"Origin": BASE, "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


@pytest.mark.parametrize("ruta, cuerpo", POSTS)
def test_post_desde_127_0_0_1_tambien_vale(tmp_path, ruta, cuerpo):
    _escribir_token(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=COMPRA)

    c = _cliente(tmp_path, handler, base_url="http://127.0.0.1:8020")
    r = c.post(ruta, json=cuerpo, headers={"Origin": "http://127.0.0.1:8020", "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200


@pytest.mark.parametrize("ruta", ["/api/proxy/guard/compras", CONTEXTO, DECISION])
def test_host_ajeno_da_403(tmp_path, ruta):
    _escribir_token(tmp_path)
    for base in ("http://evil.test:8020", "http://localhost:9999"):
        c = _cliente(tmp_path, _no_llamar, base_url=base)
        assert c.get(ruta).status_code == 403, base
        r = c.post(ruta, json={"compra_id": "cmp-1", "omitir": True}, headers={"Origin": base})
        assert r.status_code == 403, base


@pytest.mark.parametrize("ruta", [CONTEXTO, DECISION])
def test_get_a_las_rutas_post_no_llega_al_proxy(tmp_path, ruta):
    # Solo POST: un GET cae en los estáticos (404) y nunca llega al proxy.
    assert _cliente(tmp_path, _no_llamar).get(ruta).status_code in (404, 405)


# ---------------------------------------------------------------------------
# Cruzado: el reenvío del panel pasa los controles del proxy de verdad
# ---------------------------------------------------------------------------


def test_cruzado_el_proxy_real_recibe_los_post_de_nuestro_agente(tmp_path):
    """Token, Host y JSON pasan los controles del proxy: contesta 404 por la
    compra (no 403 ni 415) y GET /guard/compras tiene la forma esperada."""
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
    cfg = Config(repo_root=tmp_path, proxy_url=PROXY, proxy_transport=httpx.ASGITransport(app=proxy_app))
    c = TestClient(create_app(cfg), base_url=BASE)

    assert c.get("/api/proxy/guard/compras").json() == {"compras": []}
    for ruta, cuerpo in POSTS:
        r = _post(c, ruta, cuerpo)
        assert r.status_code == 404, ruta
        assert r.json() == {"error": "compra_desconocida"}
