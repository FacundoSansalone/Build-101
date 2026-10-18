"""Tipos comunes de la fuente del fabricante y el formato Icecat (puro).

El Guard no hace I/O: la ficha del fabricante se consulta afuera (proxy,
adaptador del benchmark) y entra a `evaluar()` dentro del catálogo, como
`DatoExterno` con origen "fabricante" (ver `guard.fuentes`).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from contracts import ValorAtributo
from guard.atributos import atributos_validos
from guard.fuentes import DatoExterno, dato_externo
from guard.normalizar import gtin_valido

ATRIBUCION_ICECAT = "Specs Icecat"
MAX_ATRIBUTOS = 400


@dataclass(frozen=True)
class FichaFabricante:
    """Lo que dice el fabricante de un GTIN. `fixture` = datos de prueba."""

    gtin: str                            # GTIN-14 consultado
    marca: str | None
    nombre: str | None
    categoria: str | None                # categoría de Icecat (no es la de Google)
    atributos: dict[str, ValorAtributo]  # nombres canónicos (guard.atributos)
    fuente: str                          # "icecat" o "fixture"
    fixture: bool
    gtins: tuple[str, ...] = ()          # GTIN confirmados por el fabricante
    atribucion: str = ATRIBUCION_ICECAT

    def a_dato_externo(self) -> DatoExterno:
        datos: dict[str, Any] = {
            "atributos": {nombre: valor.model_dump() for nombre, valor in self.atributos.items()},
        }
        if self.marca:
            datos["brand"] = self.marca
        return dato_externo(
            "fabricante", gtin=self.gtin, datos=datos, fixture=self.fixture, fuente=self.fuente
        )


@dataclass(frozen=True)
class ResultadoFabricante:
    """Respuesta de una consulta. Siempre dice de qué fuente vino y si es un
    fixture, también cuando no hay dato."""

    gtin: str | None   # normalizado; None si el pedido no era un GTIN válido
    fuente: str        # "fixture" o "icecat"
    fixture: bool
    ficha: FichaFabricante | None
    # "ok", "gtin_invalido", "no_encontrado", "timeout", "error_http" o "formato"
    motivo: str = "ok"
    detalle: dict[str, Any] = field(default_factory=dict)

    @property
    def sin_dato(self) -> bool:
        return self.ficha is None


class FuenteFabricante(Protocol):
    nombre: str

    def consultar(self, gtin: Any) -> ResultadoFabricante:
        """Nunca lanza: si falla, `ficha=None` y el motivo (el Guard escala)."""
        ...


def datos_externos(resultados: Iterable[ResultadoFabricante]) -> list[DatoExterno]:
    """Los resultados con ficha, listos para `guard.fuentes.con_fuentes()`."""
    return [r.ficha.a_dato_externo() for r in resultados if r.ficha is not None]


# ---------------------------------------------------------------------------
# Formato Icecat (JSON de live.icecat.biz/api)
# ---------------------------------------------------------------------------


def _texto(valor: Any) -> str | None:
    if isinstance(valor, dict):
        valor = valor.get("Value", valor.get("_"))
    return valor.strip() if isinstance(valor, str) and valor.strip() else None


def _valor_feature(feature: dict) -> Any:
    valor = feature.get("Value", feature.get("RawValue"))
    if not isinstance(valor, str) or not valor.strip():
        return None
    if feature.get("Type") == "multi_dropdown":
        partes = [p.strip() for p in valor.split(",") if p.strip()]
        return partes or None
    return valor.strip()


def _atributos_icecat(grupos: Any) -> dict[str, ValorAtributo]:
    crudos: dict[str, Any] = {}
    for grupo in grupos if isinstance(grupos, list) else []:
        features = grupo.get("Features") if isinstance(grupo, dict) else None
        for feature in features if isinstance(features, list) else []:
            if not isinstance(feature, dict) or len(crudos) >= MAX_ATRIBUTOS:
                continue
            definicion = feature.get("Feature") if isinstance(feature.get("Feature"), dict) else {}
            nombre = _texto(definicion.get("Name"))
            valor = _valor_feature(feature)
            if nombre is None or valor is None:
                continue
            medida = definicion.get("Measure") if isinstance(definicion.get("Measure"), dict) else {}
            signos = medida.get("Signs") if isinstance(medida.get("Signs"), dict) else {}
            unidad = _texto(signos.get("_")) or None
            crudos.setdefault(nombre, {"valor": valor, "unidad": unidad})
    return atributos_validos(crudos)


def parsear_icecat(
    datos: Any, *, gtin: str, fuente: str, fixture: bool = False
) -> FichaFabricante | None:
    """Respuesta de Icecat → `FichaFabricante`, o None si no sirve.

    No sirve: `msg` distinto de "OK", sin `data.GeneralInfo`, o un producto
    cuyos GTIN confirmados no incluyen el consultado (es otro producto).
    """
    if not isinstance(datos, dict) or datos.get("msg") != "OK":
        return None
    data = datos.get("data")
    info = data.get("GeneralInfo") if isinstance(data, dict) else None
    if not isinstance(info, dict):
        return None
    lista = info.get("GTIN") if isinstance(info.get("GTIN"), list) else []
    gtins = tuple(g for g in (gtin_valido(x) for x in lista) if g)
    if gtins and gtin not in gtins:
        return None
    categoria = info.get("Category") if isinstance(info.get("Category"), dict) else {}
    return FichaFabricante(
        gtin=gtin,
        marca=_texto(info.get("Brand")),
        nombre=_texto(info.get("Title")) or _texto(info.get("ProductName")),
        categoria=_texto(categoria.get("Name")),
        atributos=_atributos_icecat(data.get("FeaturesGroups")),
        fuente=fuente,
        fixture=fixture,
        gtins=gtins,
    )
