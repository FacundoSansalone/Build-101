"""`Oportunidad.titulo` lo ve el usuario: nunca el título que declara el vendedor.

Es el título de una fuente pública (catálogo, página pública o fabricante)
si la oferta lo tiene verificado, o vacío (el dashboard muestra el SKU).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from contracts import Contexto, Entrega, Ficha, OfertasVistas
from guard.extractor import extraer_ofertas
from guard.verificador import verificar_todas
from intencion import detectar_oportunidad
from intencion.agente import titulo_publico

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "etapa2" / "computadoras.json"
DATOS = json.loads(FIXTURE.read_text("utf-8"))


class ClienteFalso:
    def __init__(self) -> None:
        salida = {"hay_oportunidad": True, "cart_id": "cart_2", "por_que": "x"}
        self.models = SimpleNamespace(
            generate_content=lambda **_: SimpleNamespace(parsed=salida, text=json.dumps(salida))
        )


def _ofertas(catalogo: dict[str, Any] | None = None) -> list:
    ofertas = extraer_ofertas(
        OfertasVistas(
            carts=copy.deepcopy(DATOS["carts"]),
            offer_details=copy.deepcopy(DATOS["offer_details"]),
        )
    )
    return verificar_todas(ofertas, catalogo or {})


def _contexto() -> Contexto:
    return Contexto(
        pedido="20 laptops for Monday",
        pregunta="¿Para qué?",
        respuesta="Entran 10 el lunes y otros 10 el jueves.",
        necesidad_entregas=[
            Entrega(cantidad=10, fecha="2026-10-19"), Entrega(cantidad=10, fecha="2026-10-22")
        ],
    )


def _oportunidad(catalogo: dict[str, Any] | None = None):
    return detectar_oportunidad(
        _contexto(), Ficha.desde_intent(DATOS["intent"]), _ofertas(catalogo), ClienteFalso()
    )


def test_sin_fuente_publica_el_titulo_queda_vacio():
    declarado = next(d["title"] for d in DATOS["offer_details"] if d["cart_id"] == "cart_2")
    oportunidad = _oportunidad()
    assert oportunidad is not None and oportunidad.sku == "OR-NB-14"
    assert oportunidad.titulo == ""
    assert declarado not in oportunidad.model_dump_json()  # ni en otro campo


def test_con_el_catalogo_el_titulo_es_el_publico():
    catalogo = {"OR-NB-14": {"sku": "OR-NB-14", "title": "Orion Notebook 14 (catálogo)"}}
    assert _oportunidad(catalogo).titulo == "Orion Notebook 14 (catálogo)"


def test_titulo_publico_de_una_oferta_sin_verificar_es_vacio():
    (oferta, *_) = extraer_ofertas(
        OfertasVistas(carts=DATOS["carts"][:1], offer_details=DATOS["offer_details"][:1])
    )
    assert oferta.titulo  # el comercio declaró uno
    assert titulo_publico(oferta) == ""
