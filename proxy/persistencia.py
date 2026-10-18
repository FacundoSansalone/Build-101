"""Registro en Supabase: veredictos con su recibo y aprobaciones humanas.

Es solo registro, no decide nada (CLAUDE.md → Decisiones):

- Escribe por la API REST de Supabase (PostgREST) con httpx, en segundo
  plano y con timeout corto: no demora la respuesta al agente.
- Si Supabase no responde, el proxy sigue igual. Avisa una vez por caída
  (log + `al_fallar`, que el proxy convierte en un evento `error`) y el JSONL
  sigue siendo la copia completa.
- No sube mandatos ni `checkout_jwt`: del checkout solo va el
  `checkout_hash` que ya está en el recibo.
- Las aprobaciones se guardan como auditoría. La regla de un solo uso sigue
  en memoria (`proxy.aprobaciones`): una aprobación no sirve después de un
  reinicio, porque las ofertas de su contexto se pierden.

Sin `SUPABASE_URL` y `SUPABASE_SERVICE_ROLE_KEY` (o `SUPABASE_SECRET_KEY`) no
hay persistencia y el proxy funciona como antes. Las tablas están en `proxy/sql/`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from contracts import Evento
from proxy.aprobaciones import Aprobacion

logger = logging.getLogger("proxy.persistencia")

TIMEOUT_SEGUNDOS = 5.0
MAX_PENDIENTES = 100  # escrituras en vuelo; si Supabase se cuelga no crece sin tope

TABLA_VEREDICTOS = "veredictos"
TABLA_APROBACIONES = "aprobaciones"


def credenciales(env: Mapping[str, str] | None = None) -> tuple[str, str] | None:
    """(url, clave) desde el entorno, o None si falta alguna.

    La clave se lee de `SUPABASE_SERVICE_ROLE_KEY` o, si falta, de
    `SUPABASE_SECRET_KEY` (el nombre con el que hoy la entrega el dashboard).
    """
    env = os.environ if env is None else env
    url = (env.get("SUPABASE_URL") or "").strip().rstrip("/")
    clave = (env.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip() or (
        env.get("SUPABASE_SECRET_KEY") or ""
    ).strip()
    if not url or not clave:
        return None
    return url, clave


def cabeceras(clave: str, prefer: str) -> dict[str, str]:
    return {
        "apikey": clave,
        "Authorization": f"Bearer {clave}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


def fila_veredicto(evento: Evento) -> dict[str, Any] | None:
    """La fila de `veredictos` para un evento `veredicto` (None si no lo es)."""
    if evento.tipo != "veredicto":
        return None
    veredicto = evento.datos.get("veredicto")
    if not isinstance(veredicto, dict):
        return None
    recibo = veredicto.get("recibo") if isinstance(veredicto.get("recibo"), dict) else {}
    return {
        "evento_id": evento.id,
        "ts": evento.ts.isoformat(),
        "contexto_id": evento.contexto_id,
        "escenario": evento.escenario,
        "guard_activo": evento.guard_activo,
        "evaluador": evento.datos.get("evaluador"),
        "fuente_catalogo": evento.datos.get("fuente"),
        "status": veredicto.get("status"),
        "cart_elegido": veredicto.get("cart_id_elegido"),
        "cart_recomendado": veredicto.get("cart_id_recomendado"),
        "motivo": veredicto.get("motivo"),
        "checkout_hash": recibo.get("checkout_hash"),
        "recibo_id": recibo.get("id"),
        "recibo_jws": recibo.get("firma"),
        "recibo_kid": recibo.get("kid"),
        # Chequeos y recibo completos: `verificaciones` lleva, campo por campo,
        # lo declarado contra el catálogo público (la evidencia del rechazo).
        "veredicto": veredicto,
    }


def fila_aprobacion(aprobacion: Aprobacion) -> dict[str, Any]:
    return {
        "id": aprobacion.id,
        "contexto_id": aprobacion.contexto_id,
        "cart_id": aprobacion.cart_id,
        "centavos": aprobacion.centavos,
        "moneda": aprobacion.moneda,
        "estado": aprobacion.estado,
        "creada": aprobacion.creada,
        "usada": aprobacion.usada,
        "task_id": aprobacion.task_id,
    }


class Persistencia:
    def __init__(
        self,
        url: str,
        clave: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        al_fallar: Callable[[str], None] | None = None,
    ) -> None:
        self._rest = f"{url.rstrip('/')}/rest/v1"
        self._clave = clave
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(TIMEOUT_SEGUNDOS),
            trust_env=False,
        )
        self.al_fallar = al_fallar
        self._tareas: set[asyncio.Task[None]] = set()
        self._caida = False
        self.escritas = 0
        self.errores = 0

    @classmethod
    def desde_entorno(cls, env: Mapping[str, str] | None = None) -> Persistencia | None:
        datos = credenciales(env)
        return cls(*datos) if datos else None

    # -- lo que llama el proxy -------------------------------------------------

    def guardar_evento(self, evento: Evento) -> None:
        fila = fila_veredicto(evento)
        if fila is not None:
            self._programar(TABLA_VEREDICTOS, fila, conflicto="evento_id")

    def guardar_aprobacion(self, aprobacion: Aprobacion) -> None:
        self._programar(TABLA_APROBACIONES, fila_aprobacion(aprobacion), conflicto="id")

    async def esperar(self) -> None:
        """Espera las escrituras en vuelo (tests y cierre)."""
        while self._tareas:
            await asyncio.gather(*list(self._tareas), return_exceptions=True)
            # Ceder una vuelta al loop: si todas ya terminaron, gather (3.12)
            # resuelve sin pasar por el loop y el `discard` del done callback
            # no llega a correr (el while no terminaba nunca).
            await asyncio.sleep(0)

    async def cerrar(self) -> None:
        try:
            await asyncio.wait_for(self.esperar(), TIMEOUT_SEGUNDOS)
        except asyncio.TimeoutError:
            logger.warning("Quedaron %d escrituras a Supabase sin terminar", len(self._tareas))
        await self._client.aclose()

    # -- interno ---------------------------------------------------------------

    def _programar(self, tabla: str, fila: dict[str, Any], *, conflicto: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("Sin event loop: no se guarda la fila de %s", tabla)
            return
        if len(self._tareas) >= MAX_PENDIENTES:
            self._fallo(f"Supabase no da abasto: se descarta una fila de {tabla}")
            return
        tarea = loop.create_task(self._upsert(tabla, fila, conflicto))
        self._tareas.add(tarea)
        tarea.add_done_callback(self._tareas.discard)

    async def _upsert(self, tabla: str, fila: dict[str, Any], conflicto: str) -> None:
        try:
            resp = await self._client.post(
                f"{self._rest}/{tabla}",
                params={"on_conflict": conflicto},
                json=fila,
                headers=cabeceras(self._clave, "resolution=merge-duplicates,return=minimal"),
            )
        except httpx.HTTPError as e:
            self._fallo(f"No se pudo escribir en Supabase ({tabla}): {type(e).__name__}")
            return
        if resp.status_code >= 300:
            # El cuerpo de PostgREST dice qué columna o tabla falta; no lleva la clave.
            self._fallo(
                f"Supabase rechazó la fila de {tabla}: HTTP {resp.status_code} "
                f"{resp.text[:300]}"
            )
            return
        self.escritas += 1
        if self._caida:
            self._caida = False
            logger.info("Supabase volvió a responder")

    def _fallo(self, mensaje: str) -> None:
        self.errores += 1
        if self._caida:
            logger.debug(mensaje)
            return
        # Avisa una vez por caída, no en cada fila.
        self._caida = True
        logger.warning("%s. El proxy sigue; queda la copia en el JSONL.", mensaje)
        if self.al_fallar is not None:
            try:
                self.al_fallar(mensaje)
            except Exception:  # noqa: BLE001 - avisar nunca rompe el flujo
                logger.exception("al_fallar falló")
