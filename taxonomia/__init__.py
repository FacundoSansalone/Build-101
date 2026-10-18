"""Taxonomía de productos de Google: árbol con IDs, carga y clasificación.

- `arbol`: `Taxonomia` y `Categoria` (puro, sin I/O) y `parsear()`.
- `cargar`: el subconjunto del repo (ramas de la demo) y el archivo oficial
  completo con caché local (descarga opcional).
- `clasificar`: clasificación jerárquica nivel por nivel con `decisiones`.
"""

from taxonomia.arbol import Categoria, Taxonomia, parsear
from taxonomia.cargar import (
    URL_TAXONOMIA,
    cargar_completa,
    cargar_subconjunto,
    descargar,
    ruta_cache,
)
from taxonomia.clasificar import (
    MAX_OPCIONES,
    NINGUNA,
    Clasificacion,
    Paso,
    clasificar,
)

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
    "clasificar",
    "descargar",
    "parsear",
    "ruta_cache",
]
