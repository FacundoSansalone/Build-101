"""Pre-Sign Guard: núcleo determinístico (Dev 2).

`evaluar()` decide con datos estructurados y verificados, sin LLM y sin I/O.
Implementa el protocolo `contracts.Evaluador`. El recibo que devuelve va
sin firmar; se firma afuera con `firmar_recibo()`.
"""

from guard.recibo import (
    cargar_o_crear_clave,
    clave_publica_jwk,
    firmar_recibo,
    verificar_recibo,
)
from guard.veredicto import evaluar

__all__ = [
    "cargar_o_crear_clave",
    "clave_publica_jwk",
    "evaluar",
    "firmar_recibo",
    "verificar_recibo",
]
