"""Almacén en Supabase (`proxy/estado.py` → `EstadoSupabase`) contra un
PostgREST falso (`httpx.MockTransport`).

Se verifica cada pedido (método, tabla, params y `Prefer`), que un id
inválido no llega a la red, que toda falla es `ErrorAlmacen` sin la clave y
que las dos operaciones que deciden (atar un contexto y reclamar una
aprobación) leen lo que dice la base.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from proxy.estado import EstadoSupabase, fila_ofertas, fila_ultimo_veredicto
from proxy.tests.test_estado import CMP1, fila_apr, fila_compra
from proxy.tipos_gateway import EntregaWebhook, ErrorAlmacen

pytestmark = pytest.mark.anyio

URL = "https://proyecto.supabase.co"
CLAVE = "clave-de-servicio-de-test"
REST = f"{URL}/rest/v1"


class FakePostgrest:
    """Registra cada pedido y responde lo que diga `responder`."""

    def __init__(self, responder: Callable[[httpx.Request], httpx.Response] | None = None):
        self.pedidos: list[httpx.Request] = []
        self.responder = responder or (lambda req: httpx.Response(201))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.pedidos.append(request)
        return self.responder(request)

    @property
    def ultimo(self) -> httpx.Request:
        return self.pedidos[-1]


def almacen(fake: FakePostgrest) -> EstadoSupabase:
    return EstadoSupabase(URL + "/", CLAVE, transport=httpx.MockTransport(fake))


def params(request: httpx.Request) -> dict[str, str]:
    return dict(request.url.params)


def cuerpo(request: httpx.Request) -> Any:
    return json.loads(request.content)


def ruta(request: httpx.Request) -> str:
    return f"{request.url.scheme}://{request.url.host}{request.url.path}"


def assert_auth(request: httpx.Request) -> None:
    assert request.headers["apikey"] == CLAVE
    assert request.headers["authorization"] == f"Bearer {CLAVE}"


# -- compras ---------------------------------------------------------------------------


async def test_guardar_compra_upsert():
    fake = FakePostgrest()
    a = almacen(fake)
    fila = fila_compra(CMP1)
    await a.guardar_compra(fila)
    req = fake.ultimo
    assert req.method == "POST" and ruta(req) == f"{REST}/gw_compras"
    assert params(req) == {"on_conflict": "id"}
    assert req.headers["prefer"] == "resolution=merge-duplicates,return=minimal"
    assert cuerpo(req) == fila
    assert_auth(req)
    await a.cerrar()


async def test_cargar_compra():
    fila = fila_compra(CMP1)
    fake = FakePostgrest(lambda req: httpx.Response(200, json=[fila]))
    a = almacen(fake)
    assert await a.cargar_compra(CMP1) == fila
    req = fake.ultimo
    assert req.method == "GET" and ruta(req) == f"{REST}/gw_compras"
    assert params(req) == {"id": f"eq.{CMP1}", "select": "*"}
    assert "prefer" not in req.headers
    fake.responder = lambda req: httpx.Response(200, json=[])
    assert await a.cargar_compra(CMP1) is None


async def test_listar_compras():
    fake = FakePostgrest(lambda req: httpx.Response(200, json=[]))
    a = almacen(fake)
    assert await a.listar_compras("acme", limite=10) == []
    assert params(fake.ultimo) == {
        "integrador_id": "eq.acme", "order": "creada.desc", "limit": "10", "select": "*",
    }
    await a.listar_compras("acme", estado="negociando")
    assert params(fake.ultimo)["estado"] == "eq.negociando"
    assert params(fake.ultimo)["limit"] == "50"
    await a.listar_compras("acme", limite=10_000)
    assert params(fake.ultimo)["limit"] == "200"


async def test_contar_activas_lee_content_range():
    fake = FakePostgrest(
        lambda req: httpx.Response(206, json=[{"id": CMP1}], headers={"Content-Range": "0-0/57"})
    )
    a = almacen(fake)
    desde = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
    # 206 es la respuesta parcial de PostgREST con limit: no es un error.
    assert await a.contar_activas("acme", desde) == 57
    req = fake.ultimo
    assert req.method == "GET" and ruta(req) == f"{REST}/gw_compras"
    assert params(req) == {
        "integrador_id": "eq.acme",
        "creada": "gte.2026-10-17T12:00:00Z",
        "estado": "not.in.(bloqueada,vencida)",
        "select": "id",
        "limit": "1",
    }
    assert req.headers["prefer"] == "count=exact"

    fake.responder = lambda req: httpx.Response(200, json=[], headers={"Content-Range": "*/0"})
    assert await a.contar_activas("acme", desde) == 0

    fake.responder = lambda req: httpx.Response(200, json=[])
    with pytest.raises(ErrorAlmacen):
        await a.contar_activas("acme", desde)
    fake.responder = lambda req: httpx.Response(200, json=[], headers={"Content-Range": "0-0/*"})
    with pytest.raises(ErrorAlmacen):
        await a.contar_activas("acme", desde)


# -- contextos ------------------------------------------------------------------------------


async def test_atar_contexto_inserta_ignorando_duplicados_y_lee_el_dueno():
    otra = "cmp-00000000000000ff"

    def responder(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(201)
        return httpx.Response(200, json=[{"compra_id": otra}])

    fake = FakePostgrest(responder)
    a = almacen(fake)
    assert await a.atar_contexto("ctx-1", CMP1, "merchant_1") == otra
    post, get = fake.pedidos
    assert post.method == "POST" and ruta(post) == f"{REST}/gw_contextos"
    assert params(post) == {"on_conflict": "contexto_id"}
    assert post.headers["prefer"] == "resolution=ignore-duplicates,return=minimal"
    assert cuerpo(post) == {"contexto_id": "ctx-1", "compra_id": CMP1, "comercio_id": "merchant_1"}
    assert get.method == "GET" and ruta(get) == f"{REST}/gw_contextos"
    assert params(get) == {"contexto_id": "eq.ctx-1", "select": "compra_id"}


async def test_atar_contexto_sin_fila_despues_del_insert_falla():
    fake = FakePostgrest(lambda req: httpx.Response(201 if req.method == "POST" else 200, json=[]))
    with pytest.raises(ErrorAlmacen):
        await almacen(fake).atar_contexto("ctx-1", CMP1, "merchant_1")


async def test_compra_de_contexto():
    fake = FakePostgrest(lambda req: httpx.Response(200, json=[{"compra_id": CMP1}]))
    a = almacen(fake)
    assert await a.compra_de_contexto("ctx-1") == CMP1
    fake.responder = lambda req: httpx.Response(200, json=[])
    assert await a.compra_de_contexto("ctx-1") is None
    fake.responder = lambda req: httpx.Response(200, json=[{"compra_id": "no-es-una-compra"}])
    with pytest.raises(ErrorAlmacen):
        await a.compra_de_contexto("ctx-1")


# -- ofertas, veredicto, aprobaciones y entregas ------------------------------------------------


async def test_upserts_por_clave_primaria():
    fake = FakePostgrest()
    a = almacen(fake)
    ahora = datetime(2026, 10, 18, tzinfo=timezone.utc)
    ofertas = fila_ofertas("ctx-1", "merchant_1", [], [], None, ahora)
    veredicto = fila_ultimo_veredicto("ctx-1", "cart_2", "escalar", 145000, "USD", ahora)
    entrega = EntregaWebhook(id="whe-" + "b" * 32, integrador_id="acme", tipo="ping",
                             compra_id=None, creada=ahora)
    await a.guardar_ofertas(ofertas)
    await a.guardar_ultimo_veredicto(veredicto)
    await a.guardar_aprobacion(fila_apr())
    await a.guardar_entrega(entrega)
    esperado = [
        ("gw_ofertas", "contexto_id", ofertas),
        ("gw_ultimos_veredictos", "contexto_id,cart_id", veredicto),
        ("gw_aprobaciones", "id", fila_apr()),
        ("gw_entregas_webhook", "id", entrega.a_json()),
    ]
    for req, (tabla, conflicto, fila) in zip(fake.pedidos, esperado, strict=True):
        assert req.method == "POST" and ruta(req) == f"{REST}/{tabla}"
        assert params(req) == {"on_conflict": conflicto}
        assert req.headers["prefer"] == "resolution=merge-duplicates,return=minimal"
        assert cuerpo(req) == fila


async def test_cargas_por_clave_primaria():
    fake = FakePostgrest(lambda req: httpx.Response(200, json=[{"x": 1}]))
    a = almacen(fake)
    assert await a.cargar_ofertas("ctx-1") == {"x": 1}
    assert ruta(fake.ultimo) == f"{REST}/gw_ofertas"
    assert params(fake.ultimo) == {"contexto_id": "eq.ctx-1", "select": "*"}
    assert await a.cargar_ultimo_veredicto("ctx-1", "cart_2") == {"x": 1}
    assert ruta(fake.ultimo) == f"{REST}/gw_ultimos_veredictos"
    assert params(fake.ultimo) == {"contexto_id": "eq.ctx-1", "cart_id": "eq.cart_2", "select": "*"}


async def test_cargar_aprobacion_pendiente():
    fake = FakePostgrest(lambda req: httpx.Response(200, json=[fila_apr()]))
    a = almacen(fake)
    assert await a.cargar_aprobacion_pendiente("ctx-1", "cart_2") == fila_apr()
    req = fake.ultimo
    assert req.method == "GET" and ruta(req) == f"{REST}/gw_aprobaciones"
    assert params(req) == {
        "contexto_id": "eq.ctx-1", "cart_id": "eq.cart_2", "estado": "eq.pendiente",
        "order": "creada.desc", "limit": "1", "select": "*",
    }


async def test_reclamar_aprobacion():
    fake = FakePostgrest(lambda req: httpx.Response(200, json=[{"id": "apr-1"}]))
    a = almacen(fake)
    assert await a.reclamar_aprobacion("apr-1", usada="2026-10-18T12:00:00Z", task_id="t-1")
    req = fake.ultimo
    assert req.method == "PATCH" and ruta(req) == f"{REST}/gw_aprobaciones"
    assert params(req) == {"id": "eq.apr-1", "estado": "eq.pendiente"}
    assert req.headers["prefer"] == "return=representation"
    assert cuerpo(req) == {"estado": "usada", "usada": "2026-10-18T12:00:00Z", "task_id": "t-1"}

    fake.responder = lambda req: httpx.Response(200, json=[])
    assert await a.reclamar_aprobacion("apr-1", usada="x", task_id=None) is False
    # Dos filas con el mismo id no deberían existir: no cuenta como reclamo.
    fake.responder = lambda req: httpx.Response(200, json=[{"id": "apr-1"}, {"id": "apr-1"}])
    assert await a.reclamar_aprobacion("apr-1", usada="x", task_id=None) is False
    fake.responder = lambda req: httpx.Response(200, json={"id": "apr-1"})
    with pytest.raises(ErrorAlmacen):
        await a.reclamar_aprobacion("apr-1", usada="x", task_id=None)


# -- fallas (fail-closed) ------------------------------------------------------------------------


async def test_500_da_error_almacen_sin_la_clave():
    fake = FakePostgrest(lambda req: httpx.Response(500, text=f"boom {CLAVE}"))
    with pytest.raises(ErrorAlmacen) as e:
        await almacen(fake).cargar_compra(CMP1)
    assert CLAVE not in str(e.value) and "500" in str(e.value)


async def test_404_de_tabla_inexistente_da_error_almacen():
    """Sin la migración 003 aplicada: falla cerrado y visible."""
    fake = FakePostgrest(lambda req: httpx.Response(404, json={"code": "42P01"}))
    with pytest.raises(ErrorAlmacen):
        await almacen(fake).guardar_compra(fila_compra(CMP1))


async def test_error_de_red_da_error_almacen():
    def caido(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"no conecta {CLAVE}", request=req)

    with pytest.raises(ErrorAlmacen) as e:
        await almacen(FakePostgrest(caido)).reclamar_aprobacion("apr-1", usada="x", task_id=None)
    assert CLAVE not in str(e.value)


async def test_timeout_da_error_almacen():
    def lento(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lento", request=req)

    with pytest.raises(ErrorAlmacen):
        await almacen(FakePostgrest(lento)).compra_de_contexto("ctx-1")


async def test_respuesta_que_no_es_json_o_lista():
    fake = FakePostgrest(lambda req: httpx.Response(200, text="<html>"))
    a = almacen(fake)
    with pytest.raises(ErrorAlmacen):
        await a.cargar_compra(CMP1)
    fake.responder = lambda req: httpx.Response(200, json={"id": CMP1})
    with pytest.raises(ErrorAlmacen):
        await a.cargar_compra(CMP1)


async def test_redireccion_no_se_sigue():
    fake = FakePostgrest(lambda req: httpx.Response(302, headers={"Location": "https://otro"}))
    with pytest.raises(ErrorAlmacen):
        await almacen(fake).cargar_compra(CMP1)
    assert len(fake.pedidos) == 1


@pytest.mark.parametrize(
    "llamada",
    [
        lambda a: a.cargar_compra("cmp-1&id=neq.x"),
        lambda a: a.listar_compras("acme,otro"),
        lambda a: a.listar_compras("acme", estado="negociando)"),
        lambda a: a.listar_compras("acme", limite=0),
        lambda a: a.contar_activas("ACME", datetime.now(timezone.utc)),
        lambda a: a.atar_contexto("ctx 1", CMP1, "merchant_1"),
        lambda a: a.atar_contexto("ctx-1", "cmp-zz", "merchant_1"),
        lambda a: a.atar_contexto("ctx-1", CMP1, "merchant_1/../x"),
        lambda a: a.compra_de_contexto("ctx-1)"),
        lambda a: a.cargar_ofertas(""),
        lambda a: a.cargar_ultimo_veredicto("ctx-1", "cart 2"),
        lambda a: a.cargar_aprobacion_pendiente("ctx-1", None),
        lambda a: a.reclamar_aprobacion("apr-1,apr-2", usada="x", task_id=None),
        lambda a: a.reclamar_aprobacion("apr-1", usada="x", task_id="t 1"),
        lambda a: a.guardar_compra({**fila_compra(CMP1), "id": "cmp-x"}),
        lambda a: a.guardar_ofertas({"contexto_id": "ctx&x"}),
        lambda a: a.guardar_ultimo_veredicto({"contexto_id": "ctx-1", "cart_id": "c",
                                              "status": "quizas"}),
        lambda a: a.guardar_aprobacion({**fila_apr(), "id": "apr 1"}),
    ],
)
async def test_id_invalido_no_manda_el_pedido(llamada):
    fake = FakePostgrest(lambda req: httpx.Response(200, json=[]))
    with pytest.raises(ErrorAlmacen):
        await llamada(almacen(fake))
    assert fake.pedidos == []
