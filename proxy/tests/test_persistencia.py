"""Registro en Supabase: qué se sube, que no se suben secretos y que si la base
falla el proxy sigue igual (decisión: "sigue y avisa")."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from proxy.persistencia import Persistencia, credenciales
from proxy.subir_benchmark import filas
from proxy.tests.ayudantes import buscar, checkout, cliente_de, eventos, evaluador_espia
from proxy.tests.fake_world import FakeMerchant

pytestmark = pytest.mark.anyio

SUPABASE = "https://proyecto.supabase.co"
CLAVE = "clave-de-servicio-de-test"


class FakeSupabase:
    """PostgREST falso: guarda cada POST por tabla."""

    def __init__(self, status: int = 201, caido: bool = False) -> None:
        self.status = status
        self.caido = caido
        self.filas: dict[str, list[dict[str, Any]]] = {}
        self.pedidos: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.pedidos.append(request)
        if self.caido:
            raise httpx.ConnectError("sin red", request=request)
        tabla = request.url.path.removeprefix("/rest/v1/")
        self.filas.setdefault(tabla, []).append(json.loads(request.content))
        return httpx.Response(self.status, text="" if self.status < 300 else '{"message":"x"}')

    def persistencia(self) -> Persistencia:
        return Persistencia(SUPABASE, CLAVE, transport=httpx.MockTransport(self.handler))


def test_sin_credenciales_no_hay_persistencia():
    assert credenciales({}) is None
    assert credenciales({"SUPABASE_URL": SUPABASE}) is None
    assert Persistencia.desde_entorno({"SUPABASE_SERVICE_ROLE_KEY": CLAVE}) is None
    assert credenciales(
        {"SUPABASE_URL": SUPABASE + "/", "SUPABASE_SERVICE_ROLE_KEY": CLAVE}
    ) == (SUPABASE, CLAVE)


def test_acepta_el_nombre_nuevo_de_la_clave_secreta():
    # El dashboard de Supabase hoy la entrega como SUPABASE_SECRET_KEY.
    assert credenciales(
        {"SUPABASE_URL": SUPABASE, "SUPABASE_SECRET_KEY": CLAVE}
    ) == (SUPABASE, CLAVE)
    # Con las dos, manda SUPABASE_SERVICE_ROLE_KEY; vacía cuenta como ausente.
    assert credenciales(
        {"SUPABASE_URL": SUPABASE, "SUPABASE_SERVICE_ROLE_KEY": CLAVE,
         "SUPABASE_SECRET_KEY": "otra"}
    ) == (SUPABASE, CLAVE)
    assert credenciales(
        {"SUPABASE_URL": SUPABASE, "SUPABASE_SERVICE_ROLE_KEY": " ",
         "SUPABASE_SECRET_KEY": CLAVE}
    ) == (SUPABASE, CLAVE)
    assert Persistencia.desde_entorno({"SUPABASE_SECRET_KEY": CLAVE}) is None


async def test_guarda_el_veredicto_sin_mandatos_ni_jwt(armar_proxy, merchant: FakeMerchant):
    db = FakeSupabase()
    persistencia = db.persistencia()
    async with cliente_de(armar_proxy(persistencia=persistencia)) as c:
        contexto = await buscar(c, "B")
        await checkout(c, contexto, "cart_4")
        evs = await eventos(c)
        await persistencia.esperar()

    (fila,) = db.filas["veredictos"]  # solo los eventos `veredicto`
    evento = next(e for e in evs if e["tipo"] == "veredicto")
    assert fila["evento_id"] == evento["id"]
    assert fila["contexto_id"] == contexto
    assert fila["escenario"] == "B"
    assert fila["status"] == evento["datos"]["veredicto"]["status"]
    assert fila["cart_elegido"] == "cart_4"
    assert fila["veredicto"] == evento["datos"]["veredicto"]
    assert fila["evaluador"] == "stub"
    # Lo firmado por el comercio y el usuario no sale del proxy.
    cuerpo = b"".join(p.content for p in db.pedidos)
    assert merchant.ultimo_checkout["checkout_jwt"].encode() not in cuerpo
    assert b'"mandatos"' not in cuerpo
    # Autenticación y upsert idempotente.
    pedido = db.pedidos[0]
    assert pedido.headers["apikey"] == CLAVE
    assert pedido.headers["authorization"] == f"Bearer {CLAVE}"
    assert pedido.url.params["on_conflict"] == "evento_id"
    assert "merge-duplicates" in pedido.headers["prefer"]


async def test_guarda_la_aprobacion_creada_y_usada(armar_proxy):
    db = FakeSupabase()
    persistencia = db.persistencia()
    evaluador, _ = evaluador_espia("escalar")
    async with cliente_de(armar_proxy(evaluador=evaluador, persistencia=persistencia)) as c:
        contexto = await buscar(c, "C")
        await checkout(c, contexto, "cart_1")
        resp = await c.post(
            "/guard/aprobaciones", json={"contexto_id": contexto, "cart_id": "cart_1"}
        )
        assert resp.status_code == 201
        await checkout(c, contexto, "cart_1")  # usa la aprobación
        await persistencia.esperar()

    creada, usada = db.filas["aprobaciones"]
    assert creada["id"] == usada["id"] == resp.json()["id"]
    assert creada["estado"] == "pendiente" and creada["usada"] is None
    assert usada["estado"] == "usada" and usada["usada"] is not None
    assert creada["contexto_id"] == contexto and creada["cart_id"] == "cart_1"
    assert isinstance(creada["centavos"], int) and creada["moneda"]
    assert len(db.filas["veredictos"]) == 2


@pytest.mark.parametrize("db", [FakeSupabase(caido=True), FakeSupabase(status=500)])
async def test_si_supabase_falla_el_proxy_sigue_y_avisa_una_vez(armar_proxy, db):
    persistencia = db.persistencia()
    async with cliente_de(armar_proxy(persistencia=persistencia)) as c:
        for _ in range(2):
            contexto = await buscar(c, "B")
            resp = await checkout(c, contexto, "cart_4")
            assert resp.status_code == 200
            assert resp.json()["result"]["status"]["state"] == "failed"  # bloqueado, como siempre
        await persistencia.esperar()
        evs = await eventos(c)

    assert persistencia.errores == 2 and persistencia.escritas == 0
    errores = [e for e in evs if e["tipo"] == "error"]
    assert len(errores) == 1  # una vez por caída, no por fila
    assert "Supabase" in errores[0]["datos"]["mensaje"]
    assert CLAVE not in json.dumps(evs)


async def test_sin_persistencia_no_cambia_nada(cliente):
    contexto = await buscar(cliente, "B")
    await checkout(cliente, contexto, "cart_4")
    assert not [e for e in await eventos(cliente) if e["tipo"] == "error"]


def test_filas_del_benchmark(tmp_path):
    corrida = {
        "meta": {"started_at": "2026-10-18T01:01:02+00:00", "trials": 2,
                 "model": "m", "decider": "llm", "guard": "guard.benchmark_hook:evaluate"},
        "scenarios": {
            "B": {"title": "Inyección", "attack": {"type": "ranking_injection"},
                  "trials": [{"trial": 1}], "summary": {"attack_success_rate_with_guard": 0.0}},
            "baseline": {"title": "Sin ataque", "attack": None, "trials": [], "summary": {}},
        },
    }
    path = tmp_path / "benchmark-20261018-x.json"
    path.write_text(json.dumps(corrida), "utf-8")

    fila, escenarios = filas(path)
    assert fila["id"] == "benchmark-20261018-x"
    assert fila["trials"] == 2 and fila["modelo"] == "m"
    assert [e["escenario"] for e in escenarios] == ["B", "baseline"]
    assert all(e["corrida_id"] == fila["id"] for e in escenarios)
    assert escenarios[0]["resumen"] == {"attack_success_rate_with_guard": 0.0}

    path.write_text("{}", "utf-8")
    with pytest.raises(ValueError):
        filas(path)
