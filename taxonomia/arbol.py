"""Árbol de la taxonomía de productos de Google (`taxonomy-with-ids.<locale>.txt`).

Formato del archivo oficial, una categoría por línea, padres antes que hijos:

    # Google_Product_Taxonomy_Version: 2021-09-21
    222 - Electronics
    278 - Electronics > Computers
    328 - Electronics > Computers > Laptops

Los IDs son los mismos en todos los idiomas; cambian los nombres. Todo acá es
puro (sin I/O): el archivo se lee en `taxonomia.cargar`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

SEPARADOR_RUTA = " > "
_VERSION = re.compile(r"^#\s*Google_Product_Taxonomy_Version:\s*(\S+)")
_LINEA = re.compile(r"^(\d+) - (.+)$")


@dataclass(frozen=True)
class Categoria:
    id: str
    ruta: tuple[str, ...]
    padre: str | None

    @property
    def nombre(self) -> str:
        return self.ruta[-1]

    @property
    def nivel(self) -> int:
        return len(self.ruta)

    @property
    def ruta_texto(self) -> str:
        return SEPARADOR_RUTA.join(self.ruta)


def _clave_ruta(ruta: Iterable[str]) -> tuple[str, ...]:
    return tuple(parte.strip().casefold() for parte in ruta)


class Taxonomia:
    """Categorías por ID, en el orden del archivo. Inmutable después de armarla."""

    def __init__(self, categorias: Iterable[Categoria], version: str | None = None) -> None:
        self.version = version
        self._por_id: dict[str, Categoria] = {}
        self._por_ruta: dict[tuple[str, ...], str] = {}
        self._hijos: dict[str | None, list[str]] = {None: []}
        for categoria in categorias:
            if categoria.id in self._por_id:
                raise ValueError(f"ID repetido en la taxonomía: {categoria.id}")
            clave = _clave_ruta(categoria.ruta)
            if clave in self._por_ruta:
                raise ValueError(f"Ruta repetida en la taxonomía: {categoria.ruta_texto}")
            if categoria.padre is not None and categoria.padre not in self._por_id:
                raise ValueError(f"Categoría sin su padre antes: {categoria.ruta_texto}")
            self._por_id[categoria.id] = categoria
            self._por_ruta[clave] = categoria.id
            self._hijos[categoria.id] = []
            self._hijos[categoria.padre].append(categoria.id)

    # -- acceso -----------------------------------------------------------

    def __len__(self) -> int:
        return len(self._por_id)

    def __iter__(self) -> Iterator[Categoria]:
        return iter(self._por_id.values())

    def __contains__(self, id_categoria: object) -> bool:
        return id_categoria in self._por_id

    def __getitem__(self, id_categoria: str) -> Categoria:
        return self._por_id[id_categoria]

    def get(self, id_categoria: str | None) -> Categoria | None:
        return self._por_id.get(id_categoria) if id_categoria is not None else None

    def raices(self) -> list[Categoria]:
        return self.hijos(None)

    def hijos(self, id_categoria: str | None) -> list[Categoria]:
        """Subcategorías directas (None = el primer nivel). KeyError si no existe."""
        if id_categoria is not None and id_categoria not in self._por_id:
            raise KeyError(id_categoria)
        return [self._por_id[i] for i in self._hijos[id_categoria]]

    def ancestros(self, id_categoria: str) -> list[Categoria]:
        """De la raíz al padre (sin incluir la categoría)."""
        cadena: list[Categoria] = []
        actual = self._por_id[id_categoria].padre
        while actual is not None:
            cadena.append(self._por_id[actual])
            actual = self._por_id[actual].padre
        return list(reversed(cadena))

    def es_descendiente(
        self, id_categoria: str, id_ancestro: str, incluir_igual: bool = True
    ) -> bool:
        """¿`id_categoria` está en la rama de `id_ancestro`? Desconocida: False."""
        if id_categoria not in self._por_id or id_ancestro not in self._por_id:
            return False
        if id_categoria == id_ancestro:
            return incluir_igual
        return any(c.id == id_ancestro for c in self.ancestros(id_categoria))

    def por_ruta(self, ruta: str) -> Categoria | None:
        """Busca por la ruta completa ("A > B > C"), sin importar mayúsculas."""
        id_categoria = self._por_ruta.get(_clave_ruta(ruta.split(">")))
        return self._por_id[id_categoria] if id_categoria else None

    # -- subconjunto y texto ----------------------------------------------

    def subconjunto(self, ids: Iterable[str]) -> Taxonomia:
        """Las ramas pedidas, completas, más lo necesario para llegar a ellas
        nivel por nivel: todas las raíces y los hermanos de cada ancestro."""
        incluidos: set[str] = {c.id for c in self.raices()}
        for id_rama in ids:
            for ancestro in self.ancestros(id_rama):
                incluidos.update(c.id for c in self.hijos(ancestro.id))
            pendientes = [id_rama]
            while pendientes:
                actual = pendientes.pop()
                incluidos.add(actual)
                pendientes.extend(c.id for c in self.hijos(actual))
        return Taxonomia((c for c in self if c.id in incluidos), version=self.version)

    def a_texto(self, encabezado: Iterable[str] = ()) -> str:
        """El mismo formato del archivo oficial (se puede volver a parsear)."""
        lineas = []
        if self.version:
            lineas.append(f"# Google_Product_Taxonomy_Version: {self.version}")
        lineas += [f"# {linea}" for linea in encabezado]
        lineas += [f"{c.id} - {c.ruta_texto}" for c in self]
        return "\n".join(lineas) + "\n"


def parsear(texto: str) -> Taxonomia:
    """Parsea `taxonomy-with-ids.<locale>.txt`. ValueError si una línea no es válida."""
    version: str | None = None
    por_ruta: dict[tuple[str, ...], str] = {}
    categorias: list[Categoria] = []
    for numero, linea in enumerate(texto.splitlines(), start=1):
        linea = linea.strip().lstrip("﻿")
        if not linea:
            continue
        if linea.startswith("#"):
            coincidencia = _VERSION.match(linea)
            if coincidencia and version is None:
                version = coincidencia.group(1)
            continue
        coincidencia = _LINEA.match(linea)
        if coincidencia is None:
            raise ValueError(f"Línea {numero} de la taxonomía inválida: {linea[:80]!r}")
        id_categoria, ruta_texto = coincidencia.groups()
        ruta = tuple(parte.strip() for parte in ruta_texto.split(">"))
        if not all(ruta):
            raise ValueError(f"Línea {numero} de la taxonomía con un nivel vacío")
        padre = por_ruta.get(_clave_ruta(ruta[:-1])) if len(ruta) > 1 else None
        if len(ruta) > 1 and padre is None:
            raise ValueError(f"Línea {numero}: la categoría no tiene a su padre antes")
        por_ruta.setdefault(_clave_ruta(ruta), id_categoria)
        categorias.append(Categoria(id=id_categoria, ruta=ruta, padre=padre))
    return Taxonomia(categorias, version=version)
