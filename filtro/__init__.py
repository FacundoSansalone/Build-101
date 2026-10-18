"""Filtro de seguridad (etapa 2, paso 2 de docs/FLUJO.md).

Revisa cada respuesta del proveedor antes que cualquier otra etapa. Para
enchufarlo al proxy: `GUARD_FILTRO=filtro:filtrar`. Ver filtro/README.md.
`describir()` da el modo efectivo en un texto corto, sin llamar a la red.

Es defensa en profundidad: la garantía es el Guard antes de la firma.
"""

from filtro.nucleo import (
    MOTIVO_MALICIA,
    MOTIVO_OK,
    MOTIVO_SIN_VERIFICAR,
    MOTIVO_SOLO_HEURISTICAS,
    FiltroSeguridad,
    configurar,
    describir,
    desde_entorno,
    filtrar,
)

__all__ = [
    "MOTIVO_MALICIA",
    "MOTIVO_OK",
    "MOTIVO_SIN_VERIFICAR",
    "MOTIVO_SOLO_HEURISTICAS",
    "FiltroSeguridad",
    "configurar",
    "describir",
    "desde_entorno",
    "filtrar",
]
