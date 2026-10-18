"""Revisión del núcleo del proxy (2026-10-18): fecha de la demo, oyentes,
marcas de tiempo, eventos privados, seguridad y robustez."""

from __future__ import annotations

import errno
import json
import logging
import socket
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

import proxy.__main__ as main_mod
from proxy import compras as compras_mod
from proxy.compras import RegistroCompras
from proxy.config import Config
from proxy.estado import EstadoMemoria
from proxy.seguridad import HEADER_TOKEN
from proxy.tests.ayudantes import RPC, evaluador_espia, texto_estado
from proxy.tests.fake_world import (
    HEADERS_A2A,
    cuerpo,
    pedido_create_checkout,
    pedido_find_products,
)
from proxy.tests.gateway_ayudantes import (
    GATEWAY_PUBLIC_URL,
    armar_gateway,
    buscar,
    checkout,
    cliente_local,
    cliente_publico,
    crear_compra,
)
from proxy.tests.test_compras_api import crear as crear_api
from proxy.tipos_gateway import HEADER_COMPRA

# ---------------------------------------------------------------------------
# DEMO_HOY: el "hoy" de la demo (las ofertas tienen fechas fijas)
# ---------------------------------------------------------------------------


def test_demo_hoy_desde_el_entorno():
    assert Config.desde_entorno({}).demo_hoy is None
    assert Config.desde_entorno({"DEMO_HOY": ""}).demo_hoy is None
    assert Config.desde_entorno({"DEMO_HOY": " 2026-10-18 "}).demo_hoy == date(2026, 10, 18)


@pytest.mark.parametrize("valor", ["18/10/2026", "2026-10-32", "20261018", "2026-W42-7", "hoy"])
def test_demo_hoy_invalido_no_arranca(valor):
    with pytest.raises(ValueError, match="DEMO_HOY"):
        Config.desde_entorno({"DEMO_HOY": valor})


def test_hoy_del_proxy_es_demo_hoy_si_esta_fijado(armar_proxy, config, caplog):
    assert armar_proxy().state.proxy.hoy() == date.today()
    with caplog.at_level(logging.WARNING, logger="proxy"):
        fijado = armar_proxy(config=replace(config, demo_hoy=date(2026, 10, 18)))
    assert fijado.state.proxy.hoy() == date(2026, 10, 18)
    assert any("DEMO_HOY" in r.getMessage() for r in caplog.records)


class _AgenteQueAnotaHoy:
    """Agente de intención que solo anota qué "hoy" recibe (y no pregunta nada:
    sin pregunta, el proxy usa la de siempre)."""

    nombre = "anota_hoy"

    def __init__(self) -> None:
        self.hoy: list[date] = []

    def preguntar(self, pedido, hoy, categorias=None):
        self.hoy.append(hoy)
        raise RuntimeError("sin pregunta")


@pytest.mark.anyio
async def test_el_agente_de_intencion_recibe_demo_hoy(armar_proxy, config):
    import httpx

    agente = _AgenteQueAnotaHoy()
    app = armar_proxy(
        config=replace(config, demo_hoy=date(2026, 10, 18), espera_contexto_s=0.05),
        agente=agente,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8010", timeout=10
    ) as c:
        resp = await c.post(RPC, content=cuerpo(pedido_find_products("B")), headers=HEADERS_A2A)
    assert resp.status_code == 200
    assert agente.hoy == [date(2026, 10, 18)]


# ---------------------------------------------------------------------------
# Oyentes: también [::1] (en Windows, localhost prueba primero IPv6)
# ---------------------------------------------------------------------------


def _hay_ipv6_local() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as s:
            s.bind(("::1", 0))
    except OSError:
        return False
    return True


def _cerrar(sockets: list[socket.socket]) -> None:
    for s in sockets:
        s.close()


def test_con_host_127_tambien_escucha_en_ipv6_local_al_mismo_puerto():
    if not _hay_ipv6_local():
        pytest.skip("sin IPv6 local en esta máquina")
    sockets = main_mod.sockets_de(object(), Config(port=0), False)  # puerto libre cualquiera
    try:
        assert [s.family for s in sockets] == [socket.AF_INET, socket.AF_INET6]
        assert sockets[0].getsockname()[0] == "127.0.0.1"
        assert sockets[1].getsockname()[0] == "::1"
        assert sockets[0].getsockname()[1] == sockets[1].getsockname()[1]
    finally:
        _cerrar(sockets)


def test_con_otro_host_no_suma_ipv6():
    sockets = main_mod.sockets_de(object(), Config(host="localhost", port=0), False)
    try:
        assert [s.family for s in sockets] == [socket.AF_INET]
    finally:
        _cerrar(sockets)


def _sin_ipv6(monkeypatch, error: int) -> list[socket.socket]:
    """`abrir_socket` real para IPv4 y `OSError(error)` para IPv6. Devuelve
    los sockets IPv4 que abrió (para ver que no queden abiertos)."""
    real = main_mod.abrir_socket
    abiertos: list[socket.socket] = []

    def abrir(host: str, puerto: int) -> socket.socket:
        if ":" in host:
            raise OSError(error, "IPv6 de prueba")
        sock = real(host, puerto)
        abiertos.append(sock)
        return sock

    monkeypatch.setattr(main_mod, "abrir_socket", abrir)
    return abiertos


@pytest.mark.parametrize("error", [errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT])
def test_sin_ipv6_sigue_solo_con_ipv4_y_lo_avisa(monkeypatch, caplog, error):
    _sin_ipv6(monkeypatch, error)
    with caplog.at_level(logging.WARNING, logger="proxy"):
        sockets = main_mod.sockets_de(object(), Config(port=0), False)
    try:
        assert [s.family for s in sockets] == [socket.AF_INET]
        assert any("IPv6" in r.getMessage() for r in caplog.records)
    finally:
        _cerrar(sockets)


@pytest.mark.parametrize("error", [errno.EADDRINUSE, errno.EACCES])
def test_ipv6_tomado_por_otro_proceso_no_arranca(monkeypatch, error):
    """Si otro proceso tiene [::1]:<puerto>, el agente que habla con
    localhost le hablaría a ese y no al proxy: no se arranca."""
    abiertos = _sin_ipv6(monkeypatch, error)
    with pytest.raises(SystemExit, match=r"\[::1\]"):
        main_mod.sockets_de(object(), Config(port=0), False)
    assert abiertos and all(s.fileno() == -1 for s in abiertos)  # el IPv4 se cerró


def test_un_puerto_del_proxy_no_lo_puede_tomar_otro_proceso():
    """En Windows, con SO_REUSEADDR (lo que usa uvicorn) otro socket se puede
    colgar del mismo puerto y robar conexiones: el proxy usa
    SO_EXCLUSIVEADDRUSE. En el resto, un puerto escuchando no se comparte."""
    sock = main_mod.abrir_socket("127.0.0.1", 0)
    sock.listen()
    otro = socket.socket()
    try:
        otro.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with pytest.raises(OSError):
            otro.bind(("127.0.0.1", sock.getsockname()[1]))
    finally:
        otro.close()
        sock.close()


def test_un_puerto_tomado_no_arranca_con_un_mensaje_claro():
    ocupado = main_mod.abrir_socket("127.0.0.1", 0)
    ocupado.listen()
    try:
        with pytest.raises(SystemExit, match="no arranca"):
            main_mod.sockets_de(
                object(), Config(host="localhost", port=ocupado.getsockname()[1]), False
            )
    finally:
        ocupado.close()


# ---------------------------------------------------------------------------
# `creada` y `actualizada` monótonas (el integrador ordena por `actualizada`)
# ---------------------------------------------------------------------------

_INSTANTE = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
_UN_MICRO = timedelta(microseconds=1)


@pytest.fixture
def reloj_quieto(monkeypatch):
    """El reloj no avanza (en Windows dos `datetime.now()` seguidos suelen dar
    el mismo instante) y las marcas arrancan de cero."""
    monkeypatch.setattr(compras_mod, "_ahora", lambda: _INSTANTE)
    monkeypatch.setattr(compras_mod, "_marcas", compras_mod.MarcasMonotonas())


@pytest.mark.anyio
async def test_tocar_en_el_mismo_instante_da_marcas_crecientes(reloj_quieto):
    compra = crear_api(RegistroCompras())
    assert compra.creada == compra.actualizada == _INSTANTE
    compra.tocar()
    primera = compra.actualizada
    compra.tocar()
    assert primera == _INSTANTE + _UN_MICRO
    assert compra.actualizada == primera + _UN_MICRO


@pytest.mark.anyio
async def test_dos_compras_en_el_mismo_instante_no_empatan(reloj_quieto):
    registro = RegistroCompras()
    demo = registro.crear("zapatillas", "¿para qué?", None, None)
    api = crear_api(registro)
    assert demo.creada == _INSTANTE
    assert api.creada == api.actualizada == _INSTANTE + _UN_MICRO


@pytest.mark.anyio
async def test_tocar_no_retrocede_ante_una_fila_de_otro_reloj(reloj_quieto):
    """Una compra restaurada con `actualizada` adelantada (otro proceso, otro
    reloj): el próximo cambio igual queda después."""
    compra = crear_api(RegistroCompras())
    compra.actualizada = _INSTANTE + timedelta(seconds=5)
    compra.tocar()
    assert compra.actualizada == _INSTANTE + timedelta(seconds=5) + _UN_MICRO


@pytest.mark.anyio
async def test_listar_compras_desempata_por_id():
    almacen = EstadoMemoria()
    for compra_id in ("cmp-0000000000000002", "cmp-0000000000000003", "cmp-0000000000000001"):
        await almacen.guardar_compra(_fila_compra(compra_id, _INSTANTE))
    listadas = [f["id"] for f in await almacen.listar_compras("acme")]
    assert listadas == ["cmp-0000000000000003", "cmp-0000000000000002", "cmp-0000000000000001"]


def _fila_compra(compra_id: str, creada: datetime) -> dict:
    return {
        "id": compra_id, "integrador_id": "acme", "comercio_id": "merchant_1",
        "referencia": None, "estado": "negociando", "creada": creada.isoformat(),
        "actualizada": creada.isoformat(), "datos": {"pedido": "x", "veredictos": []},
    }


@pytest.mark.anyio
async def test_hidratar_una_compra_que_cierra_su_espera_no_mueve_actualizada(armar_proxy):
    """Después de un reinicio, leer la compra cierra su espera del lado seguro
    (`desde_fila`) y lo guarda, pero eso no es un cambio de la compra: un GET
    no mueve `actualizada` (el integrador ordena por ese campo)."""
    almacen = EstadoMemoria(durable=True)
    compra = crear_api(RegistroCompras())
    compra.estado = "esperando_decision"
    compra.actualizada = _INSTANTE
    await almacen.guardar_compra(compra.a_fila())
    proxy = armar_proxy(almacen=almacen).state.proxy
    restaurada = await proxy.hidratar_compra(compra.id)
    await proxy.en_fondo.esperar()
    fila = await almacen.cargar_compra(compra.id)
    assert restaurada.estado == fila["estado"] == "vencida"
    assert fila["actualizada"] == _INSTANTE.isoformat()


# ---------------------------------------------------------------------------
# Eventos de compras de integrador: a un JSONL aparte (el principal lo sirve
# el dashboard)
# ---------------------------------------------------------------------------


def _leer_jsonl(path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(linea) for linea in path.read_text("utf-8").splitlines() if linea]


def test_jsonl_privado_desde_el_entorno(tmp_path):
    por_defecto = Config.desde_entorno({})
    assert por_defecto.jsonl_privado == por_defecto.events_jsonl.with_name(
        "guard_events_privados.jsonl"
    )
    c = Config.desde_entorno({
        "GUARD_EVENTS_JSONL": str(tmp_path / "e.jsonl"),
        "GUARD_EVENTS_JSONL_PRIVADO": str(tmp_path / "p" / "privados.jsonl"),
    })
    assert c.jsonl_privado == tmp_path / "p" / "privados.jsonl"
    # Sin variable, junto al principal (así un GUARD_EVENTS_JSONL de prueba
    # no escribe los privados en el repo).
    junto = Config.desde_entorno({"GUARD_EVENTS_JSONL": str(tmp_path / "e.jsonl")})
    assert junto.jsonl_privado == tmp_path / "guard_events_privados.jsonl"
    assert Config(events_jsonl=None).jsonl_privado is None
    with pytest.raises(ValueError, match="GUARD_EVENTS_JSONL_PRIVADO"):
        Config.desde_entorno({
            "GUARD_EVENTS_JSONL": str(tmp_path / "e.jsonl"),
            "GUARD_EVENTS_JSONL_PRIVADO": str(tmp_path / "e.jsonl"),
        })


@pytest.mark.anyio
async def test_los_eventos_de_integrador_van_al_jsonl_privado(config):
    mundo = armar_gateway(replace(config, gateway_public_url=GATEWAY_PUBLIC_URL),
                          evaluador_espia()[0])
    async with cliente_publico(mundo.app) as c:
        compra_id = (await crear_compra(c))["compra_id"]
        contexto = (await buscar(c, compra_id)).json()["result"]["contextId"]
        await checkout(c, compra_id, contexto, "cart_1")
    jwt = mundo.merchant.ultimo_checkout["checkout_jwt"]
    async with cliente_local(mundo.app) as c:  # la demo: anónimo, sin clave
        demo = await c.post(RPC, content=cuerpo(pedido_find_products("B")), headers=HEADERS_A2A)
    contexto_demo = demo.json()["result"]["contextId"]

    principal = _leer_jsonl(config.events_jsonl)
    privados = _leer_jsonl(config.jsonl_privado)
    assert {"busqueda_enviada", "ofertas_recibidas", "checkout_recibido", "veredicto"} <= {
        e["tipo"] for e in privados
    }
    assert {e["contexto_id"] for e in privados} == {contexto}
    assert all(e["contexto_id"] != contexto for e in principal)
    assert jwt not in config.events_jsonl.read_text("utf-8")
    # Los de la demo siguen en el principal, como siempre.
    assert {"busqueda_enviada", "ofertas_recibidas"} <= {
        e["tipo"] for e in principal if e["contexto_id"] == contexto_demo
    }
    # En memoria siguen todos (el SSE y el historial los filtran sin token).
    assert {e.contexto_id for e in mundo.proxy.bus.historial()} >= {contexto, contexto_demo}


# ---------------------------------------------------------------------------
# Seguridad: headers de control, /guard/state y tráfico anónimo
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_los_headers_de_control_no_llegan_al_comercio(cliente, merchant):
    """El `cliente` de los tests manda `X-Guard-Token` (como el reenvío del
    dashboard): ni ese ni un `X-Compra-Id` suelto llegan al comercio."""
    resp = await cliente.post(
        RPC, content=cuerpo(pedido_find_products("B")),
        headers={**HEADERS_A2A, HEADER_COMPRA: "cmp-0123456789abcdef"},
    )
    assert resp.status_code == 200
    _, headers = merchant.recibidos[-1]
    nombres = {k.lower() for k in headers}
    assert HEADER_TOKEN.lower() not in nombres
    assert HEADER_COMPRA.lower() not in nombres
    assert headers.get("x-a2a-extensions")  # lo de A2A sigue pasando


@pytest.mark.anyio
async def test_guard_state_sin_token_no_enumera_comercios_ni_integradores(config):
    mundo = armar_gateway(replace(config, gateway_public_url=GATEWAY_PUBLIC_URL),
                          evaluador_espia()[0])
    async with cliente_local(mundo.app) as c:
        con_token = (await c.get("/guard/state")).json()
        del c.headers[HEADER_TOKEN]
        sin_token = (await c.get("/guard/state")).json()
    assert con_token["gateway"] == {
        "integradores": 2, "comercios": ["merchant_1", "tienda_b"],
        "almacen": "memoria", "publico": True,
    }
    assert sin_token["gateway"] == {"almacen": "memoria", "publico": True}
    assert {k: v for k, v in sin_token.items() if k != "gateway"} == {
        k: v for k, v in con_token.items() if k != "gateway"
    }


async def _compra_con_contexto(mundo) -> tuple[str, str]:
    async with cliente_publico(mundo.app) as c:
        compra_id = (await crear_compra(c))["compra_id"]
        contexto = (await buscar(c, compra_id)).json()["result"]["contextId"]
    return compra_id, contexto


@pytest.mark.anyio
async def test_un_pedido_anonimo_con_el_contexto_de_una_compra_de_integrador_no_pasa(config):
    """Sin clave, un `contextId` atado a una compra de integrador: 409
    `contexto_ajeno`, no se reenvía y la compra no cambia (antes la demo la
    adoptaba: evaluaba con su ficha y podía bloquearla)."""
    mundo = armar_gateway(replace(config, gateway_public_url=GATEWAY_PUBLIC_URL),
                          evaluador_espia()[0])
    compra_id, contexto = await _compra_con_contexto(mundo)
    compra = mundo.proxy.compras.get(compra_id)
    antes = (compra.estado, compra.actualizada, len(mundo.merchant.recibidos))
    eventos_antes = len(mundo.proxy.bus.historial())
    pedido = pedido_find_products("B")
    pedido["params"]["message"]["contextId"] = contexto
    async with cliente_local(mundo.app) as c:  # anónimo
        busqueda = await c.post(RPC, content=cuerpo(pedido), headers=HEADERS_A2A)
        compra_ = await c.post(
            RPC, content=cuerpo(pedido_create_checkout(contexto, "cart_1")), headers=HEADERS_A2A
        )
    for resp in (busqueda, compra_):
        assert resp.status_code == 409
        assert resp.json()["error"]["data"]["codigo"] == "contexto_ajeno"
    assert (compra.estado, compra.actualizada, len(mundo.merchant.recibidos)) == antes
    assert len(mundo.proxy.bus.historial()) == eventos_antes


@pytest.mark.anyio
async def test_un_pedido_anonimo_con_un_contexto_de_integrador_en_el_almacen_no_pasa(config):
    """La compra ya no está en memoria (reinicio) pero el almacén sabe de
    quién es el contexto: igual 409."""
    from proxy.estado import EstadoMemoria

    almacen = EstadoMemoria(durable=True)
    config_gw = replace(config, gateway_public_url=GATEWAY_PUBLIC_URL)
    viejo = armar_gateway(config_gw, evaluador_espia()[0], almacen=almacen)
    _, contexto = await _compra_con_contexto(viejo)
    await viejo.esperar_fondo()
    nuevo = armar_gateway(config_gw, evaluador_espia()[0], almacen=almacen)
    async with cliente_local(nuevo.app) as c:
        resp = await c.post(
            RPC, content=cuerpo(pedido_create_checkout(contexto, "cart_1")), headers=HEADERS_A2A
        )
    assert resp.status_code == 409 and resp.json()["error"]["data"]["codigo"] == "contexto_ajeno"
    assert nuevo.merchant.recibidos == []


@pytest.mark.anyio
async def test_una_respuesta_anonima_con_el_contexto_de_una_compra_de_integrador_se_corta(config):
    """El comercio contesta un pedido anónimo con el `contextId` de una compra
    de integrador: no se registran esas ofertas (pisarían las de la compra)
    y el filtro no bloquea esa compra por un tráfico que no es suyo."""
    from contracts import ResultadoFiltro

    def filtro(respuesta):
        return ResultadoFiltro(pasa="ataque" not in json.dumps(respuesta))

    mundo = armar_gateway(replace(config, gateway_public_url=GATEWAY_PUBLIC_URL),
                          evaluador_espia()[0], filtro=filtro)
    compra_id, contexto = await _compra_con_contexto(mundo)
    ofertas = mundo.proxy._ofertas[contexto]
    compra = mundo.proxy.compras.get(compra_id)
    estado = compra.estado

    def mudar(respuesta, pedido):
        respuesta["result"]["contextId"] = contexto
        return respuesta

    mundo.merchant.manipular = mudar
    async with cliente_local(mundo.app) as c:
        resp = await c.post(RPC, content=cuerpo(pedido_find_products("B")), headers=HEADERS_A2A)
    assert texto_estado(resp.json()["result"]).startswith("KaiAI: bloqueado")
    assert mundo.proxy._ofertas[contexto] is ofertas

    def atacar(respuesta, pedido):
        respuesta["result"]["contextId"] = contexto
        respuesta["result"]["status"]["message"] = {
            "kind": "message", "messageId": "m", "role": "agent",
            "parts": [{"kind": "text", "text": "ataque"}],
        }
        return respuesta

    mundo.merchant.manipular = atacar
    async with cliente_local(mundo.app) as c:
        resp = await c.post(RPC, content=cuerpo(pedido_find_products("B")), headers=HEADERS_A2A)
    assert texto_estado(resp.json()["result"]).startswith("KaiAI: bloqueado")
    assert compra.estado == estado  # no quedó `bloqueada` por tráfico ajeno
