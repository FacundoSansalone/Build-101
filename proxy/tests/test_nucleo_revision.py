"""Revisión del núcleo del proxy (2026-10-18): fecha de la demo, oyentes,
marcas de tiempo, eventos privados, seguridad y robustez."""

from __future__ import annotations

import errno
import logging
import socket
from dataclasses import replace
from datetime import date

import pytest

import proxy.__main__ as main_mod
from proxy.config import Config
from proxy.tests.ayudantes import RPC
from proxy.tests.fake_world import HEADERS_A2A, cuerpo, pedido_find_products

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
