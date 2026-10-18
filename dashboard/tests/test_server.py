"""Tests del servidor del dashboard.

Todo lo que se escribe acá es sintético y de test (tmp_path): no son
resultados ni eventos reales de la demo.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from starlette.testclient import TestClient

from contracts import Evento, Ficha, Recibo
from dashboard.server import Config, create_app, jwk_publica_desde_pem

REPO = Path(__file__).resolve().parents[2]
PROXY = "http://proxy.test"


# ---------------------------------------------------------------------------
# Ayudantes
# ---------------------------------------------------------------------------


def _proxy_caido(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("proxy de test apagado", request=request)


def _cliente(tmp_path: Path, handler=_proxy_caido) -> TestClient:
    cfg = Config(
        repo_root=tmp_path,
        proxy_url=PROXY,
        proxy_transport=httpx.MockTransport(handler),
    )
    # La API del panel solo atiende Host localhost/127.0.0.1 con su puerto.
    return TestClient(create_app(cfg), base_url="http://localhost:8020")


def _resultado_test(started_at: str | None, guard: str | None, nota: str) -> dict:
    meta = {"trials": 1, "source": "direct", "decider": "target", "model": None, "guard": guard, "nota": nota}
    if started_at:
        meta["started_at"] = started_at
    return {
        "meta": meta,
        "scenarios": {
            "B": {
                "title": "escenario de test",
                "attack": None,
                "trials": [],
                "summary": {"runs": 1, "errors": 0, "chosen_counts": {}},
            }
        },
    }


def _escribir(path: Path, datos) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(datos), encoding="utf-8")
    return path


def _evento_test(i: int, tipo: str = "busqueda_enviada", **extra) -> dict:
    return Evento(
        id=f"evento-test-{i}",
        ts=datetime(2026, 1, 1, 0, 0, i, tzinfo=timezone.utc),
        contexto_id="ctx-test",
        escenario="B",
        guard_activo=False,
        tipo=tipo,
        datos={"catalog_search": "búsqueda de test"},
        **extra,
    ).model_dump(mode="json")


def _escribir_jsonl(path: Path, lineas: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Config y estáticos
# ---------------------------------------------------------------------------


def test_config_devuelve_proxy_url(tmp_path):
    r = _cliente(tmp_path).get("/api/config")
    assert r.status_code == 200
    assert r.json() == {"proxy_url": PROXY}


def test_estaticos_index_y_modulos_js(tmp_path):
    c = _cliente(tmp_path)
    r = c.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Pre-Sign Guard" in r.text
    js = c.get("/app.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]  # módulos ES en Windows
    assert c.get("/lib/jose.js").status_code == 200


# ---------------------------------------------------------------------------
# Resultados del benchmark
# ---------------------------------------------------------------------------


def test_resultados_vacio_da_404_claro(tmp_path):
    c = _cliente(tmp_path)
    assert c.get("/api/results").json() == []
    r = c.get("/api/results/latest")
    assert r.status_code == 404
    assert r.json()["error"] == "sin_resultados"


def test_latest_prefiere_el_mas_nuevo_con_guard(tmp_path):
    _escribir(tmp_path / "results/benchmark-viejo-con-guard.json",
              _resultado_test("2026-01-01T10:00:00+00:00", "guard.test:evaluate", "viejo con guard"))
    _escribir(tmp_path / "results/benchmark-medio-con-guard.json",
              _resultado_test("2026-01-02T10:00:00+00:00", "guard.test:evaluate", "medio con guard"))
    _escribir(tmp_path / "world/ap2/results/benchmark-nuevo-sin-guard.json",
              _resultado_test("2026-01-03T10:00:00+00:00", None, "nuevo sin guard"))
    c = _cliente(tmp_path)

    lista = c.get("/api/results").json()
    assert [i["nombre"] for i in lista] == [
        "benchmark-nuevo-sin-guard.json",
        "benchmark-medio-con-guard.json",
        "benchmark-viejo-con-guard.json",
    ]
    assert lista[0]["carpeta"] == "world/ap2/results"

    ultimo = c.get("/api/results/latest").json()
    assert ultimo["meta"]["nota"] == "medio con guard"
    assert ultimo["_archivo"]["id"] == "results/benchmark-medio-con-guard.json"


def test_latest_sin_guard_usa_el_mas_nuevo_y_cae_a_mtime(tmp_path):
    viejo = _escribir(tmp_path / "results/benchmark-a.json", _resultado_test(None, None, "sin fecha viejo"))
    nuevo = _escribir(tmp_path / "world/ap2/results/benchmark-b.json", _resultado_test(None, None, "sin fecha nuevo"))
    os.utime(viejo, (1_700_000_000, 1_700_000_000))
    os.utime(nuevo, (1_800_000_000, 1_800_000_000))
    _escribir(tmp_path / "results/roto.json", {"no": "es un benchmark"})
    (tmp_path / "results/ilegible.json").write_text("{", encoding="utf-8")

    c = _cliente(tmp_path)
    lista = c.get("/api/results").json()
    assert [i["nombre"] for i in lista] == ["benchmark-b.json", "benchmark-a.json"]
    assert lista[0]["fecha_de"] == "archivo"
    assert c.get("/api/results/latest").json()["meta"]["nota"] == "sin fecha nuevo"


def test_resultado_por_id(tmp_path):
    _escribir(tmp_path / "results/benchmark-x.json", _resultado_test("2026-01-01T00:00:00Z", None, "x"))
    c = _cliente(tmp_path)
    assert c.get("/api/results/results/benchmark-x.json").json()["meta"]["nota"] == "x"
    assert c.get("/api/results/world/benchmark-x.json").status_code == 404
    assert c.get("/api/results/otro/benchmark-x.json").status_code == 400
    assert c.get("/api/results/results/..%5Cbenchmark-x.json").status_code in (400, 404)


# ---------------------------------------------------------------------------
# Fixtures y catálogo (los reales del repo)
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_con_fixtures(tmp_path):
    shutil.copytree(REPO / "fixtures", tmp_path / "fixtures")
    return tmp_path


def test_fixtures_lista_en_orden_de_la_demo(repo_con_fixtures):
    lista = _cliente(repo_con_fixtures).get("/api/fixtures").json()
    assert [f["escenario"] for f in lista] == ["baseline", "A", "B", "B2", "C"]
    assert all(f["title"] for f in lista)


def test_fixture_b_y_su_ficha_con_el_contrato(repo_con_fixtures):
    c = _cliente(repo_con_fixtures)
    b = c.get("/api/fixtures/B").json()
    assert b["scenario"] == "B"
    assert b["expected"]["attack_target_cart_id"] == "cart_4"

    ficha = c.get("/api/fixtures/B/ficha").json()
    esperado = Ficha.desde_intent(b["intent"]).model_dump(mode="json")
    assert ficha == esperado
    assert ficha["confirmada"] is True
    assert c.get("/api/fixtures/Z").status_code == 404
    assert c.get("/api/fixtures/..%2e").status_code == 400


def test_catalogo_publico(repo_con_fixtures, tmp_path_factory):
    datos = _cliente(repo_con_fixtures).get("/api/catalog").json()
    assert datos["merchant"]["id"] == "merchant_1"
    vacio = tmp_path_factory.mktemp("sin_fixtures")
    assert _cliente(vacio).get("/api/catalog").status_code == 404


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def test_replay_lista_y_lee_eventos_validos(tmp_path):
    _escribir_jsonl(tmp_path / ".logs/guard_events.jsonl",
                    [json.dumps(_evento_test(1)), "no es json", json.dumps({"id": "x"})])
    _escribir_jsonl(tmp_path / "dashboard/demo/ensayo-test.jsonl",
                    [json.dumps(_evento_test(2)), json.dumps(_evento_test(3, "error"))])
    (tmp_path / "dashboard/demo/notas.txt").write_text("no es jsonl", encoding="utf-8")
    c = _cliente(tmp_path)

    lista = {r["nombre"]: r for r in c.get("/api/replay").json()}
    assert set(lista) == {"guard_events.jsonl", "ensayo-test.jsonl"}
    assert lista["guard_events.jsonl"]["origen"] == ".logs"
    assert lista["guard_events.jsonl"]["eventos"] == 1
    assert lista["guard_events.jsonl"]["invalidas"] == 2
    assert lista["ensayo-test.jsonl"]["escenarios"] == ["B"]

    r = c.get("/api/replay/guard_events.jsonl")
    assert r.status_code == 200
    assert r.headers["x-lineas-invalidas"] == "2"
    eventos = r.json()
    assert [e["id"] for e in eventos] == ["evento-test-1"]
    Evento.model_validate(eventos[0])

    assert len(c.get("/api/replay/ensayo-test.jsonl").json()) == 2


@pytest.mark.parametrize(
    "nombre",
    [
        "..",
        "..%2F..%2Fpytest.ini",
        "..%5C..%5Cpytest.ini",
        "%2e%2e%5csecreto.jsonl",
        "..%5Csecreto.jsonl",
        ".secreto.jsonl",
        "notas.txt",
        "secreto.jsonl",
        "C:%5Csecreto.jsonl",
    ],
)
def test_replay_rechaza_path_traversal(tmp_path, nombre):
    _escribir_jsonl(tmp_path / "secreto.jsonl", [json.dumps(_evento_test(9))])
    _escribir_jsonl(tmp_path / "dashboard/.secreto.jsonl", [json.dumps(_evento_test(9))])
    _escribir_jsonl(tmp_path / "dashboard/demo/ok-test.jsonl", [json.dumps(_evento_test(1))])
    (tmp_path / "dashboard/demo/notas.txt").write_text("x", encoding="utf-8")
    r = _cliente(tmp_path).get(f"/api/replay/{nombre}")
    assert r.status_code in (400, 404)
    assert "evento-test-9" not in r.text


# ---------------------------------------------------------------------------
# Clave pública del Guard
# ---------------------------------------------------------------------------


def _pem_test() -> tuple[ec.EllipticCurvePrivateKey, bytes]:
    clave = ec.generate_private_key(ec.SECP256R1())
    pem = clave.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return clave, pem


def test_clave_publica_del_proxy(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/guard/public-key"
        return httpx.Response(200, json={"kid": "kid-test", "jwk": {"kty": "EC", "crv": "P-256", "x": "a", "y": "b"}})

    r = _cliente(tmp_path, handler).get("/api/public-key")
    assert r.json()["origen"] == "proxy"
    assert r.json()["kid"] == "kid-test"


def test_clave_publica_local_si_el_proxy_no_responde(tmp_path):
    clave, pem = _pem_test()
    (tmp_path / ".temp-db").mkdir()
    (tmp_path / ".temp-db/guard_signing_key.pem").write_bytes(pem)
    r = _cliente(tmp_path).get("/api/public-key").json()
    assert r["kid"] == "guard-key-1"
    assert "temp-db" in r["origen"]
    assert "d" not in r["jwk"]  # nunca la parte privada
    x = int.from_bytes(base64.urlsafe_b64decode(r["jwk"]["x"] + "=="), "big")
    assert x == clave.public_key().public_numbers().x


def test_clave_publica_sin_nada_da_404(tmp_path):
    r = _cliente(tmp_path).get("/api/public-key")
    assert r.status_code == 404
    assert r.json()["error"] == "sin_clave"


def test_jwk_rechaza_curva_distinta():
    otra = ec.generate_private_key(ec.SECP384R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    with pytest.raises(ValueError):
        jwk_publica_desde_pem(otra)


# ---------------------------------------------------------------------------
# Reenvío al proxy
# ---------------------------------------------------------------------------


def test_estado_del_proxy_y_desconectado(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"guard_activo": True, "merchant_url": "http://m.test", "evaluador": "stub"})

    assert _cliente(tmp_path, handler).get("/api/proxy/guard/state").json()["evaluador"] == "stub"
    r = _cliente(tmp_path).get("/api/proxy/guard/state")
    assert r.status_code == 502
    assert r.json()["error"] == "proxy_desconectado"


def test_post_estado_valida_y_reenvia(tmp_path):
    token = tmp_path / ".temp-db" / "guard_api_token"  # lo escribe el proxy (N4)
    token.parent.mkdir(parents=True)
    token.write_text("t" * 43, encoding="utf-8")
    recibidos = []

    def handler(request: httpx.Request) -> httpx.Response:
        recibidos.append((request.method, json.loads(request.content)))
        return httpx.Response(200, json={"guard_activo": False, "merchant_url": "x", "evaluador": "guard"})

    c = _cliente(tmp_path, handler)
    assert c.post("/api/proxy/guard/state", json={"guard_activo": "si"}).status_code == 400
    assert c.post("/api/proxy/guard/state", json={"guard_activo": True, "otro": 1}).status_code == 400
    assert c.post("/api/proxy/guard/state", content=b"{", headers={"content-type": "application/json"}).status_code == 400
    assert recibidos == []
    r = c.post("/api/proxy/guard/state", json={"guard_activo": False})
    assert r.status_code == 200
    assert recibidos == [("POST", {"guard_activo": False})]


def test_historial_acota_limit(tmp_path):
    vistos = []

    def handler(request: httpx.Request) -> httpx.Response:
        vistos.append(request.url.params["limit"])
        return httpx.Response(200, json=[])

    c = _cliente(tmp_path, handler)
    c.get("/api/proxy/guard/events/history?limit=99999")
    c.get("/api/proxy/guard/events/history")
    assert vistos == ["1000", "200"]
    assert c.get("/api/proxy/guard/events/history?limit=abc").status_code == 400


def test_sse_se_reenvia_tal_cual(tmp_path):
    evento = json.dumps(_evento_test(1))
    cuerpo = f"event: evento\ndata: {evento}\n\n".encode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/guard/events"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=cuerpo)

    r = _cliente(tmp_path, handler).get("/api/proxy/guard/events")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.content == cuerpo
    caido = _cliente(tmp_path).get("/api/proxy/guard/events")
    assert caido.status_code == 502


# ---------------------------------------------------------------------------
# Test cruzado: recibo firmado en Python, verificado con el JS del navegador
# ---------------------------------------------------------------------------


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
@pytest.mark.parametrize("alterar", [False, True])
def test_recibo_firmado_en_python_se_verifica_en_js(tmp_path, alterar):
    clave, pem = _pem_test()
    ficha = Ficha.desde_intent({
        "user_request": "pedido de test ñ",
        "hard": {"max_price": 150.0, "currency": "USD", "size": 42},
        "preferred": {"brand": "MarcaTest"},
    })
    recibo = Recibo(
        id="recibo-test", emitido=datetime(2026, 1, 1, tzinfo=timezone.utc), ficha=ficha,
        consideradas=["cart_1", "cart_2"], filtradas={"cart_2": ["categoría ≠ test"]},
        verificaciones=[], ranking=["cart_1", "cart_2"], elegido="cart_2",
        por_que_gano="test", sin_verificar=["calidad"], status="bloqueado",
        checkout_hash="hash-test", kid="guard-key-1",
    )
    header = _b64url(json.dumps({"alg": "ES256", "kid": "guard-key-1"}).encode())
    payload = _b64url(recibo.payload_canonico())
    der = clave.sign(f"{header}.{payload}".encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    recibo.firma = f"{header}.{payload}.{_b64url(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"

    mostrado = recibo.model_dump(mode="json")  # lo que viaja en el evento
    if alterar:
        mostrado["elegido"] = "cart_1"
    entrada = tmp_path / "entrada.json"
    entrada.write_text(json.dumps({
        "recibo": mostrado,
        "clave": {"kid": "guard-key-1", "jwk": jwk_publica_desde_pem(pem)},
    }, ensure_ascii=False), encoding="utf-8")

    salida = subprocess.run(
        ["node", str(REPO / "dashboard/tests/js/verificar_cli.mjs"), str(entrada)],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=True,
    )
    resultado = json.loads(salida.stdout)
    pasos = {p["id"]: p["ok"] for p in resultado["pasos"]}
    assert pasos["firma"] is True
    assert pasos["contenido"] is (not alterar)
    assert resultado["ok"] is (not alterar)


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_mandato_real_del_sdk_de_ap2_se_decodifica_en_js(tmp_path):
    """Un CheckoutMandate SD-JWT armado con el MandateClient del SDK (como el
    Shopping Agent del sample) se decodifica con el JS del navegador."""
    mandate = pytest.importorskip("ap2.sdk.mandate")
    from ap2.sdk.generated.checkout_mandate import CheckoutMandate
    from jwcrypto.jwk import JWK

    clave_usuario = JWK.from_pyca(ec.generate_private_key(ec.SECP256R1()))
    token = mandate.MandateClient().create(
        payloads=[CheckoutMandate(
            checkout_jwt="jwt.de.test", checkout_hash="hash-de-test", iat=1, exp=2,
        )],
        issuer_key=clave_usuario,
    )
    entrada = tmp_path / "mandato.txt"
    entrada.write_text(token, encoding="utf-8")
    salida = subprocess.run(
        ["node", str(REPO / "dashboard/tests/js/mandato_cli.mjs"), str(entrada)],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=True,
    )
    resultado = json.loads(salida.stdout)
    assert resultado["header"]["alg"] == "ES256"
    assert '"checkout_hash": "hash-de-test"' in json.dumps(resultado["claims"])
    assert resultado["sinUsar"] == 0
