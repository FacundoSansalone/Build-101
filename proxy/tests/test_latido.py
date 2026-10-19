"""El latido de un pedido A2A retenido (`Config.latido_s`, Vercel).

Entre el cliente y la función, Vercel corta una conexión sin bytes a los
60 s, y un `find_products` de integrador puede quedar retenido más (el
porqué, una decisión). Con `latido_s`, si la respuesta no está a tiempo se
empieza a responder (200) y se manda un espacio cada `latido_s` (en un
stream, un comentario SSE) hasta el cuerpo final. Un error final va como
error JSON-RPC. Sin `latido_s` (la demo local), todo como siempre.

`httpx.ASGITransport` junta el cuerpo entero antes de devolverlo: para ver
cuándo sale cada byte, estos tests llaman a la app ASGI directamente.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

from proxy.config import Config
from proxy.tests.fake_world import HEADERS_A2A, FakeMerchant, cuerpo, parsear_sse, pedido_find_products
from proxy.tests.gateway_ayudantes import BASE_PUBLICA, CLAVE_ACME, cliente_publico
from proxy.tests.postgrest_falso import BaseFalsa
from proxy.tests.test_etapa2 import RESPUESTA_USUARIO, AgenteFalso
from proxy.tests.test_multi_instancia import (
    _compra_computadoras,
    _compra_en,
    _falla,
    _instancia,
)
from proxy.tipos_gateway import HEADER_COMPRA

pytestmark = pytest.mark.anyio

LATIDO_S = 0.05


@pytest.fixture
def config_latido(config_varias: Config) -> Config:
    return replace(config_varias, latido_s=LATIDO_S)


@pytest.fixture
def config_varias(config: Config) -> Config:
    from proxy.tests.gateway_ayudantes import GATEWAY_PUBLIC_URL

    return replace(
        config, gateway_public_url=GATEWAY_PUBLIC_URL, instancias="varias", sondeo_s=0.02,
        gracia_espera_s=0.4, barrera_s=3.0, espera_contexto_s=3.0, espera_decision_s=3.0,
    )


@dataclass
class Llamada:
    """Un pedido HTTP directo a la app ASGI: cada mensaje que manda la app,
    con el momento en que lo manda."""

    tarea: asyncio.Task[None]
    mensajes: list[tuple[float, dict[str, Any]]] = field(default_factory=list)
    se_va: asyncio.Event = field(default_factory=asyncio.Event)
    rpc_id: Any = None

    @property
    def inicio(self) -> dict[str, Any] | None:
        return next((m for _, m in self.mensajes if m["type"] == "http.response.start"), None)

    @property
    def trozos(self) -> list[bytes]:
        return [m.get("body", b"") for _, m in self.mensajes if m["type"] == "http.response.body"]

    @property
    def cuerpo(self) -> bytes:
        return b"".join(self.trozos)

    @property
    def terminada(self) -> bool:
        return any(
            m["type"] == "http.response.body" and not m.get("more_body", False)
            for _, m in self.mensajes
        )

    def cabecera(self, nombre: str) -> str | None:
        inicio = self.inicio or {}
        for clave, valor in inicio.get("headers", []):
            if clave.decode().lower() == nombre.lower():
                return valor.decode()
        return None


def llamar(app: Any, ruta: str, contenido: bytes, headers: dict[str, str]) -> Llamada:
    pedido = httpx.Request("POST", f"{BASE_PUBLICA}{ruta}", content=contenido, headers=headers)
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "headers": [(k.lower(), v) for k, v in pedido.headers.raw], "scheme": "http",
        "path": unquote(pedido.url.path), "raw_path": pedido.url.raw_path.split(b"?")[0],
        "query_string": pedido.url.query, "server": (pedido.url.host, pedido.url.port),
        "client": ("127.0.0.1", 123), "root_path": "",
    }
    entregado = False
    llamada: Llamada

    async def receive() -> dict[str, Any]:
        nonlocal entregado
        if not entregado:
            entregado = True
            return {"type": "http.request", "body": contenido, "more_body": False}
        await llamada.se_va.wait()
        return {"type": "http.disconnect"}

    async def send(mensaje: dict[str, Any]) -> None:
        llamada.mensajes.append((time.monotonic(), mensaje))

    llamada = Llamada(asyncio.create_task(app(scope, receive, send)))
    return llamada


def buscar_directo(app: Any, compra_id: str, *, metodo: str = "message/send") -> Llamada:
    pedido = pedido_find_products("computadoras", metodo=metodo)
    headers = {
        **HEADERS_A2A, HEADER_COMPRA: compra_id, "Authorization": f"Bearer {CLAVE_ACME}",
    }
    llamada = llamar(app, "/a2a/m/merchant_1", cuerpo(pedido), headers)
    llamada.rpc_id = pedido["id"]
    return llamada


async def _hasta(condicion: Any, tope_s: float = 5.0) -> None:
    fin = time.monotonic() + tope_s
    while not condicion():
        if time.monotonic() > fin:
            raise AssertionError("no pasó a tiempo")
        await asyncio.sleep(0.01)


# -- JSON (message/send) ------------------------------------------------------------------


async def test_un_pedido_retenido_late_y_al_final_trae_el_resultado(config_latido):
    base, merchant = BaseFalsa(), FakeMerchant()
    a = _instancia(config_latido, base, merchant, agente=AgenteFalso(cart_id=None))
    async with cliente_publico(a.app) as ca:
        compra_id = await _compra_computadoras(ca, preguntar=True)
        llamada = buscar_directo(a.app, compra_id)
        await _compra_en(ca, compra_id, "esperando_contexto")
        await asyncio.sleep(LATIDO_S * 6)
        # Retenido por el porqué: el status ya salió y van latidos.
        assert llamada.inicio is not None and llamada.inicio["status"] == 200
        assert llamada.cabecera("content-type") == "application/json"
        assert not llamada.terminada
        assert len(llamada.trozos) >= 3 and set(llamada.trozos) == {b" "}

        resp = await ca.post(
            f"/v1/compras/{compra_id}/respuesta", json={"respuesta": RESPUESTA_USUARIO}
        )
        assert resp.status_code == 200
        await asyncio.wait_for(llamada.tarea, 5)
    assert llamada.terminada
    final = json.loads(llamada.cuerpo)  # los espacios de antes no molestan
    assert final["result"]["status"]["state"] == "completed"
    assert final["id"] == llamada.rpc_id
    # El cliente A2A del sample lee con `httpx.Response.json()` y lo valida
    # con los tipos de a2a-sdk.
    assert httpx.Response(200, content=llamada.cuerpo).json() == final
    tipos = pytest.importorskip("a2a.types")
    leida = tipos.SendMessageResponse.model_validate(
        httpx.Response(200, content=llamada.cuerpo).json()
    )
    assert isinstance(leida.root, tipos.SendMessageSuccessResponse)


async def test_si_el_final_es_un_error_va_como_error_jsonrpc(config_latido):
    """La barrera falla después de que el status 200 ya salió: el 503
    `estado_no_disponible` va en el cuerpo como error JSON-RPC (con el status
    que iba a tener), nunca como un resultado."""
    base, merchant = BaseFalsa(), FakeMerchant()
    a = _instancia(config_latido, base, merchant, agente=AgenteFalso(cart_id=None))
    async with cliente_publico(a.app) as ca:
        compra_id = await _compra_computadoras(ca, preguntar=True)
        llamada = buscar_directo(a.app, compra_id)
        await _compra_en(ca, compra_id, "esperando_contexto")
        await _hasta(lambda: llamada.inicio is not None)
        _falla(base, "POST", "gw_ofertas")  # las ofertas no van a quedar guardadas
        await ca.post(f"/v1/compras/{compra_id}/respuesta", json={"respuesta": RESPUESTA_USUARIO})
        await asyncio.wait_for(llamada.tarea, 5)
    assert llamada.inicio["status"] == 200
    final = json.loads(llamada.cuerpo)
    assert "result" not in final
    assert final["id"] == llamada.rpc_id
    assert final["error"]["data"] == {"codigo": "estado_no_disponible", "http_status": 503}
    assert final["error"]["message"].startswith("KaiAI: ")
    assert "cart_1" not in llamada.cuerpo.decode()
    tipos = pytest.importorskip("a2a.types")
    leida = tipos.SendMessageResponse.model_validate(final)
    assert isinstance(leida.root, tipos.JSONRPCErrorResponse)  # el cliente lo lanza como error


async def test_una_respuesta_rapida_sale_como_siempre_con_su_status(config_latido):
    base, merchant = BaseFalsa(), FakeMerchant()
    a = _instancia(replace(config_latido, latido_s=5.0), base, merchant)
    async with cliente_publico(a.app) as ca:
        compra_id = await _compra_computadoras(ca)
    rapida = buscar_directo(a.app, compra_id)
    await asyncio.wait_for(rapida.tarea, 5)
    assert rapida.inicio["status"] == 200 and rapida.cuerpo.startswith(b"{")
    assert len(rapida.trozos) == 1
    desconocida = buscar_directo(a.app, "cmp-0000000000000000")
    await asyncio.wait_for(desconocida.tarea, 5)
    assert desconocida.inicio["status"] == 404  # el error, con su status
    assert json.loads(desconocida.cuerpo)["error"]["data"]["codigo"] == "compra_desconocida"


async def test_sin_latido_todo_como_antes(config_varias):
    base, merchant = BaseFalsa(), FakeMerchant()
    assert config_varias.latido_s is None
    a = _instancia(config_varias, base, merchant, agente=AgenteFalso(cart_id=None))
    async with cliente_publico(a.app) as ca:
        compra_id = await _compra_computadoras(ca, preguntar=True)
        llamada = buscar_directo(a.app, compra_id)
        await _compra_en(ca, compra_id, "esperando_contexto")
        await asyncio.sleep(0.3)
        assert llamada.mensajes == []  # nada sale hasta tener la respuesta
        _falla(base, "POST", "gw_ofertas")
        await ca.post(f"/v1/compras/{compra_id}/respuesta", json={"respuesta": RESPUESTA_USUARIO})
        await asyncio.wait_for(llamada.tarea, 5)
    assert llamada.inicio["status"] == 503  # el error, con su status
    assert llamada.cuerpo.startswith(b"{")


async def test_si_el_cliente_se_va_el_pedido_sigue_y_escribe(config_latido):
    base, merchant = BaseFalsa(), FakeMerchant()
    agente = AgenteFalso(cart_id=None)
    a = _instancia(config_latido, base, merchant, agente=agente)
    async with cliente_publico(a.app) as ca:
        compra_id = await _compra_computadoras(ca, preguntar=True)
        llamada = buscar_directo(a.app, compra_id)
        await _compra_en(ca, compra_id, "esperando_contexto")
        await _hasta(lambda: len(llamada.trozos) >= 2)
        llamada.se_va.set()  # el cliente corta
        await asyncio.wait_for(llamada.tarea, 5)
        assert not llamada.terminada
        # La búsqueda retenida sigue: contestan y termina (y lo escribe).
        resp = await ca.post(
            f"/v1/compras/{compra_id}/respuesta", json={"respuesta": RESPUESTA_USUARIO}
        )
        assert resp.status_code == 200
        await _compra_en(ca, compra_id, "sin_oportunidad")
    assert agente.respuestas == [RESPUESTA_USUARIO]
    assert base.filas("gw_ofertas")  # las ofertas quedaron registradas


# -- SSE (message/stream) -------------------------------------------------------------------


async def test_un_stream_retenido_late_con_comentarios(config_latido):
    base, merchant = BaseFalsa(), FakeMerchant()
    a = _instancia(config_latido, base, merchant, agente=AgenteFalso(cart_id=None))
    async with cliente_publico(a.app) as ca:
        compra_id = await _compra_computadoras(ca, preguntar=True)
        llamada = buscar_directo(a.app, compra_id, metodo="message/stream")
        await _compra_en(ca, compra_id, "esperando_contexto")
        await asyncio.sleep(LATIDO_S * 6)
        assert llamada.inicio["status"] == 200
        assert llamada.cabecera("content-type").startswith("text/event-stream")
        assert len(llamada.trozos) >= 3 and set(llamada.trozos) == {b": latido\n\n"}
        await ca.post(f"/v1/compras/{compra_id}/respuesta", json={"respuesta": RESPUESTA_USUARIO})
        await asyncio.wait_for(llamada.tarea, 5)
    eventos = parsear_sse(llamada.cuerpo)  # los comentarios no son eventos
    assert eventos and all("result" in e for e in eventos)
    assert eventos[-1]["result"]["status"]["state"] == "completed"
    assert any("ap2.cart" in json.dumps(e) for e in eventos)
    # Como lo lee el cliente A2A del sample (httpx-sse): los latidos no llegan.
    sse = pytest.importorskip("httpx_sse._decoders")
    decodificador, leidos = sse.SSEDecoder(), []
    for linea in llamada.cuerpo.decode().splitlines():
        evento = decodificador.decode(linea)
        if evento is not None and evento.data:
            leidos.append(json.loads(evento.data))
    assert leidos == eventos


async def test_un_stream_retenido_con_error_final_termina_con_un_evento_de_error(config_latido):
    base, merchant = BaseFalsa(), FakeMerchant()
    a = _instancia(config_latido, base, merchant, agente=AgenteFalso(cart_id=None))
    async with cliente_publico(a.app) as ca:
        compra_id = await _compra_computadoras(ca, preguntar=True)
        llamada = buscar_directo(a.app, compra_id, metodo="message/stream")
        await _compra_en(ca, compra_id, "esperando_contexto")
        await _hasta(lambda: llamada.inicio is not None)
        _falla(base, "POST", "gw_ofertas")
        await ca.post(f"/v1/compras/{compra_id}/respuesta", json={"respuesta": RESPUESTA_USUARIO})
        await asyncio.wait_for(llamada.tarea, 5)
    eventos = parsear_sse(llamada.cuerpo)
    assert [e["error"]["data"]["codigo"] for e in eventos] == ["estado_no_disponible"]
    assert "cart_1" not in llamada.cuerpo.decode()


# -- config ---------------------------------------------------------------------------------


def test_el_latido_esta_apagado_por_defecto():
    assert Config().latido_s is None
    assert Config.desde_entorno({}).latido_s is None


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [("", None), ("0", None), ("off", None), ("15", 15.0), ("0.5", 0.5), ("60", 60.0)],
)
def test_la_variable_del_latido(valor, esperado):
    assert Config.desde_entorno({"GATEWAY_LATIDO_S": valor}).latido_s == esperado


@pytest.mark.parametrize("valor", ["-1", "61", "nan", "inf", "quince"])
def test_un_latido_invalido_no_arma_la_config(valor):
    with pytest.raises(ValueError):
        Config.desde_entorno({"GATEWAY_LATIDO_S": valor})
    with pytest.raises(ValueError):
        replace(Config(), latido_s=float(valor) if valor not in ("quince",) else -1.0)
