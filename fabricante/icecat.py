"""`IcecatFabricante`: ficha del fabricante en vivo desde Icecat, por GTIN.

Implementado y **apagado por defecto** (`FABRICANTE_FUENTE=icecat` lo
prende). Pide `GET https://live.icecat.biz/api?lang=EN&shopname=<usuario>&GTIN=<gtin>`
con los tokens en las cabeceras `api-token` y `content-token` (nunca en la
URL). Timeout corto, respuesta acotada y caché (memoria y disco). Si algo
falla (red, HTTP, formato, otro producto) devuelve "sin dato": el Guard no
tiene fuente independiente y el obligatorio escala.

Atribución: los datos de Icecat se muestran con "Specs Icecat"
(https://icecat.biz), ver `fabricante/README.md`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from fabricante.base import ResultadoFabricante, parsear_icecat
from guard.normalizar import gtin_valido

logger = logging.getLogger(__name__)

URL_ICECAT = "https://live.icecat.biz/api"
TAMANO_MAXIMO = 2 * 1024 * 1024
CACHE_POR_DEFECTO = Path(__file__).resolve().parents[1] / ".temp-db" / "icecat"


@dataclass
class IcecatFabricante:
    usuario: str
    api_token: str | None = None
    content_token: str | None = None
    idioma: str = "EN"
    url: str = URL_ICECAT
    timeout: float = 3.0
    cache_dir: Path | str | None = None
    ttl: float = 7 * 24 * 3600.0  # segundos de vida de la caché en disco
    transport: httpx.BaseTransport | None = field(default=None, repr=False)
    nombre: str = "icecat"
    _memoria: dict[str, ResultadoFabricante] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.usuario or not self.usuario.strip():
            raise ValueError("Icecat necesita un usuario (ICECAT_USUARIO)")
        if self.timeout <= 0:
            raise ValueError("El timeout de Icecat tiene que ser > 0")
        if not re.fullmatch(r"[A-Za-z]{2}(_[A-Za-z]{2})?", self.idioma or ""):
            raise ValueError("ICECAT_IDIOMA tiene que ser un código como EN o ES")  # va al nombre de la caché
        self.cache_dir = Path(self.cache_dir) if self.cache_dir is not None else CACHE_POR_DEFECTO

    # -- caché ----------------------------------------------------------------

    def _ruta_cache(self, gtin: str) -> Path:
        clave = hashlib.sha256(f"{self.url}|{self.idioma}|{gtin}".encode()).hexdigest()[:24]
        return Path(self.cache_dir) / f"{gtin}-{self.idioma}-{clave}.json"

    def _de_cache(self, gtin: str) -> dict | None:
        ruta = self._ruta_cache(gtin)
        try:
            if time.time() - ruta.stat().st_mtime > self.ttl:
                return None
            datos = json.loads(ruta.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return datos if isinstance(datos, dict) else None

    def _a_cache(self, gtin: str, datos: dict) -> None:
        ruta = self._ruta_cache(gtin)
        try:
            ruta.parent.mkdir(parents=True, exist_ok=True)
            temporal = ruta.with_suffix(".tmp")
            temporal.write_text(json.dumps(datos, ensure_ascii=False), "utf-8")
            temporal.replace(ruta)
        except OSError as error:  # la caché es una ayuda: si falla, se sigue
            logger.warning("No se pudo guardar la caché de Icecat: %s", error)

    # -- consulta ---------------------------------------------------------------

    def consultar(self, gtin: Any) -> ResultadoFabricante:
        normalizado = gtin_valido(gtin)
        if normalizado is None:
            return ResultadoFabricante(None, "icecat", False, None, "gtin_invalido")
        if normalizado in self._memoria:
            return self._memoria[normalizado]
        datos = self._de_cache(normalizado)
        if datos is None:
            datos, motivo = self._pedir(normalizado)
            if datos is None:
                return ResultadoFabricante(normalizado, "icecat", False, None, motivo)
        ficha = parsear_icecat(datos, gtin=normalizado, fuente="icecat")
        if ficha is None:
            resultado = ResultadoFabricante(normalizado, "icecat", False, None, "no_encontrado")
        else:
            self._a_cache(normalizado, datos)
            resultado = ResultadoFabricante(normalizado, "icecat", False, ficha, "ok")
        self._memoria[normalizado] = resultado
        return resultado

    def _pedir(self, gtin: str) -> tuple[dict | None, str]:
        cabeceras = {"Accept": "application/json"}
        if self.api_token:
            cabeceras["api-token"] = self.api_token
        if self.content_token:
            cabeceras["content-token"] = self.content_token
        # Icecat recibe el EAN/UPC como se imprime: sin el cero de relleno del GTIN-14.
        parametros = {"lang": self.idioma, "shopname": self.usuario, "GTIN": gtin.removeprefix("0")}
        try:
            with httpx.Client(transport=self.transport, timeout=self.timeout) as cliente:
                respuesta = cliente.get(self.url, params=parametros, headers=cabeceras)
        except httpx.TimeoutException:
            logger.warning("Icecat no respondió a tiempo (GTIN %s)", gtin)
            return None, "timeout"
        except httpx.HTTPError as error:
            logger.warning("Icecat no disponible (GTIN %s): %s", gtin, type(error).__name__)
            return None, "error_http"
        if respuesta.status_code == 404:
            return None, "no_encontrado"
        if respuesta.status_code != 200:
            logger.warning("Icecat respondió HTTP %s (GTIN %s)", respuesta.status_code, gtin)
            return None, "error_http"
        if len(respuesta.content) > TAMANO_MAXIMO:
            return None, "formato"
        try:
            datos = respuesta.json()
        except ValueError:
            return None, "formato"
        return (datos, "ok") if isinstance(datos, dict) else (None, "formato")
