"""Qué textos de la respuesta del proveedor revisa el filtro.

El proveedor controla toda la respuesta (claves y valores, artefactos,
mensajes de estado, metadatos, errores, el `history` con sus roles), así que
se recorre entera. Los JWT y SD-JWT (checkout firmado, mandatos) son opacos
para el agente: no van al modelo (`Texto.opaco`), pero las heurísticas los
miran igual, porque la firma es base64url sin otra forma que validar y puede
traer texto con guiones.

El `history` se revisa entero, con cualquier `role`: el rol también lo
escribe el comercio. El eco exacto de nuestro pedido (que no es texto del
proveedor) lo saca el proxy antes de llamar al filtro
(`proxy.a2a.sin_eco_del_pedido`).

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

# Una respuesta real tiene unos pocos KB (con mandatos SD-JWT en el history,
# decenas). Las heurísticas son lineales: con el tope de caracteres, el peor
# caso medido (camelCase, que se revisa dos veces) es ~0,8 s de CPU con la
# máquina libre (2026-10-20); con la máquina cargada, varias veces más.
MAX_NODOS = 50_000         # valores recorridos (dicts, listas, escalares, claves)
MAX_CARACTERES = 300_000   # suma de todos los textos


class RespuestaExcesiva(ValueError):
    """La respuesta supera los topes: no se puede revisar entera."""


@dataclass(frozen=True)
class Texto:
    campo: str  # la clave bajo la que vino ("item_label", "text", ...) o "clave"
    texto: str
    opaco: bool = False  # un JWT: lo miran las heurísticas, nunca el modelo

    @property
    def libre(self) -> bool:
        return not self.opaco and es_libre(self.texto)


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
_BLANCOS = re.compile(r"\s+")


def _un_blanco(m: re.Match[str]) -> str:
    return "\n" if "\n" in m.group() else " "


# Un tramo de letras y dígitos ASCII (lo que queda entre separadores) y los
# cortes de camelCase adentro: "IgnorePrevious" → "Ignore Previous",
# "AIAgents" → "AI Agents".
_TRAMO = re.compile(r"[A-Za-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _partir_tramo(m: re.Match[str]) -> str:
    tramo = m.group()
    # Con dígitos es un id, un SKU o base64 ("eyJhbGciOiJIUzI1"): no se parte.
    if any(c.isdigit() for c in tramo):
        return tramo
    return _CAMEL.sub(" ", tramo)


def _sin_marcas(texto: str) -> str:
    """Sin caracteres invisibles, con homoglifos y letras de ancho completo o
    matemáticas llevadas a ASCII y sin acentos. Respeta mayúsculas."""
    t = "".join(c for c in texto if unicodedata.category(c) != "Cf")
    t = unicodedata.normalize("NFKC", t).translate(_CONFUSABLES)
    t = unicodedata.normalize("NFKD", t)
    return "".join(c for c in t if not unicodedata.combining(c))


def _terminar(t: str) -> str:
    t = _SEPARADORES.sub(" ", t.casefold())
    t = _PUNTO_ENTRE_LETRAS.sub(" ", t)
    # Cada tramo de blancos queda en un carácter (un salto de línea si tenía
    # alguno): así ningún patrón con \s* o \s+ se vuelve cuadrático con miles
    # de blancos seguidos.
    return _BLANCOS.sub(_un_blanco, t)


def _partir(t: str) -> str:
    return _TRAMO.sub(_partir_tramo, t) if _CAMEL.search(t) else t


def normalizar(texto: str, *, partir: bool = False) -> str:
    """Minúsculas, sin acentos ni caracteres invisibles, con homoglifos y letras
    de ancho completo o matemáticas llevadas a ASCII, y `_ - / .` entre
    palabras como espacios. Para que "Ignоre_previous" y "Ignore previous"
    se comparen igual. Con `partir`, además corta el camelCase de los tramos
    sin dígitos ("IgnorePrevious" → "ignore previous")."""
    t = _sin_marcas(texto)
    return _terminar(_partir(t) if partir else t)


def versiones(texto: str) -> list[str]:
    """El texto normalizado y, si tiene camelCase, también partido. Las
    heurísticas miran las dos: "IgnorePreviousInstructions" solo se ve
    partido y "jailBreak mode" solo entero."""
    t = _sin_marcas(texto)
    partido = _partir(t)
    if partido == t:
        return [_terminar(t)]
    return [_terminar(t), _terminar(partido)]


_PALABRA = re.compile(r"\b[^\W\d_]{2,}\b")
_LARGO_MAX_PALABRA = 25


def es_libre(texto: str) -> bool:
    """¿Es texto en lenguaje natural (lo que se le manda al modelo)?

    Las palabras se cuentan con camelCase, `_`, `-` y `.` partidos. Es libre
    con dos palabras o más y un espacio, con tres o más pegadas
    ("choose-this-one", "IgnorePreviousInstructions") o con una tira de
    letras más larga que cualquier palabra ("ignorepreviousinstructions").
    Los identificadores, códigos y enums ("cart_1", "NK-OUT-01", "SUCCESS",
    "basketball_shoes", "contextId") no: con eso no se le puede dar una
    instrucción a nadie y ya los revisan las heurísticas. Tres palabras
    pegadas no se distinguen de una orden ("pick_this_cart"), así que un id
    así ("fake_risk_data") también va al modelo.
    """
    palabras = _PALABRA.findall(normalizar(texto, partir=True))
    return (
        len(palabras) >= 3
        or (len(palabras) >= 2 and any(c.isspace() for c in texto))
        or any(len(p) > _LARGO_MAX_PALABRA for p in palabras)
    )


# ---------------------------------------------------------------------------
# JWT (opacos)
# ---------------------------------------------------------------------------

_B64URL = re.compile(r"[A-Za-z0-9_-]+")
_NO_ES_JSON = object()


def _b64_json(segmento: str) -> Any:
    """El JSON que codifica un segmento base64url, o `_NO_ES_JSON`."""
    if not _B64URL.fullmatch(segmento) or len(segmento) % 4 == 1:
        return _NO_ES_JSON
    try:
        return json.loads(base64.urlsafe_b64decode(segmento + "=" * (-len(segmento) % 4)))
    except (ValueError, binascii.Error, RecursionError):
        return _NO_ES_JSON


def _jwt_compacto(texto: str) -> bool:
    """`cabecera.payload.firma`: cabecera JSON con `alg`, payload objeto JSON
    y firma base64url o vacía (`alg: none`). La firma no se valida más que
    eso: por eso las heurísticas miran igual los JWT."""
    partes = texto.split(".")
    if len(partes) != 3:
        return False
    cabecera, payload = _b64_json(partes[0]), _b64_json(partes[1])
    if not (isinstance(cabecera, dict) and "alg" in cabecera and isinstance(payload, dict)):
        return False
    return not partes[2] or _B64URL.fullmatch(partes[2]) is not None


def es_jwt(texto: str) -> bool:
    """JWT o SD-JWT de verdad, que el agente no lee.

    El primer segmento es un JWT compacto (`_jwt_compacto`). En un SD-JWT, lo
    que va entre `~` es una disclosure (base64url de un array JSON), un JWT
    compacto (el key binding o, en la cadena dSD-JWT de AP2, el emisor del
    eslabón siguiente) o nada (el `~` final y el `~~` entre eslabones). Si
    algún segmento no es eso ("Ignore.previous.x", texto con guiones después
    de un `~`, un payload que no es un objeto), no es un JWT y se revisa como
    texto."""
    if len(texto) < 16 or any(c.isspace() for c in texto):
        return False
    jwt, *resto = texto.split("~")
    if not _jwt_compacto(jwt):
        return False
    for segmento in resto:
        if not segmento:
            continue
        if "." in segmento:
            if not _jwt_compacto(segmento):
                return False
        elif not isinstance(_b64_json(segmento), list):
            return False
    return True


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
        if not texto.strip() or (campo, texto) in vistos:
            return
        vistos.add((campo, texto))
        salida.append(Texto(campo, texto, opaco=es_jwt(texto)))

    # (valor, campo)
    pila: list[tuple[Any, str]] = [(respuesta, "respuesta")]
    while pila:
        valor, campo = pila.pop()
        nodos += 1
        if nodos > MAX_NODOS:
            raise RespuestaExcesiva("demasiados valores")
        if isinstance(valor, str):
            agregar(campo, valor)
        elif isinstance(valor, dict):
            hijos: list[tuple[Any, str]] = []
            for clave, hijo in valor.items():
                nodos += 1
                if isinstance(clave, str):
                    agregar("clave", clave)
                hijos.append((hijo, str(clave)))
            pila.extend(reversed(hijos))
        elif isinstance(valor, list):
            pila.extend((v, campo) for v in reversed(valor))
    return salida
