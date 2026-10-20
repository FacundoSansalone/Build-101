"""La demo de una pantalla (`/demo`): el input único le habla al agente de
compras por la API de ADK o, si nuestro agente tiene una pregunta pendiente,
al proxy; el modo demo contesta solo los pasos del sample que no aportan; el
selector cambia el escenario del comercio. Mismas protecciones que el resto
de la API del panel.

ADK y proxy falsos con `httpx.MockTransport`; datos sintéticos de test.
"""

from __future__ import annotations

import json
import re
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
        self.pedidos: list[httpx.Request] = []  # POST /guard/compras/pedido
        self.orden: list[str] | None = None  # si se fija, el camino de cada pedido

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.orden is not None:
            self.orden.append(f"proxy {request.url.path}")
        if self.caido:
            raise httpx.ConnectError("proxy caído", request=request)
        if request.method == "GET" and request.url.path == "/guard/compras":
            return httpx.Response(200, json={"compras": self.compras})
        if request.method == "POST" and request.url.path == "/guard/compras/pedido":
            self.pedidos.append(request)
            return httpx.Response(200, json={"vence": _iso(_ahora() + timedelta(minutes=3))})
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
         auto: bool = True, fijos: bool = False):
    adk = adk or FakeADK()
    proxy = proxy or FakeProxy()
    cfg = Config(
        repo_root=tmp_path, proxy_url=PROXY, adk_url=ADK,
        proxy_transport=httpx.MockTransport(proxy.handler),
        adk_transport=httpx.MockTransport(adk.handler),
        world_temp_dir=tmp_path / "world-temp", demo_auto=auto,
        demo_escenarios_fijos=fijos,
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


# Un sí o un no con algo más (un número, un plazo, una condición) no es un sí
# o un no: lo que agrega el usuario no se descarta en silencio.
SI_CON_CONDICIONES = (
    "Sí, pero hasta 300 dólares",
    "dale pero que sean 25",
    "si, el tope es 500 en vez de 400",
    "Ok, mejor para el viernes",
    "sí 25",
    "confirmo, si llegan el lunes",
)
SI_LIMPIOS = (
    "sí", "Si, confirmo", "dale", "ok", "okay", "confirmo", "Sí, acepto", "de acuerdo",
    "perfecto, gracias", "Listo, ¡gracias!", "claro que sí", "sí, por favor", "Está bien",
    "esta bien, dale", "bueno", "sí, confirmo la ficha", "acepto la alternativa",
)


def test_en_la_confirmacion_un_si_con_condiciones_es_una_correccion():
    pendiente = [_compra("esperando_contexto", confirmar=True)]
    for texto in SI_CON_CONDICIONES:
        ruta = demo.rutear(pendiente, texto)
        assert ruta.destino == "contexto", texto
        assert ruta.cuerpo == {"compra_id": "cmp-test01", "respuesta": texto}
    for texto in SI_LIMPIOS:
        assert demo.rutear(pendiente, texto).destino == "confirmar", texto


def test_en_la_decision_un_si_o_un_no_con_condiciones_pide_aclarar():
    pendiente = [_compra("esperando_decision")]
    for texto in (*SI_CON_CONDICIONES, "no, pero mandalas el viernes", "no hasta el lunes",
                  "no, 25", "si no llegan, no", "no sé", "no, está bien", "gracias"):
        assert demo.rutear(pendiente, texto).destino == "aclarar", texto
    for texto in SI_LIMPIOS:
        assert demo.rutear(pendiente, texto).cuerpo == {"compra_id": "cmp-test01", "decision": "aceptar"}, texto
    for texto in ("no", "No.", "no, gracias", "nop", "rechazo", "no, no me sirve", "no acepto",
                  "no, muchas gracias"):
        assert demo.rutear(pendiente, texto).cuerpo == {"compra_id": "cmp-test01", "decision": "rechazar"}, texto


def test_un_si_o_un_no_con_condiciones_en_la_decision_da_422_sin_llamar_al_proxy(tmp_path):
    proxy = FakeProxy(compras=[_compra("esperando_decision")])
    with TestClient(_app(tmp_path, proxy=proxy), base_url=BASE) as c:
        for texto in ("Sí, pero hasta 300 dólares", "no, pero mandalas el viernes"):
            r = _post(c, "/api/demo/mensaje", {"texto": texto})
            assert r.status_code == 422 and r.json()["error"] == "se_espera_si_o_no", texto
    assert proxy.posts == []


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
    eventos = _sesion(
        ev("root_agent", respuesta=("initiate_payment", {"result": "Verification required: enter the code"})),
        ev("root_agent", texto="Enter the OTP"),
    )
    estado = {"chosen_cart_id": "cart_1", "checkout_data": {}, "checkout_mandate_sdjwt": "x",
              "initiate_payment_task_id": "task-1"}
    paso = demo.siguiente_auto(estado, eventos, Counter())
    assert paso.paso == "otp" and paso.texto == "123"
    assert demo.siguiente_auto({**estado, "payment_receipt": {"id": "r"}}, eventos, Counter()) is None
    # Lo que devolvió de verdad la herramienta en la punta a punta: la tarea
    # del pago queda input_required (el desafío viaja en un data part).
    real = _sesion(ev("root_agent", respuesta=("initiate_payment", {"result": "Status: TaskState.input_required"})),
                   ev("root_agent", texto="Se requiere un código OTP."))
    assert demo.siguiente_auto(estado, real, Counter()).paso == "otp"


def test_el_agente_principal_que_narra_despues_de_elegir_sigue():
    """Visto en la punta a punta: después de elegir, root_agent contó su plan
    y esperó antes de pedir la dirección."""
    eventos = _sesion(ev("shopper", respuesta=("select_cart", {"result": "ok"})),
                      ev("root_agent", texto="Soy el root_agent. Voy a usar estas herramientas: …"))
    paso = demo.siguiente_auto({"chosen_cart_id": "cart_1"}, eventos, Counter())
    assert paso.paso == "continuar"


def test_sin_desafio_del_pago_no_se_manda_el_otp():
    """Visto en la punta a punta: el pago falló (el filtro cortó la respuesta)
    y el estado igual tenía la tarea del pago; sin el desafío no hay OTP."""
    estado = {"chosen_cart_id": "cart_1", "checkout_data": {}, "checkout_mandate_sdjwt": "x",
              "initiate_payment_task_id": "task-1"}
    fallido = _sesion(
        ev("root_agent", respuesta=("initiate_payment", {"result": "Status: failed"})),
        ev("root_agent", texto="The payment failed. Do you want to try again?"),
    )
    assert demo.siguiente_auto(estado, fallido, Counter()) is None
    # Un desafío viejo no cuenta si la última respuesta del pago falló.
    viejo = _sesion(
        ev("root_agent", respuesta=("initiate_payment", {"result": "Verification required: code"})),
        ev("root_agent", respuesta=("initiate_payment_with_otp", {"result": "Status: failed"})),
        ev("root_agent", texto="It failed."),
    )
    assert demo.siguiente_auto(estado, viejo, Counter(otp=1)) is None


def test_el_cobro_sin_lista_de_tarjetas_todavia_sigue():
    """El agente de la tarjeta a veces cuenta su plan y espera antes de pedir
    la lista: se le dice que siga."""
    eventos = _sesion(
        ev("shipping_address_collector", respuesta=("get_shipping_address", {"city": "x"})),
        ev("payment_method_collector", texto="1. Obtener tus métodos de pago 2. Presentarlos"),
    )
    paso = demo.siguiente_auto({"chosen_cart_id": "cart_1"}, eventos, Counter())
    assert paso.paso == "continuar" and paso.texto.lower().startswith("sí")
    assert demo.siguiente_auto({"chosen_cart_id": "cart_1"}, eventos, Counter(continuar=3)) is None
    # Ya con la dirección, el que narra puede ser el de la dirección.
    narra = _sesion(
        ev("shipping_address_collector", respuesta=("get_shipping_address", {"city": "x"})),
        ev("shipping_address_collector", texto="Status: transfiero al root_agent."),
    )
    assert demo.siguiente_auto({"chosen_cart_id": "cart_1"}, narra, Counter()).paso == "continuar"


def test_despues_de_un_bloqueo_del_guard_no_se_contesta_nada():
    eventos = _sesion(
        ev("root_agent", error="Gemini server error (RuntimeError: Pre-Sign Guard blocked this "
                               "purchase before signing: x). Retrying..."),
        ev("root_agent", texto="I have created the checkout. Confirm?"),
    )
    estado = {"chosen_cart_id": "cart_1", "payment_credential_token": {"value": "t"}}
    assert demo.siguiente_auto(estado, eventos, Counter()) is None


def test_el_bloqueo_se_reconoce_tambien_con_el_texto_del_proxy():
    eventos = _sesion(
        ev("root_agent", error="RuntimeError: KaiAI: bloqueado. motivo. No se presenta para firma."),
        ev("root_agent", texto="I have created the checkout. Confirm?"),
    )
    assert demo.bloqueo_del_guard(eventos)
    estado = {"chosen_cart_id": "cart_1", "payment_credential_token": {"value": "t"}}
    assert demo.siguiente_auto(estado, eventos, Counter()) is None


def test_los_parametros_autorizados_no_son_un_bloqueo():
    """Regresión (prueba de punta a punta del 2026-10-18): al aceptar la
    alternativa, find_products trae "KaiAI: el usuario autorizó …". Eso no es
    un freno: la billetera y el resto del cobro se siguen contestando solos."""
    carts = {
        "carts": [{"cart_id": "cart_2", "item_label": "x", "amount": 18000.0, "currency": "USD"}],
        "parameters_authorized_by_the_user": (
            "KaiAI: el usuario autorizó nuevos parámetros para esta compra (Cantidad: 20). "
            "La oferta cart_2 los cumple: seguí la compra con esos parámetros."
        ),
        "authorized_cart_id": "cart_2",
    }
    eventos = _sesion(
        ev("shopper", respuesta=("find_products", carts)),
        ev("shipping_address_collector", texto="¿Would you prefer to use a digital wallet?"),
    )
    assert not demo.bloqueo_del_guard(eventos)
    paso = demo.siguiente_auto({"chosen_cart_id": "cart_2"}, eventos, Counter())
    assert paso is not None and paso.paso == "billetera"


@pytest.mark.parametrize("freno", [
    "KaiAI: escalar. motivo. El checkout requiere aprobación humana.",
    "KaiAI: en pausa. Hay una alternativa esperando la decisión del usuario.",
    "Pre-Sign Guard held this checkout for human review: x",
])
def test_retener_o_pausar_tambien_frena(freno):
    eventos = _sesion(ev("root_agent", respuesta=("create_checkout", {"error": freno})))
    assert demo.bloqueo_del_guard(eventos)


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


def test_en_modo_demo_la_narracion_del_cobro_no_se_muestra(tmp_path):
    """Después de elegir el producto, en modo demo solo se ve lo último que
    dice el agente cuando espera al usuario (la narración intermedia, no)."""
    adk = FakeADK([[
        ev("shopper", texto="Status: Producto elegido.", delta={"chosen_cart_id": "cart_1"}),
        ev("shopper", texto="Status: delegando."),
        ev("shopper", texto="¿Querés elegir otro producto?"),
    ]])
    with TestClient(_app(tmp_path, adk=adk), base_url=BASE) as c:
        _post(c, "/api/demo/mensaje", {"texto": "la primera"})
        estado = _esperar(c)
    agente = [(m["texto"], m["oculto"]) for m in estado["mensajes"] if m["tipo"] == "agente"]
    assert agente == [("Status: Producto elegido.", True), ("Status: delegando.", True),
                      ("¿Querés elegir otro producto?", False)]


def test_la_pausa_entre_respuestas_automaticas_espacia_los_turnos(tmp_path, monkeypatch):
    """Con el plan gratuito de Gemini (15 pedidos por minuto) conviene
    espaciar las respuestas automáticas: DEMO_PAUSA_AUTO_S."""
    pausas: list[float] = []
    dormir = demo.asyncio.sleep

    async def sleep_falso(segundos: float, *args, **kwargs):
        pausas.append(segundos)
        await dormir(0)

    monkeypatch.setattr(demo.asyncio, "sleep", sleep_falso)
    adk = FakeADK([[ev("shipping_address_collector", texto="wallet?", delta={"chosen_cart_id": "cart_1"})],
                   [ev("shipping_address_collector", texto="ok?")],
                   [ev("shopper", texto="fin")]])
    cfg = Config(repo_root=tmp_path, proxy_url=PROXY, adk_url=ADK,
                 proxy_transport=httpx.MockTransport(FakeProxy().handler),
                 adk_transport=httpx.MockTransport(adk.handler),
                 world_temp_dir=tmp_path / "world-temp", demo_pausa_auto_s=6.0)
    with TestClient(create_app(cfg), base_url=BASE) as c:
        _post(c, "/api/demo/mensaje", {"texto": "la primera"})
        _esperar(c)
    assert adk.mensajes[0] == "la primera" and len(adk.mensajes) == 3
    assert pausas.count(6.0) == 2  # antes de cada respuesta automática


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


GENERALES = [
    "general", "general_inyeccion", "general_otra_categoria", "general_dato_falso",
    "general_miente_categoria", "general_precio_inflado", "general_entrega_partes",
    "general_entrega_partes_inyeccion", "general_sin_stock", "general_sin_resultados",
]
FIJOS = [
    "baseline", "B", "B2", "C", "notebook_honesto", "notebook_malicioso", "notebook_dato_falso",
    "auriculares_honesto", "auriculares_malicioso", "computadoras", "computadoras_malicioso",
]


@pytest.fixture
def mundo(monkeypatch):
    """El mundo con el comercio general y los escenarios fijos (lo que conoce
    `demo_world`), sin depender de la copia de world/ que tenga el entorno."""

    def fijar(escenarios=(*GENERALES, *FIJOS), generales=GENERALES):
        monkeypatch.setattr(demo, "_escenarios_del_mundo", lambda: set(escenarios))
        monkeypatch.setattr(demo, "_generales_del_mundo", lambda: list(generales))

    fijar()
    return fijar


def _grupos(c: TestClient) -> dict[str, list[tuple[str, str]]]:
    datos = c.get("/api/demo/escenarios").json()
    return {g["categoria"]: [(o["id"], o["etiqueta"]) for o in g["opciones"]] for g in datos["grupos"]}


def test_el_selector_ofrece_los_comportamientos_del_comercio_general(tmp_path, mundo):
    with TestClient(_app(tmp_path), base_url=BASE) as c:
        datos = c.get("/api/demo/escenarios").json()
    (grupo,) = datos["grupos"]  # sin DEMO_ESCENARIOS_FIJOS, solo el comercio general
    assert grupo["categoria"] == "Comportamiento del comercio" and grupo["tipo"] == "general"
    assert [(o["id"], o["etiqueta"]) for o in grupo["opciones"]] == [
        ("general", "Honesto"),
        ("general_inyeccion", "Inyección en la oferta"),
        ("general_otra_categoria", "Ofrece otra categoría a tiempo"),
        ("general_dato_falso", "Miente un dato"),
        ("general_miente_categoria", "Miente la categoría"),
        ("general_precio_inflado", "Precio inflado"),
        ("general_entrega_partes", "Entrega en partes"),
        ("general_entrega_partes_inyeccion", "Entrega en partes + inyección"),
        ("general_sin_stock", "Sin stock suficiente"),
        ("general_sin_resultados", "Sin resultados"),
    ]
    honestos = {o["id"]: o["honesto"] for o in grupo["opciones"]}
    assert honestos["general"] is True and honestos["general_entrega_partes"] is True
    assert honestos["general_inyeccion"] is False and honestos["general_dato_falso"] is False
    assert honestos["general_sin_resultados"] is None
    # El comercio general responde a cualquier pedido: no hay pedido de ejemplo.
    assert {o["pedido"] for o in grupo["opciones"]} == {None}
    assert datos["activo"] is None


def test_las_sugerencias_de_pedido_no_dependen_del_escenario_ni_de_una_fecha(tmp_path, mundo):
    with TestClient(_app(tmp_path), base_url=BASE) as c:
        sugerencias = c.get("/api/demo/escenarios").json()["sugerencias"]
        _post(c, "/api/demo/escenario", {"escenario": "general_inyeccion"})
        assert c.get("/api/demo/escenarios").json()["sugerencias"] == sugerencias
    textos = [s["texto"] for s in sugerencias]
    assert len(textos) == 4 and all(s["etiqueta"] for s in sugerencias)
    assert "monitor de 27 pulgadas" in textos[0] and "400 USD" in textos[0] and "viernes" in textos[0]
    assert "20 computadoras" in textos[1] and "lunes" in textos[1] and "20000 USD" in textos[1]
    assert "cancelación de ruido" in textos[2] and "150 USD" in textos[2]
    assert "talle 42" in textos[3] and "correr" in textos[3] and "120 USD" in textos[3]
    for texto in textos:  # días de la semana, nunca una fecha fija
        assert 0 < len(texto) <= demo.MAX_TEXTO
        assert not re.search(r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}", texto), texto


def test_los_escenarios_fijos_solo_con_demo_escenarios_fijos(tmp_path, mundo):
    with TestClient(_app(tmp_path), base_url=BASE) as c:
        assert list(_grupos(c)) == ["Comportamiento del comercio"]
        r = _post(c, "/api/demo/escenario", {"escenario": "B"})
        assert r.status_code == 400 and "B" not in r.json()["opciones"]
    assert not (tmp_path / "world-temp" / "demo_scenario").exists()
    with TestClient(_app(tmp_path, fijos=True), base_url=BASE) as c:
        grupos = _grupos(c)
        assert list(grupos) == ["Comportamiento del comercio", "Escenarios fijos (prueba)"]
        assert [e for e, _ in grupos["Escenarios fijos (prueba)"]] == FIJOS
        etiquetas = dict(grupos["Escenarios fijos (prueba)"])
        assert etiquetas["notebook_dato_falso"] == "Notebook · miente un dato"
        assert etiquetas["B"] == "Zapatillas · inyección" and etiquetas["C"] == "Zapatillas · precio inflado"
        assert etiquetas["computadoras"] == "Computadoras · entrega en dos partes"
        assert _post(c, "/api/demo/escenario", {"escenario": "B"}).status_code == 200


def test_sin_el_comercio_general_en_el_mundo_no_se_ofrece(tmp_path, mundo):
    """Mientras world/ no tenga los presets generales, no aparecen (y elegirlos
    da 400). Sin DEMO_ESCENARIOS_FIJOS, el selector queda vacío."""
    mundo(escenarios=FIJOS, generales=[])
    with TestClient(_app(tmp_path), base_url=BASE) as c:
        assert c.get("/api/demo/escenarios").json()["grupos"] == []
        assert _post(c, "/api/demo/escenario", {"escenario": "general"}).status_code == 400
    with TestClient(_app(tmp_path, fijos=True), base_url=BASE) as c:
        assert list(_grupos(c)) == ["Escenarios fijos (prueba)"]
    # Solo algunos presets: los que el mundo conoce, en el mismo orden.
    mundo(escenarios=["general_dato_falso", "general"], generales=["general_dato_falso", "general"])
    with TestClient(_app(tmp_path), base_url=BASE) as c:
        assert _grupos(c)["Comportamiento del comercio"] == [
            ("general", "Honesto"), ("general_dato_falso", "Miente un dato"),
        ]


def test_un_preset_general_nuevo_del_mundo_aparece_con_un_nombre_legible(tmp_path, mundo):
    """Un comportamiento que world/ suma (`"tipo": "general"`) sin etiqueta acá
    se ofrece igual, con el nombre armado desde su id."""
    mundo(escenarios=[*GENERALES, "general_entrega_tarde"], generales=[*GENERALES, "general_entrega_tarde", "../x"])
    with TestClient(_app(tmp_path), base_url=BASE) as c:
        opciones = _grupos(c)["Comportamiento del comercio"]
        assert opciones[-1] == ("general_entrega_tarde", "Entrega tarde")
        assert all(e != "../x" for e, _ in opciones)
        assert _post(c, "/api/demo/escenario", {"escenario": "general_entrega_tarde"}).status_code == 200


def test_sin_demo_world_se_ofrece_la_lista_entera(tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "_escenarios_del_mundo", lambda: None)
    monkeypatch.setattr(demo, "_generales_del_mundo", lambda: [])
    with TestClient(_app(tmp_path), base_url=BASE) as c:
        assert [e for e, _ in _grupos(c)["Comportamiento del comercio"]] == GENERALES
    with TestClient(_app(tmp_path, fijos=True), base_url=BASE) as c:
        assert ("computadoras_malicioso", "Computadoras · inyección") in _grupos(c)["Escenarios fijos (prueba)"]


def test_el_pedido_de_ejemplo_sale_de_los_fixtures_solo_en_los_fijos(tmp_path, mundo):
    (tmp_path / "fixtures" / "etapa2").mkdir(parents=True)
    (tmp_path / "fixtures" / "etapa2" / "notebook_honesto.json").write_text(
        json.dumps({"intent": {"user_request": "pedido de test de notebook"}}), encoding="utf-8")
    # Aunque haya un fixture con ese nombre, un preset general no tiene pedido de ejemplo.
    (tmp_path / "fixtures" / "scenario_general.json").write_text(
        json.dumps({"intent": {"user_request": "no se usa"}}), encoding="utf-8")
    assert demo._pedido_de_ejemplo(Config(repo_root=tmp_path), "general") is None
    with TestClient(_app(tmp_path, fijos=True), base_url=BASE) as c:
        datos = c.get("/api/demo/escenarios").json()
    pedidos = {o["id"]: o["pedido"] for g in datos["grupos"] for o in g["opciones"]}
    assert pedidos["notebook_honesto"] == "pedido de test de notebook"
    assert pedidos["B"] is None and pedidos["general"] is None


def test_los_nombres_alcanzan_para_el_escenario_activo_aunque_no_se_ofrezca(tmp_path, mundo):
    """El comercio puede tener activo un escenario fijo (`run_world.sh B`)
    aunque el selector no lo muestre: la página igual sabe cómo nombrarlo."""
    carpeta = tmp_path / "world-temp"
    carpeta.mkdir()
    (carpeta / "demo_scenario").write_text("B", encoding="utf-8")
    with TestClient(_app(tmp_path), base_url=BASE) as c:
        datos = c.get("/api/demo/escenarios").json()
    assert datos["activo"] == "B"
    assert datos["nombres"]["B"] == {"etiqueta": "Zapatillas · inyección", "tipo": "fijo", "pedido": None}
    assert datos["nombres"]["general_precio_inflado"] == {
        "etiqueta": "Precio inflado", "tipo": "general", "pedido": None,
    }


def test_cambiar_el_escenario_escribe_el_archivo_del_comercio_y_empieza_otra_compra(tmp_path, mundo):
    adk = FakeADK()
    with TestClient(_app(tmp_path, adk=adk, fijos=True), base_url=BASE) as c:
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
        # Un comportamiento del comercio general, igual.
        r = _post(c, "/api/demo/escenario", {"escenario": "general_entrega_partes"})
        assert r.status_code == 200 and archivo.read_text(encoding="utf-8") == "general_entrega_partes"


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


def test_el_interruptor_del_intermediario_va_al_proxy_con_token_y_empieza_otra_compra(tmp_path):
    """La página apaga el intermediario con el reenvío del panel (con el
    token, sin que el navegador lo vea) y empieza una compra nueva, para que
    la corrida sin intermediario no se mezcle con la anterior."""
    _escribir_token(tmp_path)
    recibidos: list[httpx.Request] = []

    def proxy(request: httpx.Request) -> httpx.Response:
        recibidos.append(request)
        if request.url.path == "/guard/state":
            return httpx.Response(200, json={"guard_activo": json.loads(request.content)["guard_activo"]})
        return httpx.Response(200, json={"compras": []})

    cfg = Config(repo_root=tmp_path, proxy_url=PROXY, adk_url=ADK,
                 proxy_transport=httpx.MockTransport(proxy),
                 adk_transport=httpx.MockTransport(FakeADK().handler),
                 world_temp_dir=tmp_path / "world-temp")
    with TestClient(create_app(cfg), base_url=BASE) as c:
        _post(c, "/api/demo/mensaje", {"texto": "uno"})
        antes = _esperar(c)
        assert _post(c, "/api/demo/nueva", {}).status_code == 200
        r = _post(c, "/api/proxy/guard/state", {"guard_activo": False})
        assert r.status_code == 200 and r.json() == {"guard_activo": False}
        despues = c.get("/api/demo/estado").json()
    (estado,) = [p for p in recibidos if p.url.path == "/guard/state"]
    assert estado.headers["x-guard-token"] == TOKEN and "origin" not in estado.headers
    assert TOKEN not in r.text
    assert despues["mensajes"] == [] and despues["sesion"]["creada"] >= antes["sesion"]["creada"]


def test_la_pagina_tiene_el_interruptor_visible(tmp_path):
    with TestClient(_app(tmp_path), base_url=BASE) as c:
        html = c.get("/demo").text
    assert 'id="intermediario"' in html and 'role="switch"' in html
    assert "Intermediario:" in html
    assert 'id="contraste"' in html  # con y sin intermediario, lado a lado


def test_el_estado_no_expone_secretos(tmp_path):
    _escribir_token(tmp_path)
    with TestClient(_app(tmp_path), base_url=BASE) as c:
        texto = c.get("/api/demo/estado").text + c.get("/api/demo/escenarios").text
    assert TOKEN not in texto


def test_config_de_la_demo_desde_el_entorno(monkeypatch):
    monkeypatch.setenv("ADK_URL", "http://127.0.0.1:9000/")
    monkeypatch.setenv("DEMO_AUTO", "0")
    monkeypatch.setenv("WORLD_TEMP_DB_DIR", "otra/carpeta")
    monkeypatch.setenv("DEMO_PAUSA_AUTO_S", "6")
    monkeypatch.setenv("DEMO_ESCENARIOS_FIJOS", "1")
    cfg = config_desde_entorno()
    assert cfg.adk_url == "http://127.0.0.1:9000"
    assert cfg.demo_auto is False
    assert cfg.demo_escenarios_fijos is True
    assert cfg.demo_pausa_auto_s == 6.0
    assert cfg.world_temp_dir.is_absolute() and cfg.world_temp_dir.name == "carpeta"
    monkeypatch.delenv("ADK_URL")
    monkeypatch.delenv("DEMO_AUTO")
    monkeypatch.delenv("WORLD_TEMP_DB_DIR")
    monkeypatch.setenv("DEMO_PAUSA_AUTO_S", "no-es-un-numero")
    monkeypatch.setenv("DEMO_ESCENARIOS_FIJOS", "0")
    cfg = config_desde_entorno()
    assert cfg.adk_url == "http://127.0.0.1:8000" and cfg.demo_auto is True
    assert cfg.demo_escenarios_fijos is False
    monkeypatch.delenv("DEMO_ESCENARIOS_FIJOS")
    assert config_desde_entorno().demo_escenarios_fijos is False  # por defecto, apagado
    assert cfg.demo_pausa_auto_s == 0.0
    assert cfg.world_temp_dir.parts[-3:] == ("world", "ap2", ".temp-db")


def test_el_relato_conoce_las_flexibilidades_fijas_del_intake():
    """`lib/relato.js` dice "lo que entendió" con las flexibilidades del agente
    de decisiones, que son texto fijo: si cambian acá, la línea las perdería."""
    from intencion.agente_decisiones import FLEXIBILIDADES

    relato = (Path(demo.__file__).parent / "static" / "lib" / "relato.js").read_text(encoding="utf-8")
    for _, descripcion in FLEXIBILIDADES.values():
        assert json.dumps(descripcion, ensure_ascii=False) in relato, descripcion


# ---------------------------------------------------------------------------
# El pedido con las palabras del usuario (proxy/pedido_usuario.py)
# ---------------------------------------------------------------------------


def test_lo_que_dijo_el_usuario_se_registra_en_el_proxy_antes_que_el_agente(tmp_path):
    """El agente de compras le pasa al comercio una paráfrasis: el intake del
    proxy lee además lo que escribió el usuario. En cada mensaje al agente va
    todo lo dicho en la compra: una búsqueda en otro turno es otra compra
    para el proxy, y así no repregunta lo que el usuario ya dijo."""
    _escribir_token(tmp_path)
    adk, proxy = FakeADK(), FakeProxy()
    orden: list[str] = []
    proxy.orden = orden
    adk_handler = adk.handler
    adk.handler = lambda request: (orden.append(f"adk {request.url.path}"), adk_handler(request))[1]
    with TestClient(_app(tmp_path, adk=adk, proxy=proxy), base_url=BASE) as c:
        pedido = "Comprame 20 computadoras para el lunes, hasta 20000 USD."
        assert _post(c, "/api/demo/mensaje", {"texto": f"  {pedido} "}).status_code == 202
        _esperar(c)
        _post(c, "/api/demo/mensaje", {"texto": "Sí, la primera."})  # sigue la misma compra
        _esperar(c)
        _post(c, "/api/demo/nueva", {})
        _post(c, "/api/demo/mensaje", {"texto": "Ahora unos auriculares."})
        _esperar(c)
    assert [json.loads(r.content) for r in proxy.pedidos] == [
        {"texto": pedido},
        {"texto": f"{pedido} Sí, la primera."},
        {"texto": "Ahora unos auriculares."},  # otra compra: desde cero
    ]
    for r in proxy.pedidos:  # con el token del proxy, como los otros POST de /guard/*
        assert r.headers["x-guard-token"] == TOKEN and "origin" not in r.headers
    assert orden.index("proxy /guard/compras/pedido") < orden.index("adk /run_sse")
    assert adk.mensajes == [pedido, "Sí, la primera.", "Ahora unos auriculares."]


def test_sin_proxy_o_sin_token_el_mensaje_va_igual_al_agente(tmp_path):
    for proxy, token in ((FakeProxy(caido=True), True), (FakeProxy(), False)):
        if token:
            _escribir_token(tmp_path)
        else:
            (tmp_path / ".temp-db" / "guard_api_token").unlink(missing_ok=True)
        adk = FakeADK()
        with TestClient(_app(tmp_path, adk=adk, proxy=proxy), base_url=BASE) as c:
            r = _post(c, "/api/demo/mensaje", {"texto": "una notebook"})
            assert r.status_code == 202 and r.json() == {"destino": "agente"}
            _esperar(c)
        assert adk.mensajes == ["una notebook"]
        assert proxy.pedidos == []


def test_la_respuesta_a_nuestro_agente_no_se_registra_como_pedido(tmp_path):
    _escribir_token(tmp_path)
    proxy = FakeProxy([_compra("esperando_contexto")])
    with TestClient(_app(tmp_path, proxy=proxy), base_url=BASE) as c:
        assert _post(c, "/api/demo/mensaje", {"texto": "Entran 10 el lunes y 10 el jueves."}).status_code == 200
    assert proxy.pedidos == [] and len(proxy.posts) == 1


def test_la_ruta_y_el_tope_del_pedido_son_los_del_proxy():
    from proxy.app import RUTAS_POST_CONTROL
    from proxy.pedido_usuario import LARGO_MAXIMO

    assert demo.RUTA_PEDIDO_USUARIO in RUTAS_POST_CONTROL  # con token, Host local y JSON
    assert demo.MAX_TEXTO <= LARGO_MAXIMO  # todo lo que acepta el input entra


def test_mientras_se_registra_el_pedido_la_conversacion_queda_ocupada(tmp_path):
    """Otro mensaje que llega en ese momento recibe 409: así el pedido que se
    registró es el mismo que se le manda al agente."""
    _escribir_token(tmp_path)
    proxy = FakeProxy()
    vistos: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/guard/compras/pedido":
            vistos.append(app.state.demo.ocupado)  # la conversación, en ese momento
        return proxy.handler(request)

    app = create_app(Config(
        repo_root=tmp_path, proxy_url=PROXY, adk_url=ADK, world_temp_dir=tmp_path / "world-temp",
        proxy_transport=httpx.MockTransport(handler), adk_transport=httpx.MockTransport(FakeADK().handler),
    ))
    with TestClient(app, base_url=BASE) as c:
        assert _post(c, "/api/demo/mensaje", {"texto": "una notebook"}).status_code == 202
        _esperar(c)
    assert vistos == [True]


def test_nueva_compra_mientras_se_registra_el_pedido_no_lo_manda_a_la_nueva(tmp_path):
    """"Nueva compra" (o el interruptor del Guard) llega mientras el primer
    mensaje espera el registro del pedido en el proxy. Al volver, ese texto
    ya es de la compra anterior: no arranca en la nueva ni le toca `ocupado`
    (acá, otro mensaje de la compra nueva ya la ocupó)."""
    _escribir_token(tmp_path)
    proxy, adk = FakeProxy(), FakeADK()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/guard/compras/pedido":
            app.state.demo.nueva()
            app.state.demo.ocupado = True  # la compra nueva ya tiene su mensaje en curso
        return proxy.handler(request)

    app = create_app(Config(
        repo_root=tmp_path, proxy_url=PROXY, adk_url=ADK, world_temp_dir=tmp_path / "world-temp",
        proxy_transport=httpx.MockTransport(handler), adk_transport=httpx.MockTransport(adk.handler),
    ))
    with TestClient(app, base_url=BASE) as c:
        r = _post(c, "/api/demo/mensaje", {"texto": "una notebook"})
        assert r.status_code == 409 and r.json()["error"] == "compra_reiniciada"
        conversacion = app.state.demo
        assert conversacion.ocupado is True  # el de la compra nueva, intacto
        assert conversacion.mensajes == [] and conversacion.pedido_enviado is False
    assert adk.mensajes == []


def test_nueva_compra_mientras_se_rutea_el_mensaje_tampoco_lo_usa(tmp_path):
    """Lo mismo durante la consulta de compras al proxy (el ruteo): con la
    `creada` de la compra nueva, el texto iba al agente como primer pedido."""
    _escribir_token(tmp_path)
    proxy, adk = FakeProxy(), FakeADK()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/guard/compras":
            app.state.demo.nueva()
        return proxy.handler(request)

    app = create_app(Config(
        repo_root=tmp_path, proxy_url=PROXY, adk_url=ADK, world_temp_dir=tmp_path / "world-temp",
        proxy_transport=httpx.MockTransport(handler), adk_transport=httpx.MockTransport(adk.handler),
    ))
    with TestClient(app, base_url=BASE) as c:
        r = _post(c, "/api/demo/mensaje", {"texto": "una notebook"})
        assert r.status_code == 409 and r.json()["error"] == "compra_reiniciada"
        assert app.state.demo.mensajes == [] and app.state.demo.ocupado is False
    assert adk.mensajes == [] and proxy.pedidos == []


def test_lo_dicho_que_no_entra_deja_el_pedido_y_el_ultimo_mensaje(tmp_path):
    conversacion = demo.Conversacion(Config(repo_root=tmp_path))
    pedido = "Comprame 20 computadoras para el lunes, hasta 20000 USD."
    conversacion.registrar("usuario", texto=pedido, destino="agente")
    conversacion.registrar("usuario", texto="x" * 600, destino="agente")
    conversacion.registrar("usuario", texto="Entran 10 el lunes.", destino="contexto")  # a nuestro agente
    texto = conversacion.pedido_con("Subí el tope a 25000 USD.")
    assert texto == f"{pedido} {'x' * 600} Subí el tope a 25000 USD."  # entra entero
    assert "Entran 10" not in texto  # lo que fue a nuestro agente no es el pedido
    conversacion.registrar("usuario", texto="y" * 600, destino="agente")
    texto = conversacion.pedido_con("Subí el tope a 25000 USD.")
    assert len(texto) <= demo.MAX_TEXTO
    assert texto == f"{pedido} … Subí el tope a 25000 USD."
