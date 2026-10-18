"""Eventos para el dashboard: memoria (últimos 200), suscriptores SSE y JSONL.

Los eventos de compras de integrador (`privado=True`) van a otro JSONL: el
principal lo sirve el dashboard para el replay y esos llevan checkout_jwt y
mandatos de clientes del gateway. En memoria y en el SSE están todos (el
proxy los filtra sin token, `ProxyGuard._visible`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contracts import Evento
from proxy.config import MAX_SUSCRIPTORES_SSE

logger = logging.getLogger("proxy.eventos")

CAPACIDAD = 200


class BusDeEventos:
    def __init__(
        self,
        jsonl: Path | None,
        capacidad: int = CAPACIDAD,
        max_suscriptores: int = MAX_SUSCRIPTORES_SSE,
        jsonl_privado: Path | None = None,
    ) -> None:
        self._buffer: deque[Evento] = deque(maxlen=capacidad)
        self._suscriptores: set[asyncio.Queue[Evento]] = set()
        self._jsonl = jsonl
        # Sin archivo privado, los privados no se copian a ningún JSONL.
        self._jsonl_privado = jsonl_privado
        self._max_suscriptores = max_suscriptores

    def emitir(
        self,
        tipo: str,
        datos: dict[str, Any] | None = None,
        *,
        guard_activo: bool,
        contexto_id: str | None = None,
        escenario: str | None = None,
        privado: bool = False,
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
        self._escribir_jsonl(evento, self._jsonl_privado if privado else self._jsonl)
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

    def suscribir(self) -> tuple[list[Evento], asyncio.Queue[Evento]] | None:
        """Los eventos en memoria y una cola con los que vengan.

        None si ya hay `max_suscriptores` conectados (cada uno guarda hasta
        1000 eventos en su cola: el tope acota la memoria).
        """
        if len(self._suscriptores) >= self._max_suscriptores:
            return None
        cola: asyncio.Queue[Evento] = asyncio.Queue(maxsize=1000)
        self._suscriptores.add(cola)
        return list(self._buffer), cola

    def desuscribir(self, cola: asyncio.Queue[Evento]) -> None:
        self._suscriptores.discard(cola)

    def _escribir_jsonl(self, evento: Evento, path: Path | None) -> None:
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Lleva mandatos y checkouts completos: el archivo se crea solo
            # legible por el dueño (en Windows el modo se ignora).
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(evento.model_dump_json() + "\n")
        except OSError as e:
            logger.warning("No se pudo escribir %s: %s", path, e)
