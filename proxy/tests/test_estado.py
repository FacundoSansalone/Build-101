"""Almacén de estado en memoria (`proxy/estado.py`), tareas en segundo plano y
la elección del almacén desde el entorno.

`EstadoMemoria` tiene que comportarse como la base: copias (nadie muta por
referencia), atadura de contexto donde gana el primero, reclamo de una
aprobación una sola vez y ids validados antes de tocar nada.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from proxy.aprobaciones import Aprobacion
from proxy.compras import RegistroCompras
from proxy.estado import (
    EstadoMemoria,
    EstadoSupabase,
    TareasEnFondo,
    estado_desde_entorno,
    fila_aprobacion_gw,
    fila_ofertas,
    fila_ultimo_veredicto,
)
from proxy.tests.test_compras_api import crear
from proxy.tipos_gateway import EntregaWebhook, ErrorAlmacen

pytestmark = pytest.mark.anyio

AHORA = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


def fila_compra(compra_id: str, *, integrador: str = "acme", estado: str = "negociando",
                creada: datetime = AHORA) -> dict:
    return {
        "id": compra_id, "integrador_id": integrador, "comercio_id": "merchant_1",
        "referencia": None, "estado": estado, "creada": creada.isoformat(),
        "actualizada": creada.isoformat(), "datos": {"pedido": "x", "veredictos": []},
    }


def fila_apr(aprobacion_id: str = "apr-1", *, creada: str = "2026-10-18T12:00:00+00:00",
             estado: str = "pendiente") -> dict:
    return fila_aprobacion_gw(
        Aprobacion(id=aprobacion_id, contexto_id="ctx-1", cart_id="cart_2", centavos=145000,
                   moneda="USD", creada=creada, estado=estado),
        integrador_id="acme", compra_id="cmp-0000000000000001",
    )


CMP1 = "cmp-0000000000000001"
CMP2 = "cmp-0000000000000002"


# -- compras -------------------------------------------------------------------------


async def test_guardar_y_cargar_compra_con_copias():
    almacen = EstadoMemoria()
    assert almacen.nombre == "memoria" and almacen.durable is False
    fila = fila_compra(CMP1)
    await almacen.guardar_compra(fila)
    fila["datos"]["pedido"] = "mutada después de guardar"
    cargada = await almacen.cargar_compra(CMP1)
    assert cargada["datos"]["pedido"] == "x"
    cargada["estado"] = "vencida"
    assert (await almacen.cargar_compra(CMP1))["estado"] == "negociando"
    assert await almacen.cargar_compra("cmp-ffffffffffffffff") is None


async def test_guardar_compra_real_y_volver():
    almacen = EstadoMemoria(durable=True)
    compra = crear(RegistroCompras())
    await almacen.guardar_compra(compra.a_fila())
    assert await almacen.cargar_compra(compra.id) == compra.a_fila()


async def test_guardar_compra_es_upsert():
    almacen = EstadoMemoria()
    await almacen.guardar_compra(fila_compra(CMP1))
    await almacen.guardar_compra(fila_compra(CMP1, estado="bloqueada"))
    assert (await almacen.cargar_compra(CMP1))["estado"] == "bloqueada"
    assert len(await almacen.listar_compras("acme")) == 1


async def test_listar_compras_filtra_ordena_y_limita():
    almacen = EstadoMemoria()
    await almacen.guardar_compra(fila_compra(CMP1, creada=AHORA))
    await almacen.guardar_compra(fila_compra(CMP2, creada=AHORA + timedelta(minutes=1),
                                             estado="bloqueada"))
    await almacen.guardar_compra(fila_compra("cmp-0000000000000003", integrador="otro"))
    assert [f["id"] for f in await almacen.listar_compras("acme")] == [CMP2, CMP1]
    assert [f["id"] for f in await almacen.listar_compras("acme", limite=1)] == [CMP2]
    assert [f["id"] for f in await almacen.listar_compras("acme", estado="negociando")] == [CMP1]
    assert await almacen.listar_compras("nadie") == []


async def test_contar_activas():
    almacen = EstadoMemoria()
    desde = AHORA - timedelta(hours=24)
    await almacen.guardar_compra(fila_compra(CMP1))                                  # cuenta
    await almacen.guardar_compra(fila_compra(CMP2, estado="esperando_decision"))     # cuenta
    await almacen.guardar_compra(fila_compra("cmp-0000000000000003", estado="bloqueada"))
    await almacen.guardar_compra(fila_compra("cmp-0000000000000004", estado="vencida"))
    await almacen.guardar_compra(
        fila_compra("cmp-0000000000000005", creada=desde - timedelta(seconds=1))
    )                                                                                # fuera del TTL
    await almacen.guardar_compra(fila_compra("cmp-0000000000000006", integrador="otro"))
    assert await almacen.contar_activas("acme", desde) == 2
    assert await almacen.contar_activas("otro", desde) == 1


@pytest.mark.parametrize(
    "cambio",
    [{"id": "cmp-1"}, {"integrador_id": "ACME!"}, {"estado": "inventado"},
     {"creada": "ayer"}, {"datos": None}],
)
async def test_guardar_compra_invalida_da_error(cambio):
    almacen = EstadoMemoria()
    fila = fila_compra(CMP1)
    fila.update(cambio)
    with pytest.raises(ErrorAlmacen):
        await almacen.guardar_compra(fila)


async def test_max_compras_borra_la_mas_vieja_y_sus_contextos():
    almacen = EstadoMemoria(max_compras=1)
    await almacen.guardar_compra(fila_compra(CMP1))
    await almacen.atar_contexto("ctx-1", CMP1, "merchant_1")
    await almacen.guardar_compra(fila_compra(CMP2))
    assert await almacen.cargar_compra(CMP1) is None
    assert await almacen.compra_de_contexto("ctx-1") is None


# -- contextos -------------------------------------------------------------------------


async def test_atar_contexto_gana_el_primero():
    almacen = EstadoMemoria()
    await almacen.guardar_compra(fila_compra(CMP1))
    await almacen.guardar_compra(fila_compra(CMP2))
    assert await almacen.compra_de_contexto("ctx-1") is None
    assert await almacen.atar_contexto("ctx-1", CMP1, "merchant_1") == CMP1
    assert await almacen.atar_contexto("ctx-1", CMP2, "merchant_1") == CMP1
    assert await almacen.atar_contexto("ctx-1", CMP1, "merchant_1") == CMP1
    assert await almacen.compra_de_contexto("ctx-1") == CMP1


async def test_atar_contexto_de_compra_no_guardada_falla():
    """Como la clave foránea de `gw_contextos`."""
    with pytest.raises(ErrorAlmacen):
        await EstadoMemoria().atar_contexto("ctx-1", CMP1, "merchant_1")


@pytest.mark.parametrize(
    "args",
    [("ctx 1", CMP1, "merchant_1"), ("ctx-1", "cmp-x", "merchant_1"),
     ("ctx-1", CMP1, "Merchant 1"), ("ctx&estado=eq.x", CMP1, "merchant_1")],
)
async def test_atar_contexto_con_id_invalido(args):
    almacen = EstadoMemoria()
    await almacen.guardar_compra(fila_compra(CMP1))
    with pytest.raises(ErrorAlmacen):
        await almacen.atar_contexto(*args)


# -- ofertas y último veredicto ----------------------------------------------------------


async def test_ofertas_y_ultimo_veredicto():
    almacen = EstadoMemoria()
    carts = [{"cart_id": "cart_1", "item_label": "x", "amount": 10.0, "currency": "USD"}]
    fila = fila_ofertas("ctx-1", "merchant_1", carts, [{"cart_id": "cart_1"}], "computadoras", AHORA)
    assert fila == {
        "contexto_id": "ctx-1", "comercio_id": "merchant_1", "carts": carts,
        "offer_details": [{"cart_id": "cart_1"}], "escenario": "computadoras",
        "registradas": AHORA.isoformat(),
    }
    carts[0]["amount"] = 1.0  # la fila es una copia
    assert fila["carts"][0]["amount"] == 10.0
    await almacen.guardar_ofertas(fila)
    assert await almacen.cargar_ofertas("ctx-1") == fila
    assert await almacen.cargar_ofertas("ctx-2") is None

    veredicto = fila_ultimo_veredicto("ctx-1", "cart_2", "escalar", 145000, "USD", AHORA)
    await almacen.guardar_ultimo_veredicto(veredicto)
    await almacen.guardar_ultimo_veredicto({**veredicto, "status": "bloqueado"})
    assert (await almacen.cargar_ultimo_veredicto("ctx-1", "cart_2"))["status"] == "bloqueado"
    assert await almacen.cargar_ultimo_veredicto("ctx-1", "cart_3") is None
    with pytest.raises(ErrorAlmacen):
        await almacen.guardar_ultimo_veredicto({**veredicto, "status": "quizas"})


# -- aprobaciones ---------------------------------------------------------------------------


async def test_cargar_aprobacion_pendiente_la_mas_nueva():
    almacen = EstadoMemoria(durable=True)
    assert await almacen.cargar_aprobacion_pendiente("ctx-1", "cart_2") is None
    await almacen.guardar_aprobacion(fila_apr("apr-1", creada="2026-10-18T12:00:00+00:00"))
    await almacen.guardar_aprobacion(fila_apr("apr-2", creada="2026-10-18T12:05:00+00:00"))
    await almacen.guardar_aprobacion(
        fila_apr("apr-3", creada="2026-10-18T12:10:00+00:00", estado="usada")
    )
    assert (await almacen.cargar_aprobacion_pendiente("ctx-1", "cart_2"))["id"] == "apr-2"


async def test_reclamar_aprobacion_una_sola_vez():
    almacen = EstadoMemoria(durable=True)
    await almacen.guardar_aprobacion(fila_apr("apr-1"))
    assert await almacen.reclamar_aprobacion("apr-1", usada="2026-10-18T12:01:00Z", task_id=None)
    assert not await almacen.reclamar_aprobacion("apr-1", usada="otra", task_id=None)
    assert not await almacen.reclamar_aprobacion("apr-inexistente", usada="x", task_id=None)
    assert await almacen.cargar_aprobacion_pendiente("ctx-1", "cart_2") is None


async def test_reclamar_aprobacion_concurrente():
    """Diez reintentos a la vez: uno solo la gasta."""
    almacen = EstadoMemoria(durable=True)
    await almacen.guardar_aprobacion(fila_apr("apr-1"))
    resultados = await asyncio.gather(
        *(almacen.reclamar_aprobacion("apr-1", usada="x", task_id=f"t-{i}") for i in range(10))
    )
    assert resultados.count(True) == 1


async def test_reclamar_con_id_invalido():
    with pytest.raises(ErrorAlmacen):
        await EstadoMemoria().reclamar_aprobacion("apr 1", usada="x", task_id=None)


# -- entregas ------------------------------------------------------------------------------


async def test_guardar_entrega():
    almacen = EstadoMemoria()
    entrega = EntregaWebhook(id="whe-" + "a" * 32, integrador_id="acme", tipo="ping",
                             compra_id=None, creada=AHORA)
    await almacen.guardar_entrega(entrega)
    entrega.estado = "entregada"
    await almacen.guardar_entrega(entrega)
    assert almacen._entregas["whe-" + "a" * 32]["estado"] == "entregada"
    await almacen.cerrar()


# -- tareas en segundo plano -------------------------------------------------------------------


async def test_tareas_en_fondo_corren_y_avisan_fallas():
    avisos: list[str] = []
    fondo = TareasEnFondo(al_fallar=avisos.append)
    hechas: list[str] = []

    async def bien() -> None:
        hechas.append("ok")

    async def falla() -> None:
        raise ErrorAlmacen("HTTP 500")

    async def explota() -> None:
        raise RuntimeError("secreto que no tiene que salir")

    fondo.programar(bien(), "compra")
    fondo.programar(falla(), "ofertas")
    fondo.programar(explota(), "veredicto")
    await fondo.esperar()
    assert hechas == ["ok"]
    assert fondo.errores == 2
    assert any("ofertas" in a and "HTTP 500" in a for a in avisos)
    assert any("veredicto" in a and "RuntimeError" in a for a in avisos)
    assert not any("secreto" in a for a in avisos)


async def test_tareas_en_fondo_tope():
    avisos: list[str] = []
    fondo = TareasEnFondo(max_pendientes=1, al_fallar=avisos.append)
    liberar = asyncio.Event()

    async def lenta() -> None:
        await liberar.wait()

    fondo.programar(lenta(), "una")
    fondo.programar(lenta(), "dos")  # no entra: se descarta sin warning de corrutina
    assert fondo.pendientes == 1 and fondo.descartadas == 1
    assert avisos and "dos" in avisos[0]
    liberar.set()
    await fondo.esperar()
    assert fondo.pendientes == 0


async def test_tareas_en_fondo_al_fallar_que_lanza_no_rompe():
    def mal(_: str) -> None:
        raise RuntimeError("x")

    fondo = TareasEnFondo(al_fallar=mal)

    async def falla() -> None:
        raise ErrorAlmacen("x")

    fondo.programar(falla(), "compra")
    await fondo.esperar()
    assert fondo.errores == 1


async def test_tareas_en_fondo_cerrar_cancela():
    avisos: list[str] = []
    fondo = TareasEnFondo(al_fallar=avisos.append)

    async def colgada() -> None:
        await asyncio.sleep(3600)

    fondo.programar(colgada(), "compra")
    await fondo.cerrar(timeout_s=0.01)
    assert fondo.pendientes == 0
    assert avisos and "cancel" in avisos[-1]


def test_tareas_en_fondo_sin_loop_no_lanza():
    avisos: list[str] = []
    fondo = TareasEnFondo(al_fallar=avisos.append)

    async def nunca() -> None:  # pragma: no cover
        raise AssertionError

    fondo.programar(nunca(), "compra")
    assert fondo.descartadas == 1 and "sin loop" in avisos[0]


# -- desde el entorno ------------------------------------------------------------------------


def test_estado_desde_entorno_por_defecto_memoria():
    almacen = estado_desde_entorno({})
    assert isinstance(almacen, EstadoMemoria) and almacen.durable is False
    assert isinstance(estado_desde_entorno({"GATEWAY_ESTADO": " Memoria "}), EstadoMemoria)


def test_las_credenciales_solas_no_activan_supabase():
    env = {"SUPABASE_URL": "https://x.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": "k"}
    assert isinstance(estado_desde_entorno(env), EstadoMemoria)


async def test_supabase_explicito():
    env = {"GATEWAY_ESTADO": "supabase", "SUPABASE_URL": "https://x.supabase.co",
           "SUPABASE_SECRET_KEY": "clave-secreta"}
    almacen = estado_desde_entorno(env)
    assert isinstance(almacen, EstadoSupabase) and almacen.durable is True
    assert "clave-secreta" not in repr(almacen)
    await almacen.cerrar()


def test_supabase_sin_credenciales_da_value_error():
    with pytest.raises(ValueError):
        estado_desde_entorno({"GATEWAY_ESTADO": "supabase"})


def test_valor_desconocido_da_value_error():
    with pytest.raises(ValueError):
        estado_desde_entorno({"GATEWAY_ESTADO": "redis"})
