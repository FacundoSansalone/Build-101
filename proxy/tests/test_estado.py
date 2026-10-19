"""Almacén de estado en memoria (`proxy/estado.py`), tareas en segundo plano y
la elección del almacén desde el entorno.

`EstadoMemoria` tiene que comportarse como la base: copias (nadie muta por
referencia), atadura de contexto donde gana el primero, reclamo de una
aprobación una sola vez y ids validados antes de tocar nada.
"""

from __future__ import annotations

import asyncio
import contextvars
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
from proxy.tipos_gateway import EntregaWebhook, ErrorAlmacen, EscrituraDecisivaFallida

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


async def test_esperar_termina_con_una_tarea_terminada_sin_discard(monkeypatch):
    """Regresión: con la tarea ya terminada y su `discard` agendado pero sin
    correr, `gather` (3.12) resuelve sin ceder al loop y `esperar()` daba
    vueltas para siempre (colgaba los tests del gateway en Windows)."""
    fondo = TareasEnFondo()
    llamadas = 0
    gather_real = asyncio.gather

    def gather_contado(*args, **kwargs):
        nonlocal llamadas
        llamadas += 1
        assert llamadas < 100, "esperar() no le cede el loop al done callback"
        return gather_real(*args, **kwargs)

    async def nada() -> None:
        return None

    fondo.programar(nada(), "compra")
    await asyncio.sleep(0)  # la tarea corre y termina; su discard queda en cola
    assert fondo.pendientes == 1
    monkeypatch.setattr(asyncio, "gather", gather_contado)
    await fondo.esperar()
    assert fondo.pendientes == 0


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


# -- tareas en segundo plano: barrera y decisivas (varias instancias) ----------------


async def test_barrera_sin_tareas_y_con_tareas_que_terminan():
    fondo = TareasEnFondo()
    assert await fondo.barrera(0.1) is True
    hechas: list[str] = []

    async def escribir(nombre: str) -> None:
        await asyncio.sleep(0)
        hechas.append(nombre)

    fondo.programar(escribir("compra"), "compra")
    fondo.programar(escribir("ofertas"), "ofertas", decisiva=True)
    assert await fondo.barrera(1.0) is True
    assert sorted(hechas) == ["compra", "ofertas"] and fondo.pendientes == 0


async def test_barrera_vence_sin_cancelar_y_sin_decisivas_no_lanza():
    """Sin `decisiva`, todo como hoy: una escritura lenta o que falla no
    corta la respuesta (la barrera solo dice si terminaron)."""
    fondo = TareasEnFondo()
    liberar = asyncio.Event()

    async def lenta() -> None:
        await liberar.wait()

    async def falla() -> None:
        raise ErrorAlmacen("HTTP 500")

    fondo.programar(lenta(), "compra")
    fondo.programar(falla(), "veredicto")
    assert await fondo.barrera(0.01) is False
    assert fondo.pendientes == 1  # no se canceló
    liberar.set()
    assert await fondo.barrera(1.0) is True
    assert fondo.errores == 1 and fondo.fallas_decisivas == 0


async def test_decisiva_que_falla_hace_lanzar_la_barrera_una_vez():
    avisos: list[str] = []
    fondo = TareasEnFondo(al_fallar=avisos.append)

    async def falla() -> None:
        raise ErrorAlmacen("HTTP 503")

    async def bien() -> None:
        return None

    fondo.programar(bien(), "compra", decisiva=True)
    fondo.programar(falla(), "ofertas", decisiva=True)
    with pytest.raises(EscrituraDecisivaFallida) as e:
        await fondo.barrera(1.0)
    assert isinstance(e.value, ErrorAlmacen)  # quien ya da 503 por ErrorAlmacen no cambia
    assert e.value.fallas == ("ofertas",)
    assert any("ofertas" in a for a in avisos)
    # Informada, no se repite en la barrera siguiente del mismo pedido.
    assert await fondo.barrera(0.1) is True
    assert fondo.fallas_decisivas == 1


async def test_decisiva_sin_terminar_al_vencer_la_barrera():
    fondo = TareasEnFondo()
    liberar = asyncio.Event()

    async def lenta_que_falla() -> None:
        await liberar.wait()
        raise ErrorAlmacen("tarde")

    fondo.programar(lenta_que_falla(), "compra", decisiva=True)
    with pytest.raises(EscrituraDecisivaFallida) as e:
        await fondo.barrera(0.01)
    assert e.value.fallas == ("compra (sin confirmar)",)
    # Si después falla, ya estaba informada: no vuelve a salir.
    liberar.set()
    assert await fondo.barrera(1.0) is True


async def test_decisivas_de_cada_pedido_por_separado():
    """La falla de un pedido no la recibe la barrera de otro (cada pedido
    HTTP corre en su propia tarea y tiene su ámbito)."""
    fondo = TareasEnFondo()

    async def falla() -> None:
        raise ErrorAlmacen("x")

    async def bien() -> None:
        return None

    async def pedido(coro, descripcion: str) -> tuple[str, ...]:
        fondo.programar(coro, descripcion, decisiva=True)
        await asyncio.sleep(0)
        try:
            await fondo.barrera(1.0)
        except EscrituraDecisivaFallida as e:
            return e.fallas
        return ()

    malo, bueno = await asyncio.gather(
        asyncio.create_task(pedido(falla(), "ofertas")),
        asyncio.create_task(pedido(bien(), "compra")),
    )
    assert malo == ("ofertas",) and bueno == ()


async def test_ambito_explicito_aisla_dentro_de_la_misma_tarea():
    fondo = TareasEnFondo()

    async def falla() -> None:
        raise ErrorAlmacen("x")

    with fondo.ambito():
        fondo.programar(falla(), "ofertas", decisiva=True)
        with fondo.ambito():
            assert await fondo.barrera(1.0) is True  # el ámbito de adentro no la ve
        with pytest.raises(EscrituraDecisivaFallida):
            await fondo.barrera(1.0)


async def test_decisiva_no_se_descarta_por_el_tope():
    fondo = TareasEnFondo(max_pendientes=1)
    liberar = asyncio.Event()

    async def lenta() -> None:
        await liberar.wait()

    fondo.programar(lenta(), "una")
    fondo.programar(lenta(), "dos", decisiva=True)  # entra igual
    assert fondo.pendientes == 2 and fondo.descartadas == 0
    liberar.set()
    assert await fondo.barrera(1.0) is True


def test_decisiva_sin_loop_queda_como_falla():
    fondo = TareasEnFondo()

    async def nunca() -> None:  # pragma: no cover
        raise AssertionError

    contexto = contextvars.copy_context()
    contexto.run(fondo.programar, nunca(), "compra", decisiva=True)
    assert fondo.descartadas == 1

    async def barrera() -> tuple[str, ...]:
        try:
            await fondo.barrera(0.1)
        except EscrituraDecisivaFallida as e:
            return e.fallas
        return ()

    # El ámbito vive en el contexto donde se programó.
    assert contexto.run(asyncio.run, barrera()) == ("compra (sin loop)",)


async def test_decisiva_cancelada_al_cerrar():
    fondo = TareasEnFondo()

    async def colgada() -> None:
        await asyncio.sleep(3600)

    fondo.programar(colgada(), "compra", decisiva=True)
    await asyncio.sleep(0)
    await fondo.cerrar(timeout_s=0.01)
    with pytest.raises(EscrituraDecisivaFallida) as e:
        await fondo.barrera(0.1)
    assert e.value.fallas == ("compra (cancelada)",)
