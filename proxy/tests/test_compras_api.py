"""Compras de integrador (`proxy/compras.py`) y aprobaciones hidratadas
(`proxy/aprobaciones.py`): lo que agrega el gateway de producción.

Lo que importa: la ficha del integrador queda fija, una fila del almacén
vuelve igual (`a_fila`/`desde_fila`) y, después de un reinicio, ninguna
espera queda abierta: el porqué pendiente se sigue literal y la oportunidad
pendiente vence (el silencio no es autorización). Una aprobación usada nunca
vuelve a quedar disponible.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from contracts import Cambio, Contexto, Entrega, Ficha, Flexibilidad, Oportunidad, Requisito
from proxy.aprobaciones import Aprobacion, RegistroAprobaciones, aprobacion_desde_fila
from proxy.compras import (
    ESTADOS_TERMINALES_API,
    MAX_VEREDICTOS_POR_COMPRA,
    Compra,
    EstadoInvalido,
    RegistroCompras,
)
from proxy.estado import fila_aprobacion_gw
from proxy.tipos_gateway import ErrorAlmacen, FotoCompra

pytestmark = pytest.mark.anyio

CLAVES_A_JSON_DE_ANTES = {
    "compra_id", "estado", "creada", "contexto_id", "pedido", "pregunta",
    "requisitos", "contexto", "oportunidad", "vence", "decision",
    "requisitos_autorizados",
}


def ficha_api() -> Ficha:
    return Ficha(
        pedido_original="20 computadoras para el lunes",
        requisitos=[
            Requisito(campo="precio", operador="maximo", valor=20000, obligatorio=True),
            Requisito(campo="moneda", operador="igual", valor="USD", obligatorio=True),
            Requisito(campo="cantidad", operador="minimo", valor=20, obligatorio=True),
        ],
        confirmada=True,
        categoria_taxonomia="298",
    )


def ficha_demo() -> Ficha:
    return Ficha(
        pedido_original="zapatillas",
        requisitos=[Requisito(campo="precio", operador="maximo", valor=1, obligatorio=True)],
        confirmada=True,
    )


def contexto() -> Contexto:
    return Contexto(
        pedido="20 computadoras para el lunes",
        pregunta="¿Para qué las necesitás?",
        respuesta="un aula que arranca el lunes, 10 alcanzan para el lunes",
        necesidad="equipar un aula",
        necesidad_entregas=[Entrega(cantidad=10, fecha=date(2026, 10, 19))],
        flexibilidades=[Flexibilidad(descripcion="entregas en dos partes", origen="dicha")],
    )


def oportunidad() -> Oportunidad:
    return Oportunidad(
        id="op-1",
        cart_id="cart_3",
        sku="NB-14-16",
        titulo="Notebook 14",
        precio=19800.0,
        moneda="USD",
        cambios=[Cambio(campo="entregas", pedido=[{"cantidad": 20, "fecha": "2026-10-19"}],
                        ofrecido=[{"cantidad": 10, "fecha": "2026-10-19"}])],
        por_que="cubre la necesidad",
        requisitos_propuestos=[
            Requisito(campo="precio", operador="maximo", valor=20000, obligatorio=True),
        ],
    )


def crear(registro: RegistroCompras, **kw) -> Compra:
    datos = {
        "integrador_id": "acme",
        "comercio_id": "merchant_1",
        "pedido": "20 computadoras para el lunes",
        "ficha": ficha_api(),
        "referencia": "ord-123",
        "porque": None,
        "preguntar_porque": False,
        "intencion_activa": True,
    }
    datos.update(kw)
    return registro.crear_api(**datos)


# -- crear_api ------------------------------------------------------------------


async def test_crear_api_queda_creada_con_la_ficha_resuelta():
    registro = RegistroCompras()
    compra = crear(registro, porque="para un aula")
    assert compra.id.startswith("cmp-") and len(compra.id) == 20
    assert compra.estado == "creada"
    assert compra.origen == "api"
    assert compra.pregunta == ""
    assert compra.ficha_resuelta is True
    assert compra.ficha == ficha_api()
    assert compra.integrador_id == "acme" and compra.comercio_id == "merchant_1"
    assert compra.referencia == "ord-123" and compra.porque == "para un aula"
    assert compra.contexto_id is None
    assert compra.actualizada is not None
    assert not compra.contexto_listo.is_set()  # la etapa de contexto no corrió
    assert registro.get(compra.id) is compra
    assert compra.requisitos == ficha_api().requisitos


async def test_la_ficha_de_la_demo_no_pisa_la_del_integrador():
    registro = RegistroCompras()
    compra = crear(registro)
    assert compra.resolver_ficha(ficha_demo()) == ficha_api()
    assert compra.ficha == ficha_api()


async def test_una_compra_de_api_viva_no_sale_de_memoria_por_el_tope():
    """Revisión adversarial: el tope de memoria sacaba compras de integrador
    vivas y, con ellas, las ataduras que dicen de quién es el contexto (el
    dashboard podía aprobar su `escalar`) y su espera. Ahora solo salen las
    terminadas (bloqueadas, vencidas o pasadas del TTL)."""
    registro = RegistroCompras(maximo=2)
    a, b, c = crear(registro), crear(registro), crear(registro)
    assert registro.atar(a, "ctx-a")
    assert registro.get(a.id) is a and registro.get(b.id) is b and registro.get(c.id) is c
    assert registro.de_contexto("ctx-a") is a

    a.estado = "bloqueada"  # terminada: ya puede salir
    crear(registro)
    assert registro.get(a.id) is None and registro.de_contexto("ctx-a") is None
    assert registro.get(b.id) is b and registro.get(c.id) is c


def _reloj_que_avanza(monkeypatch) -> None:
    """Cada lectura del reloj avanza 1 ms: en Windows dos `datetime.now()`
    seguidos pueden dar el mismo instante (una compra recién creada tendría
    edad 0 y `0 > ttl_s=0` es falso)."""
    base = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
    lecturas = iter(range(1, 10**6))
    monkeypatch.setattr("proxy.compras._ahora", lambda: base + timedelta(milliseconds=next(lecturas)))


async def test_una_compra_de_api_esperando_no_sale_aunque_pase_el_ttl(monkeypatch):
    _reloj_que_avanza(monkeypatch)
    registro = RegistroCompras(maximo=1, ttl_s=0)
    a = crear(registro, preguntar_porque=True)
    a.nueva_espera("¿para qué?")
    b = crear(registro)
    assert registro.get(a.id) is a  # tiene una espera viva
    assert registro.get(b.id) is None  # pasó el TTL (0 s) y no espera nada


async def test_las_compras_de_api_no_desplazan_a_las_de_la_demo_ni_al_reves():
    registro = RegistroCompras(maximo=2)
    api = [crear(registro) for _ in range(3)]
    demo = [registro.crear("pedido", "¿para qué?", None, f"ctx-{i}") for i in range(3)]
    assert all(registro.get(c.id) is c for c in api)
    assert registro.get(demo[0].id) is None  # la demo, como siempre: las más viejas
    assert registro.get(demo[1].id) is demo[1] and registro.get(demo[2].id) is demo[2]


# -- a_json (aditivo) -------------------------------------------------------------


async def test_a_json_conserva_las_claves_de_antes_y_suma_tres():
    registro = RegistroCompras()
    vieja = registro.crear("pedido", "¿para qué?", None, "ctx-1")
    datos = vieja.a_json()
    assert set(datos) == CLAVES_A_JSON_DE_ANTES | {"origen", "integrador_id", "comercio_id"}
    assert datos["origen"] == "agente"
    assert datos["integrador_id"] is None and datos["comercio_id"] is None

    api = crear(registro).a_json()
    assert api["origen"] == "api" and api["integrador_id"] == "acme"
    assert api["comercio_id"] == "merchant_1"
    assert api["estado"] == "creada"


# -- a_fila / desde_fila -------------------------------------------------------------


async def test_ida_y_vuelta_con_ficha_contexto_y_oportunidad():
    registro = RegistroCompras()
    compra = crear(registro, porque="para un aula", preguntar_porque=True)
    registro.atar(compra, "ctx-1")
    compra.estado = "aceptada"
    compra.decision = "aceptada"
    compra.contexto = contexto()
    compra.oportunidad = oportunidad()
    compra.requisitos_autorizados = list(oportunidad().requisitos_propuestos)
    compra.registrar_veredicto({"status": "escalar", "cart_id": "cart_2"})
    compra.aprobacion_pendiente = {"contexto_id": "ctx-1", "cart_id": "cart_2",
                                   "centavos": 145000, "moneda": "USD"}

    fila = compra.a_fila()
    assert set(fila) == {"id", "integrador_id", "comercio_id", "referencia", "estado",
                         "creada", "actualizada", "datos"}
    assert set(fila["datos"]) == {
        "pedido", "pregunta", "origen", "preguntar_porque", "porque", "intencion_activa",
        "ficha_llm", "ficha", "ficha_resuelta", "contexto_id", "contexto", "oportunidad",
        "vence", "decision", "requisitos_autorizados", "veredictos", "aprobacion_pendiente",
    }
    # La fila es JSON puro: se puede mandar tal cual a PostgREST.
    json.dumps(fila)

    vuelta = Compra.desde_fila(fila)
    assert vuelta is not compra
    assert vuelta.a_fila()["datos"] == fila["datos"]
    assert vuelta.id == compra.id and vuelta.creada == compra.creada
    assert vuelta.actualizada == compra.actualizada
    assert vuelta.ficha == compra.ficha
    assert vuelta.contexto.model_dump(mode="json") == contexto().model_dump(mode="json")
    assert vuelta.oportunidad.model_dump(mode="json") == oportunidad().model_dump(mode="json")
    assert vuelta.requisitos_autorizados == compra.requisitos_autorizados
    assert vuelta.estado == "aceptada" and vuelta.decision == "aceptada"
    assert vuelta.origen == "api" and vuelta.integrador_id == "acme"
    assert vuelta.referencia == "ord-123" and vuelta.porque == "para un aula"
    assert vuelta.preguntar_porque is True and vuelta.intencion_activa is True
    assert vuelta.veredictos == compra.veredictos
    assert vuelta.aprobacion_pendiente == compra.aprobacion_pendiente
    # La copia no comparte listas con la original.
    vuelta.veredictos.append({"x": 1})
    assert len(compra.veredictos) == 1


async def test_a_fila_no_comparte_referencias():
    compra = crear(RegistroCompras())
    compra.registrar_veredicto({"status": "aprobado"})
    fila = compra.a_fila()
    fila["datos"]["veredictos"][0]["status"] = "bloqueado"
    assert compra.veredictos[0]["status"] == "aprobado"


async def test_esperando_decision_vuelve_vencida():
    compra = crear(RegistroCompras())
    compra.estado = "esperando_decision"
    compra.oportunidad = oportunidad()
    compra.fijar_plazo(300)
    vuelta = Compra.desde_fila(compra.a_fila())
    assert vuelta.estado == "vencida" and vuelta.decision == "vencida"
    assert vuelta.vence is None
    assert vuelta.resuelta.is_set() and vuelta.contexto_listo.is_set()
    assert "vencida" in ESTADOS_TERMINALES_API
    # Nadie puede decidir sobre una compra vencida (409 en la API).
    registro = RegistroCompras()
    registro.restaurar(vuelta)
    with pytest.raises(EstadoInvalido):
        registro.decidir(vuelta.id, "aceptar")


async def test_esperando_contexto_vuelve_negociando_sin_contexto():
    compra = crear(RegistroCompras(), preguntar_porque=True)
    compra.nueva_espera("¿Para qué las necesitás?")
    compra.fijar_plazo(300)
    compra.contexto = contexto()  # aunque hubiera uno a medias
    vuelta = Compra.desde_fila(compra.a_fila())
    assert vuelta.estado == "negociando"
    assert vuelta.contexto is None
    assert vuelta.pregunta == "" and vuelta.vence is None
    assert vuelta.contexto_listo.is_set() and vuelta.resuelta.is_set()
    registro = RegistroCompras()
    registro.restaurar(vuelta)
    with pytest.raises(EstadoInvalido):
        registro.responder(vuelta.id, "tarde")


async def test_desde_fila_crea_futures_nuevos_en_el_loop_actual():
    compra = crear(RegistroCompras())
    vuelta = Compra.desde_fila(compra.a_fila())
    assert vuelta._respuesta is not compra._respuesta
    assert vuelta._decision is not compra._decision
    assert not vuelta._respuesta.done() and not vuelta._decision.done()
    assert vuelta._respuesta.get_loop() is asyncio.get_running_loop()


async def test_creada_vuelve_creada_y_sin_contexto_listo():
    """La etapa de contexto todavía no corrió: la va a correr la primera búsqueda."""
    vuelta = Compra.desde_fila(crear(RegistroCompras()).a_fila())
    assert vuelta.estado == "creada"
    assert not vuelta.contexto_listo.is_set()
    assert vuelta.resuelta.is_set()


@pytest.mark.parametrize("estado", ["negociando", "sin_oportunidad", "bloqueada", "rechazada"])
async def test_los_demas_estados_vuelven_iguales(estado):
    compra = crear(RegistroCompras())
    compra.estado = estado
    vuelta = Compra.desde_fila(compra.a_fila())
    assert vuelta.estado == estado
    assert vuelta.contexto_listo.is_set() and vuelta.resuelta.is_set()


@pytest.mark.parametrize(
    "romper",
    [
        lambda f: f.update(id="cmp-../../x"),
        lambda f: f.update(estado="inventado"),
        lambda f: f.update(creada="ayer"),
        lambda f: f.pop("datos"),
        lambda f: f.update(datos="no es un objeto"),
        lambda f: f["datos"].update(ficha={"requisitos": "no"}),
        lambda f: f["datos"].update(oportunidad={"id": "op-1"}),
        lambda f: f["datos"].update(contexto={"pedido": 3}),
        lambda f: f["datos"].update(requisitos_autorizados=[{"campo": "precio"}]),
        lambda f: f["datos"].update(decision="quizas"),
        lambda f: f["datos"].update(origen="otro"),
        lambda f: f["datos"].update(veredictos="x"),
        lambda f: f["datos"].update(aprobacion_pendiente=[1]),
        lambda f: f["datos"].pop("pedido"),
    ],
)
async def test_fila_corrupta_da_error_almacen(romper):
    fila = crear(RegistroCompras()).a_fila()
    romper(fila)
    with pytest.raises(ErrorAlmacen):
        Compra.desde_fila(fila)


async def test_fila_con_fecha_sin_zona_se_toma_como_utc():
    fila = crear(RegistroCompras()).a_fila()
    fila["creada"] = "2026-10-18T12:00:00"
    vuelta = Compra.desde_fila(fila)
    assert vuelta.creada == datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


# -- veredictos y foto -------------------------------------------------------------


async def test_registrar_veredicto_recorta_y_toca(monkeypatch):
    _reloj_que_avanza(monkeypatch)
    compra = crear(RegistroCompras())
    compra.actualizada = compra.creada - timedelta(hours=1)
    for i in range(MAX_VEREDICTOS_POR_COMPRA + 5):
        compra.registrar_veredicto({"n": i})
    assert len(compra.veredictos) == MAX_VEREDICTOS_POR_COMPRA == 20
    assert compra.veredictos[0] == {"n": 5}
    assert compra.veredictos[-1] == {"n": 24}
    assert compra.actualizada > compra.creada


async def test_foto():
    registro = RegistroCompras()
    compra = crear(registro)
    registro.atar(compra, "ctx-1")
    registro.atar(compra, "ctx-2")
    compra.registrar_veredicto({"status": "aprobado"})
    foto = compra.foto(contextos=registro.contextos_de(compra.id),
                       aprobaciones=[{"id": "apr-1"}])
    assert isinstance(foto, FotoCompra)
    assert foto.compra_id == compra.id
    assert foto.integrador_id == "acme" and foto.comercio_id == "merchant_1"
    assert foto.referencia == "ord-123"
    assert foto.estado == "creada"
    assert foto.contextos == ("ctx-1", "ctx-2")
    assert foto.ficha == ficha_api()
    assert foto.intencion_activa is True
    assert foto.pregunta is None
    assert foto.veredictos == ({"status": "aprobado"},)
    assert foto.aprobaciones == ({"id": "apr-1"},)
    assert foto.actualizada == compra.actualizada
    # Inmutable y sin referencias a la compra.
    foto.veredictos[0]["status"] = "bloqueado"
    assert compra.veredictos[0]["status"] == "aprobado"


async def test_foto_muestra_la_pregunta_solo_mientras_se_espera():
    compra = crear(RegistroCompras(), preguntar_porque=True)
    compra.nueva_espera("¿Para qué?")
    vence = compra.fijar_plazo(60)
    foto = compra.foto(contextos=[], aprobaciones=[])
    assert foto.estado == "esperando_contexto"
    assert foto.pregunta == "¿Para qué?" and foto.vence == vence
    compra.estado = "negociando"
    assert compra.foto(contextos=[], aprobaciones=[]).pregunta is None


async def test_foto_sin_ficha_resuelta_no_muestra_la_del_llm():
    registro = RegistroCompras()
    compra = registro.crear("pedido", "¿para qué?", ficha_demo(), "ctx-1")
    assert compra.foto(contextos=[], aprobaciones=[]).ficha is None


# -- registro: restaurar, contextos_de, de_integrador --------------------------------------


async def test_restaurar_no_duplica_y_gana_la_de_memoria():
    registro = RegistroCompras()
    compra = crear(registro)
    copia = Compra.desde_fila(compra.a_fila())
    assert registro.restaurar(copia) is compra
    assert len(registro.de_integrador("acme")) == 1


async def test_restaurar_pone_la_compra_y_su_contexto():
    origen = RegistroCompras()
    compra = crear(origen)
    origen.atar(compra, "ctx-1")
    registro = RegistroCompras()
    vuelta = registro.restaurar(Compra.desde_fila(compra.a_fila()))
    assert registro.get(compra.id) is vuelta
    assert registro.de_contexto("ctx-1") is vuelta
    assert registro.contextos_de(compra.id) == ["ctx-1"]


async def test_restaurar_no_le_quita_el_contexto_a_otra_compra():
    origen = RegistroCompras()
    compra = crear(origen)
    origen.atar(compra, "ctx-1")
    registro = RegistroCompras()
    otra = crear(registro)
    registro.atar(otra, "ctx-1")
    registro.restaurar(Compra.desde_fila(compra.a_fila()))
    assert registro.de_contexto("ctx-1") is otra


async def test_contextos_de_y_de_integrador():
    registro = RegistroCompras()
    a = crear(registro)
    b = crear(registro, integrador_id="otro")
    c = crear(registro)
    registro.atar(a, "ctx-a1")
    registro.atar(b, "ctx-b")
    registro.atar(a, "ctx-a2")
    assert registro.contextos_de(a.id) == ["ctx-a1", "ctx-a2"]
    assert registro.contextos_de(c.id) == []
    assert registro.contextos_de("cmp-0000000000000000") == []
    c.creada = a.creada + timedelta(seconds=1)
    assert registro.de_integrador("acme") == [c, a]
    assert registro.de_integrador("otro") == [b]
    assert registro.de_integrador("nadie") == []


async def test_de_integrador_ordena_por_creacion_no_por_restauracion():
    registro = RegistroCompras()
    vieja = crear(RegistroCompras())
    vieja.creada -= timedelta(hours=1)
    nueva = crear(registro)
    registro.restaurar(Compra.desde_fila(vieja.a_fila()))
    assert [c.id for c in registro.de_integrador("acme")] == [nueva.id, vieja.id]


# -- aprobaciones ----------------------------------------------------------------------


def _escalada(registro: RegistroAprobaciones, ctx: str = "ctx-1", cart: str = "cart_2"):
    registro.registrar_veredicto(ctx, cart, "escalar", 145000, "USD")
    return registro.aprobar(ctx, cart)[0]


def test_ultimo_y_pendiente():
    registro = RegistroAprobaciones()
    assert registro.ultimo("ctx-1", "cart_2") is None
    assert registro.pendiente("ctx-1", "cart_2") is None
    aprobacion = _escalada(registro)
    ultimo = registro.ultimo("ctx-1", "cart_2")
    assert (ultimo.status, ultimo.centavos, ultimo.moneda) == ("escalar", 145000, "USD")
    assert registro.pendiente("ctx-1", "cart_2") == aprobacion
    registro.consumir("ctx-1", "cart_2", 145000, "USD")
    assert registro.pendiente("ctx-1", "cart_2") is None


def test_restaurar_pendiente_la_deja_disponible():
    registro = RegistroAprobaciones()
    aprobacion = Aprobacion(id="apr-1", contexto_id="ctx-1", cart_id="cart_2",
                            centavos=145000, moneda="USD", creada="2026-10-18T12:00:00+00:00")
    registro.restaurar_pendiente(aprobacion)
    assert registro.pendiente("ctx-1", "cart_2") == aprobacion
    assert registro.disponible("ctx-1", "cart_2", 145000, "USD") == aprobacion
    assert registro.disponible("ctx-1", "cart_2", 145001, "USD") is None


def test_restaurar_pendiente_no_pisa_la_de_memoria():
    registro = RegistroAprobaciones()
    en_memoria = _escalada(registro)
    otra = Aprobacion(id="apr-vieja", contexto_id="ctx-1", cart_id="cart_2",
                      centavos=1, moneda="USD", creada="2026-10-18T00:00:00+00:00")
    registro.restaurar_pendiente(otra)
    assert registro.pendiente("ctx-1", "cart_2") == en_memoria


def test_restaurar_pendiente_ignora_las_usadas():
    registro = RegistroAprobaciones()
    usada = Aprobacion(id="apr-1", contexto_id="ctx-1", cart_id="cart_2", centavos=1,
                       moneda="USD", creada="x", estado="usada", usada="y")
    registro.restaurar_pendiente(usada)
    assert registro.pendiente("ctx-1", "cart_2") is None


def test_restaurar_pendiente_no_revive_una_usada_en_este_proceso():
    registro = RegistroAprobaciones()
    aprobacion = _escalada(registro)
    assert registro.consumir("ctx-1", "cart_2", 145000, "USD") is not None
    registro.restaurar_pendiente(aprobacion)  # la fila vieja del almacén
    assert registro.pendiente("ctx-1", "cart_2") is None


def test_descartar():
    registro = RegistroAprobaciones()
    aprobacion = _escalada(registro)
    assert registro.descartar("apr-inexistente") is False
    assert registro.descartar(aprobacion.id) is True
    assert registro.pendiente("ctx-1", "cart_2") is None
    assert registro.descartar(aprobacion.id) is False
    assert registro.listar()["pendientes"] == []


def test_aprobacion_desde_fila_ida_y_vuelta():
    aprobacion = Aprobacion(id="apr-1", contexto_id="ctx-1", cart_id="cart_2",
                            centavos=145000, moneda="USD", creada="2026-10-18T12:00:00+00:00")
    fila = fila_aprobacion_gw(aprobacion, integrador_id="acme", compra_id="cmp-0123456789abcdef")
    assert fila["integrador_id"] == "acme" and fila["compra_id"] == "cmp-0123456789abcdef"
    # gw_aprobaciones solo la escribe /v1: vuelve marcada como del dueño.
    assert aprobacion_desde_fila(fila) == replace(aprobacion, via_api=True)


@pytest.mark.parametrize(
    "cambio",
    [
        {"id": "apr 1; drop"},
        {"contexto_id": None},
        {"centavos": "145000"},
        {"centavos": True},
        {"centavos": 1.5},
        {"moneda": None},
        {"estado": "quizas"},
        {"creada": 3},
        {"task_id": 7},
    ],
)
def test_aprobacion_desde_fila_corrupta(cambio):
    fila = {"id": "apr-1", "contexto_id": "ctx-1", "cart_id": "cart_2", "centavos": 1,
            "moneda": "USD", "estado": "pendiente", "creada": "2026-10-18T12:00:00+00:00",
            "usada": None, "task_id": None}
    fila.update(cambio)
    with pytest.raises(ErrorAlmacen):
        aprobacion_desde_fila(fila)
