"""Adaptador del Guard para el benchmark de `world/` (decisión 8 del paso 0).

    evaluate(intent, offers_seen, chosen_cart_id) -> dict

Acá vive el I/O que `evaluar()` no hace: cargar el catálogo público (en vivo
con un timeout corto y, si no responde, `fixtures/public_catalog.json`) y
consultar la ficha del fabricante por GTIN (`fabricante/`, por defecto los
fixtures rotulados; `FABRICANTE_FUENTE=icecat` para Icecat en vivo). La ficha
del pedido sale de `guard.ficha.ficha_desde_intent()` (con los atributos).

Se enchufa así, desde `world/ap2`:

    PYTHONUTF8=1 PYTHONPATH=../.. uv run --no-sync --package ap2-samples \\
      python -m demo_world.benchmark --source direct --decider target \\
      --trials 1 --guard guard.benchmark_hook:evaluate
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from contracts import OfertasVistas
from fabricante import FuenteFabricante, ResultadoFabricante, datos_externos, fuente_desde_entorno
from guard.ficha import ficha_desde_intent
from guard.fuentes import DatoExterno, con_fuentes
from guard.normalizar import gtin_valido
from guard.veredicto import evaluar

logger = logging.getLogger(__name__)

RAIZ_REPO = Path(__file__).resolve().parents[1]
URL_CATALOGO = os.environ.get("GUARD_CATALOG_URL", "http://localhost:8011/products")
RUTA_CATALOGO_FIXTURE = RAIZ_REPO / "fixtures" / "public_catalog.json"
TIMEOUT_CATALOGO = 1.0  # segundos: el catálogo es local; si no está, fixture


@dataclass(frozen=True)
class CatalogoPublico:
    """Catálogo público ya cargado, listo para `evaluar()`."""

    comercio_id: str | None
    productos: dict[str, dict] = field(default_factory=dict)  # sku -> producto
    fuente: str = "fixture"  # "http" o "fixture"


# El catálogo público es estático (world/ siempre sirve el mismo), así que se
# carga una vez por proceso y URL.
_CACHE: dict[str, CatalogoPublico] = {}


def catalogo_desde_json(datos: Any, fuente: str) -> CatalogoPublico:
    """`{"merchant": {...}, "products": [...]}` → `CatalogoPublico`.

    Lanza ValueError si el formato no es el del catálogo público.
    """
    if not isinstance(datos, dict) or not isinstance(datos.get("products"), list):
        raise ValueError("El catálogo no tiene una lista 'products'")
    productos = {
        p["sku"]: p
        for p in datos["products"]
        if isinstance(p, dict) and isinstance(p.get("sku"), str)
    }
    comercio = datos.get("merchant")
    comercio_id = comercio.get("id") if isinstance(comercio, dict) else None
    return CatalogoPublico(comercio_id=comercio_id, productos=productos, fuente=fuente)


def cargar_catalogo(
    url: str = URL_CATALOGO,
    timeout: float = TIMEOUT_CATALOGO,
    forzar: bool = False,
) -> CatalogoPublico:
    """Catálogo en vivo (`GET url`) o, si falla, el fixture del repo."""
    if not forzar and url in _CACHE:
        return _CACHE[url]
    try:
        respuesta = httpx.get(url, timeout=timeout)
        respuesta.raise_for_status()
        catalogo = catalogo_desde_json(respuesta.json(), fuente="http")
    except (httpx.HTTPError, ValueError) as error:
        logger.warning(
            "Catálogo público en vivo no disponible (%s): uso %s",
            error,
            RUTA_CATALOGO_FIXTURE,
        )
        datos = json.loads(RUTA_CATALOGO_FIXTURE.read_text(encoding="utf-8"))
        catalogo = catalogo_desde_json(datos, fuente="fixture")
    _CACHE[url] = catalogo
    return catalogo


# Fuente del fabricante (FABRICANTE_FUENTE=fixture|icecat|ninguna): una por proceso.
_SIN_CARGAR = object()
_FUENTE_FABRICANTE: Any = _SIN_CARGAR


def fuente_fabricante() -> FuenteFabricante | None:
    """La fuente del entorno (por defecto el fixture). Mal configurada: ninguna
    (sin ficha del fabricante el Guard escala lo obligatorio, nunca aprueba)."""
    global _FUENTE_FABRICANTE
    if _FUENTE_FABRICANTE is _SIN_CARGAR:
        try:
            _FUENTE_FABRICANTE = fuente_desde_entorno()
        except ValueError as error:
            logger.warning("Fuente del fabricante mal configurada (%s): sin fabricante", error)
            _FUENTE_FABRICANTE = None
    return _FUENTE_FABRICANTE


def fichas_del_fabricante(
    offer_details: list[dict], productos: dict[str, dict], fuente: FuenteFabricante | None
) -> list[DatoExterno]:
    """Consulta al fabricante por cada GTIN: el que declara el comercio y el que
    publica el catálogo para ese SKU. Lo que falla queda sin dato."""
    if fuente is None:
        return []
    gtins: list[str] = []
    for detalle in offer_details:
        if not isinstance(detalle, dict):
            continue
        producto = productos.get(detalle.get("sku")) if isinstance(detalle.get("sku"), str) else None
        for crudo in (detalle.get("gtin"), (producto or {}).get("gtin")):
            gtin = gtin_valido(crudo)
            if gtin and gtin not in gtins:
                gtins.append(gtin)
    resultados: list[ResultadoFabricante] = []
    for gtin in gtins:
        try:
            resultado = fuente.consultar(gtin)
        except Exception as error:  # noqa: BLE001 - una fuente rota es "sin dato"
            logger.warning("La fuente del fabricante falló (GTIN %s): %s", gtin, type(error).__name__)
            continue
        if resultado.sin_dato:
            logger.info("Sin ficha del fabricante para %s (%s)", gtin, resultado.motivo)
        resultados.append(resultado)
    return datos_externos(resultados)


def evaluate(intent: dict, offers_seen: dict, chosen_cart_id: str) -> dict:
    """Contrato del benchmark: devuelve el `Veredicto` como dict JSON."""
    ficha = ficha_desde_intent(intent)
    catalogo = cargar_catalogo()
    detalles = offers_seen.get("offer_details", [])
    ofertas_vistas = OfertasVistas(
        carts=offers_seen.get("carts", []),
        offer_details=detalles,
        comercio_id=catalogo.comercio_id,
    )
    externos = fichas_del_fabricante(detalles, catalogo.productos, fuente_fabricante())
    veredicto = evaluar(
        ficha, ofertas_vistas, chosen_cart_id, con_fuentes(catalogo.productos, externos)
    )
    return veredicto.model_dump(mode="json")
