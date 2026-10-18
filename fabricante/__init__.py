"""Ficha del fabricante por GTIN (fuente de verdad que el vendedor no controla).

- `FixtureFabricante`: archivos en formato Icecat en `fixtures/fabricante/`,
  rotulados como fixture en cada resultado (por defecto).
- `IcecatFabricante`: Icecat en vivo, apagado por defecto
  (`FABRICANTE_FUENTE=icecat`). Specs Icecat.

El Guard no hace I/O: esto se consulta afuera y entra a `evaluar()` dentro del
catálogo (`guard.fuentes.con_fuentes(catalogo, datos_externos(resultados))`).
"""

from fabricante.base import (
    ATRIBUCION_ICECAT,
    FichaFabricante,
    FuenteFabricante,
    ResultadoFabricante,
    datos_externos,
    parsear_icecat,
)
from fabricante.entorno import fuente_desde_entorno
from fabricante.fixture import FixtureFabricante
from fabricante.icecat import IcecatFabricante

__all__ = [
    "ATRIBUCION_ICECAT",
    "FichaFabricante",
    "FixtureFabricante",
    "FuenteFabricante",
    "IcecatFabricante",
    "ResultadoFabricante",
    "datos_externos",
    "fuente_desde_entorno",
    "parsear_icecat",
]
