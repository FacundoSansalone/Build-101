"""Aprobaciones humanas de checkouts retenidos (`escalar`).

Decisión del equipo (CLAUDE.md → Ronda de revisión 2026-10-17): cuando el
Guard da `escalar`, el proxy retiene el checkout y el humano decide en el
dashboard ("Aprobar igual"). La aprobación:

- es de un solo uso;
- vale para un contexto y un cart;
- queda atada al monto (y la moneda) del último checkout que el Guard
  escaló en ese contexto/cart: si el reintento trae otro monto, no aplica.

El proxy no decide: el Guard vuelve a evaluar cada reintento y la aprobación
solo cuenta si el veredicto nuevo es otra vez `escalar` (nunca salta un
`bloqueado`). Sin I/O.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Any

MAX_REGISTROS = 500


@dataclass(frozen=True)
class UltimoVeredicto:
    status: str
    centavos: int | None
    moneda: str | None


@dataclass(frozen=True)
class Aprobacion:
    id: str
    contexto_id: str
    cart_id: str
    centavos: int
    moneda: str | None
    creada: str
    estado: str = "pendiente"  # pendiente | usada
    usada: str | None = None
    task_id: str | None = None

    def a_json(self) -> dict[str, Any]:
        datos = asdict(self)
        datos["monto"] = self.centavos / 100
        return datos


class SinEscalar(Exception):
    """No hay un `escalar` vigente para ese contexto y cart."""


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


class RegistroAprobaciones:
    def __init__(self) -> None:
        self._ultimo: OrderedDict[tuple[str, str], UltimoVeredicto] = OrderedDict()
        self._pendientes: OrderedDict[tuple[str, str], Aprobacion] = OrderedDict()
        self._usadas: deque[Aprobacion] = deque(maxlen=200)

    def registrar_veredicto(
        self,
        contexto_id: str,
        cart_id: str,
        status: str,
        centavos: int | None,
        moneda: str | None,
    ) -> None:
        clave = (contexto_id, cart_id)
        self._ultimo[clave] = UltimoVeredicto(status, centavos, moneda)
        self._ultimo.move_to_end(clave)
        while len(self._ultimo) > MAX_REGISTROS:
            self._ultimo.popitem(last=False)

    def aprobar(self, contexto_id: str, cart_id: str) -> tuple[Aprobacion, bool]:
        """(aprobación, creada). Si ya había una pendiente para el mismo monto,
        devuelve esa. Lanza SinEscalar si el último veredicto no fue escalar."""
        clave = (contexto_id, cart_id)
        ultimo = self._ultimo.get(clave)
        if ultimo is None or ultimo.status != "escalar":
            raise SinEscalar(
                "el último veredicto de ese contexto y cart no es escalar"
            )
        if ultimo.centavos is None:
            raise SinEscalar("el checkout escalado no tiene un monto legible")
        previa = self._pendientes.get(clave)
        if (
            previa is not None
            and previa.centavos == ultimo.centavos
            and previa.moneda == ultimo.moneda
        ):
            return previa, False
        aprobacion = Aprobacion(
            id=f"apr-{uuid.uuid4().hex[:12]}",
            contexto_id=contexto_id,
            cart_id=cart_id,
            centavos=ultimo.centavos,
            moneda=ultimo.moneda,
            creada=_ahora(),
        )
        self._pendientes[clave] = aprobacion
        while len(self._pendientes) > MAX_REGISTROS:
            self._pendientes.popitem(last=False)
        return aprobacion, True

    def disponible(
        self, contexto_id: str, cart_id: str, centavos: int | None, moneda: str | None
    ) -> Aprobacion | None:
        previa = self._pendientes.get((contexto_id, cart_id))
        if previa is None or centavos is None:
            return None
        if previa.centavos != centavos or previa.moneda != moneda:
            return None
        return previa

    def consumir(
        self,
        contexto_id: str,
        cart_id: str,
        centavos: int | None,
        moneda: str | None,
        task_id: str | None = None,
    ) -> Aprobacion | None:
        """Usa la aprobación si existe y es para ese monto. Un solo uso."""
        previa = self.disponible(contexto_id, cart_id, centavos, moneda)
        if previa is None:
            return None
        del self._pendientes[(contexto_id, cart_id)]
        usada = replace(previa, estado="usada", usada=_ahora(), task_id=task_id)
        self._usadas.append(usada)
        return usada

    def listar(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "pendientes": [a.a_json() for a in self._pendientes.values()],
            "usadas": [a.a_json() for a in self._usadas],
        }
