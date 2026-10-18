"""Aprobaciones humanas de checkouts retenidos (`escalar`).

Decisión del equipo (CLAUDE.md → Ronda de revisión 2026-10-17): cuando el
Guard da `escalar`, el proxy retiene el checkout y el humano decide en el
dashboard ("Aprobar igual"). La aprobación:

- es de un solo uso;
- vale para un contexto y un cart;
- queda atada al monto y la moneda **firmados** del último checkout que el
  Guard escaló en ese contexto/cart (`a2a.monto_firmado`: el total entero
  del JWT, sin fallbacks): si el reintento firma otro monto u otra moneda,
  no aplica. Si el checkout escalado no tiene un total firmado exacto, no se
  puede aprobar.

El proxy no decide: el Guard vuelve a evaluar cada reintento y la aprobación
solo cuenta si el veredicto nuevo es otra vez `escalar` (nunca salta un
`bloqueado`). Sin I/O.

Gateway de producción: con un almacén durable (`proxy/estado.py`), el núcleo
hidrata la aprobación pendiente con `restaurar_pendiente` y la reclama en el
almacén antes de usarla (un solo uso, atómico en la base). Si guardarla en
el almacén falla, la saca de memoria con `descartar`.

Compras de integrador: solo las autoriza su dueño, por `/v1` (diseño §1.3).
El registro recuerda qué (contexto, cart) son de una compra de integrador
(`registrar_veredicto(..., de_api=True)`) y lo usa para decidir, sin
depender de que la compra siga en memoria: el dashboard no las puede aprobar
(`DeIntegrador`) y, para ellas, solo cuenta una aprobación creada por `/v1`
(`Aprobacion.via_api`, `solo_api=True`).
"""

from __future__ import annotations

import uuid
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Any

from proxy.tipos_gateway import ID_SEGURO_RE, ErrorAlmacen, es_id

MAX_REGISTROS = 500


@dataclass(frozen=True)
class UltimoVeredicto:
    status: str
    centavos: int | None
    moneda: str | None
    de_api: bool = False  # el checkout es de una compra de integrador


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
    via_api: bool = False  # la creó el dueño de la compra por /v1

    def a_json(self) -> dict[str, Any]:
        datos = asdict(self)
        del datos["via_api"]  # interno: el JSON de siempre (dashboard y /v1)
        datos["monto"] = self.centavos / 100
        return datos


def aprobacion_desde_fila(fila: dict[str, Any]) -> Aprobacion:
    """La aprobación de una fila de `gw_aprobaciones` (las columnas de más,
    `integrador_id` y `compra_id`, son solo auditoría). Esa tabla solo la
    escribe `/v1`, así que la aprobación vuelve con `via_api=True`.

    Una fila que no valida da `ErrorAlmacen`: nunca se arma una aprobación a
    medias, porque una aprobación es una autorización."""
    try:
        if not isinstance(fila, dict):
            raise TypeError("la fila no es un objeto")
        for clave in ("id", "contexto_id", "cart_id"):
            if not es_id(ID_SEGURO_RE, fila[clave]):
                raise ValueError(f"{clave} inválido")
        centavos = fila["centavos"]
        if not isinstance(centavos, int) or isinstance(centavos, bool):
            raise TypeError("centavos no es entero")
        moneda = fila["moneda"]
        if not isinstance(moneda, str) or not moneda:
            raise TypeError("moneda inválida")
        estado = fila["estado"]
        if estado not in ("pendiente", "usada"):
            raise ValueError("estado desconocido")
        creada = fila["creada"]
        if not isinstance(creada, str):
            raise TypeError("creada no es texto")
        usada = fila.get("usada")
        task_id = fila.get("task_id")
        for valor in (usada, task_id):
            if valor is not None and not isinstance(valor, str):
                raise TypeError("se esperaba texto")
    except (KeyError, TypeError, ValueError) as e:
        raise ErrorAlmacen(f"fila de aprobación inválida ({e})") from None
    return Aprobacion(
        id=fila["id"],
        contexto_id=fila["contexto_id"],
        cart_id=fila["cart_id"],
        centavos=centavos,
        moneda=moneda,
        creada=creada,
        estado=estado,
        usada=usada,
        task_id=task_id,
        via_api=True,
    )


class SinEscalar(Exception):
    """No hay un `escalar` vigente para ese contexto y cart."""


class DeIntegrador(Exception):
    """El checkout es de una compra de integrador: lo aprueba su dueño por /v1."""


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
        *,
        de_api: bool = False,
    ) -> None:
        """`de_api`: el contexto es de una compra de integrador. Una vez
        marcado, queda marcado (un contexto no deja de ser de su compra)."""
        clave = (contexto_id, cart_id)
        previo = self._ultimo.get(clave)
        de_api = de_api or (previo is not None and previo.de_api)
        self._ultimo[clave] = UltimoVeredicto(status, centavos, moneda, de_api)
        self._ultimo.move_to_end(clave)
        while len(self._ultimo) > MAX_REGISTROS:
            self._ultimo.popitem(last=False)

    def aprobar(
        self, contexto_id: str, cart_id: str, *, via_api: bool = False
    ) -> tuple[Aprobacion, bool]:
        """(aprobación, creada). Si ya había una pendiente para el mismo monto,
        devuelve esa. Lanza SinEscalar si el último veredicto no fue escalar
        y DeIntegrador si es de una compra de integrador y no llega por /v1."""
        clave = (contexto_id, cart_id)
        ultimo = self._ultimo.get(clave)
        if ultimo is not None and ultimo.de_api and not via_api:
            raise DeIntegrador("el checkout es de una compra de integrador")
        if ultimo is None or ultimo.status != "escalar":
            raise SinEscalar(
                "el último veredicto de ese contexto y cart no es escalar"
            )
        if ultimo.centavos is None or ultimo.moneda is None:
            raise SinEscalar(
                "el checkout escalado no tiene un total firmado exacto (entero en "
                "centavos y moneda en el checkout_jwt): no se puede aprobar"
            )
        previa = self._pendientes.get(clave)
        if (
            previa is not None
            and previa.centavos == ultimo.centavos
            and previa.moneda == ultimo.moneda
            and previa.via_api == via_api
        ):
            return previa, False
        aprobacion = Aprobacion(
            id=f"apr-{uuid.uuid4().hex[:12]}",
            contexto_id=contexto_id,
            cart_id=cart_id,
            centavos=ultimo.centavos,
            moneda=ultimo.moneda,
            creada=_ahora(),
            via_api=via_api,
        )
        self._pendientes[clave] = aprobacion
        while len(self._pendientes) > MAX_REGISTROS:
            self._pendientes.popitem(last=False)
        return aprobacion, True

    def disponible(
        self,
        contexto_id: str,
        cart_id: str,
        centavos: int | None,
        moneda: str | None,
        *,
        solo_api: bool = False,
    ) -> Aprobacion | None:
        """La pendiente de ese contexto, cart y monto. `solo_api`: el checkout
        es de una compra de integrador y solo vale una creada por /v1."""
        previa = self._pendientes.get((contexto_id, cart_id))
        if previa is None or centavos is None or moneda is None:
            return None
        if solo_api and not previa.via_api:
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
        *,
        solo_api: bool = False,
    ) -> Aprobacion | None:
        """Usa la aprobación si existe y es para ese monto. Un solo uso."""
        previa = self.disponible(contexto_id, cart_id, centavos, moneda, solo_api=solo_api)
        if previa is None:
            return None
        return self.usar(previa, task_id)

    def usar(self, aprobacion: Aprobacion, task_id: str | None = None) -> Aprobacion:
        """Marca usada una aprobación que ya se reclamó (en el almacén
        durable). La saca de las pendientes si sigue ahí: otro checkout
        concurrente puede haberla descartado entre el reclamo y este uso, y
        el reclamo ganado igual vale (si no, se quemaría sin pasar nada)."""
        if self._pendientes.get((aprobacion.contexto_id, aprobacion.cart_id)) == aprobacion:
            del self._pendientes[(aprobacion.contexto_id, aprobacion.cart_id)]
        usada = replace(aprobacion, estado="usada", usada=_ahora(), task_id=task_id)
        self._usadas.append(usada)
        return usada

    # -- gateway: hidratación desde el almacén ---------------------------------

    def ultimo(self, contexto_id: str, cart_id: str) -> UltimoVeredicto | None:
        """El último veredicto registrado para ese contexto y cart."""
        return self._ultimo.get((contexto_id, cart_id))

    def pendiente(self, contexto_id: str, cart_id: str) -> Aprobacion | None:
        """La aprobación pendiente de ese contexto y cart, sin mirar el monto
        (para eso está `disponible`)."""
        return self._pendientes.get((contexto_id, cart_id))

    def restaurar_pendiente(self, aprobacion: Aprobacion) -> None:
        """Vuelve a poner en memoria una aprobación pendiente del almacén.

        Solo si no hay otra pendiente para ese contexto y cart (la de memoria
        es la más nueva) y si no se usó ya en este proceso: una aprobación
        usada nunca vuelve a quedar disponible."""
        if aprobacion.estado != "pendiente":
            return
        clave = (aprobacion.contexto_id, aprobacion.cart_id)
        if clave in self._pendientes:
            return
        if any(u.id == aprobacion.id for u in self._usadas):
            return
        self._pendientes[clave] = aprobacion
        while len(self._pendientes) > MAX_REGISTROS:
            self._pendientes.popitem(last=False)

    def descartar(self, aprobacion_id: str) -> bool:
        """Saca una pendiente por id (rollback: no se pudo guardar en el
        almacén durable). True si estaba."""
        for clave, aprobacion in self._pendientes.items():
            if aprobacion.id == aprobacion_id:
                del self._pendientes[clave]
                return True
        return False

    def listar(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "pendientes": [a.a_json() for a in self._pendientes.values()],
            "usadas": [a.a_json() for a in self._usadas],
        }
