"""La categoría de la taxonomía de Google en la ficha del intake.

Con un cliente de decisiones, `taxonomia.clasificar` baja nivel por nivel y
el intake completa `Ficha.categoria_taxonomia` (el último nivel seguro). Sin
cliente, o si no llega ni al primer nivel, queda vacía. El modelo es falso.
"""

from __future__ import annotations

from typing import Any

from decisiones import DecisionesError, Rechazo, RespuestaEleccion
from intencion.agente_decisiones import AgenteDecisiones, categoria_taxonomia

RUTA_LAPTOPS = ("222", "278", "328")  # Electronics > Computers > Laptops


class Clasificador:
    """Elige, en cada nivel, el que está en la ruta de las notebooks."""

    nombre = "falso"
    modelo = "falso-1"

    def __init__(self, probabilidad: float = 0.95) -> None:
        self.probabilidad = probabilidad
        self.estados: list[Any] = []

    def decidir(self, estado: Any, qs: list) -> dict[str, Any]:
        self.estados.append(estado)
        (q,) = qs
        opcion = next((i for i in RUTA_LAPTOPS if i in q.opciones), "ninguna")
        p = self.probabilidad
        return {q.nombre: RespuestaEleccion(q.nombre, opcion, {opcion: p}, p)}


class Caido:
    nombre = "caido"

    def decidir(self, estado: Any, qs: list) -> dict[str, Any]:
        raise DecisionesError("sin red")


def test_clasifica_la_notebook_hasta_la_hoja():
    cliente = Clasificador()
    assert categoria_taxonomia("Quiero una notebook para programar", cliente) == "328"
    assert len(cliente.estados) == 3  # un nivel por llamada
    assert cliente.estados[0]["producto"] == "Quiero una notebook para programar"


def test_sin_cliente_queda_vacia():
    assert categoria_taxonomia("Quiero una notebook", None) is None


def test_sin_confianza_devuelve_el_ultimo_nivel_seguro():
    class Duda(Clasificador):
        def decidir(self, estado, qs):
            r = super().decidir(estado, qs)
            if len(self.estados) == 3:  # duda en el último nivel
                (q,) = qs
                r[q.nombre] = RespuestaEleccion(q.nombre, "328", {"328": 0.4}, 0.4)
            return r

    assert categoria_taxonomia("Quiero una notebook", Duda()) == "278"


def test_si_la_api_falla_en_la_raiz_queda_vacia():
    assert categoria_taxonomia("Quiero una notebook", Caido()) is None


def test_un_rechazo_en_la_raiz_queda_vacia():
    class Rechaza:
        nombre = "rechaza"

        def decidir(self, estado, qs):
            return {q.nombre: Rechazo(q.nombre) for q in qs}

    assert categoria_taxonomia("Quiero una notebook", Rechaza()) is None


def test_el_agente_clasifica_con_su_cliente():
    cliente = Clasificador()
    assert AgenteDecisiones(cliente).clasificar_taxonomia("Una notebook") == "328"
    assert cliente.estados
