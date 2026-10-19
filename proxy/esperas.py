"""Las esperas de una compra: el porqué y la decisión sobre una oportunidad.

Una espera retiene la llamada del agente del cliente hasta que el usuario
contesta (por `/v1`), omite o vence el plazo. Hay dos formas de esperar,
con la misma interfaz (`Esperas`):

- `EsperaLocal` (`Config.instancias="una"`, la demo local y el gateway de
  una sola instancia): los `Future` en memoria de `Compra`, como siempre. La
  respuesta llega a la misma instancia que retiene la llamada.
- `EsperaCompartida` (`instancias="varias"`, Vercel): la espera vive en el
  almacén (`gw_esperas`, `proxy/sql/004_serverless.sql`) y la respuesta puede
  llegar a cualquier instancia (diseño 2.5 del plan de Vercel):

  1. **Abrir** (la instancia A, la que retiene): fija `vence`, abre la espera
     (una abierta por compra, atómico en la base), escribe la compra con
     await (`esperando_*`, `espera_id`, la pregunta o la oportunidad) y
     recién ahí el núcleo emite el evento y el webhook. Si algo falla, el
     núcleo va al lado seguro: el porqué sigue literal y la oportunidad
     queda vencida.
  2. **Responder** (cualquier instancia, por `/v1`): un PATCH condicionado a
     que la espera siga abierta y no haya vencido. Gana una sola respuesta.
     Quien responde no escribe la compra: con una espera abierta, la fila de
     la compra solo la escribe la instancia que la retiene.
  3. **Esperar** (A): mira la espera cada `sondeo_s` (con retroceso hasta
     `SONDEO_MAXIMO_S`); si contestaron en esta misma instancia, se despierta
     enseguida. Al llegar a `vence` la cierra (si en el medio alguien
     contestó, usa esa respuesta). Si el almacén falla, sigue mirando hasta
     `vence` y después la da por vencida.
  4. **Si A muere** (la instancia se congela o se corta la llamada): la
     espera queda abierta. Quien lea la compra después de `vence +
     gracia_espera_s` la cierra del lado seguro (`compra_de_fila`): cierra la
     espera y escribe la compra condicionada a su estado y su `espera_id`.
     Antes de eso la compra se muestra esperando.

El silencio no es autorización: una decisión que no llegó (o que no se pudo
leer) es `vencida`. Este módulo no emite eventos ni webhooks: lo hace el
núcleo (`proxy/app.py`) cuando la fila ya está escrita.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Protocol

from proxy.compras import (
    ESTADOS_ESPERANDO,
    LARGO_MAXIMO_RESPUESTA,
    Compra,
    EstadoInvalido,
)
from proxy.estado import fila_espera
from proxy.tipos_gateway import ID_ESPERA_RE, AlmacenEstado, ErrorAlmacen, TipoEspera, es_id

logger = logging.getLogger("proxy.esperas")

Apertura = Literal["abierta", "ocupada", "fallo"]
"""Cómo salió `abrir`: `abierta` (esta instancia la retiene), `ocupada` (la
compra ya tiene otra espera abierta: la retiene otra búsqueda, que es a la
que hay que esperar) o `fallo` (el almacén no la pudo abrir o escribir: lado
seguro)."""

SONDEO_MAXIMO_S = 2.0
RETROCESO = 1.5
DECISIONES = frozenset({"aceptar", "rechazar"})
_SIEMPRE = datetime.max.replace(tzinfo=timezone.utc)

# `escribir(compra, estado_esperado, espera_esperada) -> bool`: la escritura
# condicionada de la compra que hace el núcleo (con su candado por compra,
# para que vaya en orden con las de segundo plano).
Escritor = Callable[[Compra, Any, "str | None"], Awaitable[bool]]


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class FinEspera:
    """Cómo terminó una espera.

    `valor`: el porqué (texto, o None si el usuario omitió) o la decisión
    (`aceptar` / `rechazar`, o None). `vencida`: nadie contestó a tiempo."""

    valor: Any
    vencida: bool


class Esperas(Protocol):
    compartida: bool

    def retiene(self, compra_id: str) -> bool:
        """¿Esta instancia retiene la espera abierta de esa compra?"""
        ...

    async def abrir(self, compra: Compra, tipo: TipoEspera, *, estado_previo: str) -> Apertura:
        """La compra ya tiene el estado de espera, la pregunta o la
        oportunidad y `vence`. `estado_previo` es el que tenía la fila."""
        ...

    async def esperar(self, compra: Compra, tipo: TipoEspera) -> FinEspera: ...

    async def responder(
        self, compra: Compra, tipo: TipoEspera, valor: Any, *, oportunidad_id: str | None = None
    ) -> bool:
        """True solo para la respuesta que gana."""
        ...

    def soltar(self, compra: Compra) -> None:
        """La espera terminó (o se cortó la llamada): deja de retenerla."""
        ...


# -- una sola instancia ---------------------------------------------------------


class EsperaLocal:
    """Lo de siempre: los `Future` de `Compra`. Con una sola instancia la
    respuesta llega a la misma que retiene la llamada, y la compra la guarda
    el núcleo en segundo plano, en el mismo orden de siempre (por eso abrir
    no escribe nada)."""

    compartida = False

    def retiene(self, compra_id: str) -> bool:
        return False

    async def abrir(self, compra: Compra, tipo: TipoEspera, *, estado_previo: str) -> Apertura:
        return "abierta"

    async def esperar(self, compra: Compra, tipo: TipoEspera) -> FinEspera:
        if tipo == "porque":
            respuesta, vencida = await compra.esperar_respuesta()
            return FinEspera(respuesta, vencida)
        decision = await compra.esperar_decision()
        return FinEspera(decision, decision is None)

    async def responder(
        self, compra: Compra, tipo: TipoEspera, valor: Any, *, oportunidad_id: str | None = None
    ) -> bool:
        try:
            if tipo == "porque":
                compra.responder_en_memoria(valor)
            else:
                if oportunidad_id is not None and (
                    compra.oportunidad is None or compra.oportunidad.id != oportunidad_id
                ):
                    return False
                compra.decidir_en_memoria(valor)
        except EstadoInvalido:
            return False
        return True

    def soltar(self, compra: Compra) -> None:
        return None


# -- varias instancias ------------------------------------------------------------


@dataclass
class _Retenida:
    espera_id: str
    # Lo pone `responder` cuando la respuesta llega a esta misma instancia.
    despertar: asyncio.Event = field(default_factory=asyncio.Event)


class EsperaCompartida:
    """La espera en el almacén (`gw_esperas`): ver el docstring del módulo."""

    compartida = True

    def __init__(
        self,
        almacen: AlmacenEstado,
        escribir: Escritor,
        *,
        sondeo_s: float,
        gracia_s: float,
    ) -> None:
        self._almacen = almacen
        self._escribir = escribir
        self.sondeo_s = sondeo_s
        self.gracia_s = gracia_s
        self._retenidas: dict[str, _Retenida] = {}

    def retiene(self, compra_id: str) -> bool:
        return compra_id in self._retenidas

    # -- abrir (A) --------------------------------------------------------------

    async def abrir(self, compra: Compra, tipo: TipoEspera, *, estado_previo: str) -> Apertura:
        oportunidad_id = compra.oportunidad.id if compra.oportunidad is not None else None
        if compra.vence is None or (tipo == "decision" and oportunidad_id is None):
            return "fallo"
        try:
            fila = fila_espera(
                compra.id, tipo, compra.vence, creada=_ahora(),
                oportunidad_id=oportunidad_id if tipo == "decision" else None,
            )
            abierta = await self._almacen.abrir_espera(fila)
        except ErrorAlmacen as e:
            logger.error("No se pudo abrir la espera de la compra %s: %s", compra.id, e)
            return "fallo"
        if abierta is None:
            logger.info("La compra %s ya tiene una espera abierta en otra búsqueda", compra.id)
            return "ocupada"
        espera_id = fila["id"]
        # Antes de escribir: con la espera retenida acá, el núcleo escribe la
        # fila con el `espera_id` nuevo.
        self._retenidas[compra.id] = _Retenida(espera_id)
        compra.espera_id = espera_id
        try:
            escrita = await self._escribir(compra, estado_previo, compra.espera_guardada)
        except ErrorAlmacen as e:
            logger.error("No se pudo escribir la compra %s al abrir la espera: %s", compra.id, e)
            escrita = False
        if escrita:
            return "abierta"
        # Sin la fila de la compra nadie puede contestar: se cierra la espera
        # (si tampoco se puede, vence sola y nadie la puede responder después).
        self._retenidas.pop(compra.id, None)
        compra.espera_id = None
        try:
            await self._almacen.vencer_espera(espera_id, _ahora())
        except ErrorAlmacen as e:
            logger.error("No se pudo cerrar la espera %s: %s", espera_id, e)
        return "fallo"

    # -- esperar (A) ------------------------------------------------------------

    async def esperar(self, compra: Compra, tipo: TipoEspera) -> FinEspera:
        retenida = self._retenidas.get(compra.id)
        vence = compra.vence
        if retenida is None or vence is None:
            return FinEspera(None, True)
        espera_id = retenida.espera_id
        paso = self.sondeo_s
        try:
            while True:
                restante = (vence - _ahora()).total_seconds()
                if restante <= 0:
                    break
                try:
                    await asyncio.wait_for(retenida.despertar.wait(), min(paso, restante))
                except asyncio.TimeoutError:
                    pass
                retenida.despertar.clear()
                paso = min(paso * RETROCESO, max(self.sondeo_s, SONDEO_MAXIMO_S))
                try:
                    fila = await self._almacen.leer_espera(espera_id)
                except ErrorAlmacen as e:
                    # Se vuelve a mirar hasta `vence`; después, vencida.
                    logger.warning("No se pudo leer la espera %s: %s", espera_id, e)
                    continue
                if fila is None or fila.get("estado") == "vencida":
                    return FinEspera(None, True)  # la cerró otro (o no está): lado seguro
                if fila.get("estado") == "respondida":
                    return self.fin(fila, tipo, compra)
            try:
                fila = await self._almacen.vencer_espera(espera_id, _ahora())
            except ErrorAlmacen as e:
                logger.error("No se pudo cerrar la espera vencida %s: %s", espera_id, e)
                return FinEspera(None, True)
            if fila is not None and fila.get("estado") == "respondida":
                return self.fin(fila, tipo, compra)  # contestaron justo antes de vencer
            return FinEspera(None, True)
        finally:
            compra.vence = None

    @staticmethod
    def fin(fila: dict[str, Any] | None, tipo: TipoEspera, compra: Compra) -> FinEspera:
        """La respuesta de una espera `respondida`, validada. Lo que no cumple
        el formato no autoriza nada: un porqué raro se da por omitido (sigue
        literal) y una decisión rara, por vencida."""
        if not isinstance(fila, dict) or fila.get("estado") != "respondida":
            return FinEspera(None, True)
        if fila.get("tipo") != tipo:
            return FinEspera(None, True)
        respuesta = fila.get("respuesta")
        valor = respuesta.get("valor") if isinstance(respuesta, dict) else None
        if tipo == "porque":
            if isinstance(valor, str) and 0 < len(valor.strip()) <= LARGO_MAXIMO_RESPUESTA:
                return FinEspera(valor.strip(), False)
            return FinEspera(None, valor is not None)
        oportunidad = compra.oportunidad
        if oportunidad is None or fila.get("oportunidad_id") != oportunidad.id:
            return FinEspera(None, True)
        if valor in DECISIONES:
            return FinEspera(valor, False)
        return FinEspera(None, True)

    # -- responder (cualquier instancia) ----------------------------------------

    async def responder(
        self, compra: Compra, tipo: TipoEspera, valor: Any, *, oportunidad_id: str | None = None
    ) -> bool:
        """PATCH condicionado en el almacén: True solo para quien gana. No
        escribe la compra (la escribe la instancia que retiene la espera).
        `ErrorAlmacen` si el almacén falla (503: no quedó respondida)."""
        espera_id = compra.espera_id
        if not es_id(ID_ESPERA_RE, espera_id):
            return False
        ganada = await self._almacen.responder_espera(
            espera_id, {"valor": valor}, _ahora(), tipo=tipo, oportunidad_id=oportunidad_id
        )
        retenida = self._retenidas.get(compra.id)
        if ganada and retenida is not None and retenida.espera_id == espera_id:
            retenida.despertar.set()  # la llamada retenida está en esta instancia
        return ganada

    def soltar(self, compra: Compra) -> None:
        # Solo la propia: otra búsqueda de la misma compra en esta instancia
        # (que no abrió nada) no suelta la que retiene la primera.
        retenida = self._retenidas.get(compra.id)
        if retenida is not None and retenida.espera_id == compra.espera_id:
            del self._retenidas[compra.id]

    # -- leer una compra (cualquier instancia) ----------------------------------

    async def compra_de_fila(self, fila: dict[str, Any], *, ahora: datetime | None = None) -> Compra:
        """La compra de una fila de `gw_compras` con varias instancias.

        Si no está esperando, como siempre. Si está esperando, se mira su
        espera: mientras siga viva (hasta `vence + gracia`) se lee tal cual,
        sin cerrar nada; pasada la gracia, la instancia que la retenía murió y
        se cierra del lado seguro (`_normalizar`). `ErrorAlmacen` si el
        almacén falla: quien llama responde 503."""
        estado = fila.get("estado") if isinstance(fila, dict) else None
        if estado not in ESTADOS_ESPERANDO:
            return Compra.desde_fila(fila)
        ahora = ahora or _ahora()
        espera_id = fila.get("espera_id")
        espera = await self._almacen.leer_espera(espera_id) if es_id(ID_ESPERA_RE, espera_id) else None
        if espera is not None and espera.get("compra_id") != fila.get("id"):
            espera = None
        vence = _fecha(espera.get("vence")) if espera is not None else None
        if vence is None and isinstance(fila.get("datos"), dict):
            vence = _fecha(fila["datos"].get("vence"))
        if vence is not None:
            viva_hasta = vence + timedelta(seconds=self.gracia_s)
            if ahora < viva_hasta:
                return Compra.desde_fila(fila, viva_hasta=viva_hasta, ahora=ahora)
        return await self._normalizar(fila, espera, ahora)

    async def _normalizar(
        self, fila: dict[str, Any], espera: dict[str, Any] | None, ahora: datetime
    ) -> Compra:
        """Cierra la espera huérfana y escribe la compra condicionada a que la
        fila siga como se leyó (estado y `espera_id`): si en el medio la
        escribió otro (la instancia que la retenía no había muerto), manda esa.

        - el porqué pasa a `negociando` sin contexto (el pedido literal);
        - la oportunidad pasa a `vencida`, salvo que el usuario haya decidido
          antes de vencer (la espera quedó `respondida`): esa decisión es suya
          y es la que ya le devolvió `POST /decision`."""
        if espera is not None and espera.get("estado") == "abierta":
            espera = await self._almacen.vencer_espera(espera["id"], ahora)
        compra = Compra.desde_fila(fila, viva_hasta=_SIEMPRE, ahora=ahora)
        if compra.estado == "esperando_contexto":
            compra.cerrar_porque()
        else:
            fin = self.fin(espera, "decision", compra)
            compra.aplicar_decision(None if fin.vencida else fin.valor)
            compra.vence = None
        compra.espera_id = None
        compra.contexto_listo.set()
        compra.resuelta.set()
        escrita = await self._escribir(compra, fila["estado"], compra.espera_guardada)
        if escrita:
            logger.warning(
                "Compra %s: la espera quedó huérfana y se cerró del lado seguro (%s)",
                compra.id, compra.estado,
            )
            return compra
        nueva = await self._almacen.cargar_compra(compra.id)
        if nueva is None:
            raise ErrorAlmacen("la compra no está en el almacén")
        if nueva.get("estado") in ESTADOS_ESPERANDO:
            return compra  # sigue sin escribir: se muestra cerrada, sin tocar la fila
        return Compra.desde_fila(nueva)

    async def sondear_compra(
        self, compra_id: str, listo: Callable[[dict[str, Any]], bool], tope_s: float
    ) -> dict[str, Any] | None:
        """La fila de la compra en cuanto cumple `listo`, mirando hasta
        `tope_s` (None si no llegó). Las fallas del almacén se saltean."""
        loop = asyncio.get_running_loop()
        fin = loop.time() + max(0.0, tope_s)
        paso = min(self.sondeo_s, 0.25)
        while True:
            try:
                fila = await self._almacen.cargar_compra(compra_id)
            except ErrorAlmacen:
                fila = None
            if isinstance(fila, dict) and listo(fila):
                return fila
            restante = fin - loop.time()
            if restante <= 0:
                return None
            await asyncio.sleep(min(paso, restante))
            paso = min(paso * RETROCESO, 0.5)


def _fecha(valor: Any) -> datetime | None:
    if isinstance(valor, datetime):
        fecha = valor
    elif isinstance(valor, str):
        try:
            fecha = datetime.fromisoformat(valor)
        except ValueError:
            return None
    else:
        return None
    return fecha if fecha.tzinfo is not None else fecha.replace(tzinfo=timezone.utc)
