"""`FixtureFabricante`: fichas en formato Icecat guardadas en `fixtures/fabricante/`.

Son **datos de prueba**, no de Icecat: los genera `world/`
(`demo_world.export_fixtures`) para la demo. Cada resultado va rotulado
(`fuente="fixture"`, `fixture=True`), también cuando no hay dato, y el Guard
lo dice en el detalle de la evidencia.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fabricante.base import FichaFabricante, ResultadoFabricante, parsear_icecat
from guard.normalizar import gtin_valido

logger = logging.getLogger(__name__)

RAIZ_REPO = Path(__file__).resolve().parents[1]
CARPETA_POR_DEFECTO = RAIZ_REPO / "fixtures" / "fabricante"


class FixtureFabricante:
    nombre = "fixture"

    def __init__(self, carpeta: Path | str | None = None) -> None:
        self.carpeta = Path(carpeta) if carpeta is not None else CARPETA_POR_DEFECTO
        self._indice: dict[str, Any] | None = None

    def _cargar(self) -> dict[str, Any]:
        """GTIN-14 -> respuesta Icecat. Se indexa por los GTIN que confirma cada
        archivo (no por el nombre del archivo). Un archivo roto se saltea."""
        if self._indice is None:
            indice: dict[str, Any] = {}
            for ruta in sorted(self.carpeta.glob("*.json")) if self.carpeta.is_dir() else []:
                try:
                    datos = json.loads(ruta.read_text("utf-8"))
                    gtins = [gtin_valido(g) for g in datos["data"]["GeneralInfo"]["GTIN"]]
                except (OSError, ValueError, KeyError, TypeError) as error:
                    logger.warning("Fixture del fabricante ilegible (%s): %s", ruta.name, error)
                    continue
                for gtin in gtins:
                    if gtin:
                        indice.setdefault(gtin, datos)
            self._indice = indice
        return self._indice

    def consultar(self, gtin: Any) -> ResultadoFabricante:
        normalizado = gtin_valido(gtin)
        if normalizado is None:
            return ResultadoFabricante(None, "fixture", True, None, "gtin_invalido")
        datos = self._cargar().get(normalizado)
        ficha: FichaFabricante | None = None
        if datos is not None:
            ficha = parsear_icecat(datos, gtin=normalizado, fuente="fixture", fixture=True)
        motivo = "ok" if ficha is not None else ("formato" if datos is not None else "no_encontrado")
        return ResultadoFabricante(normalizado, "fixture", True, ficha, motivo)
