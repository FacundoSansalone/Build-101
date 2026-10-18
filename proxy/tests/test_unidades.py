"""Piezas sueltas: stub, carga del evaluador, config y helpers A2A."""

from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts import Ficha, OfertasVistas, Recibo, Veredicto
from proxy import a2a, guard_stub
from proxy.config import REPO_ROOT, Config
from proxy.evaluador import cargar_evaluador, clave_publica, firmar_recibo
from proxy.fuentes import cargar_intent
from proxy.tests.fake_world import cargar_fixture

# ---------------------------------------------------------------------------
# guard_stub
# ---------------------------------------------------------------------------


def _entradas(escenario: str = "B"):
    fixture = cargar_fixture(escenario)
    ficha = Ficha.desde_intent(fixture["intent"])
    ofertas = OfertasVistas(
        carts=fixture["carts"],
        offer_details=fixture["offer_details"],
        comercio_id="merchant_1",
    )
    return ficha, ofertas


def test_stub_bloquea_el_ultimo_cart():
    ficha, ofertas = _entradas()
    v = guard_stub.evaluar(ficha, ofertas, "cart_4", {}, checkout_hash="h")
    assert isinstance(v, Veredicto)
    assert v.status == "bloqueado"
    assert v.cart_id_elegido == "cart_4"
    assert v.motivo.startswith("STUB")
    assert v.recibo is not None and v.recibo.checkout_hash == "h"
    assert v.recibo.status == "bloqueado"


@pytest.mark.parametrize("cart_id", ["cart_1", "cart_2", "cart_3"])
def test_stub_aprueba_los_demas(cart_id):
    ficha, ofertas = _entradas()
    v = guard_stub.evaluar(ficha, ofertas, cart_id, {})
    assert v.status == "aprobado"
    assert "cart_4" not in v.ranking


def test_stub_es_deterministico_con_ahora():
    ficha, ofertas = _entradas()
    ahora = datetime(2026, 10, 17, tzinfo=timezone.utc)
    a = guard_stub.evaluar(ficha, ofertas, "cart_1", {}, ahora=ahora)
    b = guard_stub.evaluar(ficha, ofertas, "cart_1", {}, ahora=ahora)
    assert a == b


def test_stub_sin_carts_bloquea():
    ficha, _ = _entradas()
    v = guard_stub.evaluar(ficha, OfertasVistas(carts=[], offer_details=[]), "cart_1", {})
    assert v.status == "bloqueado"


# ---------------------------------------------------------------------------
# cargar_evaluador()
# ---------------------------------------------------------------------------


def test_con_guard_evaluar_usa_el_real(monkeypatch):
    modulo = types.ModuleType("guard")

    def evaluar(*args, **kwargs):  # pragma: no cover - solo se referencia
        raise AssertionError

    modulo.evaluar = evaluar
    monkeypatch.setitem(sys.modules, "guard", modulo)
    cargado = cargar_evaluador()
    assert cargado.nombre == "guard"
    assert cargado.evaluar is evaluar

# (sin guard real: ver test_evaluador_fail_closed.py)


# ---------------------------------------------------------------------------
# Firma del recibo con guard.recibo (si existe)
# ---------------------------------------------------------------------------


def _veredicto_con_recibo() -> Veredicto:
    ficha, ofertas = _entradas()
    return guard_stub.evaluar(ficha, ofertas, "cart_1", {}, checkout_hash="h")


def _guard_recibo_falso(monkeypatch, firmar):
    from cryptography.hazmat.primitives.asymmetric import ec

    clave = ec.generate_private_key(ec.SECP256R1())
    paquete = types.ModuleType("guard")
    modulo = types.ModuleType("guard.recibo")
    modulo.cargar_o_crear_clave = lambda path: clave
    modulo.firmar_recibo = firmar
    paquete.recibo = modulo
    monkeypatch.setitem(sys.modules, "guard", paquete)
    monkeypatch.setitem(sys.modules, "guard.recibo", modulo)
    return clave


def test_firmar_recibo_sin_guard_recibo_lo_deja_igual(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "guard", types.ModuleType("guard"))
    monkeypatch.setitem(sys.modules, "guard.recibo", None)  # import falla
    v = _veredicto_con_recibo()
    assert firmar_recibo(v, tmp_path / "k.pem") == v


def test_firmar_recibo_que_devuelve_recibo(monkeypatch, tmp_path):
    def firmar(recibo: Recibo, clave) -> Recibo:
        return recibo.model_copy(update={"firma": "jws", "kid": "guard-key-1"})

    _guard_recibo_falso(monkeypatch, firmar)
    v = firmar_recibo(_veredicto_con_recibo(), tmp_path / "k.pem")
    assert v.recibo.firma == "jws" and v.recibo.kid == "guard-key-1"


def test_firmar_recibo_que_devuelve_jws(monkeypatch, tmp_path):
    _guard_recibo_falso(monkeypatch, lambda recibo, clave: "a.b.c")
    v = firmar_recibo(_veredicto_con_recibo(), tmp_path / "k.pem")
    assert v.recibo.firma == "a.b.c" and v.recibo.kid == "guard-key-1"


def test_clave_publica_jwk(monkeypatch, tmp_path):
    _guard_recibo_falso(monkeypatch, lambda recibo, clave: "a.b.c")
    datos = clave_publica(tmp_path / "k.pem")
    assert datos["kid"] == "guard-key-1"
    assert datos["jwk"]["kty"] == "EC" and datos["jwk"]["crv"] == "P-256"
    assert "d" not in datos["jwk"]  # nunca la privada


def test_clave_publica_sin_guard_recibo(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "guard.recibo", None)
    assert clave_publica(tmp_path / "k.pem") is None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_por_defecto():
    c = Config.desde_entorno({})
    assert c.port == 8010
    # Servicios locales por IPv4: en Windows `localhost` prueba primero ::1,
    # donde el comercio (127.0.0.1) y el catálogo (0.0.0.0) no escuchan, y
    # cada intento fallido suma ~2 s.
    assert c.merchant_upstream_url == "http://127.0.0.1:8001/a2a/merchant_agent"
    assert c.catalogo_url == "http://127.0.0.1:8011"
    # La URL pública que ve el cliente A2A no cambia (la usa MERCHANT_AGENT_URL).
    assert c.proxy_public_url == "http://localhost:8010/a2a/merchant_agent"
    assert c.guard_activo is True
    assert c.events_jsonl == REPO_ROOT / ".logs" / "guard_events.jsonl"
    assert c.guard_key_path == REPO_ROOT / ".temp-db" / "guard_signing_key.pem"
    assert c.rpc_path == "/a2a/merchant_agent"


def test_config_desde_entorno(tmp_path):
    c = Config.desde_entorno(
        {
            "PROXY_PORT": "9010",
            "MERCHANT_UPSTREAM_URL": "http://m:1/a2a/merchant_agent/",
            "PROXY_PUBLIC_URL": "http://p:2/x/y",
            "CATALOGO_URL": "http://c:3/",
            "GUARD_ACTIVO": "false",
            "GUARD_EVENTS_JSONL": str(tmp_path / "e.jsonl"),
            "GUARD_KEY_PATH": "otra/clave.pem",
        }
    )
    assert c.port == 9010
    assert c.merchant_upstream_url == "http://m:1/a2a/merchant_agent"
    assert c.proxy_public_url == "http://p:2/x/y"
    assert c.rpc_path == "/x/y"
    assert c.catalogo_url == "http://c:3"
    assert c.guard_activo is False
    assert c.events_jsonl == tmp_path / "e.jsonl"
    assert c.guard_key_path == REPO_ROOT / "otra" / "clave.pem"


@pytest.mark.parametrize("valor", ["quizas", ""])
def test_config_guard_activo_invalido(valor):
    with pytest.raises(ValueError):
        Config.desde_entorno({"GUARD_ACTIVO": valor})


# ---------------------------------------------------------------------------
# Fuentes y helpers A2A
# ---------------------------------------------------------------------------


def test_intent_desde_demo_world():
    intent = cargar_intent()
    assert intent["hard"]["max_price"] == 150.0


def test_intent_cae_al_fixture(monkeypatch):
    monkeypatch.setitem(sys.modules, "demo_world.scenario", None)
    monkeypatch.setitem(sys.modules, "demo_world", None)
    intent = cargar_intent()
    assert intent == cargar_fixture("baseline")["intent"]


def test_reescribir_card_no_muta_la_original():
    card = {"url": "http://a", "additionalInterfaces": [{"url": "http://a", "transport": "JSONRPC"}]}
    nueva = a2a.reescribir_card(card, "http://p")
    assert nueva["url"] == "http://p"
    assert nueva["additionalInterfaces"][0]["url"] == "http://p"
    assert card["url"] == "http://a"
    assert card["additionalInterfaces"][0]["url"] == "http://a"


def test_decodificar_jwt():
    header, payload = a2a.decodificar_jwt(
        "eyJhbGciOiJFUzI1NiJ9.eyJpZCI6ImNhcnRfMSJ9.c2ln"
    )
    assert header == {"alg": "ES256"} and payload == {"id": "cart_1"}
    assert a2a.decodificar_jwt("no-es-un-jwt") == (None, None)


def test_rutas_relativas_a_la_raiz():
    assert REPO_ROOT == Path(__file__).resolve().parents[2]
