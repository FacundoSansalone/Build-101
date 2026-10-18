"""Cargar la taxonomía: el subconjunto del repo o el archivo oficial completo.

- `cargar_subconjunto()`: las ramas de la demo (calzado, computadoras,
  audio/auriculares) más todas las raíces. Está en el repo
  (`taxonomia/datos/`) y no necesita red.
- `cargar_completa(locale)`: el archivo oficial `taxonomy-with-ids.<locale>.txt`
  desde la caché local (`.temp-db/taxonomia/`, ignorada por git, o
  `TAXONOMIA_CACHE_DIR`). Con `descargar=True` lo baja si no está. Nunca se
  descarga en los tests.

URL vigente (ayuda de Merchant Center, "Product taxonomy"):
https://www.google.com/basepages/producttype/taxonomy-with-ids.en-US.txt
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

import httpx

from taxonomia.arbol import Taxonomia, parsear

URL_TAXONOMIA = "https://www.google.com/basepages/producttype/taxonomy-with-ids.{locale}.txt"
RAIZ_REPO = Path(__file__).resolve().parents[1]
RUTA_SUBCONJUNTO = Path(__file__).resolve().parent / "datos" / "taxonomy-with-ids.en-US.subconjunto.txt"
CACHE_POR_DEFECTO = RAIZ_REPO / ".temp-db" / "taxonomia"
TAMANO_MAXIMO = 5 * 1024 * 1024  # el archivo en-US pesa ~0,5 MB
MINIMO_CATEGORIAS = 2  # una respuesta que no es la taxonomía no parsea o queda vacía

_LOCALE = re.compile(r"[a-z]{2}-[A-Z]{2}")


@lru_cache(maxsize=1)
def cargar_subconjunto() -> Taxonomia:
    return parsear(RUTA_SUBCONJUNTO.read_text("utf-8"))


def ruta_cache(locale: str, cache_dir: Path | str | None = None) -> Path:
    if not _LOCALE.fullmatch(locale or ""):
        raise ValueError(f"Locale inválido (formato xx-XX): {locale!r}")
    base = Path(cache_dir or os.environ.get("TAXONOMIA_CACHE_DIR") or CACHE_POR_DEFECTO)
    return base / f"taxonomy-with-ids.{locale}.txt"


def descargar(
    locale: str = "en-US",
    *,
    cache_dir: Path | str | None = None,
    timeout: float = 30.0,
    transport: httpx.BaseTransport | None = None,
) -> Path:
    """Baja el archivo oficial y lo deja en la caché. Lo valida antes de guardarlo:
    si no es la taxonomía (o falla la red) no queda nada en la caché."""
    destino = ruta_cache(locale, cache_dir)
    with httpx.Client(transport=transport, timeout=timeout, follow_redirects=True) as cliente:
        respuesta = cliente.get(URL_TAXONOMIA.format(locale=locale))
    respuesta.raise_for_status()
    if len(respuesta.content) > TAMANO_MAXIMO:
        raise ValueError("La taxonomía descargada es demasiado grande")
    texto = respuesta.content.decode("utf-8")
    if len(parsear(texto)) < MINIMO_CATEGORIAS:
        raise ValueError("La descarga no parece la taxonomía de productos de Google")
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporal = destino.with_suffix(".tmp")
    temporal.write_text(texto, "utf-8")
    temporal.replace(destino)
    return destino


def cargar_completa(
    locale: str = "en-US",
    *,
    cache_dir: Path | str | None = None,
    descargar: bool = False,
    timeout: float = 30.0,
    transport: httpx.BaseTransport | None = None,
) -> Taxonomia:
    """El archivo completo desde la caché (y, si se pide, lo descarga antes)."""
    ruta = ruta_cache(locale, cache_dir)
    if not ruta.is_file():
        if not descargar:
            raise FileNotFoundError(
                f"No está {ruta}. Bajalo con: python -m taxonomia descargar --locale {locale}"
            )
        _descargar(locale, cache_dir=cache_dir, timeout=timeout, transport=transport)
    return parsear(ruta.read_text("utf-8"))


_descargar = descargar  # `cargar_completa(descargar=...)` tapa el nombre
