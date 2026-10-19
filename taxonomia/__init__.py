"""Taxonomía de productos de Google: árbol con IDs, carga y clasificación.

- `arbol`: `Taxonomia` y `Categoria` (puro, sin I/O) y `parsear()`.
- `cargar`: el subconjunto del repo (ramas de la demo) y el archivo oficial
  completo con caché local (descarga opcional).
- `clasificar`: clasificación jerárquica nivel por nivel con `decisiones`.
"""

import importlib
from typing import Any

from taxonomia.arbol import Categoria, Taxonomia, parsear
from taxonomia.cargar import (
    URL_TAXONOMIA,
    cargar_completa,
    cargar_subconjunto,
    descargar,
    ruta_cache,
)
from taxonomia.palabras import categoria_por_palabras

# `clasificar` usa `decisiones` (el adaptador de modelos): se importa recién
# cuando se pide, para que el Guard pueda usar el árbol sin cargar un LLM.
_DE_CLASIFICAR = frozenset({"MAX_OPCIONES", "NINGUNA", "Clasificacion", "Paso", "clasificar"})


def __getattr__(nombre: str) -> Any:
    if nombre in _DE_CLASIFICAR:
        # Importar el submódulo deja `taxonomia.clasificar` apuntando al módulo:
        # se reemplaza por los nombres de adentro (la función, como antes).
        modulo = importlib.import_module("taxonomia.clasificar")
        for propio in _DE_CLASIFICAR:
            globals()[propio] = getattr(modulo, propio)
        return globals()[nombre]
    raise AttributeError(f"module 'taxonomia' has no attribute {nombre!r}")

__all__ = [
    "MAX_OPCIONES",
    "NINGUNA",
    "URL_TAXONOMIA",
    "Categoria",
    "Clasificacion",
    "Paso",
    "Taxonomia",
    "cargar_completa",
    "cargar_subconjunto",
    "categoria_por_palabras",
    "clasificar",
    "descargar",
    "parsear",
    "ruta_cache",
]
