"""Compras de la etapa 2 (docs/FLUJO.md): contexto, oportunidad y parámetros.

Una compra empieza con el primer `find_products` de un contexto, con el
Guard prendido y el agente de intención disponible. Guarda:

- el pedido y los **parámetros autorizados** (`ficha`): la ficha confirmada
  de la demo si el escenario la tiene o, si no, la que armó el agente con el
  pedido (`ficha_llm`), hasta que el usuario acepte otros;
- el **contexto** (el porqué que contó el usuario);
- la **oportunidad** que se le consultó y la decisión.

Las dos esperas (la respuesta del porqué y la decisión) son `Future`s que
resuelve la API de control. Si llega otra búsqueda de la misma compra
mientras tanto, espera el mismo resultado (`contexto_listo`, `resuelta`).
El silencio no es autorización: si vence la decisión, la compra queda
`vencida` y no se entrega nada.

Todo vive en memoria, como las aprobaciones: un reinicio del proxy corta las
compras en curso (el agente del cliente recibe un error y no compra).
"""

from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from contracts import Contexto, Ficha, Oportunidad, Requisito

EstadoCompra = Literal[
    "esperando_contexto",  # se le preguntó el porqué al usuario
    "negociando",          # contexto listo (u omitido): el pedido va al proveedor
    "esperando_decision",  # hay una oportunidad: la respuesta del proveedor está retenida
    "sin_oportunidad",     # la negociación sigue con el pedido original
    "bloqueada",           # el filtro de seguridad cortó la conversación
    "aceptada",
    "rechazada",
    "vencida",
]
Decision = Literal["aceptar", "rechazar"]

MAX_COMPRAS = 200
LARGO_MAXIMO_RESPUESTA = 1000


class CompraDesconocida(LookupError):
    pass


class EstadoInvalido(RuntimeError):
    def __init__(self, estado: str) -> None:
        super().__init__(f"La compra está en estado {estado}")
        self.estado = estado


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Compra:
    id: str
    creada: datetime
    pedido: str
    pregunta: str
    ficha_llm: Ficha | None              # los parámetros que armó el agente con el pedido
    ficha: Ficha | None = None           # parámetros autorizados vigentes (ver resolver)
    contexto_id: str | None = None
    estado: EstadoCompra = "esperando_contexto"
    contexto: Contexto | None = None
    oportunidad: Oportunidad | None = None
    vence: datetime | None = None
    decision: Literal["aceptada", "rechazada", "vencida"] | None = None
    requisitos_autorizados: list[Requisito] | None = None
    ficha_resuelta: bool = False
    _respuesta: asyncio.Future = field(default=None, repr=False)  # type: ignore[assignment]
    _decision: asyncio.Future = field(default=None, repr=False)  # type: ignore[assignment]
    contexto_listo: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    resuelta: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def requisitos(self) -> list[Requisito]:
        """Los parámetros vigentes: los resueltos o, antes, los del pedido."""
        ficha = self.ficha if self.ficha_resuelta else self.ficha_llm
        return list(ficha.requisitos) if ficha else []

    def resolver_ficha(self, confirmada: Ficha | None) -> Ficha | None:
        """Una sola vez: la ficha confirmada de la demo, si hay, manda sobre
        la del agente (el LLM puede extraer de más o de menos: CLAUDE.md →
        "Ficha en vivo"). Después de aceptar una oportunidad no se toca."""
        if not self.ficha_resuelta:
            self.ficha = confirmada if confirmada is not None else self.ficha_llm
            self.ficha_resuelta = True
        return self.ficha

    def a_json(self) -> dict[str, Any]:
        return {
            "compra_id": self.id,
            "estado": self.estado,
            "creada": self.creada.isoformat(),
            "contexto_id": self.contexto_id,
            "pedido": self.pedido,
            "pregunta": self.pregunta,
            "requisitos": [r.model_dump(mode="json") for r in self.requisitos],
            "contexto": self.contexto.model_dump(mode="json") if self.contexto else None,
            "oportunidad": self.oportunidad.model_dump(mode="json")
            if self.oportunidad
            else None,
            "vence": self.vence.isoformat() if self.vence else None,
            "decision": self.decision,
            "requisitos_autorizados": [
                r.model_dump(mode="json") for r in self.requisitos_autorizados
            ]
            if self.requisitos_autorizados is not None
            else None,
        }

    # -- esperas -------------------------------------------------------------

    def fijar_plazo(self, segundos: float) -> datetime:
        """El vencimiento de la espera que sigue (va en el evento al dashboard)."""
        self.vence = _ahora() + timedelta(seconds=max(0.0, segundos))
        return self.vence

    def restante(self) -> float:
        if self.vence is None:
            return 0.0
        return max(0.0, (self.vence - _ahora()).total_seconds())

    async def esperar_respuesta(self) -> tuple[str | None, bool]:
        """(respuesta, vencido). Respuesta None = el usuario omitió."""
        try:
            respuesta = await asyncio.wait_for(asyncio.shield(self._respuesta), self.restante())
            return respuesta, False
        except asyncio.TimeoutError:
            return None, True
        finally:
            self.vence = None

    async def esperar_decision(self) -> Decision | None:
        """La decisión del usuario, o None si venció."""
        try:
            return await asyncio.wait_for(asyncio.shield(self._decision), self.restante())
        except asyncio.TimeoutError:
            return None
        finally:
            self.vence = None


class RegistroCompras:
    def __init__(self, maximo: int = MAX_COMPRAS) -> None:
        self._compras: OrderedDict[str, Compra] = OrderedDict()
        self._por_contexto: dict[str, str] = {}
        self._maximo = maximo

    def crear(
        self,
        pedido: str,
        pregunta: str,
        ficha_llm: Ficha | None,
        contexto_id: str | None,
    ) -> Compra:
        loop = asyncio.get_running_loop()
        compra = Compra(
            id=f"cmp-{uuid.uuid4().hex[:16]}",
            creada=_ahora(),
            pedido=pedido,
            pregunta=pregunta,
            ficha_llm=ficha_llm,
            _respuesta=loop.create_future(),
            _decision=loop.create_future(),
        )
        self._compras[compra.id] = compra
        if contexto_id:
            self.atar(compra, contexto_id)
        while len(self._compras) > self._maximo:
            _, vieja = self._compras.popitem(last=False)
            for ctx in [c for c, cid in self._por_contexto.items() if cid == vieja.id]:
                del self._por_contexto[ctx]
        return compra

    def atar(self, compra: Compra, contexto_id: str) -> bool:
        """Ata el contextId (lo crea el comercio en su primera respuesta).

        False si ese contexto ya es de otra compra: un comercio no puede
        "mudar" una compra al contexto de otra (y llevarse sus parámetros).
        """
        actual = self._por_contexto.get(contexto_id)
        if actual is not None and actual != compra.id:
            return False
        compra.contexto_id = compra.contexto_id or contexto_id
        self._por_contexto[contexto_id] = compra.id
        return True

    def get(self, compra_id: str) -> Compra | None:
        return self._compras.get(compra_id)

    def de_contexto(self, contexto_id: str | None) -> Compra | None:
        if not contexto_id:
            return None
        compra_id = self._por_contexto.get(contexto_id)
        return self._compras.get(compra_id) if compra_id else None

    def listar(self) -> list[dict[str, Any]]:
        return [c.a_json() for c in reversed(self._compras.values())]

    # -- lo que llega de la API de control -----------------------------------

    def responder(self, compra_id: str, respuesta: str | None) -> Compra:
        """La respuesta del porqué (None = omitir)."""
        compra = self._buscar(compra_id)
        if compra.estado != "esperando_contexto" or compra._respuesta.done():
            raise EstadoInvalido(compra.estado)
        compra._respuesta.set_result(respuesta)
        compra.estado = "negociando"
        return compra

    def decidir(self, compra_id: str, decision: Decision) -> Compra:
        compra = self._buscar(compra_id)
        if compra.estado != "esperando_decision" or compra._decision.done():
            raise EstadoInvalido(compra.estado)
        compra._decision.set_result(decision)
        return compra

    def _buscar(self, compra_id: str) -> Compra:
        compra = self._compras.get(compra_id)
        if compra is None:
            raise CompraDesconocida(compra_id)
        return compra
