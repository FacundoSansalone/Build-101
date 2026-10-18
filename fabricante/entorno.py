"""Qué fuente del fabricante usar, según el entorno.

| Variable | Valor por defecto | Qué hace |
|---|---|---|
| `FABRICANTE_FUENTE` | `fixture` | `fixture` (archivos de `fixtures/fabricante/`), `icecat` (en vivo) o `ninguna` |
| `FABRICANTE_FIXTURES_DIR` | `fixtures/fabricante` | Carpeta de los fixtures |
| `ICECAT_USUARIO` | — | Usuario de Icecat (`shopname`). Obligatorio con `icecat` |
| `ICECAT_API_TOKEN` / `ICECAT_CONTENT_TOKEN` | — | Tokens (van en cabeceras) |
| `ICECAT_IDIOMA` | `EN` | Idioma de la ficha |
| `ICECAT_TIMEOUT` | `3` | Segundos por consulta |
| `ICECAT_CACHE_DIR` | `.temp-db/icecat` | Caché en disco (ignorada por git) |
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from fabricante.base import FuenteFabricante
from fabricante.fixture import FixtureFabricante
from fabricante.icecat import IcecatFabricante


def fuente_desde_entorno(env: Mapping[str, str] | None = None) -> FuenteFabricante | None:
    """La fuente configurada; None con `ninguna`. ValueError si la
    configuración no es válida (p. ej. `icecat` sin usuario)."""
    env = os.environ if env is None else env
    fuente = (env.get("FABRICANTE_FUENTE") or "fixture").strip().lower()
    if fuente == "ninguna":
        return None
    if fuente == "fixture":
        return FixtureFabricante(env.get("FABRICANTE_FIXTURES_DIR") or None)
    if fuente != "icecat":
        raise ValueError(f"FABRICANTE_FUENTE desconocida: {fuente!r} (fixture, icecat o ninguna)")
    usuario = (env.get("ICECAT_USUARIO") or "").strip()
    if not usuario:
        raise ValueError("FABRICANTE_FUENTE=icecat necesita ICECAT_USUARIO")
    try:
        timeout = float(env.get("ICECAT_TIMEOUT") or 3.0)
    except ValueError as error:
        raise ValueError("ICECAT_TIMEOUT tiene que ser un número de segundos") from error
    return IcecatFabricante(
        usuario=usuario,
        api_token=(env.get("ICECAT_API_TOKEN") or "").strip() or None,
        content_token=(env.get("ICECAT_CONTENT_TOKEN") or "").strip() or None,
        idioma=(env.get("ICECAT_IDIOMA") or "EN").strip(),
        timeout=timeout,
        cache_dir=env.get("ICECAT_CACHE_DIR") or None,
    )
