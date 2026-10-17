"""Eventos para el dashboard: memoria (últimos 200), suscriptores SSE y JSONL."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contracts import Evento

logger = logging.getLogger("proxy.eventos")

CAPACIDAD = 200


class BusDeEventos:
    def __init__(self, jsonl: Path | None, capacidad: int = CAPACIDAD) -> None:
        self._buffer: deque[Evento] = deque(maxlen=capacidad)
        self._suscriptores: set[asyncio.Queue[Evento]] = set()
        self._jsonl = jsonl

    def emitir(
        self,
        tipo: str,
        datos: dict[str, Any] | None = None,
        *,
        guard_activo: bool,
        contexto_id: str | None = None,
        escenario: str | None = None,
    ) -> Evento:
        evento = Evento(
            id=uuid.uuid4().hex,
            ts=datetime.now(timezone.utc),
            contexto_id=contexto_id,
            escenario=escenario,
            guard_activo=guard_activo,
            tipo=tipo,
            datos=datos or {},
        )
        self._buffer.append(evento)
        self._escribir_jsonl(evento)
        for cola in list(self._suscriptores):
            try:
                cola.put_nowait(evento)
            except asyncio.QueueFull:
                logger.warning("Suscriptor SSE lento: se descarta el evento %s", evento.id)
        logger.info(
            "evento %-18s contexto=%s escenario=%s guard=%s",
            tipo,
            contexto_id,
            escenario,
            "sí" if guard_activo else "no",
        )
        return evento

    def historial(self, limit: int | None = None) -> list[Evento]:
        eventos = list(self._buffer)
        return eventos[-limit:] if limit else eventos

    def suscribir(self) -> tuple[list[Evento], asyncio.Queue[Evento]]:
        """Devuelve los eventos en memoria y una cola con los que vengan."""
        cola: asyncio.Queue[Evento] = asyncio.Queue(maxsize=1000)
        self._suscriptores.add(cola)
        return list(self._buffer), cola

    def desuscribir(self, cola: asyncio.Queue[Evento]) -> None:
        self._suscriptores.discard(cola)

    def _escribir_jsonl(self, evento: Evento) -> None:
        if self._jsonl is None:
            return
        try:
            self._jsonl.parent.mkdir(parents=True, exist_ok=True)
            with self._jsonl.open("a", encoding="utf-8") as f:
                f.write(evento.model_dump_json() + "\n")
        except OSError as e:
            logger.warning("No se pudo escribir %s: %s", self._jsonl, e)
