"""El pedido con las palabras del usuario (la demo de una pantalla, `/demo`).

El agente de compras le pasa al comercio una paráfrasis del pedido y puede
perder datos que el usuario dio: "Comprame 20 computadoras para el lunes,
hasta 20000 USD." llegó como "20 computadoras por un total de 20000 USD", y
el intake repreguntaba el plazo. El dashboard registra lo que el usuario le
dijo al agente en la compra (en cada mensaje: una búsqueda en otro turno es
otra compra para el proxy) y el proxy lo usa en la próxima compra que
detecta: el intake lee las palabras del usuario y la búsqueda del agente.

Garantías:

- Entra solo por `POST /guard/compras/pedido`, la API de control local
  (`ControlLocal`: Host local, JSON y `X-Guard-Token`). Nunca sale del
  comercio ni del agente de compras, que no tienen el token.
- Uno por vez (uno nuevo reemplaza al anterior), de hasta `LARGO_MAXIMO`
  caracteres, sin caracteres de control; vence a los `VIGENCIA_S` segundos y
  se usa una sola vez: en la próxima compra nueva. Una compra que ya existe
  no cambia.
- Sin registro, o vencido, todo como antes: el pedido es la búsqueda.
- No autoriza nada por sí: el intake lo lee como lo que dijo el usuario, con
  los umbrales de siempre (lo inferido o dudoso sigue necesitando que el
  usuario confirme la ficha) y la ficha confirmada de la demo sigue
  mandando sobre la del intake.
- El tráfico de integradores (`/v1` y A2A con clave) no lo usa: su contexto
  es otra etapa (`ProxyGuard._etapa_contexto_api`).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

LARGO_MAXIMO = 1000  # como una respuesta al porqué
# Entre que el usuario escribe y el agente busca puede haber una repregunta
# del agente de compras: unos minutos alcanzan; más, ya es otra compra.
VIGENCIA_S = 180.0

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _limpio(texto: str) -> str:
    """En una línea, sin caracteres de control ni espacios de más."""
    return " ".join(_CONTROL.sub("", texto).split())


class PedidoDelUsuario:
    """Lo que escribió el usuario, para la próxima compra (uno por vez)."""

    def __init__(
        self, vigencia_s: float = VIGENCIA_S, reloj: Callable[[], float] = time.monotonic
    ) -> None:
        self.vigencia_s = vigencia_s
        self._reloj = reloj
        self._texto: str | None = None
        self._vence = 0.0

    def registrar(self, texto: object) -> datetime:
        """Lo guarda (reemplaza al anterior) y devuelve cuándo vence.
        ValueError si no es texto, está vacío o se pasa de `LARGO_MAXIMO`."""
        if not isinstance(texto, str) or len(texto) > LARGO_MAXIMO:
            raise ValueError("Se espera un texto de hasta LARGO_MAXIMO caracteres")
        limpio = _limpio(texto)
        if not limpio:
            raise ValueError("El pedido está vacío")
        self._texto, self._vence = limpio, self._reloj() + self.vigencia_s
        return datetime.now(timezone.utc) + timedelta(seconds=self.vigencia_s)

    @property
    def pendiente(self) -> bool:
        return self._texto is not None and self._reloj() < self._vence

    def tomar(self) -> str | None:
        """El texto, si no venció, y lo borra: se usa una sola vez."""
        texto = self._texto if self.pendiente else None
        self._texto = None
        return texto


def _comparable(texto: str) -> str:
    return _limpio(texto).casefold()


def pedido_para_intake(busqueda: str, del_usuario: str | None) -> str:
    """El pedido que lee el intake: las palabras del usuario y, si agrega
    algo, la búsqueda del agente ("<usuario> / <búsqueda>"). Primero el
    usuario: de ahí sale el idioma de las preguntas. Sin palabras del
    usuario, la búsqueda tal cual (como siempre)."""
    usuario = _limpio(del_usuario or "")
    if not usuario:
        return busqueda
    if _comparable(busqueda) in _comparable(usuario):
        return usuario
    return f"{usuario} / {_limpio(busqueda)}"
