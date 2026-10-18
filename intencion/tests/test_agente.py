"""Tests del agente de intención con un cliente de Gemini falso.

Lo que se prueba es lo que hace el código alrededor del LLM: valida la
salida, arma los parámetros, filtra candidatas y descarta propuestas que
contradicen los datos. El comportamiento del modelo real se mide aparte con
`python -m intencion.evaluar_casos`.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from contracts import Contexto, Entrega, Ficha, OfertasVistas
from guard.extractor import extraer_ofertas
from intencion import (
    PREGUNTA_POR_DEFECTO,
    RespuestaInvalida,
    armar_contexto,
    candidatas,
    detectar_oportunidad,
    preguntar,
)

HOY = date(2026, 10, 18)  # domingo
FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "etapa2" / "computadoras.json"


class ClienteFalso:
    """Imita `google.genai.Client`: devuelve las respuestas en orden y guarda
    lo que se le pidió."""

    def __init__(self, *respuestas: Any) -> None:
        self.respuestas = list(respuestas)
        self.pedidos: list[dict[str, Any]] = []
        self.models = SimpleNamespace(generate_content=self._generar)

    def _generar(self, **kwargs: Any) -> Any:
        self.pedidos.append(kwargs)
        respuesta = self.respuestas.pop(0)
        if isinstance(respuesta, Exception):
            raise respuesta
        if isinstance(respuesta, str):
            return SimpleNamespace(parsed=None, text=respuesta)
        return SimpleNamespace(parsed=respuesta, text=json.dumps(respuesta))


@pytest.fixture
def datos() -> dict:
    return json.loads(FIXTURE.read_text("utf-8"))


def _ofertas(datos: dict) -> list:
    return extraer_ofertas(
        OfertasVistas(
            carts=copy.deepcopy(datos["carts"]),
            offer_details=copy.deepcopy(datos["offer_details"]),
        )
    )


def _ficha(datos: dict) -> Ficha:
    return Ficha.desde_intent(datos["intent"])


def _contexto(entregas=((10, "2026-10-19"), (10, "2026-10-22"))) -> Contexto:
    return Contexto(
        pedido="20 laptops for Monday",
        pregunta="¿Para qué las necesitás?",
        respuesta="Entran 10 trabajadores el lunes y otros 10 el jueves.",
        necesidad="Que cada grupo tenga su computadora cuando empieza.",
        necesidad_entregas=[Entrega(cantidad=c, fecha=f) for c, f in entregas],
    )


# -- preguntar() -----------------------------------------------------------------


def test_preguntar_arma_los_parametros_explicitos():
    cliente = ClienteFalso(
        {
            "pregunta": "¿Para qué necesitás las 20 computadoras para el lunes?",
            "cantidad": 20,
            "entrega_fecha": "2026-10-19",
            "presupuesto": 20000,
            "moneda": "usd",
            "categoria": "laptops",
        }
    )
    resultado = preguntar("20 laptops for Monday, up to 20000 USD", HOY, cliente, categorias=["laptops"])
    assert resultado.pregunta.startswith("¿Para qué")
    por_campo = {r.campo: r for r in resultado.requisitos}
    assert por_campo["precio"].valor == 20000.0 and por_campo["precio"].operador == "maximo"
    assert por_campo["moneda"].valor == "USD"
    assert por_campo["categoria"].valor == "laptops"
    assert por_campo["cantidad"].valor == 20
    assert por_campo["entregas"].valor == [{"cantidad": 20, "fecha": "2026-10-19"}]
    assert all(r.obligatorio for r in resultado.requisitos)
    ficha = resultado.ficha("20 laptops")
    assert ficha.confirmada and ficha.pedido_original == "20 laptops"
    # El modelo recibe la fecha de hoy para resolver "el lunes".
    assert "2026-10-18" in cliente.pedidos[0]["config"]["system_instruction"]


@pytest.mark.parametrize(
    ("campo", "valor", "falta"),
    [
        ("categoria", "computers", "categoria"),  # fuera del catálogo
        ("moneda", "dólares", "moneda"),
        ("presupuesto", -5, "precio"),
        ("presupuesto", True, "precio"),
        ("cantidad", 2.5, "cantidad"),
        ("entrega_fecha", "2026-10-01", "entregas"),  # en el pasado
        ("entrega_fecha", "el lunes", "entregas"),
    ],
)
def test_preguntar_descarta_lo_que_no_valida(campo, valor, falta):
    salida = {
        "pregunta": "¿Para qué?",
        "cantidad": 20,
        "entrega_fecha": "2026-10-19",
        "presupuesto": 20000,
        "moneda": "USD",
        "categoria": "laptops",
        campo: valor,
    }
    resultado = preguntar("x", HOY, ClienteFalso(salida), categorias=["laptops"])
    assert falta not in {r.campo for r in resultado.requisitos}


def test_preguntar_sin_pregunta_usa_la_de_siempre():
    resultado = preguntar("x", HOY, ClienteFalso({"pregunta": "  "}))
    assert resultado.pregunta == PREGUNTA_POR_DEFECTO
    assert resultado.requisitos == []


def test_preguntar_con_respuesta_ilegible_lanza():
    with pytest.raises(RespuestaInvalida):
        preguntar("x", HOY, ClienteFalso("no es json"))


def test_preguntar_con_pedido_vacio_no_llama_al_modelo():
    cliente = ClienteFalso()
    with pytest.raises(ValueError):
        preguntar("   ", HOY, cliente)
    assert cliente.pedidos == []


# -- armar_contexto() ------------------------------------------------------------


def test_armar_contexto_toma_la_necesidad_y_las_flexibilidades():
    cliente = ClienteFalso(
        {
            "necesidad": "Que cada grupo tenga su computadora el día que empieza.",
            "necesidad_entregas": [
                {"cantidad": 10, "fecha": "2026-10-19"},
                {"cantidad": 10, "fecha": "2026-10-22"},
            ],
            "flexibilidades": [
                {"descripcion": "Entregas parciales", "origen": "inferida"},
                {"descripcion": "x", "origen": "inventada"},  # origen inválido: afuera
            ],
        }
    )
    contexto = armar_contexto("20 laptops", "¿Para qué?", "Entran 10 y 10", HOY, cliente)
    assert contexto.respuesta == "Entran 10 y 10"
    assert [(e.cantidad, e.fecha.isoformat()) for e in contexto.necesidad_entregas] == [
        (10, "2026-10-19"),
        (10, "2026-10-22"),
    ]
    assert [f.origen for f in contexto.flexibilidades] == ["inferida"]
    # Lo único que lee el modelo: el pedido, la pregunta y la respuesta.
    enviado = json.loads(cliente.pedidos[0]["contents"])
    assert set(enviado) == {"pedido", "pregunta", "respuesta_del_usuario"}


def test_armar_contexto_con_cronograma_invalido_lo_deja_vacio():
    cliente = ClienteFalso(
        {
            "necesidad": "x",
            "necesidad_entregas": [{"cantidad": 10, "fecha": "lunes"}],
            "flexibilidades": [],
        }
    )
    contexto = armar_contexto("p", "q", "r", HOY, cliente)
    assert contexto.necesidad_entregas == []


def test_armar_contexto_sin_respuesta_no_llama_al_modelo():
    cliente = ClienteFalso()
    with pytest.raises(ValueError):
        armar_contexto("p", "q", "  ", HOY, cliente)
    assert cliente.pedidos == []


# -- detectar_oportunidad() ------------------------------------------------------


def test_propone_la_oferta_que_cubre_la_necesidad(datos):
    por_que = "Llegan 10 el lunes y 10 el miércoles: antes del jueves."
    cliente = ClienteFalso({"hay_oportunidad": True, "cart_id": "cart_2", "por_que": por_que})
    oportunidad = detectar_oportunidad(_contexto(), _ficha(datos), _ofertas(datos), cliente)
    assert oportunidad is not None
    assert oportunidad.cart_id == "cart_2"
    # La explicación es una plantilla con los datos, no el texto del modelo.
    assert oportunidad.por_que != por_que
    assert "10 el lunes 2026-10-19 y 10 el miércoles 2026-10-21" in oportunidad.por_que
    # Los cambios salen de los datos de la oferta, no del texto del modelo.
    assert [c.campo for c in oportunidad.cambios] == ["entregas"]
    assert oportunidad.cambios[0].ofrecido == [
        {"cantidad": 10, "fecha": "2026-10-19"},
        {"cantidad": 10, "fecha": "2026-10-21"},
    ]
    propuestos = {r.campo: r.valor for r in oportunidad.requisitos_propuestos}
    assert propuestos["entregas"] == oportunidad.cambios[0].ofrecido
    assert propuestos["precio"] == 20000.0 and propuestos["moneda"] == "USD"
    assert propuestos["cantidad"] == 20


def test_descarta_si_el_modelo_elige_una_que_no_llega_a_tiempo(datos):
    # cart_1 entrega 10 el viernes: el segundo grupo empieza el jueves.
    cliente = ClienteFalso({"hay_oportunidad": True, "cart_id": "cart_1", "por_que": "x"})
    assert detectar_oportunidad(_contexto(), _ficha(datos), _ofertas(datos), cliente) is None


def test_descarta_un_cart_que_no_es_candidato(datos):
    cliente = ClienteFalso({"hay_oportunidad": True, "cart_id": "cart_99", "por_que": "x"})
    assert detectar_oportunidad(_contexto(), _ficha(datos), _ofertas(datos), cliente) is None


def test_si_el_modelo_dice_que_no_no_hay_oportunidad(datos):
    cliente = ClienteFalso({"hay_oportunidad": False, "cart_id": None, "por_que": "Ninguna."})
    assert detectar_oportunidad(_contexto(), _ficha(datos), _ofertas(datos), cliente) is None


def test_sin_respuesta_del_usuario_no_hay_analisis(datos):
    cliente = ClienteFalso()
    contexto = _contexto().model_copy(update={"respuesta": None})
    assert detectar_oportunidad(contexto, _ficha(datos), _ofertas(datos), cliente) is None
    assert cliente.pedidos == []


def test_si_una_oferta_cumple_el_pedido_no_se_busca_nada(datos):
    datos["offer_details"][1]["deliveries"] = [{"quantity": 20, "date": "2026-10-19"}]
    cliente = ClienteFalso()
    ofertas = _ofertas(datos)
    assert candidatas(_ficha(datos), ofertas) == []
    assert detectar_oportunidad(_contexto(), _ficha(datos), ofertas, cliente) is None
    assert cliente.pedidos == []


def test_nunca_propone_pasarse_del_tope(datos):
    datos["carts"][1]["amount"] = 25000.0  # supera el tope de 20000
    ofertas = _ofertas(datos)
    assert [o.cart_id for o in candidatas(_ficha(datos), ofertas)] == ["cart_1"]
    cliente = ClienteFalso({"hay_oportunidad": True, "cart_id": "cart_2", "por_que": "x"})
    assert detectar_oportunidad(_contexto(), _ficha(datos), ofertas, cliente) is None


def test_sin_cronograma_en_la_necesidad_no_cambia_entregas(datos):
    # Si el usuario no dio un cronograma, el código no puede comprobar que la
    # entrega le sirva: no se propone cambiar fechas.
    cliente = ClienteFalso({"hay_oportunidad": True, "cart_id": "cart_2", "por_que": "x"})
    contexto = _contexto(entregas=())
    assert detectar_oportunidad(contexto, _ficha(datos), _ofertas(datos), cliente) is None


def test_el_modelo_solo_ve_campos_tipados_y_textos_acotados(datos):
    datos["offer_details"][1]["title"] = "Orion " + "A" * 500
    cliente = ClienteFalso({"hay_oportunidad": False, "cart_id": None, "por_que": "x"})
    detectar_oportunidad(_contexto(), _ficha(datos), _ofertas(datos), cliente)
    enviado = json.loads(cliente.pedidos[0]["contents"])
    oferta = next(o for o in enviado["ofertas_candidatas"] if o["cart_id"] == "cart_2")
    assert len(oferta["titulo"]) <= 80
    assert "description" not in json.dumps(enviado)  # el texto libre no viaja
    assert "We cannot" not in json.dumps(enviado)  # ni el item_label


def test_sin_tope_y_moneda_no_se_propone_nada(datos):
    # Si la ficha no trae el mandato (el LLM no extrajo tope o moneda), el
    # detector no tiene contra qué acotar: no propone.
    sin_mandato = Ficha(
        pedido_original="x",
        requisitos=[r for r in _ficha(datos).requisitos if r.campo not in ("precio", "moneda")],
        confirmada=True,
    )
    cliente = ClienteFalso()
    assert candidatas(sin_mandato, _ofertas(datos)) == []
    assert detectar_oportunidad(_contexto(), sin_mandato, _ofertas(datos), cliente) is None
    assert cliente.pedidos == []


class ErrorDeGemini(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"{code}")
        self.code = code


def test_reintenta_si_el_modelo_esta_saturado(monkeypatch):
    import intencion.agente as agente

    monkeypatch.setattr(agente, "ESPERA_REINTENTO_S", 0)
    cliente = ClienteFalso(ErrorDeGemini(503), ErrorDeGemini(429), {"pregunta": "¿Para qué?"})
    assert preguntar("x", HOY, cliente).pregunta == "¿Para qué?"
    assert len(cliente.pedidos) == 3


def test_no_reintenta_un_error_que_no_es_transitorio(monkeypatch):
    import intencion.agente as agente

    monkeypatch.setattr(agente, "ESPERA_REINTENTO_S", 0)
    cliente = ClienteFalso(ErrorDeGemini(400), {"pregunta": "¿Para qué?"})
    with pytest.raises(ErrorDeGemini):
        preguntar("x", HOY, cliente)
    assert len(cliente.pedidos) == 1
