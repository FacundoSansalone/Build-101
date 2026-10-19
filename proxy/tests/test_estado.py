"""Almacén de estado en memoria (`proxy/estado.py`), tareas en segundo plano y
la elección del almacén desde el entorno.

`EstadoMemoria` tiene que comportarse como la base: copias (nadie muta por
referencia), atadura de contexto donde gana el primero, reclamo de una
aprobación una sola vez y ids validados antes de tocar nada.

El contrato de varias instancias (004_serverless.sql: esperas, compra
condicionada, trigger, aprobación idempotente, eventos y cola de webhooks)
corre contra los dos almacenes: `EstadoMemoria` y `EstadoSupabase` sobre un
PostgREST falso (`postgrest_falso.BaseFalsa`). En Supabase, cada "instancia"
es otro cliente contra la misma base, como dos funciones de Vercel.
"""

from __future__ import annotations

import asyncio
import contextvars
from datetime import datetime, timedelta, timezone

import pytest

from contracts import Evento
from proxy.aprobaciones import Aprobacion
from proxy.compras import ESTADOS_TERMINALES_API, RegistroCompras
from proxy.estado import (
    CUALQUIERA,
    ESTADOS_TERMINALES,
    EstadoMemoria,
    EstadoSupabase,
    TareasEnFondo,
    estado_desde_entorno,
    evento_desde_fila,
    fila_aprobacion_gw,
    fila_espera,
    fila_evento,
    fila_ofertas,
    fila_ultimo_veredicto,
)
from proxy.tests.postgrest_falso import BaseFalsa
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
             estado: str = "pendiente", centavos: int = 145000) -> dict:
    return fila_aprobacion_gw(
        Aprobacion(id=aprobacion_id, contexto_id="ctx-1", cart_id="cart_2", centavos=centavos,
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
    """Dos pendientes del mismo contexto y cart solo con totales distintos
    (el índice de 004 no deja dos con el mismo total)."""
    almacen = EstadoMemoria(durable=True)
    assert await almacen.cargar_aprobacion_pendiente("ctx-1", "cart_2") is None
    await almacen.guardar_aprobacion(fila_apr("apr-1", creada="2026-10-18T12:00:00+00:00"))
    await almacen.guardar_aprobacion(
        fila_apr("apr-2", creada="2026-10-18T12:05:00+00:00", centavos=150000)
    )
    await almacen.guardar_aprobacion(
        fila_apr("apr-3", creada="2026-10-18T12:10:00+00:00", estado="usada")
    )
    assert (await almacen.cargar_aprobacion_pendiente("ctx-1", "cart_2"))["id"] == "apr-2"
    con_total = await almacen.cargar_aprobacion_pendiente(
        "ctx-1", "cart_2", centavos=145000, moneda="USD"
    )
    assert con_total["id"] == "apr-1"
    assert await almacen.cargar_aprobacion_pendiente("ctx-1", "cart_2", moneda="EUR") is None


async def test_guardar_aprobacion_pendiente_repetida_falla_como_el_indice():
    """Como `gw_aprobaciones_pendiente_uidx`: otra pendiente con el mismo
    contexto, cart y total es un 409 en la base. Usada, ya no choca."""
    almacen = EstadoMemoria(durable=True)
    await almacen.guardar_aprobacion(fila_apr("apr-1"))
    with pytest.raises(ErrorAlmacen):
        await almacen.guardar_aprobacion(fila_apr("apr-2"))
    await almacen.guardar_aprobacion(fila_apr("apr-1"))  # la misma: upsert por id
    await almacen.guardar_aprobacion(fila_apr("apr-1", estado="usada"))
    await almacen.guardar_aprobacion(fila_apr("apr-2"))


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


# -- contrato de los dos almacenes con varias instancias (004) --------------------


URL_FALSA = "https://proyecto.supabase.co"
ESP1 = "esp-00000000000000a1"
ESP2 = "esp-00000000000000a2"
VENCE = AHORA + timedelta(seconds=60)


@pytest.fixture(params=["memoria", "supabase"])
def instancias(request):
    """Fábrica de "instancias del proxy" sobre el mismo estado: en memoria,
    el mismo almacén; en Supabase, otro cliente contra la misma base falsa."""
    if request.param == "memoria":
        compartido = EstadoMemoria(durable=True)
        return lambda: compartido
    base = BaseFalsa()
    return lambda: EstadoSupabase(URL_FALSA, "clave", transport=base.transporte())


def compra_en(estado: str, compra_id: str = CMP1, *, actualizada: datetime = AHORA,
              **extra) -> dict:
    fila = fila_compra(compra_id, estado=estado)
    fila["actualizada"] = actualizada.isoformat()
    fila.update(extra)
    return fila


def momento(fila: dict, columna: str) -> datetime:
    return datetime.fromisoformat(fila[columna])


def test_terminales_iguales_en_compras_y_en_el_almacen():
    """El trigger de 004 usa los mismos estados terminales que `compras.py`."""
    assert set(ESTADOS_TERMINALES) == set(ESTADOS_TERMINALES_API) == {"bloqueada", "vencida"}


# -- compra: trigger y escritura condicionada


@pytest.mark.parametrize("terminal", ["bloqueada", "vencida"])
async def test_compra_no_sale_de_un_estado_terminal(instancias, terminal):
    a, b = instancias(), instancias()
    await a.guardar_compra(compra_en(terminal))
    await b.guardar_compra(compra_en("negociando", actualizada=AHORA + timedelta(seconds=5)))
    otro = "vencida" if terminal == "bloqueada" else "bloqueada"
    await b.guardar_compra(compra_en(otro, actualizada=AHORA + timedelta(seconds=6)))
    assert (await a.cargar_compra(CMP1))["estado"] == terminal
    # En el mismo estado terminal sí se escribe (otro veredicto, por ejemplo).
    fila = compra_en(terminal, actualizada=AHORA + timedelta(seconds=7))
    fila["datos"]["veredictos"] = [{"status": "bloqueado"}]
    await b.guardar_compra(fila)
    assert (await a.cargar_compra(CMP1))["datos"]["veredictos"] == [{"status": "bloqueado"}]


@pytest.mark.parametrize("terminal", ["bloqueada", "vencida"])
async def test_el_paso_a_terminal_entra_aunque_llegue_con_actualizada_menor(instancias, terminal):
    """Una copia vieja con el reloj adelantado llegó antes que el corte: el
    corte igual entra (si no, la conversación cortada seguiría) y
    `actualizada` no retrocede."""
    a, b = instancias(), instancias()
    await a.guardar_compra(compra_en("negociando"))
    await b.guardar_compra(compra_en("negociando", actualizada=AHORA + timedelta(seconds=5)))
    await a.guardar_compra(compra_en(terminal, actualizada=AHORA + timedelta(seconds=1)))
    fila = await b.cargar_compra(CMP1)
    assert fila["estado"] == terminal
    assert momento(fila, "actualizada") == AHORA + timedelta(seconds=5)
    # Por la escritura condicionada, igual.
    await a.guardar_compra(
        compra_en("sin_oportunidad", CMP2, actualizada=AHORA + timedelta(seconds=9))
    )
    assert await b.guardar_compra_condicionada(
        compra_en(terminal, CMP2, actualizada=AHORA), estado_esperado="sin_oportunidad",
        espera_id_esperado=CUALQUIERA,
    )
    assert (await a.cargar_compra(CMP2))["estado"] == terminal


async def test_compra_descarta_la_escritura_vieja(instancias):
    a, b = instancias(), instancias()
    await a.guardar_compra(compra_en("negociando", actualizada=AHORA + timedelta(seconds=2)))
    await b.guardar_compra(compra_en("esperando_decision", actualizada=AHORA + timedelta(seconds=1)))
    assert (await a.cargar_compra(CMP1))["estado"] == "negociando"
    # Con la misma `actualizada` pasa (una normalización al leer no la mueve).
    await b.guardar_compra(compra_en("sin_oportunidad", actualizada=AHORA + timedelta(seconds=2)))
    assert (await a.cargar_compra(CMP1))["estado"] == "sin_oportunidad"


async def test_guardar_compra_sin_espera_id_deja_la_que_estaba(instancias):
    a = instancias()
    await a.guardar_compra(compra_en("esperando_contexto", espera_id=ESP1))
    sin_columna = compra_en("esperando_contexto", actualizada=AHORA + timedelta(seconds=1))
    await a.guardar_compra(sin_columna)
    assert (await a.cargar_compra(CMP1))["espera_id"] == ESP1
    await a.guardar_compra({**sin_columna, "espera_id": None})
    assert (await a.cargar_compra(CMP1)).get("espera_id") is None
    with pytest.raises(ErrorAlmacen):
        await a.guardar_compra({**sin_columna, "espera_id": "esp-1&id=neq.x"})


async def test_guardar_compra_condicionada_normaliza_sin_pisar(instancias):
    a, b = instancias(), instancias()
    await a.guardar_compra(compra_en("esperando_contexto", espera_id=ESP1))
    normalizada = compra_en("negociando", espera_id=None)
    # Otra espera, u otro estado: no coincide con lo que se leyó.
    assert not await b.guardar_compra_condicionada(
        normalizada, estado_esperado="esperando_contexto", espera_id_esperado=ESP2
    )
    assert not await b.guardar_compra_condicionada(
        normalizada, estado_esperado="negociando", espera_id_esperado=ESP1
    )
    assert await b.guardar_compra_condicionada(
        normalizada, estado_esperado="esperando_contexto", espera_id_esperado=ESP1
    )
    fila = await a.cargar_compra(CMP1)
    assert fila["estado"] == "negociando" and fila.get("espera_id") is None
    # La segunda normalización (otra instancia con la misma foto) no pisa.
    assert not await a.guardar_compra_condicionada(
        normalizada, estado_esperado="esperando_contexto", espera_id_esperado=ESP1
    )
    # Sin espera (`is.null`), con varios estados y sin condición de espera.
    siguiente = compra_en("sin_oportunidad", actualizada=AHORA + timedelta(seconds=1))
    assert await a.guardar_compra_condicionada(
        siguiente, estado_esperado=("negociando", "aceptada"), espera_id_esperado=None
    )
    tercera = compra_en("aceptada", actualizada=AHORA + timedelta(seconds=2))
    assert await b.guardar_compra_condicionada(
        tercera, estado_esperado=frozenset({"sin_oportunidad"}), espera_id_esperado=CUALQUIERA
    )
    assert (await a.cargar_compra(CMP1))["estado"] == "aceptada"
    # Una compra que no está no se crea.
    assert not await a.guardar_compra_condicionada(
        compra_en("aceptada", CMP2), estado_esperado="aceptada", espera_id_esperado=CUALQUIERA
    )
    assert await a.cargar_compra(CMP2) is None


async def test_compra_condicionada_por_actualizada_y_con_el_trigger(instancias):
    a = instancias()
    await a.guardar_compra(compra_en("negociando"))
    nueva = compra_en("sin_oportunidad", actualizada=AHORA + timedelta(seconds=1))
    assert not await a.guardar_compra_condicionada(
        nueva, estado_esperado="negociando", espera_id_esperado=CUALQUIERA,
        actualizada_esperada=AHORA - timedelta(seconds=1),
    )
    assert await a.guardar_compra_condicionada(
        nueva, estado_esperado="negociando", espera_id_esperado=CUALQUIERA,
        actualizada_esperada=AHORA,
    )
    # El trigger también corre en la escritura condicionada.
    vieja = compra_en("negociando", actualizada=AHORA)
    assert not await a.guardar_compra_condicionada(
        vieja, estado_esperado="sin_oportunidad", espera_id_esperado=CUALQUIERA
    )
    await a.guardar_compra(compra_en("bloqueada", actualizada=AHORA + timedelta(seconds=2)))
    assert not await a.guardar_compra_condicionada(
        compra_en("negociando", actualizada=AHORA + timedelta(seconds=3)),
        estado_esperado="bloqueada", espera_id_esperado=CUALQUIERA,
    )
    assert (await a.cargar_compra(CMP1))["estado"] == "bloqueada"


@pytest.mark.parametrize(
    "kwargs",
    [{"estado_esperado": "inventado", "espera_id_esperado": None},
     {"estado_esperado": (), "espera_id_esperado": None},
     {"estado_esperado": "negociando", "espera_id_esperado": "esp-1"},
     {"estado_esperado": "negociando", "espera_id_esperado": None, "actualizada_esperada": "ayer"}],
)
async def test_compra_condicionada_con_condicion_invalida(instancias, kwargs):
    with pytest.raises(ErrorAlmacen):
        await instancias().guardar_compra_condicionada(compra_en("negociando"), **kwargs)


# -- esperas


async def test_abrir_espera_una_abierta_por_compra(instancias):
    a, b = instancias(), instancias()
    await a.guardar_compra(compra_en("negociando"))
    primera = fila_espera(CMP1, "porque", VENCE, creada=AHORA)
    abierta = await a.abrir_espera(primera)
    assert abierta["id"] == primera["id"] and abierta["estado"] == "abierta"
    assert momento(abierta, "vence") == VENCE and abierta["respuesta"] is None
    # Otra instancia no puede abrir otra para la misma compra.
    assert await b.abrir_espera(fila_espera(CMP1, "porque", VENCE, creada=AHORA)) is None
    # Reintentar la misma (un timeout después del insert) devuelve la abierta.
    assert (await b.abrir_espera(primera))["id"] == primera["id"]
    assert (await b.espera_abierta_de(CMP1))["id"] == primera["id"]
    assert (await b.leer_espera(primera["id"]))["tipo"] == "porque"
    assert await b.leer_espera("esp-" + "0" * 16) is None
    # Cerrada, se puede abrir la siguiente: el índice es solo de las abiertas.
    await a.vencer_espera(primera["id"], AHORA)
    assert await a.espera_abierta_de(CMP1) is None
    segunda = await b.abrir_espera(
        fila_espera(CMP1, "decision", VENCE, creada=AHORA, oportunidad_id="opo-1")
    )
    assert segunda["oportunidad_id"] == "opo-1"
    # Una cerrada con el mismo id no se reabre.
    assert await a.abrir_espera({**primera}) is None


async def test_abrir_espera_sin_la_compra_falla(instancias):
    """La clave foránea de `gw_esperas`: no hay espera de una compra que no está."""
    with pytest.raises(ErrorAlmacen):
        await instancias().abrir_espera(fila_espera(CMP1, "porque", VENCE, creada=AHORA))


@pytest.mark.parametrize(
    "cambio",
    [{"id": "esp-1"}, {"compra_id": "cmp-x"}, {"tipo": "otro"}, {"estado": "respondida"},
     {"tipo": "decision"}, {"oportunidad_id": "opo-1"}, {"vence": "mañana"},
     {"respuesta": "ya"}, {"resuelta": AHORA.isoformat()}],
)
async def test_abrir_espera_invalida(instancias, cambio):
    a = instancias()
    await a.guardar_compra(compra_en("negociando"))
    with pytest.raises(ErrorAlmacen):
        await a.abrir_espera({**fila_espera(CMP1, "porque", VENCE, creada=AHORA), **cambio})


async def test_responder_gana_una_sola_vez_entre_dos_instancias(instancias):
    a, b = instancias(), instancias()
    await a.guardar_compra(compra_en("esperando_decision"))
    espera = await a.abrir_espera(
        fila_espera(CMP1, "decision", VENCE, creada=AHORA, oportunidad_id="opo-1")
    )
    resultados = await asyncio.gather(
        a.responder_espera(espera["id"], "aceptar", AHORA, oportunidad_id="opo-1"),
        b.responder_espera(espera["id"], "rechazar", AHORA, oportunidad_id="opo-1"),
    )
    assert sorted(resultados) == [False, True]
    fila = await b.leer_espera(espera["id"])
    assert fila["estado"] == "respondida" and fila["resuelta"] is not None
    assert fila["respuesta"] == ("aceptar" if resultados[0] else "rechazar")


async def test_responder_exige_abierta_sin_vencer_y_la_misma_oportunidad(instancias):
    a = instancias()
    await a.guardar_compra(compra_en("esperando_decision"))
    espera = await a.abrir_espera(
        fila_espera(CMP1, "decision", VENCE, creada=AHORA, oportunidad_id="opo-1")
    )
    assert not await a.responder_espera(espera["id"], "aceptar", AHORA, oportunidad_id="opo-2")
    assert not await a.responder_espera(espera["id"], "aceptar", AHORA, tipo="porque")
    assert not await a.responder_espera(espera["id"], "aceptar", VENCE)  # `vence > ahora`
    assert not await a.responder_espera("esp-" + "f" * 16, "aceptar", AHORA)
    assert (await a.leer_espera(espera["id"]))["estado"] == "abierta"
    assert await a.responder_espera(
        espera["id"], "aceptar", AHORA, tipo="decision", oportunidad_id="opo-1"
    )
    assert not await a.responder_espera(espera["id"], "rechazar", AHORA)  # ya respondida


async def test_responder_el_porque_omitido_o_con_datos(instancias):
    a = instancias()
    await a.guardar_compra(compra_en("esperando_contexto"))
    espera = await a.abrir_espera(fila_espera(CMP1, "porque", VENCE, creada=AHORA))
    assert await a.responder_espera(espera["id"], None, AHORA)  # omitir
    fila = await a.leer_espera(espera["id"])
    assert fila["estado"] == "respondida" and fila["respuesta"] is None
    otra = await a.abrir_espera(fila_espera(CMP1, "porque", VENCE, creada=AHORA))
    assert await a.responder_espera(otra["id"], {"texto": "para la oficina", "n": (1, 2)}, AHORA)
    assert (await a.leer_espera(otra["id"]))["respuesta"] == {"texto": "para la oficina", "n": [1, 2]}


@pytest.mark.parametrize("respuesta", [float("nan"), object(), "x" * 20_000])
async def test_responder_con_respuesta_invalida(instancias, respuesta):
    a = instancias()
    await a.guardar_compra(compra_en("esperando_contexto"))
    espera = await a.abrir_espera(fila_espera(CMP1, "porque", VENCE, creada=AHORA))
    with pytest.raises(ErrorAlmacen):
        await a.responder_espera(espera["id"], respuesta, AHORA)
    assert (await a.leer_espera(espera["id"]))["estado"] == "abierta"


async def test_vencer_y_responder_en_carrera(instancias):
    a, b = instancias(), instancias()
    await a.guardar_compra(compra_en("esperando_contexto"))
    # B responde antes: A, al vencer, recibe la fila con la respuesta.
    e1 = await a.abrir_espera(fila_espera(CMP1, "porque", VENCE, creada=AHORA))
    assert await b.responder_espera(e1["id"], "para la oficina nueva", AHORA)
    fila = await a.vencer_espera(e1["id"], VENCE)
    assert fila["estado"] == "respondida" and fila["respuesta"] == "para la oficina nueva"
    # A vence antes: la respuesta de B llega tarde y no gana.
    e2 = await a.abrir_espera(fila_espera(CMP1, "porque", VENCE, creada=AHORA))
    vencida = await a.vencer_espera(e2["id"], VENCE)
    assert vencida["estado"] == "vencida" and vencida["respuesta"] is None
    assert momento(vencida, "resuelta") == VENCE
    assert not await b.responder_espera(e2["id"], "tarde", AHORA)
    # Vencer otra vez devuelve la misma fila sin cambiarla.
    otra_vez = await b.vencer_espera(e2["id"], VENCE + timedelta(seconds=30))
    assert otra_vez["estado"] == "vencida" and momento(otra_vez, "resuelta") == VENCE
    assert await a.vencer_espera("esp-" + "f" * 16, VENCE) is None
    # A la vez: una sola cambia la espera y las dos ven lo mismo.
    e3 = await a.abrir_espera(fila_espera(CMP1, "porque", VENCE, creada=AHORA))
    respondio, fila = await asyncio.gather(
        b.responder_espera(e3["id"], "x", AHORA), a.vencer_espera(e3["id"], AHORA)
    )
    assert fila["estado"] == ("respondida" if respondio else "vencida")


# -- aprobaciones idempotentes


async def test_crear_aprobacion_pendiente_idempotente_entre_instancias(instancias):
    a, b = instancias(), instancias()
    primera, creada_a = await a.crear_aprobacion_pendiente(fila_apr("apr-a"))
    segunda, creada_b = await b.crear_aprobacion_pendiente(fila_apr("apr-b"))
    assert (creada_a, creada_b) == (True, False)
    assert primera["id"] == segunda["id"] == "apr-a"
    # Usada, la aprobación siguiente del mismo checkout es otra.
    assert await b.reclamar_aprobacion("apr-a", usada="2026-10-18T12:01:00Z", task_id=None)
    tercera, creada = await a.crear_aprobacion_pendiente(fila_apr("apr-c"))
    assert creada and tercera["id"] == "apr-c"
    # Otro total firmado es otra pendiente.
    otra, creada = await b.crear_aprobacion_pendiente(fila_apr("apr-d", centavos=150000))
    assert creada and otra["id"] == "apr-d"
    assert [f["id"] for f in await a.aprobaciones_de_compra(CMP1)] == ["apr-a", "apr-c", "apr-d"]
    assert await a.aprobaciones_de_compra(CMP2) == []


async def test_crear_aprobacion_pendiente_a_la_vez(instancias):
    resultados = await asyncio.gather(
        *(instancias().crear_aprobacion_pendiente(fila_apr(f"apr-{i}")) for i in range(5))
    )
    assert sum(1 for _, creada in resultados if creada) == 1
    assert len({fila["id"] for fila, _ in resultados}) == 1


@pytest.mark.parametrize(
    "cambio", [{"estado": "usada"}, {"centavos": None}, {"centavos": 1.5}, {"moneda": "US D"}]
)
async def test_crear_aprobacion_pendiente_invalida(instancias, cambio):
    with pytest.raises(ErrorAlmacen):
        await instancias().crear_aprobacion_pendiente({**fila_apr(), **cambio})


# -- lecturas por compra y por contexto


async def test_contextos_de_compra_y_ultimos_veredictos(instancias):
    a, b = instancias(), instancias()
    await a.guardar_compra(compra_en("negociando"))
    await a.guardar_compra(compra_en("negociando", CMP2))
    for ctx in ("ctx-a", "ctx-b"):
        await a.atar_contexto(ctx, CMP1, "merchant_1")
    await b.atar_contexto("ctx-c", CMP2, "merchant_1")
    assert await b.contextos_de_compra(CMP1) == ["ctx-a", "ctx-b"]
    assert await a.contextos_de_compra(CMP2) == ["ctx-c"]
    assert await a.contextos_de_compra("cmp-" + "f" * 16) == []

    await a.guardar_ultimo_veredicto(fila_ultimo_veredicto("ctx-a", "cart_1", "aprobado", 100, "USD", AHORA))
    await b.guardar_ultimo_veredicto(
        fila_ultimo_veredicto("ctx-a", "cart_2", "escalar", 145000, "USD", AHORA + timedelta(seconds=1))
    )
    await b.guardar_ultimo_veredicto(fila_ultimo_veredicto("ctx-b", "cart_1", "bloqueado", None, None, AHORA))
    filas = await a.ultimos_veredictos_de_contexto("ctx-a")
    assert [(f["cart_id"], f["status"]) for f in filas] == [("cart_2", "escalar"), ("cart_1", "aprobado")]
    with pytest.raises(ErrorAlmacen):
        await a.contextos_de_compra("cmp-1&x")


# -- eventos


def evento(n: int, *, tipo: str = "veredicto", compra_id: str = CMP1) -> Evento:
    return Evento(
        id=f"evt-{n}", ts=AHORA + timedelta(seconds=n), contexto_id="ctx-1", escenario=None,
        guard_activo=True, tipo=tipo, datos={"compra_id": compra_id, "n": n},
    )


async def test_eventos_por_compra_en_orden(instancias):
    a, b = instancias(), instancias()
    await a.guardar_evento(fila_evento(evento(1), integrador_id="acme", compra_id=CMP1))
    await b.guardar_evento(fila_evento(evento(2, compra_id=CMP2), integrador_id="acme", compra_id=CMP2))
    await b.guardar_evento(fila_evento(evento(3), integrador_id="acme", compra_id=CMP1))
    await a.guardar_evento(fila_evento(evento(1), integrador_id="acme", compra_id=CMP1))  # no duplica
    await a.guardar_evento(fila_evento(evento(4, tipo="oportunidad"), integrador_id="acme", compra_id=CMP1))
    filas = await b.listar_eventos(CMP1)
    assert [f["id"] for f in filas] == ["evt-1", "evt-3", "evt-4"]
    ordenes = [f["orden"] for f in filas]
    assert ordenes == sorted(ordenes) and len(set(ordenes)) == 3
    assert [f["id"] for f in await a.listar_eventos(CMP1, ordenes[0])] == ["evt-3", "evt-4"]
    assert [f["id"] for f in await a.listar_eventos(CMP1, 0, limite=1)] == ["evt-1"]
    assert await a.listar_eventos(CMP1, ordenes[-1]) == []
    assert [f["id"] for f in await a.listar_eventos(CMP2)] == ["evt-2"]
    assert evento_desde_fila(filas[2]) == evento(4, tipo="oportunidad")


@pytest.mark.parametrize(
    "cambio",
    [{"id": "evt 1"}, {"compra_id": None}, {"integrador_id": "ACME"}, {"tipo": "Tipo-Raro"},
     {"guard_activo": "si"}, {"datos": []}, {"contexto_id": "ctx,1"}],
)
async def test_guardar_evento_invalido(instancias, cambio):
    fila = {**fila_evento(evento(1), integrador_id="acme", compra_id=CMP1), **cambio}
    with pytest.raises(ErrorAlmacen):
        await instancias().guardar_evento(fila)


async def test_listar_eventos_con_desde_invalido(instancias):
    with pytest.raises(ErrorAlmacen):
        await instancias().listar_eventos(CMP1, -1)


def test_evento_desde_fila_invalida():
    with pytest.raises(ErrorAlmacen):
        evento_desde_fila({"id": "evt-1", "tipo": "inventado"})


# -- cola de webhooks


def entrega(letra: str = "a", *, creada: datetime = AHORA, integrador: str = "acme") -> EntregaWebhook:
    return EntregaWebhook(id="whe-" + letra * 32, integrador_id=integrador,
                          tipo="compra.veredicto", compra_id=CMP1, creada=creada)


async def test_cola_de_webhooks_una_instancia_por_entrega(instancias):
    a, b = instancias(), instancias()
    cuerpo = '{"datos":{},"id":"whe-a","texto":"señal ✓"}'.encode()
    await a.encolar_entrega(entrega("a"), cuerpo, proximo_intento=AHORA)
    await a.encolar_entrega(entrega("b"), b"{}", proximo_intento=AHORA + timedelta(seconds=30))
    hasta = AHORA + timedelta(seconds=20)
    tomadas_a, tomadas_b = await asyncio.gather(
        a.tomar_entregas(AHORA, hasta=hasta), b.tomar_entregas(AHORA, hasta=hasta)
    )
    tomadas = tomadas_a + tomadas_b
    assert [f["id"] for f in tomadas] == ["whe-" + "a" * 32]  # la b todavía no toca
    tomada = tomadas[0]
    assert tomada["cuerpo"].encode("utf-8") == cuerpo  # los mismos bytes
    assert momento(tomada, "tomada_hasta") == hasta
    # Mientras está tomada, nadie más la toma.
    assert await b.tomar_entregas(AHORA + timedelta(seconds=10), hasta=AHORA + timedelta(seconds=40)) == []

    # El intento falló: vuelve a la cola con su próximo intento.
    e = entrega("a")
    e.intentos, e.error, e.status_http, e.actualizada = 1, "http_5xx", 503, AHORA
    reintento = AHORA + timedelta(seconds=4)
    assert not await b.marcar_entrega(e, tomada_hasta=hasta + timedelta(seconds=1), proximo_intento=reintento)
    assert await a.marcar_entrega(e, tomada_hasta=tomada["tomada_hasta"], proximo_intento=reintento)
    assert not await a.marcar_entrega(e, tomada_hasta=tomada["tomada_hasta"], proximo_intento=reintento)
    assert await a.tomar_entregas(AHORA + timedelta(seconds=3), hasta=AHORA + timedelta(seconds=9)) == []
    otra_vez = await b.tomar_entregas(AHORA + timedelta(seconds=5), hasta=AHORA + timedelta(seconds=25))
    assert [f["id"] for f in otra_vez] == ["whe-" + "a" * 32]
    assert otra_vez[0]["intentos"] == 1 and otra_vez[0]["error"] == "http_5xx"

    # Entregada: no se toma más; a los 60 s solo queda la b.
    e.estado, e.intentos, e.status_http, e.error = "entregada", 2, 200, None
    assert await b.marcar_entrega(e, tomada_hasta=otra_vez[0]["tomada_hasta"])
    despues = await a.tomar_entregas(AHORA + timedelta(seconds=60), hasta=AHORA + timedelta(seconds=80))
    assert [f["id"] for f in despues] == ["whe-" + "b" * 32]


async def test_toma_vencida_la_toma_otra_instancia(instancias):
    a, b = instancias(), instancias()
    await a.encolar_entrega(entrega("a"), b"{}", proximo_intento=AHORA)
    [de_a] = await a.tomar_entregas(AHORA, hasta=AHORA + timedelta(seconds=10))
    # A murió con la entrega tomada: pasada la toma, B la toma y A ya no la marca.
    [de_b] = await b.tomar_entregas(AHORA + timedelta(seconds=11), hasta=AHORA + timedelta(seconds=30))
    e = entrega("a")
    e.estado, e.intentos = "entregada", 1
    assert not await a.marcar_entrega(e, tomada_hasta=de_a["tomada_hasta"])
    assert await b.marcar_entrega(e, tomada_hasta=de_b["tomada_hasta"])


async def test_listar_entregas_sin_el_cuerpo_y_registro_que_no_borra_la_cola(instancias):
    a = instancias()
    await a.guardar_entrega(entrega("a", creada=AHORA))
    await a.encolar_entrega(entrega("b", creada=AHORA + timedelta(seconds=1)), b'{"x":1}',
                            proximo_intento=AHORA)
    await a.guardar_entrega(entrega("c", integrador="otro"))
    filas = await a.listar_entregas("acme")
    assert [f["id"] for f in filas] == ["whe-" + "b" * 32, "whe-" + "a" * 32]
    assert set(filas[0]) == set(entrega("b").a_json())  # sin cuerpo ni columnas de la cola
    assert len(await a.listar_entregas("acme", limite=1)) == 1
    # El registro de un intento (`guardar_entrega`) no borra el cuerpo encolado.
    e = entrega("b", creada=AHORA + timedelta(seconds=1))
    e.intentos = 1
    await a.guardar_entrega(e)
    [tomada] = await a.tomar_entregas(AHORA, hasta=AHORA + timedelta(seconds=5))
    assert tomada["cuerpo"] == '{"x":1}' and tomada["intentos"] == 1


async def test_cola_rechaza_lo_invalido(instancias):
    a = instancias()
    terminada = entrega("a")
    terminada.estado = "entregada"
    with pytest.raises(ErrorAlmacen):
        await a.encolar_entrega(terminada, b"{}", proximo_intento=AHORA)
    with pytest.raises(ErrorAlmacen):
        await a.encolar_entrega(entrega("a"), "no son bytes", proximo_intento=AHORA)
    with pytest.raises(ErrorAlmacen):
        await a.encolar_entrega(entrega("a"), b"\xff\xfe", proximo_intento=AHORA)
    with pytest.raises(ErrorAlmacen):
        await a.tomar_entregas(AHORA, hasta=AHORA)
    with pytest.raises(ErrorAlmacen):
        await a.marcar_entrega(entrega("a"), tomada_hasta=AHORA)  # pendiente sin próximo intento
