"""Atributos genéricos entre la intención y el Guard.

El Guard publica qué campos y operadores sabe chequear
(`guard.chequeos.CAMPOS_SOPORTADOS` y `OPERADORES_SOPORTADOS`) y el intake
los lee: un atributo confirmado (memoria, color, cancelación de ruido...) va
a los parámetros autorizados. El detector de oportunidades lee el valor de
la oferta con `observado_de(requisito, ...)` y nombra los atributos por su
nombre, no como "atributo".
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_args

import pytest

from contracts import Contexto, Entrega, Ficha, OfertasVistas, Requisito
from guard.chequeos import CAMPOS_SOPORTADOS, OPERADORES_SOPORTADOS, cumple
from guard.extractor import extraer_ofertas
from intencion import detectar_oportunidad
from intencion.agente import valor_ofrecido
from intencion.agente_decisiones import requisitos_verificables

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "etapa2" / "computadoras.json"
RAM = Requisito(
    campo="atributo", atributo="memoria_ram", operador="minimo", valor=16, unidad="GB",
    obligatorio=True,
)


class ClienteFalso:
    def __init__(self, salida: dict[str, Any]) -> None:
        self.salida = salida
        self.pedidos: list[dict[str, Any]] = []
        self.models = SimpleNamespace(generate_content=self._generar)

    def _generar(self, **kwargs: Any) -> Any:
        self.pedidos.append(kwargs)
        return SimpleNamespace(parsed=self.salida, text=json.dumps(self.salida))


@pytest.fixture
def datos() -> dict:
    return json.loads(FIXTURE.read_text("utf-8"))


def _ofertas(datos: dict, ram: int = 16) -> list:
    detalles = copy.deepcopy(datos["offer_details"])
    for d in detalles:
        d["atributos"] = {"memoria_ram": {"valor": ram, "unidad": "GB"}}
    return extraer_ofertas(OfertasVistas(carts=copy.deepcopy(datos["carts"]), offer_details=detalles))


def _contexto() -> Contexto:
    return Contexto(
        pedido="20 laptops for Monday",
        pregunta="¿Para qué las necesitás?",
        respuesta="Entran 10 trabajadores el lunes y otros 10 el jueves.",
        necesidad_entregas=[
            Entrega(cantidad=10, fecha="2026-10-19"), Entrega(cantidad=10, fecha="2026-10-22")
        ],
    )


# -- lo que publica el Guard ----------------------------------------------------------


def test_el_guard_publica_lo_que_sabe_chequear():
    operadores = set(get_args(Requisito.model_fields["operador"].annotation))
    assert OPERADORES_SOPORTADOS == operadores  # todos los del contrato
    assert {"atributo", "comercio", "precio", "moneda", "cantidad", "entregas"} <= CAMPOS_SOPORTADOS


@pytest.mark.parametrize("operador", sorted(OPERADORES_SOPORTADOS))
def test_cada_operador_de_un_atributo_se_puede_evaluar_sin_lanzar(datos, operador):
    requisito = RAM.model_copy(update={"operador": operador})
    (oferta, *_) = _ofertas(datos)
    assert cumple(requisito, oferta) in (True, False, None)


def test_el_intake_autoriza_atributos_y_minimo():
    color = Requisito(campo="atributo", atributo="color", operador="igual", valor="negro",
                      obligatorio=False)
    si, no = requisitos_verificables([RAM, color])
    assert si == [RAM, color] and no == []


# -- el detector ------------------------------------------------------------------------


def test_valor_ofrecido_de_un_atributo(datos):
    (oferta, *_) = _ofertas(datos, ram=8)
    assert valor_ofrecido(RAM, oferta) == {"valor": 8, "unidad": "GB"}
    precio = Requisito(campo="precio", operador="maximo", valor=1.0, obligatorio=True)
    assert valor_ofrecido(precio, oferta) == oferta.precio


def test_el_detector_nombra_los_atributos_y_los_mantiene_en_lo_propuesto(datos):
    ficha = Ficha.desde_intent(datos["intent"])
    ficha = ficha.model_copy(update={"requisitos": [*ficha.requisitos, RAM]})
    cliente = ClienteFalso({"hay_oportunidad": True, "cart_id": "cart_2", "por_que": "x"})
    oportunidad = detectar_oportunidad(_contexto(), ficha, _ofertas(datos), cliente)
    enviado = json.loads(cliente.pedidos[0]["contents"])
    assert "memoria_ram: mínimo 16 GB" in enviado["pedido_explicito"]
    assert not any(p.startswith("atributo") for p in enviado["pedido_explicito"])
    assert oportunidad is not None
    (ram,) = [r for r in oportunidad.requisitos_propuestos if r.campo == "atributo"]
    assert ram == RAM  # con su nombre y su unidad


def test_una_oferta_sin_la_memoria_pedida_no_se_propone(datos):
    ficha = Ficha.desde_intent(datos["intent"])
    ficha = ficha.model_copy(update={"requisitos": [*ficha.requisitos, RAM]})
    cliente = ClienteFalso({"hay_oportunidad": True, "cart_id": "cart_2", "por_que": "x"})
    assert detectar_oportunidad(_contexto(), ficha, _ofertas(datos, ram=8), cliente) is None
    assert cliente.pedidos == []  # ni le llega al modelo
