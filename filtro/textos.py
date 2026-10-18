"""Qué textos de la respuesta del proveedor revisa el filtro.

El proveedor controla toda la respuesta (claves y valores, artefactos,
mensajes de estado, metadatos, errores), así que se recorre entera. Se dejan
afuera solo dos cosas:

- el eco del pedido del comprador en `history` (mensajes con `role: user` en
  el `history` de la tarea): es lo que mandó el agente del cliente y nadie lo
  vuelve a leer;
- los JWT y SD-JWT (checkout firmado, mandatos): son opacos para el agente.

Recorre sin recursión y con topes: una respuesta que no se puede recorrer
entera no se puede declarar segura (`RespuestaExcesiva`, el filtro bloquea).
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

MAX_NODOS = 50_000           # valores recorridos (dicts, listas, escalares, claves)
MAX_CARACTERES = 1_000_000   # suma de todos los textos


class RespuestaExcesiva(ValueError):
    """La respuesta supera los topes: no se puede revisar entera."""


@dataclass(frozen=True)
class Texto:
    campo: str  # la clave bajo la que vino ("item_label", "text", ...) o "clave"
    texto: str

    @property
    def libre(self) -> bool:
        return es_libre(self.texto)


# ---------------------------------------------------------------------------
# Normalización (solo para comparar: nunca se devuelve al proveedor ni al agente)
# ---------------------------------------------------------------------------

# Letras cirílicas y griegas que se ven iguales a letras latinas.
_HOMOGLIFOS = {
    # cirílico
    "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h", "о": "o",
    "р": "p", "с": "c", "т": "t", "у": "y", "х": "x", "і": "i", "ї": "i", "ј": "j",
    "ѕ": "s", "ԁ": "d", "һ": "h", "ԛ": "q", "ԝ": "w", "ӏ": "l",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P",
    "С": "C", "Т": "T", "Х": "X", "У": "Y", "І": "I", "Ј": "J", "Ѕ": "S",
    # griego
    "α": "a", "ο": "o", "ν": "v", "ι": "i", "κ": "k", "ρ": "p", "τ": "t", "υ": "u",
    "χ": "x", "ε": "e", "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I",
    "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
}
_CONFUSABLES = str.maketrans({**_HOMOGLIFOS, "’": "'", "‘": "'", "`": "'", "´": "'"})
# Griegas que se confunden con latinas (μ, π, Ω y el resto no: aparecen en
# unidades y especificaciones).
GRIEGAS_CONFUSABLES = frozenset(c for c in _HOMOGLIFOS if "GREEK" in unicodedata.name(c, ""))

_SEPARADORES = re.compile(r"[_\-‐-―~*|\\/]+")
_PUNTO_ENTRE_LETRAS = re.compile(r"(?<=[a-z])\.(?=[a-z])")
_BLANCOS = re.compile(r"[ \t\r\f\v]+")


def normalizar(texto: str) -> str:
    """Minúsculas, sin acentos ni caracteres invisibles, con homoglifos y letras
    de ancho completo o matemáticas llevadas a ASCII, y `_ - / .` entre
    palabras como espacios. Para que "Ignоre_previous" y "Ignore previous"
    se comparen igual."""
    t = "".join(c for c in texto if unicodedata.category(c) != "Cf")
    t = unicodedata.normalize("NFKC", t).translate(_CONFUSABLES)
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c)).casefold()
    t = _SEPARADORES.sub(" ", t)
    t = _PUNTO_ENTRE_LETRAS.sub(" ", t)
    return _BLANCOS.sub(" ", t)


_PALABRA = re.compile(r"\b[^\W\d_]{2,}\b")


def es_libre(texto: str) -> bool:
    """¿Es texto en lenguaje natural (lo que se le manda al modelo)?

    Dos palabras o más con un espacio, o cuatro o más pegadas con `_` o `-`.
    Los identificadores, códigos y enums ("cart_1", "SUCCESS",
    "basketball_shoes") no: con eso no se le puede dar una instrucción a nadie
    y ya los revisan las heurísticas.
    """
    palabras = len(_PALABRA.findall(normalizar(texto)))
    return palabras >= 4 or (palabras >= 2 and any(c.isspace() for c in texto))


# ---------------------------------------------------------------------------
# JWT (opacos)
# ---------------------------------------------------------------------------

_B64URL = re.compile(r"[A-Za-z0-9_-]+")
_SEGMENTO_SD = re.compile(r"[A-Za-z0-9_.-]*")


def es_jwt(texto: str) -> bool:
    """JWT o SD-JWT de verdad: tres segmentos base64url y una cabecera JSON con
    `alg`. Algo con forma de JWT pero sin cabecera ("Ignore.previous.x") no."""
    if len(texto) < 16 or any(c.isspace() for c in texto):
        return False
    jwt, *resto = texto.split("~")
    if not all(_SEGMENTO_SD.fullmatch(s) for s in resto):
        return False
    partes = jwt.split(".")
    if len(partes) != 3 or not all(_B64URL.fullmatch(p) for p in partes[:2]):
        return False
    if partes[2] and not _B64URL.fullmatch(partes[2]):
        return False
    try:
        crudo = base64.urlsafe_b64decode(partes[0] + "=" * (-len(partes[0]) % 4))
        cabecera = json.loads(crudo)
    except (ValueError, binascii.Error):
        return False
    return isinstance(cabecera, dict) and "alg" in cabecera


# ---------------------------------------------------------------------------
# Recorrido
# ---------------------------------------------------------------------------


def extraer_textos(respuesta: Any) -> list[Texto]:
    """Todos los textos de la respuesta (valores y claves), en orden de
    aparición y sin repetidos. Lanza `RespuestaExcesiva` si supera los topes."""
    salida: list[Texto] = []
    vistos: set[tuple[str, str]] = set()
    nodos = 0
    caracteres = 0

    def agregar(campo: str, texto: str) -> None:
        nonlocal caracteres
        caracteres += len(texto)
        if caracteres > MAX_CARACTERES:
            raise RespuestaExcesiva("demasiado texto")
        if not texto.strip() or es_jwt(texto) or (campo, texto) in vistos:
            return
        vistos.add((campo, texto))
        salida.append(Texto(campo, texto))

    # (valor, campo, es un mensaje del history de la tarea)
    pila: list[tuple[Any, str, bool]] = [(respuesta, "respuesta", False)]
    while pila:
        valor, campo, en_history = pila.pop()
        nodos += 1
        if nodos > MAX_NODOS:
            raise RespuestaExcesiva("demasiados valores")
        if isinstance(valor, str):
            agregar(campo, valor)
        elif isinstance(valor, dict):
            if en_history and valor.get("role") == "user":
                continue  # eco del pedido del comprador
            hijos: list[tuple[Any, str, bool]] = []
            for clave, hijo in valor.items():
                nodos += 1
                if isinstance(clave, str):
                    agregar("clave", clave)
                es_history = valor is respuesta and clave == "history"
                if es_history and isinstance(hijo, list):
                    hijos.extend((m, "history", True) for m in hijo)
                else:
                    hijos.append((hijo, str(clave), False))
            pila.extend(reversed(hijos))
        elif isinstance(valor, list):
            pila.extend((v, campo, False) for v in reversed(valor))
    return salida
