"""Pre-Sign Guard: núcleo determinístico (Dev 2).

`evaluar()` decide con datos estructurados y verificados, sin LLM y sin I/O.
Implementa el protocolo `contracts.Evaluador`.
"""

from guard.veredicto import evaluar

__all__ = ["evaluar"]
