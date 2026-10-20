"""AgenteDecisiones: el protocolo `Agente` con juicios tipados (decisiones/).

Se prueba con un cliente de decisiones falso: qué preguntas arma, que el
intake y el contexto de un mismo turno salen de una sola llamada, que la
necesidad son las palabras del usuario y que la detección de oportunidades
se delega (sin detector, no hay).
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from contracts import Contexto, Entrega, Ficha, Requisito, Turno
from decisiones import DecisionesError, Rechazo, RespuestaEleccion, RespuestaPredicado
from intencion.agente_decisiones import (
    AgenteDecisiones,
    PreguntaIntake,
    cronograma_candidato,
    requisitos_verificables,
)
from intencion.intake import PORQUE

HOY = date(2026, 10, 18)  # domingo
CATEGORIAS = ["basketball_shoes", "laptops"]
PEDIDO = "Comprame 20 computadoras para el lunes, hasta 20000 USD."
RESPUESTA = "Entran 10 trabajadores el lunes y otros 10 el jueves."


def eleccion(opcion: str, p: float) -> RespuestaEleccion:
    return RespuestaEleccion("x", opcion, {opcion: p}, p)


class DecisionesFalso:
    nombre = "falso"
    modelo = "falso-1"

    def __init__(self, responder=None) -> None:
        self.responder = responder or (lambda estado, qs: {})
        self.pedidos: list[tuple[Any, list]] = []

    def decidir(self, estado, preguntas):
        self.pedidos.append((estado, preguntas))
        if isinstance(self.responder, Exception):
            raise self.responder
        salida = self.responder(estado, preguntas)
        return {p.nombre: salida.get(p.nombre, Rechazo(p.nombre)) for p in preguntas}


def _opcion(qs, nombre: str, contiene: str) -> str:
    q = next(q for q in qs if q.nombre == nombre)
    return next(k for k, d in q.opciones.items() if contiene in (d or ""))


def responder_demo(estado, qs) -> dict[str, Any]:
    """Lo que contestaría el modelo en la demo de las computadoras."""
    nombres = {q.nombre for q in qs}
    r: dict[str, Any] = {
        "categoria": eleccion("laptops", 0.95),
        "cantidad": eleccion(_opcion(qs, "cantidad", "20 ("), 0.95),
        "precio": eleccion(_opcion(qs, "precio", "20000"), 0.97),
        "precio_alcance": eleccion("total", 0.9),
        "moneda": eleccion("USD", 0.98),
        "plazo": eleccion(_opcion(qs, "plazo", "2026-10-19"), 0.9),
        "plazo_firmeza": eleccion("exige", 0.9),
    }
    for n in nombres:
        if n.startswith("cron_"):
            r[n] = RespuestaPredicado(n, 0.93)
        if n == "flex_entregas_parciales":
            r[n] = eleccion("inferida", 0.8)
        if n == "flex_otra_marca":
            r[n] = eleccion("no", 0.9)
    return r


# -- preguntar ------------------------------------------------------------------------


def test_preguntar_arma_la_pregunta_por_plantilla_y_los_parametros_dichos():
    cliente = DecisionesFalso(responder_demo)
    agente = AgenteDecisiones(cliente)
    pregunta = agente.preguntar(PEDIDO, HOY, CATEGORIAS)
    assert isinstance(pregunta, PreguntaIntake)
    assert pregunta.pregunta == PORQUE["es"]  # no falta nada: solo el porqué
    campos = {r.campo: r.valor for r in pregunta.requisitos}
    assert campos["precio"] == 20000.0 and campos["moneda"] == "USD"
    assert campos["cantidad"] == 20
    # "computadoras" es la rama general (278): el modelo eligió laptops, pero
    # eso achica lo que dijo el usuario. Sin requisito: la rama la verifica el Guard.
    assert "categoria" not in campos
    assert pregunta.intake is not None and pregunta.intake.completo
    categoria = next(p for p in pregunta.intake.parametros if p.nombre == "categoria")
    assert (categoria.estado, categoria.eleccion, categoria.valor) == ("resuelto", "278", "computadoras")
    assert len(cliente.pedidos) == 1
    # Sin respuestas todavía no hay preguntas de contexto.
    assert not any(q.nombre.startswith(("cron_", "flex_")) for q in cliente.pedidos[0][1])


def test_preguntar_sin_datos_pide_lo_que_falta():
    agente = AgenteDecisiones(DecisionesFalso())  # todo Rechazo = no sé
    pregunta = agente.preguntar("Necesito algo", HOY, CATEGORIAS)
    assert pregunta.pregunta.startswith(PORQUE["es"])
    assert "me falta saber" in pregunta.pregunta
    assert pregunta.requisitos == []


def test_preguntar_propaga_la_falla_del_cliente():
    agente = AgenteDecisiones(DecisionesFalso(DecisionesError("caído")))
    with pytest.raises(DecisionesError):
        agente.preguntar(PEDIDO, HOY, CATEGORIAS)


# -- contexto e intake del mismo turno: una sola llamada ---------------------------------


def test_intake_y_contexto_del_mismo_turno_son_una_llamada():
    cliente = DecisionesFalso(responder_demo)
    agente = AgenteDecisiones(cliente)
    agente.preguntar(PEDIDO, HOY, CATEGORIAS)
    turnos = [Turno(pregunta=PORQUE["es"], respuesta=RESPUESTA)]
    resultado = agente.evaluar_intake(PEDIDO, turnos, HOY, CATEGORIAS)
    contexto = agente.armar_contexto(PEDIDO, PORQUE["es"], RESPUESTA, HOY)
    assert len(cliente.pedidos) == 2  # preguntar + este turno
    assert resultado.respondidos == 1 and resultado.siguiente == "confirmar"
    assert contexto.respuesta == RESPUESTA
    # Las categorías del primer turno se recuerdan para el pedido.
    qs = cliente.pedidos[1][1]
    categoria = next(q for q in qs if q.nombre == "categoria")
    assert "laptops" in categoria.opciones


def test_el_turno_siguiente_recuerda_lo_resuelto():
    """Si el modelo duda de un valor que ya estaba resuelto (y lo sigue
    eligiendo), no se vuelve a preguntar."""

    def responder(estado, qs):
        r = responder_demo(estado, qs)
        if estado["conversacion"]:
            clave = _opcion(qs, "precio", "20000")
            r["precio"] = RespuestaEleccion("precio", clave, {clave: 0.52, "no_dicho": 0.48}, 0.04)
        return r

    agente = AgenteDecisiones(DecisionesFalso(responder))
    agente.preguntar(PEDIDO, HOY, CATEGORIAS)
    r = agente.evaluar_intake(PEDIDO, [Turno(pregunta=PORQUE["es"], respuesta=RESPUESTA)], HOY, CATEGORIAS)
    assert "precio" not in r.faltan
    # Otro agente, sin el turno anterior en su caché: lo recalcula y recuerda igual.
    cliente = DecisionesFalso(responder)
    otro = AgenteDecisiones(cliente)
    r = otro.evaluar_intake(PEDIDO, [Turno(pregunta=PORQUE["es"], respuesta=RESPUESTA)], HOY, CATEGORIAS)
    assert "precio" not in r.faltan
    assert [len(estado["conversacion"]) for estado, _ in cliente.pedidos] == [0, 1]


def test_el_orden_no_importa_contexto_primero():
    cliente = DecisionesFalso(responder_demo)
    agente = AgenteDecisiones(cliente)
    agente.preguntar(PEDIDO, HOY, CATEGORIAS)
    agente.armar_contexto(PEDIDO, PORQUE["es"], RESPUESTA, HOY)
    agente.evaluar_intake(PEDIDO, [Turno(pregunta=PORQUE["es"], respuesta=RESPUESTA)], HOY, CATEGORIAS)
    assert len(cliente.pedidos) == 2


def test_armar_contexto_con_la_necesidad_del_usuario_y_el_cronograma():
    agente = AgenteDecisiones(DecisionesFalso(responder_demo))
    contexto = agente.armar_contexto(PEDIDO, PORQUE["es"], RESPUESTA, HOY)
    assert isinstance(contexto, Contexto)
    assert contexto.necesidad == RESPUESTA  # sus palabras, no un texto del modelo
    assert contexto.necesidad_entregas == [
        Entrega(cantidad=10, fecha=date(2026, 10, 19)),
        Entrega(cantidad=10, fecha=date(2026, 10, 22)),
    ]
    flex = {(f.descripcion, f.origen) for f in contexto.flexibilidades}
    assert len(flex) == 1 and next(iter(flex))[1] == "inferida"
    assert contexto.historial == []


def test_armar_contexto_con_historial():
    agente = AgenteDecisiones(DecisionesFalso(responder_demo))
    historial = [Turno(pregunta="Para hacer la compra me falta saber X.", respuesta="Hasta 20000 USD")]
    contexto = agente.armar_contexto(PEDIDO, PORQUE["es"], RESPUESTA, HOY, historial=historial)
    assert contexto.historial == historial
    estado = agente.cliente.pedidos[-1][0]
    assert [t["respuesta"] for t in estado["conversacion"]] == [RESPUESTA, "Hasta 20000 USD"]


def test_cronograma_con_baja_probabilidad_no_entra():
    def responder(estado, qs):
        r = responder_demo(estado, qs)
        for n in list(r):
            if n.startswith("cron_"):
                r[n] = RespuestaPredicado(n, 0.2)
        return r

    contexto = AgenteDecisiones(DecisionesFalso(responder)).armar_contexto(
        PEDIDO, PORQUE["es"], RESPUESTA, HOY
    )
    assert contexto.necesidad_entregas == []


def test_las_flexibilidades_dichas_quedan_atadas_a_su_campo():
    """"me da igual la marca y si llegan unos días después": marca y entregas
    cambiables (`campos_cambiables` lee la etiqueta "Campo: …")."""
    from intencion.agente import campo_de_flexibilidad, campos_cambiables
    from intencion.agente_decisiones import interpretar_contexto
    from intencion.intake import UMBRALES

    turnos = [Turno(pregunta="¿Para qué?",
                    respuesta="Para la oficina, me da igual la marca y si llegan unos días después.")]
    respuestas = {
        "flex_otra_marca": eleccion("dicha", 0.9),
        "flex_otra_fecha": eleccion("dicha", 0.9),
        "flex_otra_cantidad": eleccion("no", 0.9),
    }
    contexto = interpretar_contexto(respuestas, PEDIDO, turnos, [], HOY, UMBRALES)
    assert sorted(campo_de_flexibilidad(f) for f in contexto.flexibilidades) == ["entregas", "marca"]
    assert campos_cambiables(contexto) == {"entregas", "marca"}


@pytest.mark.parametrize(
    "clave, campo",
    [("otra_marca", "marca"), ("otra_cantidad", "cantidad"),
     ("otra_fecha", "entregas"), ("entregas_parciales", "entregas")],
)
def test_cada_flexibilidad_tiene_su_campo(clave, campo):
    from intencion.agente import campo_de_flexibilidad
    from intencion.agente_decisiones import interpretar_contexto
    from intencion.intake import UMBRALES

    turnos = [Turno(pregunta="¿Para qué?", respuesta="Para la oficina.")]
    contexto = interpretar_contexto(
        {f"flex_{clave}": eleccion("dicha", 0.95)}, PEDIDO, turnos, [], HOY, UMBRALES
    )
    (flexibilidad,) = contexto.flexibilidades
    assert campo_de_flexibilidad(flexibilidad) == campo


def test_una_flexibilidad_dicha_con_duda_cuenta_como_inferida():
    """Sin cita textual (el modelo solo elige), "dicha" pide la confianza de lo
    resuelto: entre `duda` y `resuelto` es inferida y no habilita cambios."""
    from intencion.agente import campos_cambiables
    from intencion.agente_decisiones import interpretar_contexto
    from intencion.intake import UMBRALES

    turnos = [Turno(pregunta="¿Para qué?", respuesta="Para la oficina, la marca no importa tanto.")]
    contexto = interpretar_contexto(
        {"flex_otra_marca": eleccion("dicha", 0.7)}, PEDIDO, turnos, [], HOY, UMBRALES
    )
    assert [f.origen for f in contexto.flexibilidades] == ["inferida"]
    assert campos_cambiables(contexto) == set()


def test_armar_contexto_sin_respuesta_es_error():
    with pytest.raises(ValueError):
        AgenteDecisiones(DecisionesFalso()).armar_contexto(PEDIDO, "¿Para qué?", "  ", HOY)


@pytest.mark.parametrize(
    "texto, pares",
    [
        (RESPUESTA, [(10, "2026-10-19"), (10, "2026-10-22")]),
        ("10 people start on Monday and 10 more on Thursday.", [(10, "2026-10-19"), (10, "2026-10-22")]),
        ("Es para mí.", []),
        ("Necesito 20 para el lunes", [(20, "2026-10-19")]),
    ],
)
def test_cronograma_candidato(texto, pares):
    assert [(c, f) for c, f, _ in cronograma_candidato([texto], HOY)] == pares


@pytest.mark.parametrize(
    "texto",
    [
        # "dos" y "10" en la misma cláusula: el par del lunes no se forma.
        "Hay dos grupos: 10 empiezan el lunes y 10 el jueves",
        # "a las 9" también es un número: ninguna cláusula forma su par.
        "entran 10 a las 9 del lunes y otros 10 a las 9 del jueves",
    ],
)
def test_un_cronograma_a_medias_no_se_arma(texto):
    """Con solo "10 el jueves", el cronograma sería más laxo que lo que contó
    (habilitaría una oferta que no llega el lunes): sin cronograma."""
    assert cronograma_candidato([texto], HOY) == []


def test_un_cronograma_a_medias_se_descarta_solo_en_esa_respuesta():
    respuestas = ["Hay dos grupos: 10 empiezan el lunes y 10 el jueves", "Necesito 20 para el lunes"]
    assert [(c, f) for c, f, _ in cronograma_candidato(respuestas, HOY)] == [(20, "2026-10-19")]


# -- oportunidad: se delega ----------------------------------------------------------------


class DetectorFalso:
    def __init__(self) -> None:
        self.vistos: list[tuple[Contexto, Ficha, list]] = []

    def detectar_oportunidad(self, contexto, ficha, ofertas):
        self.vistos.append((contexto, ficha, ofertas))
        return "oportunidad"


def _contexto() -> Contexto:
    return Contexto(
        pedido=PEDIDO, pregunta=PORQUE["es"], respuesta=RESPUESTA,
        historial=[Turno(pregunta="¿Tope?", respuesta="Hasta 20000 USD")],
    )


def test_sin_detector_no_hay_oportunidad():
    agente = AgenteDecisiones(DecisionesFalso())
    assert agente.detectar_oportunidad(_contexto(), Ficha(pedido_original="x", requisitos=[]), []) is None


def test_la_oportunidad_se_delega_con_todas_las_respuestas_y_la_ficha_verificable():
    detector = DetectorFalso()
    agente = AgenteDecisiones(DecisionesFalso(), detector=detector)
    ficha = Ficha(
        pedido_original="x",
        requisitos=[
            Requisito(campo="precio", operador="maximo", valor=100.0, obligatorio=True),
            Requisito(campo="atributo", atributo="memoria_ram", operador="minimo", valor=16,
                      unidad="GB", obligatorio=True),
        ],
        confirmada=True,
    )
    assert agente.detectar_oportunidad(_contexto(), ficha, ["o"]) == "oportunidad"
    ((contexto, ficha_vista, ofertas),) = detector.vistos
    assert contexto.respuesta == f"{RESPUESTA} / Hasta 20000 USD"
    # El Guard chequea atributos y `minimo`: al detector le llega la ficha entera.
    assert [r.campo for r in ficha_vista.requisitos] == ["precio", "atributo"]
    assert ofertas == ["o"]


_REQS = [
    Requisito(campo="precio", operador="maximo", valor=100.0, obligatorio=True),
    Requisito(campo="talle", operador="incluye", valor=42, obligatorio=True),
    Requisito(campo="atributo", atributo="color", operador="igual", valor="negro", obligatorio=True),
    Requisito(campo="talle", operador="minimo", valor=40, obligatorio=False),
]


def test_requisitos_verificables_el_guard_chequea_atributos_y_minimo():
    si, no = requisitos_verificables(_REQS)
    assert si == _REQS and no == []


def test_requisitos_verificables_lee_lo_que_publica_el_guard(monkeypatch):
    """Si el Guard no supiera chequear algo, no va a los parámetros autorizados."""
    from guard import chequeos

    monkeypatch.setattr(chequeos, "CAMPOS_SOPORTADOS", frozenset({"precio", "talle"}))
    monkeypatch.setattr(chequeos, "OPERADORES_SOPORTADOS", frozenset({"maximo", "incluye"}))
    si, no = requisitos_verificables(_REQS)
    assert [r.campo for r in si] == ["precio", "talle"]
    assert [(r.campo, r.operador) for r in no] == [("atributo", "igual"), ("talle", "minimo")]


def test_nombre_y_modelo():
    agente = AgenteDecisiones(DecisionesFalso())
    assert agente.nombre == "decisiones"
    assert agente.modelo == "falso/falso-1"
