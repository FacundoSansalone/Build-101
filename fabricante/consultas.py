"""Consultar al fabricante por las ofertas de una compra (I/O, afuera del Guard).

Lo usan el proxy (antes de `evaluar()`, en `create_checkout` y para las
oportunidades) y el adaptador del benchmark (`guard.benchmark_hook`):

    gtins = gtins_de_ofertas(offer_details, productos_del_catalogo)
    resultados = consultar_gtins(fuente, gtins)
    catalogo = con_fuentes(catalogo, datos_externos(resultados))

Una fuente que falla (red, timeout, cualquier excepción) da "sin dato" para
ese GTIN, nunca una excepción: sin fuente independiente el Guard escala lo
obligatorio. `resumen()` es lo que el proxy registra en el evento
`veredicto` (qué fuente, si es un fixture y qué GTIN tuvieron dato).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

from fabricante.base import FuenteFabricante, ResultadoFabricante
from guard.normalizar import gtin_valido

logger = logging.getLogger(__name__)

MAX_GTINS = 20  # por compra: un comercio no puede hacernos consultar sin fin


def gtins_de_ofertas(offer_details: Iterable[Any], productos: Mapping[str, Any]) -> list[str]:
    """Los GTIN a consultar, normalizados a 14 dígitos y sin repetir: el que
    declara el comercio y el que publica el catálogo para ese SKU."""
    gtins: list[str] = []
    for detalle in offer_details:
        if not isinstance(detalle, dict):
            continue
        sku = detalle.get("sku")
        producto = productos.get(sku) if isinstance(sku, str) else None
        publico = producto.get("gtin") if isinstance(producto, dict) else None
        for crudo in (detalle.get("gtin"), publico):
            gtin = gtin_valido(crudo)
            if gtin and gtin not in gtins:
                gtins.append(gtin)
    return gtins[:MAX_GTINS]


def _es_fixture(fuente: Any) -> bool:
    return getattr(fuente, "nombre", None) == "fixture"


def consultar_gtins(
    fuente: FuenteFabricante | None, gtins: Iterable[str]
) -> list[ResultadoFabricante]:
    """Un resultado por GTIN. Lo que falla queda sin dato (`motivo="error"`)."""
    if fuente is None:
        return []
    nombre = str(getattr(fuente, "nombre", "desconocida"))
    resultados: list[ResultadoFabricante] = []
    for gtin in gtins:
        try:
            resultado = fuente.consultar(gtin)
        except Exception as error:  # noqa: BLE001 - una fuente rota es "sin dato"
            logger.warning("La fuente del fabricante falló (GTIN %s): %s", gtin, type(error).__name__)
            resultado = ResultadoFabricante(gtin, nombre, _es_fixture(fuente), None, "error")
        if resultado.sin_dato:
            logger.info("Sin ficha del fabricante para %s (%s)", gtin, resultado.motivo)
        resultados.append(resultado)
    return resultados


def resumen(
    fuente: FuenteFabricante | None, resultados: Iterable[ResultadoFabricante]
) -> dict[str, Any]:
    """Qué fuente del fabricante se usó y qué devolvió (para el evento
    `veredicto`). Un fixture va rotulado (`fixture: true`): son datos de
    prueba, no de la fuente en vivo."""
    if fuente is None:
        return {"fuente": "ninguna", "fixture": False, "consultas": []}
    return {
        "fuente": str(getattr(fuente, "nombre", "desconocida")),
        "fixture": _es_fixture(fuente),
        "consultas": [
            {"gtin": r.gtin, "con_dato": not r.sin_dato, "motivo": r.motivo} for r in resultados
        ],
    }
