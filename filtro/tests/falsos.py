"""Cliente de decisiones falso: sin red, con probabilidades fijas por pregunta."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from decisiones import Pregunta, Rechazo, Respuesta, RespuestaPredicado


class ClienteFalso:
    """Imita `decisiones.ClienteDecisiones`.

    - `probabilidades`: nombre de la pregunta → probabilidad de "sí" (0.01 si
      no está);
    - `error`: excepción que lanza en cada llamada;
    - `rechazos`: preguntas que contesta con `Rechazo` ("no sé").

    Guarda cada llamada (estado y preguntas) para revisar qué se mandó.
    """

    nombre = "falso"

    def __init__(
        self,
        probabilidades: Mapping[str, float] | None = None,
        *,
        error: Exception | None = None,
        rechazos: tuple[str, ...] = (),
    ) -> None:
        self.probabilidades = dict(probabilidades or {})
        self.error = error
        self.rechazos = rechazos
        self.llamadas: list[tuple[Any, list[Pregunta]]] = []

    def decidir(self, estado: Any, preguntas: list[Pregunta]) -> dict[str, Respuesta]:
        self.llamadas.append((estado, list(preguntas)))
        if self.error is not None:
            raise self.error
        salida: dict[str, Respuesta] = {}
        for p in preguntas:
            if p.nombre in self.rechazos:
                salida[p.nombre] = Rechazo(p.nombre)
            else:
                salida[p.nombre] = RespuestaPredicado(
                    p.nombre, self.probabilidades.get(p.nombre, 0.01)
                )
        return salida
