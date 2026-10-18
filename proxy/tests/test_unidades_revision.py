"""Piezas puras nuevas de la ronda de revisión: helpers de A2A, aprobaciones
y bus de eventos."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from contracts import GUARD_VEREDICTO_KEY
from proxy import a2a
from proxy.aprobaciones import RegistroAprobaciones, SinEscalar
from proxy.eventos import BusDeEventos

# ---------------------------------------------------------------------------
# JSON estricto y búsqueda de claves
# ---------------------------------------------------------------------------


def test_parsear_json_estricto():
    assert a2a.parsear_json(b'{"a": 1}') == ({"a": 1}, None)
    for malo in (b'{"a": NaN}', b'{"a": Infinity}', b'{"a": -Infinity}', b"{", b"\xff"):
        valor, error = a2a.parsear_json(malo)
        assert valor is None and error
    profundo = b"[" * 100_000 + b"]" * 100_000
    assert a2a.parsear_json(profundo)[0] is None


def test_contar_clave_a_cualquier_profundidad():
    obj = {"a": [{"ap2.checkout": 1}, {"b": {"ap2.checkout": {"ap2.checkout": 2}}}]}
    assert a2a.contar_clave(obj, "ap2.checkout") == 3
    assert a2a.contar_clave([], "x") == 0


def test_menciona_checkout_aunque_no_sea_json_estricto():
    assert a2a.menciona_checkout(b'{"ap2.checkout": {"amount": NaN}}')
    assert a2a.menciona_checkout(b'basura "ap2.checkout" basura')
    assert not a2a.menciona_checkout(b'{"otra": NaN}')


# ---------------------------------------------------------------------------
# guard.veredicto del comercio
# ---------------------------------------------------------------------------


def test_quitar_veredictos_ajenos_no_toca_lo_demas():
    checkout = {"ap2.checkout": {"checkout_jwt": "a.b.c"}}
    task = {
        "kind": "task",
        "artifacts": [
            {"artifactId": "1", "parts": [{"kind": "data", "data": checkout}]},
            {"artifactId": "2", "parts": [{"kind": "data", "data": {GUARD_VEREDICTO_KEY: {}}}]},
            {"artifactId": "3", "parts": [
                {"kind": "data", "data": {"risk_data": "x"}},
                {"kind": "data", "data": {GUARD_VEREDICTO_KEY: {}}},
            ]},
        ],
        "status": {"message": {"parts": [{"kind": "data", "data": {GUARD_VEREDICTO_KEY: {}}}]}},
    }
    limpio, cambio = a2a.quitar_veredictos_ajenos(task)
    assert cambio
    assert [a["artifactId"] for a in limpio["artifacts"]] == ["1", "3"]
    assert limpio["artifacts"][0]["parts"][0]["data"] == checkout
    assert limpio["artifacts"][1]["parts"] == [{"kind": "data", "data": {"risk_data": "x"}}]
    assert limpio["status"]["message"]["parts"] == []
    assert len(task["artifacts"]) == 3  # el original no se muta
    sin_nada = {"kind": "task", "artifacts": []}
    assert a2a.quitar_veredictos_ajenos(sin_nada) == (sin_nada, False)


# ---------------------------------------------------------------------------
# Monto del checkout (atadura de la aprobación humana)
# ---------------------------------------------------------------------------


def _jwt(payload: dict) -> str:
    b = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJFUzI1NiJ9.{b}.c2ln"


# monto_firmado(): ver test_aprobacion_monto_firmado.py (N2, sin fallbacks)


# ---------------------------------------------------------------------------
# Lectura acotada del SSE
# ---------------------------------------------------------------------------


def _lineas(trozos: list[bytes], limite: int = 1000) -> list[str]:
    async def correr():
        async def fuente():
            for t in trozos:
                yield t

        return [linea async for linea in a2a.lineas_acotadas(fuente(), limite)]

    return asyncio.run(correr())


@pytest.mark.parametrize(
    "trozos",
    [
        [b"a\r\nb\nc\rd\n\n"],
        [b"a\r", b"\nb\n", b"c\r", b"d\n", b"\n"],
        [bytes([c]) for c in b"a\r\nb\nc\rd\n\n"],
    ],
    ids=["entero", "cr-partido", "byte-a-byte"],
)
def test_lineas_acotadas_cortes_de_linea(trozos):
    assert _lineas(trozos) == ["a", "b", "c", "d", ""]


def test_lineas_acotadas_respeta_el_tope():
    with pytest.raises(a2a.LimiteExcedido):
        _lineas([b"x" * 100] * 10, limite=500)


def test_lineas_acotadas_utf8_partido_entre_trozos():
    texto = "ñandú\n".encode()
    assert _lineas([texto[i : i + 1] for i in range(len(texto))]) == ["ñandú"]


def test_leer_acotado():
    async def correr(limite):
        async def fuente():
            for _ in range(4):
                yield b"abc"

        return await a2a.leer_acotado(fuente(), limite)

    assert asyncio.run(correr(12)) == b"abc" * 4
    with pytest.raises(a2a.LimiteExcedido) as e:
        asyncio.run(correr(7))
    assert e.value.parcial == b"abcabca"


def test_es_final():
    assert a2a.es_final({"kind": "status-update", "final": True})
    assert not a2a.es_final({"kind": "status-update", "final": False})
    assert a2a.es_final({"kind": "task", "status": {"state": "completed"}})
    assert not a2a.es_final({"kind": "task", "status": {"state": "working"}})
    assert not a2a.es_final({"kind": "artifact-update"})


def test_leer_pedido_tasks_get_y_json_no_estricto():
    p = a2a.leer_pedido(json.dumps(
        {"id": 1, "jsonrpc": "2.0", "method": "tasks/get", "params": {"id": "t-1"}}
    ).encode())
    assert p.es_de_tarea and p.task_id == "t-1" and p.tipo == "otro"
    assert a2a.leer_pedido(b'{"params": NaN}') == a2a.Pedido()


# ---------------------------------------------------------------------------
# Aprobaciones y bus de eventos
# ---------------------------------------------------------------------------


def test_registro_de_aprobaciones():
    r = RegistroAprobaciones()
    with pytest.raises(SinEscalar):
        r.aprobar("c", "cart_1")
    r.registrar_veredicto("c", "cart_1", "bloqueado", 100, "USD")
    with pytest.raises(SinEscalar):
        r.aprobar("c", "cart_1")
    r.registrar_veredicto("c", "cart_1", "escalar", None, "USD")
    with pytest.raises(SinEscalar):
        r.aprobar("c", "cart_1")  # sin monto legible no se puede atar
    r.registrar_veredicto("c", "cart_1", "escalar", 14500, "USD")
    a, creada = r.aprobar("c", "cart_1")
    assert creada and a.estado == "pendiente" and a.a_json()["monto"] == 145.0
    assert r.aprobar("c", "cart_1") == (a, False)
    assert r.consumir("c", "cart_1", 14600, "USD") is None
    assert r.consumir("c", "cart_1", 14500, "EUR") is None
    assert r.consumir("otro", "cart_1", 14500, "USD") is None
    usada = r.consumir("c", "cart_1", 14500, "USD", "t-1")
    assert usada.estado == "usada" and usada.task_id == "t-1"
    assert r.consumir("c", "cart_1", 14500, "USD") is None  # un solo uso
    lista = r.listar()
    assert lista["pendientes"] == [] and [x["id"] for x in lista["usadas"]] == [a.id]


def test_aprobar_otra_vez_con_otro_monto_reemplaza_la_pendiente():
    r = RegistroAprobaciones()
    r.registrar_veredicto("c", "cart_1", "escalar", 100, "USD")
    a, _ = r.aprobar("c", "cart_1")
    r.registrar_veredicto("c", "cart_1", "escalar", 200, "USD")
    b, creada = r.aprobar("c", "cart_1")
    assert creada and b.id != a.id and b.centavos == 200
    assert [x["id"] for x in r.listar()["pendientes"]] == [b.id]


def test_bus_con_tope_de_suscriptores(tmp_path):
    bus = BusDeEventos(tmp_path / "e.jsonl", max_suscriptores=2)
    a, b = bus.suscribir(), bus.suscribir()
    assert a and b and bus.suscribir() is None
    bus.desuscribir(a[1])
    assert bus.suscribir() is not None
