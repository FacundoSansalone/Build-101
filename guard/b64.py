"""base64url sin relleno (JWS/JWT): lo usan el recibo y el checkout firmado."""

from __future__ import annotations

import base64

_ALFABETO_B64URL = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def b64url(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode("ascii")


def b64url_decode(texto: str) -> bytes:
    """Decodifica base64url sin relleno; lanza ValueError si es inválido."""
    if not texto or any(c not in _ALFABETO_B64URL for c in texto):
        raise ValueError("base64url inválido")
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))
