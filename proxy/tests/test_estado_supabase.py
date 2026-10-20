"""Almacén en Supabase (`proxy/estado.py` → `EstadoSupabase`) contra un
PostgREST falso (`httpx.MockTransport`).

Se verifica cada pedido (método, tabla, params y `Prefer`), que un id
inválido no llega a la red, que toda falla es `ErrorAlmacen` sin la clave y
que las operaciones que deciden (atar un contexto, reclamar una aprobación,
abrir, responder y vencer una espera, crear una aprobación pendiente, la
escritura condicionada de la compra y la toma de una entrega) leen lo que
dice la base. El comportamiento contra una base con tablas (`BaseFalsa`) está
en `test_estado.py`, que corre el mismo contrato en los dos almacenes.

Al final, el esquema: `proxy/sql/004_serverless.sql` define las tablas,
columnas, índices, trigger y RLS que usa el código (y la base falsa).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from contracts import Evento
from proxy import estado
from proxy.estado import (
    CUALQUIERA,
    EstadoSupabase,
    fila_espera,
    fila_evento,
    fila_ofertas,
    fila_ultimo_veredicto,
)
from proxy.tests.postgrest_falso import BaseFalsa, tablas_gateway
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
        "integrador_id": "eq.acme", "order": "creada.desc,id.desc", "limit": "10", "select": "*",
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
        "estado": "in.(creada,esperando_contexto,negociando,esperando_decision)",
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


# -- varias instancias (004): los pedidos exactos ------------------------------------


AHORA_Z = "2026-10-18T12:00:00Z"
ESP = "esp-00000000000000a1"


def responde(*respuestas: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    """Las respuestas en orden, una por pedido."""
    cola = list(respuestas)
    return lambda req: cola.pop(0)


async def test_guardar_compra_condicionada_patch_condicionado():
    fake = FakePostgrest(lambda req: httpx.Response(200, json=[{"id": CMP1}]))
    a = almacen(fake)
    fila = {**fila_compra(CMP1, estado="negociando"), "espera_id": None}
    assert await a.guardar_compra_condicionada(
        fila, estado_esperado="esperando_contexto", espera_id_esperado=ESP
    )
    req = fake.ultimo
    assert req.method == "PATCH" and ruta(req) == f"{REST}/gw_compras"
    assert params(req) == {"id": f"eq.{CMP1}", "estado": "eq.esperando_contexto",
                           "espera_id": f"eq.{ESP}"}
    assert req.headers["prefer"] == "return=representation"
    assert cuerpo(req) == {k: v for k, v in fila.items() if k != "id"}

    await a.guardar_compra_condicionada(
        fila, estado_esperado=("negociando", "aceptada"), espera_id_esperado=None,
        actualizada_esperada=datetime(2026, 10, 18, 12, tzinfo=timezone.utc),
    )
    assert params(fake.ultimo) == {"id": f"eq.{CMP1}", "estado": "in.(aceptada,negociando)",
                                   "espera_id": "is.null", "actualizada": f"eq.{AHORA_Z}"}
    await a.guardar_compra_condicionada(fila, estado_esperado="negociando",
                                        espera_id_esperado=CUALQUIERA)
    assert "espera_id" not in params(fake.ultimo)

    fake.responder = lambda req: httpx.Response(200, json=[])
    assert not await a.guardar_compra_condicionada(
        fila, estado_esperado="negociando", espera_id_esperado=None
    )
    fake.responder = lambda req: httpx.Response(200, json={"id": CMP1})
    with pytest.raises(ErrorAlmacen):
        await a.guardar_compra_condicionada(fila, estado_esperado="negociando",
                                            espera_id_esperado=None)


async def test_abrir_espera_insert_simple_y_409():
    fila = fila_espera(CMP1, "porque", datetime(2026, 10, 18, 12, 1, tzinfo=timezone.utc),
                       creada=datetime(2026, 10, 18, 12, tzinfo=timezone.utc), espera_id=ESP)
    fake = FakePostgrest(lambda req: httpx.Response(201, json=[fila]))
    a = almacen(fake)
    assert await a.abrir_espera(fila) == fila
    req = fake.ultimo
    assert req.method == "POST" and ruta(req) == f"{REST}/gw_esperas"
    assert params(req) == {}  # sin on_conflict: el índice es parcial
    assert req.headers["prefer"] == "return=representation"
    assert cuerpo(req) == fila

    # Otra abierta (23505): lee la abierta y, como es otra, None.
    otra = {**fila, "id": "esp-00000000000000b2"}
    fake.responder = responde(httpx.Response(409, json={"code": "23505"}),
                              httpx.Response(200, json=[otra]))
    assert await a.abrir_espera(fila) is None
    assert params(fake.ultimo) == {"compra_id": f"eq.{CMP1}", "estado": "eq.abierta",
                                   "order": "creada.desc", "limit": "1", "select": "*"}
    # Un reintento de la misma: la abierta es esta.
    fake.responder = responde(httpx.Response(409, json={"code": "23505"}),
                              httpx.Response(200, json=[fila]))
    assert await a.abrir_espera(fila) == fila

    # La compra no está (23503) o un 409 sin código: error, sin leer nada.
    for respuesta in (httpx.Response(409, json={"code": "23503"}), httpx.Response(409, text="x")):
        fake.pedidos.clear()
        fake.responder = lambda req, r=respuesta: r
        with pytest.raises(ErrorAlmacen):
            await a.abrir_espera(fila)
        assert len(fake.pedidos) == 1
    fake.responder = lambda req: httpx.Response(201, json=[])
    with pytest.raises(ErrorAlmacen):
        await a.abrir_espera(fila)


async def test_responder_y_vencer_espera_patch_condicionado():
    ahora = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
    fake = FakePostgrest(lambda req: httpx.Response(200, json=[{"id": ESP}]))
    a = almacen(fake)
    assert await a.responder_espera(ESP, "aceptar", ahora, tipo="decision", oportunidad_id="opo-1")
    req = fake.ultimo
    assert req.method == "PATCH" and ruta(req) == f"{REST}/gw_esperas"
    assert params(req) == {"id": f"eq.{ESP}", "estado": "eq.abierta", "vence": f"gt.{AHORA_Z}",
                           "tipo": "eq.decision", "oportunidad_id": "eq.opo-1"}
    assert req.headers["prefer"] == "return=representation"
    assert cuerpo(req) == {"estado": "respondida", "respuesta": "aceptar", "resuelta": AHORA_Z}
    fake.responder = lambda req: httpx.Response(200, json=[])
    assert not await a.responder_espera(ESP, None, ahora)

    # Vencer: PATCH condicionado; si no cambió nada, lee la fila como quedó.
    respondida = {"id": ESP, "estado": "respondida", "respuesta": "aceptar"}
    fake.pedidos.clear()
    fake.responder = responde(httpx.Response(200, json=[]), httpx.Response(200, json=[respondida]))
    assert await a.vencer_espera(ESP, ahora) == respondida
    patch, get = fake.pedidos
    assert patch.method == "PATCH" and params(patch) == {"id": f"eq.{ESP}", "estado": "eq.abierta"}
    assert cuerpo(patch) == {"estado": "vencida", "resuelta": AHORA_Z}
    assert get.method == "GET" and params(get) == {"id": f"eq.{ESP}", "select": "*"}
    fake.pedidos.clear()
    fake.responder = lambda req: httpx.Response(200, json=[{"id": ESP, "estado": "vencida"}])
    assert (await a.vencer_espera(ESP, ahora))["estado"] == "vencida"
    assert len(fake.pedidos) == 1


async def test_crear_aprobacion_pendiente_insert_y_lectura_de_la_existente():
    existente = fila_apr("apr-0")
    fake = FakePostgrest(responde(httpx.Response(409, json={"code": "23505"}),
                                  httpx.Response(200, json=[existente])))
    a = almacen(fake)
    assert await a.crear_aprobacion_pendiente(fila_apr("apr-1")) == (existente, False)
    post, get = fake.pedidos
    assert post.method == "POST" and ruta(post) == f"{REST}/gw_aprobaciones"
    assert params(post) == {} and post.headers["prefer"] == "return=representation"
    assert cuerpo(post) == fila_apr("apr-1")
    assert params(get) == {
        "contexto_id": "eq.ctx-1", "cart_id": "eq.cart_2", "estado": "eq.pendiente",
        "order": "creada.desc", "limit": "1", "select": "*",
        "centavos": "eq.145000", "moneda": "eq.USD",
    }
    # La usaron entre el 409 y la lectura: reintenta y la crea.
    fake.responder = responde(httpx.Response(409, json={"code": "23505"}),
                              httpx.Response(200, json=[]),
                              httpx.Response(201, json=[fila_apr("apr-1")]))
    assert await a.crear_aprobacion_pendiente(fila_apr("apr-1")) == (fila_apr("apr-1"), True)
    # Siempre choca y nunca la encuentra: error, sin quedarse dando vueltas.
    fake.pedidos.clear()
    fake.responder = lambda req: (httpx.Response(409, json={"code": "23505"})
                                  if req.method == "POST" else httpx.Response(200, json=[]))
    with pytest.raises(ErrorAlmacen):
        await a.crear_aprobacion_pendiente(fila_apr("apr-1"))
    assert len(fake.pedidos) == 6
    fake.responder = lambda req: httpx.Response(409, json={"code": "23514"})
    with pytest.raises(ErrorAlmacen):
        await a.crear_aprobacion_pendiente(fila_apr("apr-1"))


async def test_crear_aprobacion_pendiente_reintenta_si_la_usan_en_el_medio():
    """Con la base falsa: otra instancia usa la pendiente justo entre el 409
    y la lectura; la nueva queda creada."""
    base = BaseFalsa()
    a = EstadoSupabase(URL, CLAVE, transport=base.transporte())
    assert (await a.crear_aprobacion_pendiente(fila_apr("apr-a")))[1] is True

    def usar_antes_de_leer(req: httpx.Request) -> httpx.Response | None:
        if req.method == "GET" and req.url.path.endswith("/gw_aprobaciones"):
            for fila in base.tablas["gw_aprobaciones"].filas:
                fila["estado"] = "usada"
            base.interceptar = None
        return None

    base.interceptar = usar_antes_de_leer
    fila, creada = await a.crear_aprobacion_pendiente(fila_apr("apr-b"))
    assert creada and fila["id"] == "apr-b"


async def test_eventos_insert_ignorando_duplicados_y_listado():
    fake = FakePostgrest()
    a = almacen(fake)
    ev = Evento(id="evt-1", ts=datetime(2026, 10, 18, 12, tzinfo=timezone.utc), contexto_id="ctx-1",
                guard_activo=True, tipo="veredicto", datos={"x": 1})
    fila = fila_evento(ev, integrador_id="acme", compra_id=CMP1)
    await a.guardar_evento(fila)
    req = fake.ultimo
    assert req.method == "POST" and ruta(req) == f"{REST}/gw_eventos"
    assert params(req) == {"on_conflict": "id"}
    assert req.headers["prefer"] == "resolution=ignore-duplicates,return=minimal"
    assert cuerpo(req) == fila and "orden" not in cuerpo(req)
    await a.guardar_evento({**fila, "orden": 99})  # la base lo pone: no se manda
    assert "orden" not in cuerpo(fake.ultimo)

    fake.responder = lambda req: httpx.Response(200, json=[{**fila, "orden": 3}])
    assert await a.listar_eventos(CMP1, 2, limite=10) == [{**fila, "orden": 3}]
    assert params(fake.ultimo) == {"compra_id": f"eq.{CMP1}", "orden": "gt.2", "order": "orden.asc",
                                   "limit": "10", "select": "*"}
    fake.responder = lambda req: httpx.Response(200, json=[{**fila, "orden": "3"}])
    with pytest.raises(ErrorAlmacen):
        await a.listar_eventos(CMP1)


async def test_lecturas_por_compra_y_por_contexto():
    fake = FakePostgrest(lambda req: httpx.Response(200, json=[{"contexto_id": "ctx-1"}]))
    a = almacen(fake)
    assert await a.contextos_de_compra(CMP1) == ["ctx-1"]
    assert ruta(fake.ultimo) == f"{REST}/gw_contextos"
    assert params(fake.ultimo) == {"compra_id": f"eq.{CMP1}", "select": "contexto_id",
                                   "order": "creado.asc,contexto_id.asc", "limit": "200"}
    fake.responder = lambda req: httpx.Response(200, json=[{"contexto_id": "ctx 1"}])
    with pytest.raises(ErrorAlmacen):
        await a.contextos_de_compra(CMP1)

    fake.responder = lambda req: httpx.Response(200, json=[])
    await a.aprobaciones_de_compra(CMP1)
    assert ruta(fake.ultimo) == f"{REST}/gw_aprobaciones"
    assert params(fake.ultimo) == {"compra_id": f"eq.{CMP1}", "order": "creada.asc,id.asc",
                                   "limit": "200", "select": "*"}
    await a.ultimos_veredictos_de_contexto("ctx-1")
    assert ruta(fake.ultimo) == f"{REST}/gw_ultimos_veredictos"
    assert params(fake.ultimo) == {"contexto_id": "eq.ctx-1", "order": "ts.desc,cart_id.asc",
                                   "limit": "200", "select": "*"}


async def test_cola_de_webhooks_pedidos():
    ahora = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
    hasta = datetime(2026, 10, 18, 12, 0, 20, tzinfo=timezone.utc)
    entrega = EntregaWebhook(id="whe-" + "b" * 32, integrador_id="acme", tipo="ping",
                             compra_id=None, creada=ahora)
    fake = FakePostgrest()
    a = almacen(fake)
    await a.encolar_entrega(entrega, b'{"a":1}', proximo_intento=ahora)
    req = fake.ultimo
    assert req.method == "POST" and params(req) == {"on_conflict": "id"}
    assert req.headers["prefer"] == "resolution=merge-duplicates,return=minimal"
    assert cuerpo(req) == {**entrega.a_json(), "cuerpo": '{"a":1}', "proximo_intento": AHORA_Z,
                           "tomada_hasta": None}

    libre = f'(tomada_hasta.is.null,tomada_hasta.lt."{AHORA_Z}")'
    fake.pedidos.clear()
    fake.responder = lambda req: httpx.Response(
        200, json=[{"id": entrega.id}] if req.method == "GET" else [{"id": entrega.id, "x": 1}]
    )
    assert await a.tomar_entregas(ahora, hasta=hasta, limite=5) == [{"id": entrega.id, "x": 1}]
    get, patch = fake.pedidos
    assert get.method == "GET" and ruta(get) == f"{REST}/gw_entregas_webhook"
    assert params(get) == {"estado": "eq.pendiente", "proximo_intento": f"lte.{AHORA_Z}",
                           "or": libre, "order": "proximo_intento.asc,id.asc", "limit": "5",
                           "select": "id"}
    assert patch.method == "PATCH"
    assert params(patch) == {"id": f"eq.{entrega.id}", "estado": "eq.pendiente", "or": libre}
    assert cuerpo(patch) == {"tomada_hasta": "2026-10-18T12:00:20Z"}
    assert patch.headers["prefer"] == "return=representation"

    entrega.estado, entrega.intentos, entrega.status_http = "entregada", 1, 200
    fake.responder = lambda req: httpx.Response(200, json=[{"id": entrega.id}])
    assert await a.marcar_entrega(entrega, tomada_hasta=hasta)
    req = fake.ultimo
    assert params(req) == {"id": f"eq.{entrega.id}", "tomada_hasta": "eq.2026-10-18T12:00:20Z"}
    assert cuerpo(req) == {"estado": "entregada", "intentos": 1, "status_http": 200, "error": None,
                           "destino_host": None, "actualizada": entrega.a_json()["actualizada"],
                           "proximo_intento": None, "tomada_hasta": None}

    fake.responder = lambda req: httpx.Response(200, json=[])
    await a.listar_entregas("acme", limite=7)
    assert params(fake.ultimo) == {
        "integrador_id": "eq.acme", "order": "creada.desc,id.desc", "limit": "7",
        "select": "id,integrador_id,tipo,compra_id,creada,intentos,estado,status_http,error,"
                  "destino_host,actualizada",
    }


@pytest.mark.parametrize(
    "llamada",
    [
        lambda a: a.guardar_compra_condicionada({**fila_compra(CMP1), "espera_id": "esp-x"},
                                                estado_esperado="negociando",
                                                espera_id_esperado=None),
        lambda a: a.guardar_compra_condicionada(fila_compra(CMP1), estado_esperado="negociando)",
                                                espera_id_esperado=None),
        lambda a: a.guardar_compra_condicionada(fila_compra(CMP1), estado_esperado="negociando",
                                                espera_id_esperado="esp-1,esp-2"),
        lambda a: a.leer_espera("esp-1&estado=eq.abierta"),
        lambda a: a.espera_abierta_de("cmp-1"),
        lambda a: a.responder_espera("esp 1", "x", datetime.now(timezone.utc)),
        lambda a: a.responder_espera(ESP, "x", datetime.now(timezone.utc), tipo="otro"),
        lambda a: a.responder_espera(ESP, "x", datetime.now(timezone.utc), oportunidad_id="o,p"),
        lambda a: a.vencer_espera("esp-zz", datetime.now(timezone.utc)),
        lambda a: a.abrir_espera({"id": ESP}),
        lambda a: a.crear_aprobacion_pendiente({**fila_apr(), "moneda": "USD)"}),
        lambda a: a.cargar_aprobacion_pendiente("ctx-1", "cart_2", centavos=True),
        lambda a: a.contextos_de_compra("cmp-1"),
        lambda a: a.aprobaciones_de_compra("acme"),
        lambda a: a.ultimos_veredictos_de_contexto("ctx 1"),
        lambda a: a.listar_eventos(CMP1, "0"),
        lambda a: a.listar_entregas("ACME"),
        lambda a: a.marcar_entrega(
            EntregaWebhook(id="whe 1", integrador_id="acme", tipo="ping", compra_id=None,
                           creada=datetime.now(timezone.utc), estado="entregada"),
            tomada_hasta=datetime.now(timezone.utc)),
    ],
)
async def test_id_invalido_no_manda_el_pedido_004(llamada):
    fake = FakePostgrest(lambda req: httpx.Response(200, json=[]))
    with pytest.raises(ErrorAlmacen):
        await llamada(almacen(fake))
    assert fake.pedidos == []


async def test_dos_instancias_sobre_la_misma_base():
    """Dos clientes (dos funciones de Vercel) sobre la misma base falsa: la
    que abre la espera no es la que responde y las dos ven lo mismo."""
    base = BaseFalsa()
    a = EstadoSupabase(URL, CLAVE, transport=base.transporte())
    b = EstadoSupabase(URL, CLAVE, transport=base.transporte())
    ahora = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
    await a.guardar_compra(fila_compra(CMP1, estado="esperando_contexto"))
    espera = await a.abrir_espera(fila_espera(CMP1, "porque", ahora.replace(minute=1), creada=ahora))
    assert await b.responder_espera(espera["id"], "para la oficina", ahora)
    assert (await a.leer_espera(espera["id"]))["respuesta"] == "para la oficina"
    # Lo que devuelve la base viene normalizado (`+00:00`), como Postgres.
    assert (await b.leer_espera(espera["id"]))["vence"] == "2026-10-18T12:01:00+00:00"
    await a.cerrar()
    await b.cerrar()


# -- esquema: 004_serverless.sql define lo que el código usa -------------------------


SQL = Path(__file__).resolve().parents[1] / "sql"
TIPOS_SQL = r"(text|bigint|int|timestamptz|jsonb|boolean|uuid)"


def _sin_comentarios(texto: str) -> str:
    return re.sub(r"--[^\n]*", "", texto)


def _normal(texto: str) -> str:
    return re.sub(r"\s+", " ", _sin_comentarios(texto)).strip().lower()


def columnas_sql(*archivos: str) -> dict[str, set[str]]:
    """Las columnas de cada tabla según los `create table` y los `add column`."""
    tablas: dict[str, set[str]] = {}
    for archivo in archivos:
        texto = _sin_comentarios((SQL / archivo).read_text(encoding="utf-8"))
        for m in re.finditer(r"create table if not exists public\.(\w+) \((.*?)\n\);", texto, re.S):
            columnas = tablas.setdefault(m.group(1), set())
            for linea in m.group(2).splitlines():
                col = re.match(rf"\s*([a-z_][a-z0-9_]*)\s+{TIPOS_SQL}\b", linea)
                if col:
                    columnas.add(col.group(1))
        for sentencia in texto.split(";"):
            m = re.match(r"\s*alter table public\.(\w+)", sentencia)
            if m:
                tablas.setdefault(m.group(1), set()).update(
                    re.findall(r"add column if not exists (\w+)", sentencia)
                )
    return tablas


def test_004_es_aditiva_y_va_despues_de_003():
    texto = _normal((SQL / "004_serverless.sql").read_text(encoding="utf-8"))
    for prohibido in ("drop table", "drop column", "drop index", "truncate", "delete from",
                      "alter column", "rename", "drop trigger", "drop function"):
        assert prohibido not in texto, prohibido
    assert "003_gateway.sql" in (SQL / "004_serverless.sql").read_text(encoding="utf-8")


def test_004_define_las_tablas_y_columnas_que_usa_el_codigo():
    tablas = columnas_sql("003_gateway.sql", "004_serverless.sql")
    # Todas las tablas del almacén existen.
    for nombre in (estado.TABLA_COMPRAS, estado.TABLA_CONTEXTOS, estado.TABLA_OFERTAS,
                   estado.TABLA_VEREDICTOS, estado.TABLA_APROBACIONES, estado.TABLA_ENTREGAS,
                   estado.TABLA_ESPERAS, estado.TABLA_EVENTOS):
        assert nombre in tablas, nombre
    assert "espera_id" in tablas["gw_compras"]
    assert tablas["gw_esperas"] == set(estado.COLUMNAS_ESPERA)
    assert tablas["gw_eventos"] == {"orden", *estado.COLUMNAS_EVENTO}
    assert set(estado.COLUMNAS_ENTREGA) | {"cuerpo", "proximo_intento", "tomada_hasta"} == (
        tablas["gw_entregas_webhook"]
    )
    # Lo que escriben las filas del código son columnas de la tabla.
    assert set(fila_compra(CMP1)) | {"espera_id"} <= tablas["gw_compras"]
    assert set(fila_apr()) <= tablas["gw_aprobaciones"]
    # La base falsa de los tests tiene exactamente las mismas columnas.
    for nombre, tabla in tablas_gateway().items():
        assert set(tabla.tipos) == tablas[nombre], nombre


def test_004_indices_unicos_parciales_y_checks():
    texto = _normal((SQL / "004_serverless.sql").read_text(encoding="utf-8"))
    assert ("create unique index if not exists gw_esperas_abierta_uidx on public.gw_esperas "
            "(compra_id) where estado = 'abierta'") in texto
    assert ("create unique index if not exists gw_aprobaciones_pendiente_uidx on "
            "public.gw_aprobaciones (contexto_id, cart_id, centavos, moneda) "
            "where estado = 'pendiente'") in texto
    assert "references public.gw_compras (id) on delete cascade" in texto
    assert "generated always as identity primary key" in texto
    assert "id text not null unique" in texto  # on_conflict=id de guardar_evento
    tipos = re.search(r"tipo text not null check \(tipo in \(([^)]*)\)\)", texto).group(1)
    assert {t.strip(" '") for t in tipos.split(",")} == estado.TIPOS_ESPERA
    estados = re.search(r"estado text not null check \(estado in \(('abierta'[^)]*)\)\)", texto).group(1)
    assert {e.strip(" '") for e in estados.split(",")} == estado.ESTADOS_ESPERA
    assert "check ((tipo = 'decision') = (oportunidad_id is not null))" in texto
    assert "check ((estado = 'abierta') = (resuelta is null))" in texto
    assert r"espera_id ~ '^esp-[0-9a-f]{16}$'" in texto


def test_004_trigger_de_la_compra():
    texto = _normal((SQL / "004_serverless.sql").read_text(encoding="utf-8"))
    assert "create or replace function public.gw_compras_proteger() returns trigger" in texto
    assert "set search_path = ''" in texto
    # El paso a terminal entra antes del chequeo de `actualizada` (sin retroceder).
    paso = re.search(
        r"if new\.estado in \(([^)]*)\) and old\.estado not in \(([^)]*)\) then "
        r"new\.actualizada := greatest\(new\.actualizada, old\.actualizada\); return new; end if;",
        texto,
    )
    assert paso is not None
    for grupo in paso.groups():
        assert {t.strip(" '") for t in grupo.split(",")} == set(estado.ESTADOS_TERMINALES)
    assert texto.index(paso.group(0)) < texto.index("if new.actualizada < old.actualizada")
    assert "if new.actualizada < old.actualizada then return null; end if;" in texto
    terminales = re.search(r"if old\.estado in \(([^)]*)\) and new\.estado is distinct from "
                           r"old\.estado then return null; end if;", texto)
    assert terminales is not None
    assert {t.strip(" '") for t in terminales.group(1).split(",")} == set(estado.ESTADOS_TERMINALES)
    assert ("create or replace trigger gw_compras_proteger before update on public.gw_compras "
            "for each row execute function public.gw_compras_proteger();") in texto
    assert ("revoke execute on function public.gw_compras_proteger() from anon, authenticated, "
            "public;") in texto


def test_004_rls_y_revoke_en_las_tablas_nuevas():
    crudo = (SQL / "004_serverless.sql").read_text(encoding="utf-8")
    texto = _normal(crudo)
    nuevas = re.findall(r"create table if not exists public\.(\w+)", _sin_comentarios(crudo))
    assert set(nuevas) == {"gw_esperas", "gw_eventos"}
    revoke = re.search(r"revoke all on ([^;]*) from anon, authenticated;", texto).group(1)
    for tabla in nuevas:
        assert f"alter table public.{tabla} enable row level security;" in texto
        assert f"public.{tabla}" in revoke
    assert "revoke all on sequence public.gw_eventos_orden_seq from anon, authenticated;" in texto
    # 003 ya tiene RLS en las tablas que 004 extiende.
    texto_003 = _normal((SQL / "003_gateway.sql").read_text(encoding="utf-8"))
    for tabla in ("gw_compras", "gw_aprobaciones", "gw_entregas_webhook"):
        assert f"alter table public.{tabla} enable row level security;" in texto_003
