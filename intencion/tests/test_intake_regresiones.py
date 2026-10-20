"""Regresiones del intake vistas de punta a punta (comercio general, 2026-10-19).

Pedido real: "Comprame 20 computadoras para el lunes, hasta 20000 USD." (al
intake le llega con la búsqueda del agente, `"<usuario> / <búsqueda>"`),
dicho un lunes, con el catálogo del comercio general (laptops, desktops,
tablets…). Lo que pasó:

1. **El intake des-resolvía lo que ya sabía.** El plazo (lunes 26/10)
   quedó resuelto y, ante una respuesta que no hablaba de la fecha ("Entran
   10 trabajadores el lunes y otros 10 el jueves."), pasó a faltante: la
   ficha se confirmó sin plazo y el Guard aprobó una oferta que llegaba
   tarde. Regla: lo que el usuario dijo y quedó resuelto no se pierde porque
   una respuesta no lo mencione; solo cambia si lo corrige.
2. **No aceptaba una categoría general.** "computadoras" no es una
   categoría del catálogo (laptops, desktops, tablets): repreguntó 3 veces
   "qué producto es exactamente". Es una categoría de la taxonomía de Google
   (278, padre de 328, 325 y 4745): queda resuelta con esa.
3. **"el lunes" dicho un lunes** es el de la semana que viene, igual en el
   pedido y en las respuestas; en un mismo texto, un día que sigue a otra
   fecha no cae antes que ella ("el lunes y otros 10 el jueves": el jueves
   después de ese lunes).

El modelo se finge con las probabilidades que dio OpenAI Decisions en esa
corrida (`.logs/guard_events.jsonl`).
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from contracts import Turno
from decisiones import Predicado, Rechazo, RespuestaEleccion, RespuestaPredicado
from intencion import intake
from intencion.agente_decisiones import AgenteDecisiones, cronograma_candidato
from intencion.intake import extraer, interpretar, preguntas, taxonomia_de_categoria

LUNES = date(2026, 10, 19)
DOMINGO = date(2026, 10, 18)
JUEVES = date(2026, 10, 22)
PEDIDO = (
    "Comprame 20 computadoras para el lunes, hasta 20000 USD. / "
    "20 computadoras por un total de 20000 USD"
)
RESPUESTA = "Entran 10 trabajadores el lunes y otros 10 el jueves."
# El catálogo del comercio general, como lo ordena el proxy para este pedido
# (`proxy.fuentes.categorias_para_el_pedido`: primero las de su rama).
CATALOGO = [
    "desktops", "laptops", "tablets", "backpacks", "coffee_makers", "desks",
    "digital_cameras", "game_consoles", "hard_drives", "headphones", "headsets",
    "keyboards", "mice", "mobile_phones", "monitors", "office_chairs", "printers",
    "projectors", "routers", "shoes", "speakers", "televisions", "usb_drives", "webcams",
]
COMPUTADORAS, LAPTOPS, CAMARAS, CAFETERAS = "278", "328", "142", "736"


def eleccion(nombre: str, opcion: str, p: float, **otras: float) -> RespuestaEleccion:
    return RespuestaEleccion(nombre, opcion, {opcion: p, **otras}, p)


def clave_de(lista: list, valor: Any) -> str:
    return next(c.clave for c in lista if c.valor == valor)


def parametro(resultado, nombre: str):
    return next(p for p in resultado.parametros if p.nombre == nombre)


def respuestas_del_log(cands, turno: int) -> dict[str, Any]:
    """Lo que eligió el modelo en cada turno de la corrida real."""
    lunes = clave_de(cands.fechas, "2026-10-26")
    r = {
        "categoria": eleccion("categoria", "no_dicha", (0.59, 0.65, 0.61, 0.9, 0.95)[turno]),
        "cantidad": eleccion("cantidad", clave_de(cands.cantidades, 20), 0.96),
        "precio": eleccion("precio", clave_de(cands.montos, 20000.0), 0.59 if turno == 0 else 0.84),
        "precio_alcance": eleccion("precio_alcance", "total", 0.9),
        "moneda": eleccion("moneda", "USD", 0.96),
        "plazo_firmeza": eleccion("plazo_firmeza", "exige", 0.85),
    }
    if turno == 3:
        r["plazo"] = eleccion("plazo", "no_dicho", 0.53)
    else:
        r["plazo"] = eleccion("plazo", lunes, (0.78, 0.93, 0.93, None, 0.5)[turno])
    return r


# -- 1. lo resuelto no se pierde ------------------------------------------------------------


def test_el_plazo_resuelto_no_se_pierde_en_la_conversacion_real():
    anterior = None
    for turno in range(5):
        textos = [PEDIDO, *[RESPUESTA] * turno]
        cands = extraer(textos, LUNES)
        r = interpretar(respuestas_del_log(cands, turno), cands, CATALOGO, turno, anterior=anterior)
        plazo = parametro(r, "plazo")
        if turno == 0:
            assert plazo.estado == "a_confirmar"  # 0,78: dicho, pero con dudas
        else:
            assert (plazo.estado, plazo.valor) == ("resuelto", "2026-10-26"), turno
        assert "plazo" not in r.faltan
        anterior = r
    (entregas,) = [q for q in r.requisitos_autorizables(False) if q.campo == "entregas"]
    assert entregas.valor == [{"cantidad": 20, "fecha": "2026-10-26"}]
    assert entregas.obligatorio


def test_un_valor_distinto_sin_correccion_no_pisa_lo_resuelto():
    cands0 = extraer([PEDIDO], LUNES)
    antes = interpretar(respuestas_del_log(cands0, 1), cands0, CATALOGO, 0)
    cands = extraer([PEDIDO, RESPUESTA], LUNES)
    jueves = clave_de(cands.fechas, "2026-10-29")
    ahora = respuestas_del_log(cands, 1)
    ahora["plazo"] = eleccion("plazo", jueves, 0.85)  # el modelo se confunde de fecha
    ahora["corrige_plazo"] = RespuestaPredicado("corrige_plazo", 0.1)
    plazo = parametro(interpretar(ahora, cands, CATALOGO, 1, anterior=antes), "plazo")
    assert (plazo.estado, plazo.valor) == ("resuelto", "2026-10-26")
    # Sin respuesta a la pregunta de corrección (rechazo), tampoco.
    ahora["corrige_plazo"] = Rechazo("corrige_plazo")
    plazo = parametro(interpretar(ahora, cands, CATALOGO, 1, anterior=antes), "plazo")
    assert plazo.valor == "2026-10-26"


def test_una_correccion_explicita_cambia_lo_resuelto():
    cands = extraer(["hasta 200 dólares", "no, mejor hasta 250"], DOMINGO)
    m200, m250 = clave_de(cands.montos, 200.0), clave_de(cands.montos, 250.0)
    antes = interpretar({"precio": eleccion("precio", m200, 0.95)}, cands, [], 0)
    corrige = {"corrige_precio": RespuestaPredicado("corrige_precio", 0.93)}
    r = interpretar({"precio": eleccion("precio", m250, 0.9), **corrige}, cands, [], 1, anterior=antes)
    assert (parametro(r, "precio").estado, parametro(r, "precio").valor) == ("resuelto", 250.0)
    # Corrige pero el valor nuevo es dudoso: se vuelve a preguntar.
    r = interpretar({"precio": eleccion("precio", m250, 0.55), **corrige}, cands, [], 1, anterior=antes)
    assert parametro(r, "precio").estado == "faltante"
    # Una corrección dudosa (< 0,80) no alcanza: queda lo que dijo.
    poco = {"corrige_precio": RespuestaPredicado("corrige_precio", 0.7)}
    r = interpretar({"precio": eleccion("precio", m250, 0.9), **poco}, cands, [], 1, anterior=antes)
    assert parametro(r, "precio").valor == 200.0


def test_lo_a_confirmar_no_se_borra_con_no_dicho_y_un_valor_nuevo_claro_lo_reemplaza():
    cands = extraer(["unas zapatillas, $200", "para salir"], DOMINGO)
    m200 = clave_de(cands.montos, 200.0)
    antes = interpretar({"precio": eleccion("precio", m200, 0.7)}, cands, [], 0)
    assert parametro(antes, "precio").estado == "a_confirmar"
    r = interpretar({"precio": eleccion("precio", "no_dicho", 0.9)}, cands, [], 1, anterior=antes)
    assert (parametro(r, "precio").estado, parametro(r, "precio").valor) == ("a_confirmar", 200.0)
    cands = extraer(["unas zapatillas, $200", "mejor hasta 180"], DOMINGO)
    m180 = clave_de(cands.montos, 180.0)
    r = interpretar({"precio": eleccion("precio", m180, 0.9)}, cands, [], 1, anterior=antes)
    assert parametro(r, "precio").valor == 180.0


def test_lo_recordado_se_recalcula_con_la_cantidad_corregida():
    textos = ["20 notebooks de hasta 1000 dólares cada una, para el viernes", "en realidad son 25"]
    cands = extraer(textos, DOMINGO)
    n20, n25 = clave_de(cands.cantidades, 20), clave_de(cands.cantidades, 25)
    m1000, viernes = clave_de(cands.montos, 1000.0), clave_de(cands.fechas, "2026-10-23")
    base = {
        "cantidad": eleccion("cantidad", n20, 0.95),
        "precio": eleccion("precio", m1000, 0.95),
        "precio_alcance": eleccion("precio_alcance", "por_unidad", 0.95),
        "moneda": eleccion("moneda", "USD", 0.95),
        "plazo": eleccion("plazo", viernes, 0.95),
        "plazo_firmeza": eleccion("plazo_firmeza", "exige", 0.9),
    }
    antes = interpretar(base, cands, ["laptops"], 0)
    assert parametro(antes, "precio").valor == 20000.0
    # Corrige la cantidad; del precio y del plazo no dice nada.
    ahora = {
        "cantidad": eleccion("cantidad", n25, 0.95),
        "corrige_cantidad": RespuestaPredicado("corrige_cantidad", 0.95),
        "precio": eleccion("precio", "no_dicho", 0.8),
        "plazo": eleccion("plazo", "no_dicho", 0.8),
    }
    r = interpretar(ahora, cands, ["laptops"], 1, anterior=antes)
    assert parametro(r, "cantidad").valor == 25
    precio = parametro(r, "precio")
    assert (precio.estado, precio.valor) == ("resuelto", 25000.0)  # 1000 por unidad × 25
    (entregas,) = [q for q in r.requisitos if q.campo == "entregas"]
    assert entregas.valor == [{"cantidad": 25, "fecha": "2026-10-23"}]


def test_las_preguntas_de_correccion_son_solo_de_lo_resuelto_que_dijo():
    cands0 = extraer([PEDIDO], LUNES)
    antes = interpretar(respuestas_del_log(cands0, 0), cands0, CATALOGO, 0)
    # Turno 0: plazo a confirmar (0,78) y precio faltante (0,59).
    resueltos = {p.nombre for p in antes.parametros if p.necesario and p.estado == "resuelto"}
    cands = extraer([PEDIDO, RESPUESTA], LUNES)
    qs = {q.nombre: q for q in preguntas(cands, CATALOGO, anterior=antes)}
    corrige = {n.removeprefix("corrige_") for n in qs if n.startswith("corrige_")}
    assert corrige == resueltos == {"categoria", "cantidad", "moneda"}
    assert all(isinstance(qs[f"corrige_{n}"], Predicado) for n in corrige)
    assert "20" in qs["corrige_cantidad"].instrucciones
    assert "última respuesta" in qs["corrige_cantidad"].instrucciones
    # Sin turno anterior no hay preguntas de corrección.
    assert not any(q.nombre.startswith("corrige_") for q in preguntas(cands, CATALOGO))


def test_el_agente_recuerda_aunque_el_turno_anterior_no_este_en_su_cache():
    """Otro agente (o la caché llena): recalcula el turno anterior y recuerda igual."""
    llamadas = []

    def responder(estado, qs):
        turno = len(estado["conversacion"])
        llamadas.append(turno)
        cands = extraer([estado["pedido"], *(t["respuesta"] for t in estado["conversacion"])], LUNES)
        r = respuestas_del_log(cands, 3 if turno else 1)  # el turno 3: "no_dicho" para el plazo
        return {q.nombre: r.get(q.nombre, Rechazo(q.nombre)) for q in qs}

    agente = AgenteDecisiones(_Cliente(responder))
    r = agente.evaluar_intake(PEDIDO, [Turno(pregunta=intake.PORQUE["es"], respuesta=RESPUESTA)],
                              LUNES, CATALOGO)
    assert llamadas == [0, 1]
    assert parametro(r, "plazo").valor == "2026-10-26"


class _Cliente:
    nombre = "falso"
    modelo = "falso-1"

    def __init__(self, responder) -> None:
        self.responder = responder
        self.pedidos: list[tuple[Any, list]] = []

    def decidir(self, estado, qs):
        self.pedidos.append((estado, qs))
        return self.responder(estado, qs)


def test_la_conversacion_real_con_el_agente_no_repregunta_ni_pierde_el_plazo():
    """La corrida del 2026-10-19 con `AgenteDecisiones` (lo que usa el proxy)."""

    def responder(estado, qs):
        turno = len(estado["conversacion"])
        cands = extraer([estado["pedido"], *(t["respuesta"] for t in estado["conversacion"])], LUNES)
        r = respuestas_del_log(cands, turno)
        for q in qs:
            if q.nombre.startswith("cron_"):
                r[q.nombre] = RespuestaPredicado(q.nombre, 0.9)
        return {q.nombre: r.get(q.nombre, Rechazo(q.nombre)) for q in qs}

    cliente = _Cliente(responder)
    agente = AgenteDecisiones(cliente)
    pregunta = agente.preguntar(PEDIDO, LUNES, CATALOGO)
    assert "qué producto" not in pregunta.pregunta
    turnos: list[Turno] = []
    for turno in range(1, 5):  # el usuario contestó lo mismo cuatro veces
        turnos.append(Turno(pregunta="…", respuesta=RESPUESTA))
        r = agente.evaluar_intake(PEDIDO, turnos, LUNES, CATALOGO)
        assert parametro(r, "plazo").valor == "2026-10-26", turno
        assert parametro(r, "categoria").eleccion == COMPUTADORAS
        assert r.faltan == ()
    assert r.siguiente in ("confirmar", "fin")
    contexto = agente.armar_contexto(PEDIDO, "…", RESPUESTA, LUNES)
    assert [(e.cantidad, e.fecha.isoformat()) for e in contexto.necesidad_entregas] == [
        (10, "2026-10-26"), (10, "2026-10-29"),
    ]


# -- 2. una categoría general es un producto -------------------------------------------------


def test_una_categoria_general_queda_resuelta_sin_repreguntar():
    cands = extraer([PEDIDO], LUNES)
    r = interpretar(respuestas_del_log(cands, 0), cands, CATALOGO, 0)
    categoria = parametro(r, "categoria")
    assert (categoria.estado, categoria.eleccion, categoria.origen) == ("resuelto", COMPUTADORAS, "dicho")
    # Lo que se muestra es la palabra (el dashboard pinta `valor`); el ID va en `eleccion`.
    assert categoria.valor == categoria.texto == "computadoras"
    assert categoria.requisito is None  # la rama la verifica el Guard (`categoria_taxonomia`)
    assert "categoria" not in r.faltan and not r.fuera_de_catalogo
    assert "qué producto" not in r.pregunta


def test_se_le_ofrece_al_modelo_la_categoria_general():
    cands = extraer([PEDIDO], LUNES)
    categoria = next(q for q in preguntas(cands, CATALOGO) if q.nombre == "categoria")
    assert "general" in categoria.opciones
    descripcion = categoria.opciones["general"]
    assert "computadoras" in descripcion
    assert all(c in descripcion for c in ("laptops", "desktops", "tablets"))
    assert "monitors" not in descripcion
    assert "computadoras" in categoria.instrucciones
    # Elegida por el modelo: la de la taxonomía.
    r = interpretar({"categoria": eleccion("categoria", "general", 0.7)}, cands, CATALOGO, 0)
    assert (parametro(r, "categoria").estado, parametro(r, "categoria").eleccion) == ("resuelto", COMPUTADORAS)


@pytest.mark.parametrize(
    "pedido",
    ["Comprame 20 notebooks para el lunes", "Comprame una cafetera", "Necesito algo para escuchar música"],
)
def test_no_se_ofrece_si_el_catalogo_ya_la_tiene_o_no_es_de_su_rama(pedido):
    cands = extraer([pedido], LUNES)
    categoria = next(q for q in preguntas(cands, ["laptops", "desktops", "tablets"]) if q.nombre == "categoria")
    assert "general" not in categoria.opciones


def test_el_modelo_no_achica_lo_que_dijo_el_usuario():
    cands = extraer([PEDIDO], LUNES)
    r = interpretar({"categoria": eleccion("categoria", "laptops", 0.95)}, cands, CATALOGO, 0)
    categoria = parametro(r, "categoria")
    assert (categoria.estado, categoria.eleccion, categoria.requisito) == ("resuelto", COMPUTADORAS, None)


def test_si_el_usuario_nombra_la_subcategoria_vale_la_del_catalogo():
    textos = ["Comprame 20 computadoras, notebooks si puede ser, para el lunes"]
    cands = extraer(textos, LUNES)
    r = interpretar({"categoria": eleccion("categoria", "laptops", 0.9)}, cands, CATALOGO, 0)
    assert parametro(r, "categoria").eleccion == "laptops"
    assert parametro(r, "categoria").requisito.valor == "laptops"


def test_una_respuesta_que_precisa_la_categoria_la_cambia_sin_correccion():
    cands0 = extraer([PEDIDO], LUNES)
    antes = interpretar(respuestas_del_log(cands0, 1), cands0, CATALOGO, 0)
    assert parametro(antes, "categoria").eleccion == COMPUTADORAS
    cands = extraer([PEDIDO, "Notebooks, de cualquier marca."], LUNES)
    ahora = respuestas_del_log(cands, 1)
    ahora["categoria"] = eleccion("categoria", "laptops", 0.9)
    r = interpretar(ahora, cands, CATALOGO, 1, anterior=antes)
    assert parametro(r, "categoria").eleccion == "laptops"
    # Otra rama (monitores) sin corrección no pisa lo que dijo.
    cands = extraer([PEDIDO, "Monitores no."], LUNES)
    ahora["categoria"] = eleccion("categoria", "monitors", 0.9)
    r = interpretar(ahora, cands, CATALOGO, 1, anterior=antes)
    assert parametro(r, "categoria").eleccion == COMPUTADORAS


def test_camaras_es_la_rama_general_de_webcams_y_camaras_digitales():
    cands = extraer(["Necesito 3 cámaras para la oficina, hasta 900 dólares, para el viernes"], LUNES)
    categoria = next(q for q in preguntas(cands, CATALOGO) if q.nombre == "categoria")
    assert "webcams" in categoria.opciones["general"]
    assert "digital_cameras" in categoria.opciones["general"]
    r = interpretar({"categoria": eleccion("categoria", "no_dicha", 0.9)}, cands, CATALOGO, 0)
    assert (parametro(r, "categoria").estado, parametro(r, "categoria").eleccion) == ("resuelto", CAMARAS)


def test_sin_catalogo_la_categoria_general_es_la_de_la_taxonomia():
    cands = extraer([PEDIDO], LUNES)
    r = interpretar({"categoria": eleccion("categoria", "no_dicha", 0.9)}, cands, [], 0)
    assert (parametro(r, "categoria").estado, parametro(r, "categoria").eleccion) == ("resuelto", COMPUTADORAS)
    assert not r.fuera_de_catalogo


def test_una_categoria_ajena_al_catalogo_queda_fuera_del_catalogo():
    cands = extraer(["Comprame una cafetera para el viernes"], LUNES)
    catalogo = ["laptops", "desktops", "tablets"]
    r = interpretar({"categoria": eleccion("categoria", "no_dicha", 0.9)}, cands, catalogo, 0)
    assert (parametro(r, "categoria").estado, parametro(r, "categoria").eleccion) == ("resuelto", CAFETERAS)
    assert r.fuera_de_catalogo


def test_fuera_de_catalogo_del_modelo_no_se_pisa_y_un_complemento_no_es_el_producto():
    cands = extraer(["Comprame un cargador para la computadora"], LUNES)
    assert cands.categoria is None  # "para la computadora" es un complemento
    r = interpretar({"categoria": eleccion("categoria", "fuera_de_catalogo", 0.9)}, cands, CATALOGO, 0)
    assert r.fuera_de_catalogo and parametro(r, "categoria").eleccion == "fuera_de_catalogo"
    r = interpretar({"categoria": eleccion("categoria", "no_dicha", 0.9)}, cands, CATALOGO, 0)
    assert "categoria" in r.faltan


@pytest.mark.parametrize(
    "categoria, esperado",
    [
        ("laptops", "328"), ("desktops", "325"), ("tablets", "4745"), ("monitors", "305"),
        ("digital_cameras", "152"), ("webcams", "312"), ("mobile_phones", "267"),
        ("game_consoles", "1294"), ("usb_drives", "3712"), ("televisions", "404"),
        ("hard_drives", "380"), ("office_chairs", "2045"), ("coffee_makers", "736"),
        ("running_shoes", "187"), ("zapatillas", "187"), ("notebooks", "328"),
        ("algo_raro", None),
    ],
)
def test_taxonomia_de_las_categorias_del_catalogo(categoria, esperado):
    assert taxonomia_de_categoria(categoria) == esperado


def test_todo_el_catalogo_general_tiene_su_categoria_de_la_taxonomia():
    assert [c for c in CATALOGO if taxonomia_de_categoria(c) is None] == []


# -- 3. los días de la semana ----------------------------------------------------------------


def _fechas(texto: str, hoy: date) -> list[str]:
    return [c.valor for c in extraer([texto], hoy).fechas]


def test_un_dia_dicho_ese_mismo_dia_es_el_de_la_semana_que_viene():
    """Regla: un día de la semana es el próximo, nunca hoy. Dicho un lunes,
    "el lunes" es el 26; dicho un domingo, mañana (19)."""
    assert _fechas("para el lunes", LUNES) == ["2026-10-26"]
    assert _fechas("para el lunes", DOMINGO) == ["2026-10-19"]
    assert _fechas("el jueves", LUNES) == ["2026-10-22"]
    assert _fechas("el jueves", JUEVES) == ["2026-10-29"]
    assert _fechas("hoy", LUNES) == ["2026-10-19"]


def test_el_lunes_es_el_mismo_en_el_pedido_y_en_las_respuestas():
    cands = extraer([PEDIDO, RESPUESTA, "Las necesito para el lunes."], LUNES)
    assert [c.valor for c in cands.fechas] == ["2026-10-26", "2026-10-29"]


def test_en_un_mismo_texto_un_dia_que_sigue_no_cae_antes():
    assert _fechas(RESPUESTA, LUNES) == ["2026-10-26", "2026-10-29"]
    assert _fechas(RESPUESTA, DOMINGO) == ["2026-10-19", "2026-10-22"]  # ya en orden
    assert _fechas("10 el jueves y otros 10 el lunes", LUNES) == ["2026-10-22", "2026-10-26"]
    assert _fechas("el 30/10 llegan 5 y el lunes otros 5", LUNES) == ["2026-10-30", "2026-11-02"]
    # Cada texto por su lado: "el jueves" solo es el próximo.
    cands = extraer(["para el lunes", "el jueves"], LUNES)
    assert [c.valor for c in cands.fechas] == ["2026-10-26", "2026-10-22"]


@pytest.mark.parametrize(
    "texto",
    [
        "Entran 10 el lunes. Los otros 10 entran el jueves.",
        "Entran 10 el lunes. Otros 10 entran el jueves.",
        "Entran 10 el lunes. El resto, el jueves.",
        "Entran 10 el lunes; las demás llegan el jueves.",
    ],
)
def test_la_oracion_que_sigue_la_cuenta_no_corta_la_secuencia(texto):
    """"Los otros 10…" sigue contando lo de la oración anterior: dicho un
    lunes, el jueves es el 29 (después de ese lunes), no el 22."""
    assert _fechas(texto, LUNES) == ["2026-10-26", "2026-10-29"]


def test_el_cronograma_en_dos_oraciones_sigue_el_orden():
    texto = "Entran 10 el lunes. Los otros 10 entran el jueves."
    assert [(c, f) for c, f, _ in cronograma_candidato([texto], LUNES)] == [
        (10, "2026-10-26"), (10, "2026-10-29"),
    ]


@pytest.mark.parametrize(
    "hoy, pares",
    [
        (LUNES, [(10, "2026-10-26"), (10, "2026-10-29")]),
        (date(2026, 10, 20), [(10, "2026-10-26"), (10, "2026-10-29")]),  # martes
        (DOMINGO, [(10, "2026-10-19"), (10, "2026-10-22")]),
        (date(2026, 10, 23), [(10, "2026-10-26"), (10, "2026-10-29")]),  # viernes
    ],
)
def test_el_cronograma_sigue_el_orden_del_usuario_cualquier_dia(hoy, pares):
    assert [(c, f) for c, f, _ in cronograma_candidato([RESPUESTA], hoy)] == pares


# -- 1b. lo mismo para los atributos que pidió (marca, talle, memoria…) -----------------------


def test_un_atributo_resuelto_no_se_pierde_si_el_modelo_duda_despues():
    textos = ["Comprame unas zapatillas Nike talle 42 para el viernes", "Para jugar al básquet."]
    cands = extraer(textos, DOMINGO)
    i = next(n for n, m in enumerate(cands.menciones) if m.campo == "marca")
    antes = interpretar(
        {f"pide_{i}": RespuestaPredicado(f"pide_{i}", 0.95),
         f"firmeza_{i}": eleccion(f"firmeza_{i}", "exige", 0.9)},
        cands, ["basketball_shoes"], 0,
    )
    marca = parametro(antes, "mencion:marca:nike")
    assert (marca.estado, marca.requisito.obligatorio) == ("resuelto", True)
    # El modelo duda (0,5) y no sabe si lo exige: sigue resuelto y obligatorio.
    ahora = {f"pide_{i}": RespuestaPredicado(f"pide_{i}", 0.5)}
    r = interpretar(ahora, cands, ["basketball_shoes"], 1, anterior=antes)
    marca = parametro(r, "mencion:marca:nike")
    assert (marca.estado, marca.requisito.obligatorio) == ("resuelto", True)
    assert marca.requisito in r.requisitos_autorizables(False)
    # Con la firmeza dudosa pero el pedido claro, tampoco baja.
    ahora = {f"pide_{i}": RespuestaPredicado(f"pide_{i}", 0.9)}
    marca = parametro(interpretar(ahora, cands, ["basketball_shoes"], 1, anterior=antes),
                      "mencion:marca:nike")
    assert (marca.estado, marca.requisito.obligatorio) == ("resuelto", True)
    # Un "no" claro (lo descartó: la pregunta lo dice) sí lo saca.
    ahora = {f"pide_{i}": RespuestaPredicado(f"pide_{i}", 0.1)}
    r = interpretar(ahora, cands, ["basketball_shoes"], 1, anterior=antes)
    assert not any(p.nombre == "mencion:marca:nike" for p in r.parametros)


# -- 4. el tope: sin el ruido de las cantidades ---------------------------------------------


def _opciones_precio(texto: str) -> set[float]:
    cands = extraer([texto], LUNES)
    (q,) = [q for q in preguntas(cands, ["laptops"]) if q.nombre == "precio"]
    por_clave = {c.clave: c.valor for c in cands.montos}
    return {por_clave[k] for k in q.opciones if k in por_clave}


def test_un_numero_de_cosas_no_se_ofrece_como_tope():
    """Visto con decisiones real: con "20" (de "20 computadoras") entre las
    opciones, "hasta 20000 USD" quedaba en 0,59 (faltante); sin esa opción, 0,80."""
    assert _opciones_precio(PEDIDO) == {20000.0}
    assert _opciones_precio("Quiero 2 auriculares de hasta 80.000 pesos cada uno") == {80000.0}
    assert _opciones_precio("Tengo un presupuesto de 500, el envío sale 20 dólares") == {500.0, 20.0}
    assert _opciones_precio("20 notebooks, max 1500 each") == {1500.0}
    assert _opciones_precio("zapatillas talle 42 por 150, 2 pares") == {150.0}
    # Sin un número de cosas, todos (el modelo elige).
    assert _opciones_precio("20 sillas a 300 cada una") == {20.0, 300.0}
    # Las cantidades siguen siendo cantidades.
    assert 20 in [c.valor for c in extraer([PEDIDO], LUNES).cantidades]


@pytest.mark.parametrize(
    "texto, montos",
    [
        ("quiero 3 notebooks, 800 cada notebook, en dolares", {800.0}),
        ("3 notebooks a 800 por notebook", {800.0}),
        ("5 teclados, tope 100 por teclado", {100.0}),
        ("5 keyboards, max 100 per keyboard", {100.0}),
        ("3 monitores, 300 c/u monitor", {300.0}),
        ("unas zapatillas, 150 el par de zapatillas", {150.0}),
    ],
)
def test_un_precio_por_cosa_no_es_un_numero_de_cosas(texto, montos):
    """"800 cada notebook" es un precio: si se marcaba como número de cosas,
    el tope no se ofrecía y se agotaban las repreguntas sin él."""
    assert _opciones_precio(texto) == montos


def test_las_palabras_de_una_respuesta_precisan_la_categoria_aunque_el_modelo_dude():
    cands0 = extraer([PEDIDO], LUNES)
    antes = interpretar(respuestas_del_log(cands0, 1), cands0, CATALOGO, 0)
    cands = extraer([PEDIDO, "Que sean notebooks."], LUNES)
    assert cands.categoria.id == LAPTOPS
    ahora = respuestas_del_log(cands, 1)  # el modelo: categoría "no_dicha"
    categoria = parametro(interpretar(ahora, cands, CATALOGO, 1, anterior=antes), "categoria")
    assert (categoria.estado, categoria.eleccion, categoria.valor) == ("resuelto", LAPTOPS, "notebooks")


def test_fuera_de_catalogo_recordado_no_lo_pisan_las_palabras():
    cands0 = extraer(["Comprame una funda de notebook, hasta 30 dólares"], LUNES)
    antes = interpretar({"categoria": eleccion("categoria", "fuera_de_catalogo", 0.95)},
                        cands0, CATALOGO, 0)
    assert antes.fuera_de_catalogo
    cands = extraer(["Comprame una funda de notebook, hasta 30 dólares", "Para viajar."], LUNES)
    r = interpretar({"categoria": eleccion("categoria", "no_dicha", 0.9)}, cands, CATALOGO, 1,
                    anterior=antes)
    assert r.fuera_de_catalogo and parametro(r, "categoria").eleccion == "fuera_de_catalogo"


# -- revisión: lo que no tiene que autorizar el atajo de la categoría --------------------------


def test_una_correccion_de_la_categoria_no_vuelve_a_la_de_antes():
    cands0 = extraer([PEDIDO], LUNES)
    antes = interpretar(respuestas_del_log(cands0, 1), cands0, CATALOGO, 0)
    cands = extraer([PEDIDO, "No, mejor proyectores."], LUNES)
    corrige = {"corrige_categoria": RespuestaPredicado("corrige_categoria", 0.9)}
    # Corrige con un valor dudoso: vale lo que nombró ahora (proyectores), no lo de antes.
    ahora = {**respuestas_del_log(cands, 1), **corrige,
             "categoria": eleccion("categoria", "projectors", 0.5)}
    categoria = parametro(interpretar(ahora, cands, CATALOGO, 1, anterior=antes), "categoria")
    assert (categoria.estado, categoria.eleccion) == ("resuelto", "396")
    # Corrige y no nombra ninguna: se vuelve a preguntar.
    cands = extraer([PEDIDO, "No, eso no es lo que quiero."], LUNES)
    ahora = {**respuestas_del_log(cands, 1), **corrige}
    r = interpretar(ahora, cands, CATALOGO, 1, anterior=antes)
    assert "categoria" in r.faltan


def test_el_atajo_no_pisa_otra_eleccion_del_modelo():
    cands = extraer([PEDIDO], LUNES)
    # El modelo duda pero apunta a fuera del catálogo o a otra rama: no se fuerza la nombrada.
    for opcion in ("fuera_de_catalogo", "monitors"):
        r = interpretar({"categoria": eleccion("categoria", opcion, 0.55)}, cands, CATALOGO, 0)
        assert "categoria" in r.faltan, opcion
    # Dentro de su rama (laptops, dudoso), sí: lo que dijo el usuario.
    r = interpretar({"categoria": eleccion("categoria", "laptops", 0.55)}, cands, CATALOGO, 0)
    assert parametro(r, "categoria").eleccion == COMPUTADORAS


def test_de_fuera_del_catalogo_a_otra_categoria_hacen_falta_sus_palabras():
    textos = ["Comprame una funda de notebook, hasta 30 dólares"]
    cands0 = extraer(textos, LUNES)
    antes = interpretar({"categoria": eleccion("categoria", "fuera_de_catalogo", 0.95)},
                        cands0, CATALOGO, 0)
    cands = extraer([*textos, "Para viajar."], LUNES)
    r = interpretar({"categoria": eleccion("categoria", "backpacks", 0.85)}, cands, CATALOGO, 1,
                    anterior=antes)
    assert parametro(r, "categoria").eleccion == "fuera_de_catalogo"
    cands = extraer([*textos, "En realidad quiero una mochila."], LUNES)
    r = interpretar({"categoria": eleccion("categoria", "backpacks", 0.85)}, cands, CATALOGO, 1,
                    anterior=antes)
    assert parametro(r, "categoria").eleccion == "backpacks"


def test_un_plazo_exigido_no_pasa_a_preferido_sin_correccion():
    cands0 = extraer([PEDIDO], LUNES)
    antes = interpretar(respuestas_del_log(cands0, 1), cands0, CATALOGO, 0)
    (entregas,) = [q for q in antes.requisitos if q.campo == "entregas"]
    assert entregas.obligatorio
    cands = extraer([PEDIDO, RESPUESTA], LUNES)
    ahora = respuestas_del_log(cands, 1)
    ahora["plazo_firmeza"] = eleccion("plazo_firmeza", "prefiere", 0.65)  # mismo plazo, firmeza que cambia
    (entregas,) = [q for q in interpretar(ahora, cands, CATALOGO, 1, anterior=antes).requisitos
                   if q.campo == "entregas"]
    assert entregas.obligatorio
    ahora["corrige_plazo"] = RespuestaPredicado("corrige_plazo", 0.9)
    (entregas,) = [q for q in interpretar(ahora, cands, CATALOGO, 1, anterior=antes).requisitos
                   if q.campo == "entregas"]
    assert not entregas.obligatorio


MARTES = date(2026, 10, 20)


@pytest.mark.parametrize(
    "texto, fechas",
    [
        # Un día de la semana pegado a su fecha no es otra fecha (el próximo viernes, 23).
        ("el viernes 30 de octubre", ["2026-10-30"]),
        ("el viernes 30/10", ["2026-10-30"]),
        ("lo necesito para el lunes 2 de noviembre", ["2026-11-02"]),
        ("para el miércoles 28 de octubre, 4 sillas de oficina", ["2026-10-28"]),
        ("by Friday, October 30", ["2026-10-30"]),
        # Con el número del día: el primer jueves 29 desde hoy.
        ("para el jueves 29", ["2026-10-29"]),
        ("el lunes 26 a las 9", ["2026-10-26"]),
        ("by Monday the 2nd", ["2026-11-02"]),
        # "por la mañana" es una franja del día, no mañana.
        ("el viernes por la mañana", ["2026-10-23"]),
        ("a las 9 de la mañana del jueves", ["2026-10-22"]),
        ("mañana a la mañana", ["2026-10-21"]),
        # El día de la semana que viene, no el próximo (el 21) ni el viernes de esa semana.
        ("para el miercoles de la semana que viene, 4 laptops", ["2026-10-28"]),
        ("el miércoles de la próxima semana", ["2026-10-28"]),
        ("by Wednesday next week", ["2026-10-28"]),
    ],
)
def test_un_dia_de_la_semana_con_su_fecha_es_una_sola_fecha(texto, fechas):
    assert _fechas(texto, MARTES) == fechas


def test_el_numero_del_dia_no_es_cantidad_ni_monto():
    cands = extraer(["para el jueves 29, 3 notebooks"], MARTES)
    assert [c.valor for c in cands.cantidades] == [3]
    assert 29.0 not in [c.valor for c in cands.montos]


def test_un_numero_que_no_es_el_dia_no_se_lo_come_la_fecha():
    # "10 personas" cuenta personas: el lunes es el próximo y 10, una cantidad.
    cands = extraer(["entran el lunes 10 personas"], MARTES)
    assert [c.valor for c in cands.fechas] == ["2026-10-26"]
    assert 10 in [c.valor for c in cands.cantidades]
    # Sin un viernes 29 cerca (el 29/10 es jueves), el viernes sigue siendo el próximo.
    assert _fechas("el viernes 29", MARTES) == ["2026-10-23"]
    # Un día que no es el de la fecha de al lado, tampoco se pierde.
    assert _fechas("lo necesito el lunes, el 30/10 no hay nadie", MARTES) == ["2026-10-30", "2026-10-26"]


@pytest.mark.parametrize(
    "texto, pares",
    [
        ("empiezan 10 el lunes 26 y 10 el jueves 29", [(10, "2026-10-26"), (10, "2026-10-29")]),
        ("10 el viernes 30 de octubre y 10 el lunes 2 de noviembre",
         [(10, "2026-10-30"), (10, "2026-11-02")]),
        ("Entran 10 el lunes por la mañana y 10 el jueves por la tarde",
         [(10, "2026-10-26"), (10, "2026-10-29")]),
    ],
)
def test_el_cronograma_con_el_dia_y_su_fecha(texto, pares):
    assert [(c, f) for c, f, _ in cronograma_candidato([texto], MARTES)] == pares


def test_una_fecha_que_no_tiene_que_ver_no_corre_el_dia():
    assert _fechas("El pago vence el 15/12. Lo necesito el lunes.", LUNES) == ["2026-12-15", "2026-10-26"]
    assert _fechas("preferiblemente el viernes, si no el miércoles", LUNES) == ["2026-10-23", "2026-10-21"]
    # Otra oración tampoco: del lado estricto (la fecha más temprana).
    assert _fechas("El pago vence el jueves. Las necesito para el martes.", LUNES) == [
        "2026-10-22", "2026-10-20",
    ]


def test_el_tope_no_pierde_un_monto_que_no_es_una_cantidad_de_cosas():
    assert _opciones_precio("Necesito hasta 5 notebooks, 1500 cada una") == {1500.0}
    assert _opciones_precio("3 monitores por 900, flete 20 USD") == {900.0, 20.0}
    assert _opciones_precio("20 sillas a 300 cada una, ya pagué 5000 USD de anticipo") >= {300.0, 5000.0}
    assert 20.0 not in _opciones_precio(PEDIDO)


def test_si_falla_recalcular_el_turno_anterior_sigue_sin_memoria():
    from decisiones import DecisionesError

    llamadas = []

    def responder(estado, qs):
        llamadas.append(len(estado["conversacion"]))
        if not estado["conversacion"]:
            raise DecisionesError("caído")
        cands = extraer([estado["pedido"], *(t["respuesta"] for t in estado["conversacion"])], LUNES)
        r = respuestas_del_log(cands, 1)
        return {q.nombre: r.get(q.nombre, Rechazo(q.nombre)) for q in qs}

    agente = AgenteDecisiones(_Cliente(responder))
    r = agente.evaluar_intake(PEDIDO, [Turno(pregunta="…", respuesta=RESPUESTA)], LUNES, CATALOGO)
    assert llamadas == [0, 1]
    assert parametro(r, "plazo").valor == "2026-10-26"
