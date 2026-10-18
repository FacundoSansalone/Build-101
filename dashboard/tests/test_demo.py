"""La demo de una pantalla (`/demo`): el input único le habla al agente de
compras por la API de ADK o, si nuestro agente tiene una pregunta pendiente,
al proxy; el modo demo contesta solo los pasos del sample que no aportan; el
selector cambia el escenario del comercio. Mismas protecciones que el resto
de la API del panel.

ADK y proxy falsos con `httpx.MockTransport`; datos sintéticos de test.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from dashboard import demo_api as demo
from dashboard.server import RUTAS_POST, Config, config_desde_entorno, create_app

PROXY = "http://localhost:8010"
ADK = "http://localhost:8000"
BASE = "http://localhost:8020"
TOKEN = "token-de-test-0123456789abcdefghijklmnop"
SESIONES = "/apps/shopping_agent/users/demo/sessions"


# ---------------------------------------------------------------------------
# Ayudantes
# ---------------------------------------------------------------------------


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


_n = 0


def ev(autor: str, *, texto: str | None = None, llamada: tuple[str, dict] | None = None,
       respuesta: tuple[str, Any] | None = None, error: str | None = None,
       delta: dict | None = None, pensamiento: str | None = None) -> dict[str, Any]:
    """Un evento de ADK como lo serializa `/run_sse` (camelCase)."""
    global _n
    _n += 1
    partes: list[dict[str, Any]] = []
    if pensamiento:
        partes.append({"text": pensamiento, "thought": True})
    if texto:
        partes.append({"text": texto})
    if llamada:
        partes.append({"functionCall": {"id": f"f{_n}", "name": llamada[0], "args": llamada[1]}})
    if respuesta:
        partes.append({"functionResponse": {"id": f"f{_n}", "name": respuesta[0], "response": respuesta[1]}})
    e: dict[str, Any] = {
        "id": f"ev-{_n}", "invocationId": "inv-test", "author": autor, "timestamp": time.time(),
        "actions": {"stateDelta": delta or {}},
    }
    if partes:
        e["content"] = {"role": "model", "parts": partes}
    if error:
        e["errorMessage"] = error
    return e


class FakeADK:
    """La API de `adk web` (sesiones y `/run_sse`) con turnos guionados."""

    def __init__(self, turnos: list[list[dict[str, Any]]] | None = None, caido: bool = False):
        self.turnos = list(turnos or [])
        self.caido = caido
        self.mensajes: list[str] = []
        self.pedidos: list[httpx.Request] = []
        self.sesiones: dict[str, dict[str, Any]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.pedidos.append(request)
        if self.caido:
            raise httpx.ConnectError("adk caído", request=request)
        ruta = request.url.path
        if request.method == "POST" and ruta == SESIONES:
            sid = f"ses-test-{len(self.sesiones) + 1}"
            self.sesiones[sid] = {"id": sid, "appName": "shopping_agent", "userId": "demo",
                                  "state": {}, "events": []}
            return httpx.Response(200, json=self.sesiones[sid])
        if request.method == "POST" and ruta == "/run_sse":
            cuerpo = json.loads(request.content)
            assert cuerpo["appName"] == "shopping_agent" and cuerpo["userId"] == "demo"
            texto = cuerpo["newMessage"]["parts"][0]["text"]
            assert cuerpo["newMessage"]["role"] == "user"
            self.mensajes.append(texto)
            sesion = self.sesiones[cuerpo["sessionId"]]
            eventos = self.turnos.pop(0) if self.turnos else [ev("root_agent", texto="Listo.")]
            sesion["events"].append({"author": "user", "content": {"role": "user", "parts": [{"text": texto}]}})
            for e in eventos:
                sesion["events"].append(e)
                sesion["state"].update(e.get("actions", {}).get("stateDelta") or {})
            sse = "".join(f"data: {json.dumps(e)}\n\n" for e in eventos)
            return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})
        if request.method == "GET" and ruta.startswith(SESIONES + "/"):
            sid = ruta.rsplit("/", 1)[1]
            if sid not in self.sesiones:
                return httpx.Response(404, json={"detail": "Session not found"})
            return httpx.Response(200, json=self.sesiones[sid])
        return httpx.Response(404, json={"detail": "no"})


class FakeProxy:
    """Lo que usa la demo del proxy: compras y las respuestas a nuestro agente."""

    def __init__(self, compras: list[dict[str, Any]] | None = None, caido: bool = False):
        self.compras = compras or []
        self.caido = caido
        self.posts: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.caido:
            raise httpx.ConnectError("proxy caído", request=request)
        if request.method == "GET" and request.url.path == "/guard/compras":
            return httpx.Response(200, json={"compras": self.compras})
        if request.method == "POST" and request.url.path in (
            "/guard/compras/contexto", "/guard/compras/decision"
        ):
            self.posts.append(request)
            return httpx.Response(200, json={"compra_id": json.loads(request.content)["compra_id"]})
        return httpx.Response(404, json={"error": "no"})


def _escribir_token(repo: Path) -> None:
    path = repo / ".temp-db" / "guard_api_token"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TOKEN + "\n", encoding="utf-8")


def _app(tmp_path: Path, adk: FakeADK | None = None, proxy: FakeProxy | None = None,
         auto: bool = True):
    adk = adk or FakeADK()
    proxy = proxy or FakeProxy()
    cfg = Config(
        repo_root=tmp_path, proxy_url=PROXY, adk_url=ADK,
        proxy_transport=httpx.MockTransport(proxy.handler),
        adk_transport=httpx.MockTransport(adk.handler),
        world_temp_dir=tmp_path / "world-temp", demo_auto=auto,
    )
    return create_app(cfg)


def _post(c: TestClient, ruta: str, cuerpo: Any, **headers):
    return c.post(ruta, content=json.dumps(cuerpo),
                  headers={"Origin": BASE, "Content-Type": "application/json", **headers})


def _esperar(c: TestClient, segundos: float = 5.0) -> dict[str, Any]:
    """Hasta que la conversación con el agente termina (corre en segundo plano)."""
    fin = time.monotonic() + segundos
    while True:
        estado = c.get("/api/demo/estado").json()
        if not estado["ocupado"]:
            return estado
        assert time.monotonic() < fin, "la conversación no terminó"
        time.sleep(0.02)


def _compra(estado: str, *, creada: datetime | None = None, vence: datetime | None = None,
            confirmar: bool = False, compra_id: str = "cmp-test01") -> dict[str, Any]:
    return {
        "compra_id": compra_id, "estado": estado,
        "creada": _iso(creada or _ahora()),
        "vence": _iso(vence) if vence else _iso(_ahora() + timedelta(minutes=2)),
        "pregunta": "pregunta de test", "intake": {"turno": 1 if confirmar else 0, "confirmar": confirmar},
    }


# ---------------------------------------------------------------------------
# Ruteo del input único
# ---------------------------------------------------------------------------


def test_sin_pregunta_pendiente_el_texto_va_al_agente_de_compras():
    assert demo.rutear([], "quiero una notebook").destino == "agente"
    terminadas = [_compra("negociando"), _compra("aceptada"), _compra("bloqueada")]
    assert demo.rutear(terminadas, "hola").destino == "agente"


def test_con_el_porque_pendiente_la_respuesta_va_a_nuestro_agente():
    ruta = demo.rutear([_compra("esperando_contexto")], "  la uso para programar  ")
    assert ruta.destino == "contexto"
    assert ruta.cuerpo == {"compra_id": "cmp-test01", "respuesta": "la uso para programar"}


def test_en_la_confirmacion_de_la_ficha_un_si_confirma_y_otra_cosa_corrige():
    pendiente = [_compra("esperando_contexto", confirmar=True)]
    for si in ("sí", "Si, confirmo", "dale", "ok", "confirmo"):
        ruta = demo.rutear(pendiente, si)
        assert ruta.destino == "confirmar", si
        assert ruta.cuerpo == {"compra_id": "cmp-test01", "confirmar": True}
    ruta = demo.rutear(pendiente, "el tope es 1500")
    assert ruta.destino == "contexto" and ruta.cuerpo["respuesta"] == "el tope es 1500"
    # Fuera del turno de confirmación, "sí" es una respuesta más.
    assert demo.rutear([_compra("esperando_contexto")], "sí").destino == "contexto"


def test_omitir_la_pregunta():
    ruta = demo.rutear([_compra("esperando_contexto")], "omitir")
    assert ruta.destino == "omitir" and ruta.cuerpo == {"compra_id": "cmp-test01", "omitir": True}


def test_la_decision_sobre_una_alternativa_es_si_o_no():
    pendiente = [_compra("esperando_decision")]
    assert demo.rutear(pendiente, "Sí, acepto").cuerpo == {"compra_id": "cmp-test01", "decision": "aceptar"}
    assert demo.rutear(pendiente, "no, gracias").cuerpo == {"compra_id": "cmp-test01", "decision": "rechazar"}
    assert demo.rutear(pendiente, "¿cuánto sale?").destino == "aclarar"


def test_no_se_contesta_una_pregunta_vencida_ni_de_una_corrida_anterior():
    vencida = _compra("esperando_contexto", vence=_ahora() - timedelta(seconds=1))
    assert demo.rutear([vencida], "para programar").destino == "agente"
    vieja = _compra("esperando_contexto", creada=_ahora() - timedelta(minutes=5))
    assert demo.rutear([vieja], "para programar", desde=_ahora() - timedelta(minutes=1)).destino == "agente"
    assert demo.rutear([vieja], "para programar").destino == "contexto"  # sin corrida: cualquiera vigente


def test_rutear_toma_la_pregunta_pendiente_mas_nueva():
    vieja = _compra("esperando_contexto", compra_id="cmp-vieja", creada=_ahora() - timedelta(seconds=30))
    nueva = _compra("esperando_decision", compra_id="cmp-nueva")
    assert demo.rutear([nueva, vieja], "sí").cuerpo["compra_id"] == "cmp-nueva"


# ---------------------------------------------------------------------------
# Modo demo: los pasos del sample que no aportan se contestan solos
# ---------------------------------------------------------------------------


def _sesion(*eventos) -> list[dict[str, Any]]:
    return list(eventos)


def test_billetera_primero_google_wallet_y_despues_el_permiso():
    eventos = _sesion(ev("shipping_address_collector", texto="digital wallet or manually?"))
    paso = demo.siguiente_auto({"chosen_cart_id": "cart_1"}, eventos, Counter())
    assert paso.paso == "billetera" and "Google Wallet" in paso.texto
    paso = demo.siguiente_auto({"chosen_cart_id": "cart_1"}, eventos, Counter(billetera=1))
    assert paso.paso == "billetera" and paso.texto.lower().startswith("sí")


def test_tarjeta_terminada_en_4444_de_la_lista_de_la_billetera():
    eventos = _sesion(
        ev("shipping_address_collector", respuesta=("get_shipping_address", {"city": "x"})),
        ev("payment_method_collector", respuesta=("get_payment_methods", {
            "payment_method_aliases": ["American Express ending in 8888", "American Express ending in 4444"]})),
        ev("payment_method_collector", texto="Which one?"),
    )
    paso = demo.siguiente_auto({"chosen_cart_id": "cart_1"}, eventos, Counter())
    assert paso.paso == "tarjeta" and paso.texto == "American Express ending in 4444"


def test_confirmacion_solo_con_el_checkout_firmado_en_el_estado():
    eventos = _sesion(ev("root_agent", texto="Confirm?"))
    estado = {"chosen_cart_id": "cart_1", "payment_credential_token": {"value": "t"},
              "checkout_data": {"checkout_jwt": "x"}}
    paso = demo.siguiente_auto(estado, eventos, Counter())
    assert paso.paso == "confirmar"
    # El LLM "alucina" que creó el checkout (DEMO.md): sin checkout_data no se confirma.
    sin = {k: v for k, v in estado.items() if k != "checkout_data"}
    assert demo.siguiente_auto(sin, eventos, Counter(continuar=3)) is None


def test_otp_y_fin_con_el_recibo():
    eventos = _sesion(ev("root_agent", texto="Enter the OTP"))
    estado = {"chosen_cart_id": "cart_1", "checkout_data": {}, "checkout_mandate_sdjwt": "x",
              "initiate_payment_task_id": "task-1"}
    paso = demo.siguiente_auto(estado, eventos, Counter())
    assert paso.paso == "otp" and paso.texto == "123"
    assert demo.siguiente_auto({**estado, "payment_receipt": {"id": "r"}}, eventos, Counter()) is None


def test_despues_de_un_bloqueo_del_guard_no_se_contesta_nada():
    eventos = _sesion(
        ev("root_agent", error="Gemini server error (RuntimeError: Pre-Sign Guard blocked this "
                               "purchase before signing: x). Retrying..."),
        ev("root_agent", texto="I have created the checkout. Confirm?"),
    )
    estado = {"chosen_cart_id": "cart_1", "payment_credential_token": {"value": "t"}}
    assert demo.siguiente_auto(estado, eventos, Counter()) is None


def test_la_eleccion_del_producto_es_del_usuario():
    eventos = _sesion(ev("shopper", texto="Which one would you like?"))
    assert demo.siguiente_auto({}, eventos, Counter()) is None
    assert demo.siguiente_auto({"available_carts": []}, eventos, Counter()) is None


def test_hay_un_tope_de_respuestas_automaticas():
    eventos = _sesion(ev("shipping_address_collector", texto="ok?"))
    assert demo.siguiente_auto({"chosen_cart_id": "c"}, eventos, Counter(billetera=3)) is None
    assert demo.siguiente_auto({"chosen_cart_id": "c"}, eventos, Counter(tarjeta=6, otp=6)) is None


# ---------------------------------------------------------------------------
# Endpoints: conversación con el agente de compras (ADK falso)
# ---------------------------------------------------------------------------


def test_el_primer_mensaje_crea_la_sesion_y_corre_el_agente(tmp_path):
    adk = FakeADK([[
        ev("shopper", llamada=("find_products", {"catalog_search": "laptop 16 GB"})),
        ev("shopper", respuesta=("find_products", [{"cart_id": "cart_1"}])),
        ev("shopper", pensamiento="(razonamiento interno)", texto="Here are **4** options. I recommend cart_1."),
    ]])
    with TestClient(_app(tmp_path, adk=adk), base_url=BASE) as c:
        r = _post(c, "/api/demo/mensaje", {"texto": "quiero una notebook"})
        assert r.status_code == 202 and r.json()["destino"] == "agente"
        estado = _esperar(c)
    assert adk.mensajes == ["quiero una notebook"]
    assert estado["sesion"]["id"] == "ses-test-1"
    tipos = [(m["tipo"], m.get("nombre") or m.get("texto")) for m in estado["mensajes"]]
    assert tipos == [
        ("usuario", "quiero una notebook"),
        ("herramienta", "find_products"),
        ("agente", "Here are **4** options. I recommend cart_1."),
    ]
    herramienta = estado["mensajes"][1]
    assert herramienta["args"] == {"catalog_search": "laptop 16 GB"}
    assert all(m["id"] and m["ts"] for m in estado["mensajes"])
    assert "razonamiento interno" not in json.dumps(estado)


def _turnos_compra_completa() -> list[list[dict[str, Any]]]:
    return [
        # El usuario elige: el shopper selecciona y la dirección pregunta por la billetera.
        [ev("shopper", llamada=("select_cart", {"cart_id": "cart_1"})),
         ev("shopper", respuesta=("select_cart", {"result": "Cart cart_1 selected."}),
            delta={"chosen_cart_id": "cart_1"}),
         ev("shipping_address_collector", texto="Digital wallet or manual?")],
        [ev("shipping_address_collector", texto="This is a demo... Is that ok?")],
        [ev("shipping_address_collector", respuesta=("get_shipping_address", {"city": "x"})),
         ev("payment_method_collector", respuesta=("get_payment_methods", {
             "payment_method_aliases": ["American Express ending in 4444", "Bugs's PayPal account"]})),
         ev("payment_method_collector", texto="1. Amex 4444 2. PayPal. Which?")],
        [ev("payment_method_collector", respuesta=("get_payment_credential_token", {"status": "success"}),
            delta={"payment_credential_token": {"value": "tok"}}),
         ev("root_agent", llamada=("create_checkout", {})),
         ev("root_agent", respuesta=("create_checkout", {"checkout_jwt": "x"}),
            delta={"checkout_data": {"checkout_jwt": "x"}}),
         ev("root_agent", texto="Summary: Lumen Book. Confirm?")],
        [ev("root_agent", delta={"checkout_mandate_sdjwt": "a", "payment_mandate_sdjwt": "b"}),
         ev("root_agent", llamada=("initiate_payment", {})),
         ev("root_agent", respuesta=("initiate_payment", {"result": "Verification required: OTP"}),
            delta={"initiate_payment_task_id": "task-1"}),
         ev("root_agent", texto="Please enter the OTP.")],
        [ev("root_agent", respuesta=("initiate_payment_with_otp", {"result": "Status: completed"}),
            delta={"payment_receipt": {"payment_id": "p-1"}}),
         ev("root_agent", texto="Your payment was successful!")],
    ]


def test_modo_demo_contesta_solo_billetera_tarjeta_confirmacion_y_otp(tmp_path):
    adk = FakeADK([[ev("shopper", texto="Options... which one?")], *_turnos_compra_completa()])
    with TestClient(_app(tmp_path, adk=adk), base_url=BASE) as c:
        _post(c, "/api/demo/mensaje", {"texto": "quiero una notebook"})
        _esperar(c)
        r = _post(c, "/api/demo/mensaje", {"texto": "la primera"})
        assert r.status_code == 202
        estado = _esperar(c)
    assert adk.mensajes[:2] == ["quiero una notebook", "la primera"]
    autos = adk.mensajes[2:]
    assert "Google Wallet" in autos[0]
    assert autos[1].lower().startswith("sí")
    assert autos[2] == "American Express ending in 4444"
    assert autos[3].lower().startswith("sí") and "confirmo" in autos[3].lower()
    assert autos[4] == "123"
    assert len(adk.mensajes) == 7  # después del pago no contesta nada
    mensajes = estado["mensajes"]
    assert [m["paso"] for m in mensajes if m["tipo"] == "auto"] == [
        "billetera", "billetera", "tarjeta", "confirmar", "otp"]
    tarjeta = next(m for m in mensajes if m.get("paso") == "tarjeta")
    assert tarjeta["detalle"] == "4444"
    # Lo que preguntó el agente en un paso automático no se muestra.
    visibles = [m["texto"] for m in mensajes if m["tipo"] == "agente" and not m["oculto"]]
    assert visibles == ["Options... which one?", "Your payment was successful!"]
    assert mensajes[-1]["tipo"] == "pago" and mensajes[-1]["cart_id"] == "cart_1"
    assert estado["fin"] == "pagado"


def test_con_el_modo_demo_apagado_no_contesta_nada(tmp_path):
    adk = FakeADK([[ev("shipping_address_collector", texto="Digital wallet or manual?",
                       delta={"chosen_cart_id": "cart_1"})]])
    with TestClient(_app(tmp_path, adk=adk, auto=False), base_url=BASE) as c:
        _post(c, "/api/demo/mensaje", {"texto": "la primera"})
        estado = _esperar(c)
        assert estado["auto"] is False
        r = _post(c, "/api/demo/auto", {"auto": True})
        assert r.status_code == 200 and r.json()["auto"] is True
        assert _post(c, "/api/demo/auto", {"auto": "si"}).status_code == 400
    assert adk.mensajes == ["la primera"]
    assert [m for m in estado["mensajes"] if m["tipo"] == "agente"][0]["oculto"] is False


def test_el_agente_ocupado_no_recibe_otro_mensaje(tmp_path):
    adk = FakeADK()
    app = _app(tmp_path, adk=adk)
    with TestClient(app, base_url=BASE) as c:
        app.state.demo.ocupado = True
        r = _post(c, "/api/demo/mensaje", {"texto": "otra cosa"})
        assert r.status_code == 409 and r.json()["error"] == "agente_ocupado"
    assert adk.mensajes == []


def test_texto_invalido(tmp_path):
    with TestClient(_app(tmp_path), base_url=BASE) as c:
        for malo in ({}, {"texto": ""}, {"texto": "   "}, {"texto": 3}, {"texto": "x" * 1001},
                     {"texto": "hola", "otra": 1}, []):
            assert _post(c, "/api/demo/mensaje", malo).status_code == 400, malo


def test_adk_caido_deja_un_error_y_libera_el_input(tmp_path):
    with TestClient(_app(tmp_path, adk=FakeADK(caido=True)), base_url=BASE) as c:
        assert _post(c, "/api/demo/mensaje", {"texto": "hola"}).status_code == 202
        estado = _esperar(c)
    errores = [m for m in estado["mensajes"] if m["tipo"] == "error"]
    assert errores and errores[0]["codigo"] == "adk_no_responde"
    assert "adk caído" not in json.dumps(estado)


def test_los_errores_del_agente_se_marcan_sin_texto_crudo(tmp_path):
    adk = FakeADK([[
        ev("root_agent", error="Gemini server error (RuntimeError: Pre-Sign Guard blocked this purchase "
                               "before signing: Bloqueado: x). Retrying..."),
        ev("root_agent", error="Gemini server error (ValueError: algo del comercio). Retrying..."),
        ev("root_agent", texto="Sorry, I could not create the checkout."),
    ]])
    with TestClient(_app(tmp_path, adk=adk), base_url=BASE) as c:
        _post(c, "/api/demo/mensaje", {"texto": "dale"})
        estado = _esperar(c)
    errores = [m for m in estado["mensajes"] if m["tipo"] == "error_agente"]
    assert [m["guard"] for m in errores] == [True, False]
    assert "algo del comercio" not in json.dumps(estado)


def test_respuesta_a_nuestro_agente_va_al_proxy_con_el_token(tmp_path):
    _escribir_token(tmp_path)
    adk = FakeADK()
    proxy = FakeProxy([_compra("esperando_contexto")])
    with TestClient(_app(tmp_path, adk=adk, proxy=proxy), base_url=BASE) as c:
        r = _post(c, "/api/demo/mensaje", {"texto": "para programar"})
        assert r.status_code == 200 and r.json()["destino"] == "contexto"
        estado = c.get("/api/demo/estado").json()
    (pedido,) = proxy.posts
    assert pedido.url.path == "/guard/compras/contexto"
    assert pedido.headers["x-guard-token"] == TOKEN
    assert json.loads(pedido.content) == {"compra_id": "cmp-test01", "respuesta": "para programar"}
    assert adk.pedidos == []
    assert estado["mensajes"][-1] == {**estado["mensajes"][-1], "tipo": "usuario",
                                      "texto": "para programar", "destino": "contexto"}


def test_decision_y_confirmacion_van_al_proxy(tmp_path):
    _escribir_token(tmp_path)
    proxy = FakeProxy([_compra("esperando_decision")])
    with TestClient(_app(tmp_path, proxy=proxy), base_url=BASE) as c:
        r = _post(c, "/api/demo/mensaje", {"texto": "sí"})
        assert r.status_code == 200 and r.json()["destino"] == "decision"
        r = _post(c, "/api/demo/mensaje", {"texto": "mmm"})
        assert r.status_code == 422 and r.json()["error"] == "se_espera_si_o_no"
        proxy.compras = [_compra("esperando_contexto", confirmar=True)]
        r = _post(c, "/api/demo/mensaje", {"texto": "confirmo"})
        assert r.json()["destino"] == "confirmar"
    rutas = [(p.url.path, json.loads(p.content)) for p in proxy.posts]
    assert rutas == [
        ("/guard/compras/decision", {"compra_id": "cmp-test01", "decision": "aceptar"}),
        ("/guard/compras/contexto", {"compra_id": "cmp-test01", "confirmar": True}),
    ]


def test_sin_proxy_el_texto_va_al_agente(tmp_path):
    adk = FakeADK()
    with TestClient(_app(tmp_path, adk=adk, proxy=FakeProxy(caido=True)), base_url=BASE) as c:
        assert _post(c, "/api/demo/mensaje", {"texto": "hola"}).status_code == 202
        _esperar(c)
    assert adk.mensajes == ["hola"]


def test_nueva_compra_es_una_sesion_nueva(tmp_path):
    adk = FakeADK()
    with TestClient(_app(tmp_path, adk=adk), base_url=BASE) as c:
        _post(c, "/api/demo/mensaje", {"texto": "uno"})
        primera = _esperar(c)
        r = _post(c, "/api/demo/nueva", {})
        assert r.status_code == 200
        vacia = c.get("/api/demo/estado").json()
        assert vacia["sesion"]["id"] is None and vacia["mensajes"] == []
        assert vacia["sesion"]["creada"] >= primera["sesion"]["creada"]
        _post(c, "/api/demo/mensaje", {"texto": "dos"})
        segunda = _esperar(c)
    assert segunda["sesion"]["id"] == "ses-test-2"
    assert len(adk.sesiones) == 2


# ---------------------------------------------------------------------------
# Selector de escenario del comercio
# ---------------------------------------------------------------------------


def test_escenarios_agrupados_por_categoria(tmp_path):
    with TestClient(_app(tmp_path), base_url=BASE) as c:
        datos = c.get("/api/demo/escenarios").json()
    categorias = {g["categoria"]: [o["id"] for o in g["opciones"]] for g in datos["grupos"]}
    assert categorias["Zapatillas"] == ["baseline", "B", "B2", "C"]
    assert categorias["Notebook"] == ["notebook_honesto", "notebook_malicioso", "notebook_dato_falso"]
    assert categorias["Auriculares"] == ["auriculares_honesto", "auriculares_malicioso"]
    etiquetas = {o["id"]: o["etiqueta"] for g in datos["grupos"] for o in g["opciones"]}
    assert etiquetas["notebook_dato_falso"] == "miente un dato"
    assert etiquetas["B"] == "inyección" and etiquetas["C"] == "precio inflado"
    assert datos["activo"] is None


def test_el_pedido_de_ejemplo_sale_de_los_fixtures(tmp_path):
    (tmp_path / "fixtures" / "etapa2").mkdir(parents=True)
    (tmp_path / "fixtures" / "etapa2" / "notebook_honesto.json").write_text(
        json.dumps({"intent": {"user_request": "pedido de test de notebook"}}), encoding="utf-8")
    with TestClient(_app(tmp_path), base_url=BASE) as c:
        datos = c.get("/api/demo/escenarios").json()
    pedidos = {o["id"]: o["pedido"] for g in datos["grupos"] for o in g["opciones"]}
    assert pedidos["notebook_honesto"] == "pedido de test de notebook"
    assert pedidos["B"] is None


def test_cambiar_el_escenario_escribe_el_archivo_del_comercio_y_empieza_otra_compra(tmp_path):
    adk = FakeADK()
    with TestClient(_app(tmp_path, adk=adk), base_url=BASE) as c:
        _post(c, "/api/demo/mensaje", {"texto": "uno"})
        _esperar(c)
        r = _post(c, "/api/demo/escenario", {"escenario": "notebook_dato_falso"})
        assert r.status_code == 200 and r.json()["activo"] == "notebook_dato_falso"
        archivo = tmp_path / "world-temp" / "demo_scenario"
        assert archivo.read_text(encoding="utf-8") == "notebook_dato_falso"
        estado = c.get("/api/demo/estado").json()
        assert estado["escenario"] == "notebook_dato_falso"
        assert estado["mensajes"] == [] and estado["sesion"]["id"] is None
        assert c.get("/api/demo/escenarios").json()["activo"] == "notebook_dato_falso"


@pytest.mark.parametrize("malo", [
    {"escenario": "../../etc/passwd"}, {"escenario": "inexistente"}, {"escenario": "A"},
    {"escenario": 1}, {}, {"escenario": "B", "otra": 1},
])
def test_escenario_invalido(tmp_path, malo):
    with TestClient(_app(tmp_path), base_url=BASE) as c:
        assert _post(c, "/api/demo/escenario", malo).status_code == 400
    assert not (tmp_path / "world-temp" / "demo_scenario").exists()


# ---------------------------------------------------------------------------
# Protecciones (las mismas que el resto de los POST del panel) y la página
# ---------------------------------------------------------------------------

POSTS_DEMO = ["/api/demo/mensaje", "/api/demo/escenario", "/api/demo/nueva", "/api/demo/auto"]


def test_las_rutas_nuevas_estan_protegidas_como_post():
    assert set(POSTS_DEMO) <= RUTAS_POST


@pytest.mark.parametrize("ruta", POSTS_DEMO)
def test_post_de_la_demo_exige_origin_json_y_host(tmp_path, ruta):
    adk = FakeADK()
    with TestClient(_app(tmp_path, adk=adk), base_url=BASE) as c:
        sin_origin = c.post(ruta, content="{}", headers={"Content-Type": "application/json"})
        assert sin_origin.status_code == 403
        ajeno = c.post(ruta, content="{}", headers={"Origin": "http://evil.test",
                                                     "Content-Type": "application/json"})
        assert ajeno.status_code == 403
        cruzado = _post(c, ruta, {}, **{"Sec-Fetch-Site": "cross-site"})
        assert cruzado.status_code == 403
        texto = c.post(ruta, content="{}", headers={"Origin": BASE, "Content-Type": "text/plain"})
        assert texto.status_code == 415
    with TestClient(_app(tmp_path, adk=adk), base_url="http://evil.test:8020") as c:
        assert _post(c, ruta, {}).status_code == 403
    assert adk.pedidos == []


def test_get_de_la_demo_exige_host_local(tmp_path):
    with TestClient(_app(tmp_path), base_url="http://evil.test:8020") as c:
        assert c.get("/api/demo/estado").status_code == 403
        assert c.get("/api/demo/escenarios").status_code == 403


def test_la_pagina_demo_se_sirve_sin_cache_y_sin_cdn(tmp_path):
    with TestClient(_app(tmp_path), base_url=BASE) as c:
        r = c.get("/demo")
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]
        assert r.headers["cache-control"] == "no-cache"
        html = r.text
        assert 'aria-live="polite"' in html
        sin_ns = html.replace("http://www.w3.org/2000/svg", "")
        assert "http://" not in sin_ns and "https://" not in sin_ns
        for recurso in ("demo.css", "demo.js"):
            assert recurso in html
            assert c.get(f"/{recurso}").status_code == 200
        assert c.get("/lib/relato.js").status_code == 200
        assert c.get("/lib/diagrama.js").status_code == 200
        assert c.get("/lib/demo.js").status_code == 200


def test_el_estado_no_expone_secretos(tmp_path):
    _escribir_token(tmp_path)
    with TestClient(_app(tmp_path), base_url=BASE) as c:
        texto = c.get("/api/demo/estado").text + c.get("/api/demo/escenarios").text
    assert TOKEN not in texto


def test_config_de_la_demo_desde_el_entorno(monkeypatch):
    monkeypatch.setenv("ADK_URL", "http://127.0.0.1:9000/")
    monkeypatch.setenv("DEMO_AUTO", "0")
    monkeypatch.setenv("WORLD_TEMP_DB_DIR", "otra/carpeta")
    cfg = config_desde_entorno()
    assert cfg.adk_url == "http://127.0.0.1:9000"
    assert cfg.demo_auto is False
    assert cfg.world_temp_dir.is_absolute() and cfg.world_temp_dir.name == "carpeta"
    monkeypatch.delenv("ADK_URL")
    monkeypatch.delenv("DEMO_AUTO")
    monkeypatch.delenv("WORLD_TEMP_DB_DIR")
    cfg = config_desde_entorno()
    assert cfg.adk_url == "http://127.0.0.1:8000" and cfg.demo_auto is True
    assert cfg.world_temp_dir.parts[-3:] == ("world", "ap2", ".temp-db")
