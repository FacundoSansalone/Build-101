"""Núcleo de la API `/v1` sobre el proxy (`NucleoProxy`, implementa
`tipos_gateway.NucleoGateway`; diseño del gateway, §4.3).

`proxy/api_v1.py` valida y arma las vistas; acá están las reglas que
dependen del estado del proxy:

- **Aislamiento:** toda compra se busca por integrador. Una de otro
  integrador da el mismo 404 que una que no existe (no se filtra que existe).
- **Comercios:** solo los permitidos para la clave (403). El upstream sale
  siempre del registro, nunca de lo que mande el integrador.
- **Fail-closed con el almacén:** si no se puede leer o escribir lo que hace
  falta para decidir, 503 y no se sigue (una compra que no se pudo guardar se
  saca de memoria; una aprobación que no se pudo guardar, también).
- **Autoriza el dueño:** la respuesta del porqué, la decisión sobre una
  oportunidad y la aprobación de un `escalar` llegan por acá y se aplican con
  las mismas reglas que el dashboard usa para las compras de la demo (una
  aprobación nunca hace pasar un `bloqueado`, vale para un contexto, un cart
  y el total firmado, y es de un solo uso).

No lee el entorno: todo sale del `ProxyGuard` que arma `create_app`.

**Varias instancias** (`Config.instancias="varias"`): cada método lee la
compra del almacén (salvo que esta instancia retenga su espera), responder
el porqué o decidir una oportunidad es un PATCH condicionado sobre la espera
(`proxy/esperas.py`: gana una sola respuesta, la otra da 409 `estado`) y
quien responde no escribe la compra: devuelve la foto con el estado que va a
tener. Las aprobaciones se crean en el almacén (una por contexto, cart y
total firmado: un segundo POST en otra instancia devuelve la misma) y los
contextos, las aprobaciones de la foto y las entregas de webhooks salen del
almacén. Los métodos que escriben terminan con la barrera
(`ProxyGuard.barrera`): si no quedó guardado, 503.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import dataclasses
import logging
import uuid
from datetime import datetime, timedelta, timezone
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, Literal

from contracts import Evento
from proxy import explicacion, sin_estado
from proxy.aprobaciones import Aprobacion, SinEscalar, aprobacion_desde_fila
from proxy.comercios import politica_de
from proxy.compras import ESTADOS_ESPERANDO, Compra, EstadoInvalido
from proxy.esperas import EsperaCompartida
from proxy.estado import fila_aprobacion_gw
from proxy.tipos_gateway import (
    ID_ESPERA_RE,
    Comercio,
    ErrorAlmacen,
    ErrorApi,
    FiltrosListado,
    FotoCompra,
    Integrador,
    PaginaCompras,
    SolicitudCompra,
    SolicitudEvaluar,
    SolicitudFiltrar,
    es_id,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from proxy.app import ProxyGuard

logger = logging.getLogger("proxy.gateway")


# Estados que `Compra.desde_fila` cierra después de un reinicio: una fila
# en el estado de la derecha se lee en el de la izquierda.
_NORMALIZADA_DESDE = {"negociando": "esperando_contexto", "vencida": "esperando_decision"}

# Cuánto espera `POST /decision` a que la búsqueda retenida aplique la
# decisión antes de armar la vista (en la práctica, unas vueltas del loop).
ESPERA_RESOLUCION_S = 2.0
_PASO_RESOLUCION_S = 0.01
# Con varias instancias la búsqueda retenida puede estar en otra: se mira la
# compra en el almacén hasta esto (si no llegó, la foto con el estado que va
# a tener).
ESPERA_RESOLUCION_COMPARTIDA_S = 3.0
# El listado de `/v1/compras` lee de a `LOTE_ESCANEO` filas. Con un filtro que
# se aplica acá (`alerta`, o un estado que se normaliza al leer) mira como
# mucho `MAX_ESCANEO_LISTADO` compras por pedido: si no llenó la página, la
# devuelve con `siguiente` (puede haber más).
LOTE_ESCANEO = 200
MAX_ESCANEO_LISTADO = 1000
# `/v1/resumen` cuenta como mucho estas compras del período (si hay más, lo
# dice con `completo: false`) y la explicación lee como mucho estos eventos.
MAX_COMPRAS_RESUMEN = 5000
MAX_EVENTOS_EXPLICACION = 1000
# `/v1/filtrar` y `/v1/evaluar` corren en los hilos del proxy (el pool del
# filtro y el del LLM/fabricante), los mismos que usa todo el tráfico: un
# integrador con muchas a la vez dejaba sin hilos al filtro de los demás (que
# vence y falla cerrado: todo bloqueado). A lo sumo estas en curso por
# integrador, entre las dos rutas y en esta instancia; si no, 429.
MAX_SIN_ESTADO_POR_INTEGRADOR = 2


def _no_disponible() -> ErrorApi:
    return ErrorApi(
        503, "estado_no_disponible", "El estado del gateway no está disponible; reintentá."
    )


def _desconocida() -> ErrorApi:
    return ErrorApi(404, "compra_desconocida", "No existe esa compra.")


def _estado(compra_estado: str) -> ErrorApi:
    return ErrorApi(
        409, "estado", "La compra no está esperando eso.", {"estado": compra_estado}
    )


class NucleoProxy:
    """Lo que la API `/v1` le pide al proxy, con las reglas de estado."""

    def __init__(self, proxy: ProxyGuard) -> None:
        self.proxy = proxy
        # Un candado por integrador para contar y crear (el diseño supone una
        # sola instancia): sin él, N POST concurrentes leen el mismo conteo y
        # se pasan todos del límite de compras activas.
        self._candados_creacion: dict[str, asyncio.Lock] = {}
        # `/v1/filtrar` y `/v1/evaluar` en curso por integrador (`_cupo_sin_estado`).
        self._sin_estado_en_curso: dict[str, int] = {}
        self.espera_resolucion_s = ESPERA_RESOLUCION_S
        self.espera_resolucion_compartida_s = ESPERA_RESOLUCION_COMPARTIDA_S

    @property
    def _compartidas(self) -> EsperaCompartida | None:
        return self.proxy.esperas_compartidas

    @property
    def ttl_compra_s(self) -> float:
        """La vida de una compra de integrador (`GATEWAY_COMPRA_TTL_S`)."""
        return float(self.proxy.config.compra_ttl_s)

    async def _con_barrera(self, coro: Awaitable[Any]) -> Any:
        """Corre un método que escribe y, antes de responder, la barrera
        (varias instancias o webhooks en línea; si no, nada cambia). Si una
        escritura decisiva no quedó, 503: el integrador reintenta (crear una
        aprobación o responder dos veces no duplica nada)."""
        proxy = self.proxy
        if not proxy.con_barrera:
            return await coro
        with proxy.en_fondo.ambito():
            resultado = await coro
            try:
                await proxy.barrera()
            except ErrorAlmacen as e:
                logger.error("El estado no quedó guardado antes de responder: %s", e)
                raise _no_disponible() from None
            return resultado

    async def _foto(self, compra: Compra) -> FotoCompra:
        try:
            return await self.proxy.foto_actual(compra)
        except ErrorAlmacen as e:
            logger.error("No se pudo armar la foto de la compra %s: %s", compra.id, e)
            raise _no_disponible() from None

    # -- comercios -------------------------------------------------------------

    def comercios_de(self, integrador: Integrador) -> list[Comercio]:
        return self.proxy.comercios.permitidos(integrador)

    def url_a2a(self, comercio_id: str) -> str:
        return self.proxy.url_a2a(comercio_id) or ""

    def _comercio_permitido(self, integrador: Integrador, comercio_id: str | None) -> Comercio:
        """El comercio pedido (None = el por defecto) si la clave puede usarlo;
        si no existe o no está permitido, el mismo 403: no se dice qué
        comercios tiene registrados el operador."""
        comercio = self.proxy.comercios.get(comercio_id)
        if comercio is None or comercio not in self.comercios_de(integrador):
            raise ErrorApi(
                403, "comercio_no_permitido", "El comercio no está permitido para esta clave."
            )
        return comercio

    # -- compras ---------------------------------------------------------------

    async def _compra_de(self, integrador: Integrador, compra_id: str) -> Compra:
        """La compra del integrador (memoria o almacén), o 404 / 503."""
        try:
            compra = await self.proxy.hidratar_compra(compra_id)
        except ErrorAlmacen as e:
            logger.error("No se pudo hidratar la compra %s: %s", compra_id, e)
            raise _no_disponible() from None
        if compra is None or compra.origen != "api" or compra.integrador_id != integrador.id:
            raise _desconocida()
        return compra

    async def crear_compra(self, integrador: Integrador, solicitud: SolicitudCompra) -> FotoCompra:
        comercio = self._comercio_permitido(integrador, solicitud.comercio_id)
        candado = self._candados_creacion.setdefault(integrador.id, asyncio.Lock())
        async with candado:
            # El conteo y la escritura de la compra nueva, juntos: la próxima
            # creación de este integrador ya la cuenta. (Con varias instancias
            # el candado es de cada una: el límite es aproximado.)
            return await self._con_barrera(self._crear_compra(integrador, solicitud, comercio))

    async def _crear_compra(
        self, integrador: Integrador, solicitud: SolicitudCompra, comercio: Comercio
    ) -> FotoCompra:
        proxy = self.proxy
        desde = datetime.now(timezone.utc) - timedelta(seconds=proxy.config.compra_ttl_s)
        try:
            activas = await proxy.almacen.contar_activas(integrador.id, desde)
        except ErrorAlmacen as e:
            logger.error("No se pudieron contar las compras de %s: %s", integrador.id, e)
            raise _no_disponible() from None
        if activas >= integrador.max_compras_activas:
            raise ErrorApi(
                429, "demasiadas_compras",
                "Hay demasiadas compras activas para esta clave; esperá a que terminen.",
                {"maximo": integrador.max_compras_activas},
            )
        compra = proxy.compras.crear_api(
            integrador_id=integrador.id,
            comercio_id=comercio.id,
            pedido=solicitud.pedido,
            ficha=solicitud.ficha,
            referencia=solicitud.referencia,
            porque=solicitud.porque,
            preguntar_porque=solicitud.preguntar_porque,
            intencion_activa=proxy.agente is not None,
        )
        try:
            # Con await: el tráfico A2A la busca acá y ata contextos a esta fila.
            await proxy.almacen.guardar_compra(compra.a_fila())
        except ErrorAlmacen as e:
            proxy.compras.quitar(compra.id)
            logger.error("No se pudo guardar la compra nueva de %s: %s", integrador.id, e)
            raise _no_disponible() from None
        except BaseException:
            # Cualquier otra falla (una fila que no se puede armar, la llamada
            # cancelada): sin la fila no existe para nadie. Si quedara en
            # memoria, sería una compra huérfana que no cuenta para el límite
            # de activas (se cuenta en el almacén) y nunca sale (está viva).
            proxy.compras.quitar(compra.id)
            raise
        logger.info(
            "Compra %s creada por el integrador %s (comercio %s)",
            compra.id, integrador.id, comercio.id,
        )
        return proxy.foto(compra)

    async def obtener_compra(self, integrador: Integrador, compra_id: str) -> FotoCompra:
        return await self._foto(await self._compra_de(integrador, compra_id))

    async def listar_compras(
        self, integrador: Integrador, filtros: FiltrosListado
    ) -> PaginaCompras:
        """Del almacén, la más nueva primero (`creada desc, id desc`), con los
        filtros de la base (estado, fechas, referencia, cursor) y los de acá
        (`alerta`; el estado de una fila que se normaliza al leerla). Con la
        instancia de memoria cuando existe (tiene las esperas vivas). Las
        fotos de la página, de una vez (`ProxyGuard.fotos_actuales`)."""
        try:
            compras, siguiente = await self._escanear(
                integrador, filtros, maximo=filtros.limite, maximo_escaneo=MAX_ESCANEO_LISTADO,
            )
            fotos = await self.proxy.fotos_actuales(compras)
        except ErrorAlmacen as e:
            logger.error("No se pudieron listar las compras de %s: %s", integrador.id, e)
            raise _no_disponible() from None
        return PaginaCompras(tuple(fotos), siguiente)

    async def _compra_listada(self, fila: dict[str, Any]) -> Compra:
        """La compra de una fila del listado, sin ponerla en memoria (un
        listado no desplaza a las compras en curso).

        - Varias instancias: una espera viva de otra instancia se ve
          esperando; una huérfana se cierra al leerla (`compra_de_fila`).
        - Una sola: la de memoria si está; si no, la de la fila con la espera
          cerrada (`Compra.desde_fila`), y la fila se normaliza en segundo
          plano sin mover `actualizada`."""
        proxy = self.proxy
        if self._compartidas is not None:
            return await proxy.compra_de_fila(fila)
        compra = proxy.compras.get(fila.get("id")) if isinstance(fila, dict) else None
        if compra is None:
            compra = Compra.desde_fila(fila)
            if compra.estado != fila.get("estado"):
                proxy.guardar_compra(compra, tocar=False)
        return compra

    @staticmethod
    def _entra(compra: Compra, integrador: Integrador, filtros: FiltrosListado) -> bool:
        if compra.integrador_id != integrador.id:
            return False
        if filtros.estado is not None and compra.estado != filtros.estado:
            return False
        if filtros.alerta is not None:
            alertas = explicacion.alertas_de_compra(
                {"estado": compra.estado, "veredictos": compra.veredictos}, corte=compra.corte,
            )
            if filtros.alerta not in alertas:
                return False
        return True

    async def _escanear(
        self, integrador: Integrador, filtros: FiltrosListado, *, maximo: int, maximo_escaneo: int
    ) -> tuple[list[Compra], tuple[datetime, str] | None]:
        """Las compras que pasan los filtros, de a páginas del almacén, hasta
        `maximo` (o hasta mirar `maximo_escaneo`), y dónde sigue el listado
        (None si no hay más). El cursor es la última compra mirada, haya
        entrado o no: la próxima página no la vuelve a mirar.

        Una fila que quedó esperando antes de un reinicio se lee con la
        espera cerrada (`Compra.desde_fila`): también es del estado
        normalizado aunque la fila todavía no lo diga, así que se pide a la
        base con los dos estados y se filtra acá."""
        estado = filtros.estado
        normalizado = _NORMALIZADA_DESDE.get(estado) if estado is not None else None
        estados: tuple[str, ...] | None = None
        if estado is not None:
            estados = (estado,) if normalizado is None else (estado, normalizado)
        filtra_aca = filtros.alerta is not None or normalizado is not None
        lote = LOTE_ESCANEO if filtra_aca else min(LOTE_ESCANEO, maximo + 1)
        cursor = filtros.cursor
        elegidas: list[Compra] = []
        miradas = 0
        while True:
            filas = await self.proxy.almacen.listar_compras(
                integrador.id, limite=lote, estado=estados, desde=filtros.desde,
                hasta=filtros.hasta, referencia=filtros.referencia, antes_de=cursor,
            )
            for i, fila in enumerate(filas):
                compra = await self._compra_listada(fila)
                cursor = (compra.creada, compra.id)
                if not self._entra(compra, integrador, filtros):
                    continue
                elegidas.append(compra)
                if len(elegidas) >= maximo:
                    hay_mas = i < len(filas) - 1 or len(filas) == lote
                    return elegidas, cursor if hay_mas else None
            miradas += len(filas)
            if len(filas) < lote:
                return elegidas, None
            if miradas >= maximo_escaneo:
                return elegidas, cursor

    async def explicacion_compra(
        self, integrador: Integrador, compra_id: str
    ) -> tuple[FotoCompra, list[dict[str, Any]]]:
        """La foto de la compra (404 si no es del integrador) y sus eventos
        del historial (saneados al guardarlos; la explicación los vuelve a
        mirar). Si el almacén falla, 503."""
        compra = await self._compra_de(integrador, compra_id)
        foto = await self._foto(compra)
        try:
            eventos = await self.proxy.eventos_de_compra(compra.id, MAX_EVENTOS_EXPLICACION)
        except ErrorAlmacen as e:
            logger.error("No se pudo leer el historial de la compra %s: %s", compra.id, e)
            raise _no_disponible() from None
        return foto, eventos

    async def resumen_compras(
        self, integrador: Integrador, desde: datetime, hasta: datetime
    ) -> tuple[list[FotoCompra], bool]:
        """Las compras creadas en [`desde`, `hasta`), hasta
        `MAX_COMPRAS_RESUMEN`, y si entraron todas."""
        filtros = FiltrosListado(limite=MAX_COMPRAS_RESUMEN, desde=desde, hasta=hasta)
        try:
            compras, siguiente = await self._escanear(
                integrador, filtros, maximo=MAX_COMPRAS_RESUMEN, maximo_escaneo=MAX_COMPRAS_RESUMEN,
            )
            fotos = await self.proxy.fotos_actuales(compras)
        except ErrorAlmacen as e:
            logger.error("No se pudo armar el resumen de %s: %s", integrador.id, e)
            raise _no_disponible() from None
        return fotos, siguiente is None

    async def responder_porque(
        self, integrador: Integrador, compra_id: str, respuesta: str | None
    ) -> FotoCompra:
        return await self._con_barrera(self._responder_porque(integrador, compra_id, respuesta))

    async def _responder_porque(
        self, integrador: Integrador, compra_id: str, respuesta: str | None
    ) -> FotoCompra:
        compra = await self._compra_de(integrador, compra_id)
        if self._compartidas is not None:
            return await self._responder_compartida(integrador, compra, respuesta)
        try:
            self.proxy.compras.responder(compra.id, respuesta)
        except EstadoInvalido as e:
            raise _estado(e.estado) from None
        logger.info("Compra %s: el integrador %s respondió el porqué", compra.id, integrador.id)
        return self.proxy.foto(compra)

    async def _responder_compartida(
        self, integrador: Integrador, compra: Compra, respuesta: str | None
    ) -> FotoCompra:
        """El porqué con varias instancias: gana una sola respuesta (PATCH
        condicionado sobre la espera). No se escribe la compra: la escribe la
        instancia que retiene la búsqueda. La foto es la del estado que va a
        tener (`negociando`)."""
        esperas = self._compartidas
        assert esperas is not None
        if compra.estado != "esperando_contexto" or not es_id(ID_ESPERA_RE, compra.espera_id):
            raise _estado(compra.estado)
        try:
            ganada = await esperas.responder(compra, "porque", respuesta)
        except ErrorAlmacen as e:
            logger.error("No se pudo responder la espera de la compra %s: %s", compra.id, e)
            raise _no_disponible() from None
        if not ganada:
            # Ya la contestaron (otra instancia) o venció: el porqué ya no
            # cambia nada, la compra sigue (o va a seguir) negociando.
            raise _estado("negociando")
        logger.info("Compra %s: el integrador %s respondió el porqué", compra.id, integrador.id)
        foto = await self._foto(compra)
        return dataclasses.replace(foto, estado="negociando", pregunta=None, vence=None)

    async def decidir_oportunidad(
        self,
        integrador: Integrador,
        compra_id: str,
        oportunidad_id: str,
        decision: Literal["aceptar", "rechazar"],
    ) -> FotoCompra:
        return await self._con_barrera(
            self._decidir_oportunidad(integrador, compra_id, oportunidad_id, decision)
        )

    async def _decidir_oportunidad(
        self,
        integrador: Integrador,
        compra_id: str,
        oportunidad_id: str,
        decision: Literal["aceptar", "rechazar"],
    ) -> FotoCompra:
        compra = await self._compra_de(integrador, compra_id)
        if compra.estado != "esperando_decision":
            raise _estado(compra.estado)
        if compra.oportunidad is None or compra.oportunidad.id != oportunidad_id:
            raise ErrorApi(
                409, "oportunidad_distinta", "Esa no es la oportunidad que está esperando."
            )
        if self._compartidas is not None:
            return await self._decidir_compartida(integrador, compra, oportunidad_id, decision)
        try:
            self.proxy.compras.decidir(compra.id, decision)
        except EstadoInvalido as e:
            raise _estado(e.estado) from None
        logger.warning(
            "Compra %s: el integrador %s decidió %s la oportunidad %s",
            compra.id, integrador.id, decision, oportunidad_id,
        )
        await self._esperar_resolucion(compra)
        return self.proxy.foto(compra)

    async def _decidir_compartida(
        self,
        integrador: Integrador,
        compra: Compra,
        oportunidad_id: str,
        decision: Literal["aceptar", "rechazar"],
    ) -> FotoCompra:
        """La decisión con varias instancias: gana una sola (PATCH condicionado
        a la espera abierta, sin vencer y de esa oportunidad). No se escribe
        la compra: la aplica la búsqueda retenida, en la instancia que la
        tenga. Para devolver el estado real se mira la compra unos segundos;
        si no llegó, la foto con la decisión ya aplicada sobre una copia."""
        esperas = self._compartidas
        assert esperas is not None
        if not es_id(ID_ESPERA_RE, compra.espera_id):
            raise _estado(compra.estado)
        try:
            ganada = await esperas.responder(
                compra, "decision", decision, oportunidad_id=oportunidad_id
            )
        except ErrorAlmacen as e:
            logger.error("No se pudo responder la espera de la compra %s: %s", compra.id, e)
            raise _no_disponible() from None
        if not ganada:
            raise _estado(await self._estado_tras_perder(compra))
        logger.warning(
            "Compra %s: el integrador %s decidió %s la oportunidad %s",
            compra.id, integrador.id, decision, oportunidad_id,
        )
        fila = await esperas.sondear_compra(
            compra.id,
            lambda f: f.get("estado") not in ESTADOS_ESPERANDO,
            self.espera_resolucion_compartida_s,
        )
        if fila is not None:
            try:
                return await self._foto(Compra.desde_fila(fila))
            except ErrorAlmacen:
                logger.warning("La fila de la compra %s no se pudo leer: foto proyectada", compra.id)
        proyectada = copy.copy(compra)
        proyectada.aplicar_decision(decision)
        proyectada.vence = None
        return await self._foto(proyectada)

    async def _estado_tras_perder(self, compra: Compra) -> str:
        """El estado que va a tener una compra cuya decisión ya no se pudo
        responder: la de la respuesta que ganó o, si venció, `vencida`."""
        esperas = self._compartidas
        assert esperas is not None
        try:
            espera = await self.proxy.almacen.leer_espera(compra.espera_id)  # type: ignore[arg-type]
        except ErrorAlmacen:
            return "vencida"
        fin = esperas.fin(espera, "decision", compra)
        if fin.vencida:
            return "vencida"
        return "aceptada" if fin.valor == "aceptar" else "rechazada"

    async def _esperar_resolucion(self, compra: Compra) -> None:
        """Hasta que la búsqueda retenida aplique la decisión (o el tope).

        `decidir` solo resuelve el `Future`: el estado (`aceptada`,
        `rechazada`) y la ficha nueva los pone la búsqueda cuando retoma, unas
        vueltas del loop después. Sin esperar, la respuesta del POST salía con
        `esperando_decision`. Si vence el tope no cambia nada: la decisión ya
        quedó tomada y la vista es la del momento (el GET la ve resuelta).

        Se mira el estado y no solo `resuelta`: una compra hidratada del
        almacén ya la trae puesta (`Compra.desde_fila`).
        """
        loop = asyncio.get_running_loop()
        fin = loop.time() + self.espera_resolucion_s
        while compra.estado == "esperando_decision":
            restante = fin - loop.time()
            if restante <= 0:
                return
            if compra.resuelta.is_set():
                await asyncio.sleep(min(_PASO_RESOLUCION_S, restante))
                continue
            try:
                await asyncio.wait_for(compra.resuelta.wait(), restante)
            except TimeoutError:
                return

    async def aprobar(
        self, integrador: Integrador, compra_id: str, cart_id: str, contexto_id: str | None
    ) -> tuple[dict[str, Any], bool]:
        """La aprobación humana de un `escalar` retenido de esta compra.

        Con las reglas de siempre (`RegistroAprobaciones.aprobar`): solo si el
        último veredicto de ese contexto y cart es `escalar` y tiene un total
        firmado exacto. Si ya existe una pendiente para el mismo monto, se
        devuelve esa (200). Con almacén durable se guarda con await; si falla,
        no queda aprobada en ningún lado (503).

        Con varias instancias, el último veredicto y las aprobaciones de la
        compra se leen del almacén y la aprobación se crea ahí (una pendiente
        por contexto, cart y total firmado): un segundo POST, en esta o en otra
        instancia, devuelve la misma.
        """
        return await self._con_barrera(self._aprobar(integrador, compra_id, cart_id, contexto_id))

    async def _aprobar(
        self, integrador: Integrador, compra_id: str, cart_id: str, contexto_id: str | None
    ) -> tuple[dict[str, Any], bool]:
        proxy = self.proxy
        compra = await self._compra_de(integrador, compra_id)
        if self._compartidas is not None:
            return await self._aprobar_compartida(integrador, compra, cart_id, contexto_id)
        pendiente = compra.aprobacion_pendiente
        if not isinstance(pendiente, dict):
            previa = self._aprobacion_ya_creada(compra, cart_id, contexto_id)
            if previa is not None:
                return previa, False
            raise ErrorApi(409, "sin_escalar", "No hay un checkout retenido para aprobar.")
        ctx = pendiente.get("contexto_id")
        if pendiente.get("cart_id") != cart_id or (contexto_id is not None and contexto_id != ctx):
            raise ErrorApi(
                409, "cart_distinto", "El checkout retenido es de otro cart o contexto."
            )
        if not isinstance(ctx, str):
            raise ErrorApi(409, "sin_escalar", "No hay un checkout retenido para aprobar.")
        if proxy.aprobaciones.ultimo(ctx, cart_id) is None:
            # Después de un reinicio: el último veredicto (status y total firmado).
            try:
                fila = await proxy.almacen.cargar_ultimo_veredicto(ctx, cart_id)
            except ErrorAlmacen as e:
                logger.error("No se pudo leer el último veredicto de %s: %s", ctx, e)
                raise _no_disponible() from None
            if isinstance(fila, dict):
                centavos, moneda = fila.get("centavos"), fila.get("moneda")
                proxy.aprobaciones.registrar_veredicto(
                    ctx, cart_id, str(fila.get("status")),
                    centavos if isinstance(centavos, int) and not isinstance(centavos, bool)
                    else None,
                    moneda if isinstance(moneda, str) else None,
                    de_api=True,
                )
        try:
            aprobacion, creada = proxy.aprobaciones.aprobar(ctx, cart_id, via_api=True)
        except SinEscalar:
            raise ErrorApi(
                409, "sin_escalar", "El último veredicto de ese checkout no es escalar."
            ) from None
        if creada and proxy.almacen.durable:
            try:
                await proxy.almacen.guardar_aprobacion(
                    fila_aprobacion_gw(aprobacion, integrador_id=integrador.id, compra_id=compra.id)
                )
            except ErrorAlmacen as e:
                proxy.aprobaciones.descartar(aprobacion.id)
                logger.error("No se pudo guardar la aprobación %s: %s", aprobacion.id, e)
                raise _no_disponible() from None
        if creada and proxy.persistencia is not None:
            proxy.persistencia.guardar_aprobacion(aprobacion)  # auditoría, como el dashboard
        compra.aprobacion_pendiente = None
        proxy.guardar_compra(compra)
        logger.warning(
            "Aprobación humana %s del integrador %s: compra %s, contexto %s, %s, %s centavos",
            aprobacion.id, integrador.id, compra.id, ctx, cart_id, aprobacion.centavos,
        )
        return aprobacion.a_json(), creada

    def _aprobacion_ya_creada(
        self, compra: Compra, cart_id: str, contexto_id: str | None
    ) -> dict[str, Any] | None:
        """Una aprobación pendiente que ya se creó para esta compra (el
        segundo POST de la misma aprobación da 200 con la misma)."""
        contextos = self.proxy.compras.contextos_de(compra.id)
        for ctx in contextos if contexto_id is None else [contexto_id]:
            if ctx not in contextos:
                continue
            previa = self.proxy.aprobaciones.pendiente(ctx, cart_id)
            if previa is not None:
                return previa.a_json()
        return None

    async def _aprobar_compartida(
        self, integrador: Integrador, compra: Compra, cart_id: str, contexto_id: str | None
    ) -> tuple[dict[str, Any], bool]:
        """`aprobar` con varias instancias: las mismas reglas, con el último
        veredicto del almacén, y la pendiente creada en el almacén
        (`crear_aprobacion_pendiente`: si otra instancia ya la creó para el
        mismo contexto, cart y total firmado, devuelve esa)."""
        proxy = self.proxy
        sin_escalar = ErrorApi(
            409, "sin_escalar", "El último veredicto de ese checkout no es escalar."
        )
        try:
            pendiente = compra.aprobacion_pendiente
            if not isinstance(pendiente, dict):
                previa = await self._aprobacion_ya_creada_compartida(compra, cart_id, contexto_id)
                if previa is not None:
                    return previa, False
                raise ErrorApi(409, "sin_escalar", "No hay un checkout retenido para aprobar.")
            ctx = pendiente.get("contexto_id")
            if pendiente.get("cart_id") != cart_id or (
                contexto_id is not None and contexto_id != ctx
            ):
                raise ErrorApi(
                    409, "cart_distinto", "El checkout retenido es de otro cart o contexto."
                )
            if not isinstance(ctx, str):
                raise ErrorApi(409, "sin_escalar", "No hay un checkout retenido para aprobar.")
            # El último veredicto de ese checkout, de la base (el checkout pudo
            # pasar por otra instancia): status y total firmado exacto.
            ultimo = await proxy.almacen.cargar_ultimo_veredicto(ctx, cart_id)
            if not isinstance(ultimo, dict) or ultimo.get("status") != "escalar":
                raise sin_escalar
            centavos, moneda = ultimo.get("centavos"), ultimo.get("moneda")
            if not isinstance(centavos, int) or isinstance(centavos, bool):
                raise sin_escalar
            if not isinstance(moneda, str) or not moneda:
                raise sin_escalar
            nueva = Aprobacion(
                id=f"apr-{uuid.uuid4().hex[:12]}",
                contexto_id=ctx,
                cart_id=cart_id,
                centavos=centavos,
                moneda=moneda,
                creada=datetime.now(timezone.utc).isoformat(),
                via_api=True,
            )
            fila, creada = await proxy.almacen.crear_aprobacion_pendiente(
                fila_aprobacion_gw(nueva, integrador_id=integrador.id, compra_id=compra.id)
            )
            aprobacion = aprobacion_desde_fila(fila)
        except ErrorAlmacen as e:
            logger.error("No se pudo aprobar en el almacén la compra %s: %s", compra.id, e)
            raise _no_disponible() from None
        # En memoria también: el dashboard de esta instancia no la puede aprobar.
        proxy.aprobaciones.registrar_veredicto(ctx, cart_id, "escalar", centavos, moneda, de_api=True)
        if creada and proxy.persistencia is not None:
            proxy.persistencia.guardar_aprobacion(aprobacion)  # auditoría, como el dashboard
        compra.aprobacion_pendiente = None
        proxy.guardar_compra(compra)
        logger.warning(
            "Aprobación humana %s del integrador %s: compra %s, contexto %s, %s, %s centavos%s",
            aprobacion.id, integrador.id, compra.id, ctx, cart_id, aprobacion.centavos,
            "" if creada else " (ya estaba creada)",
        )
        return aprobacion.a_json(), creada

    async def _aprobacion_ya_creada_compartida(
        self, compra: Compra, cart_id: str, contexto_id: str | None
    ) -> dict[str, Any] | None:
        """La pendiente que ya se creó para esta compra, del almacén (la pudo
        crear otra instancia): la más nueva de ese cart (y contexto)."""
        filas = await self.proxy.almacen.aprobaciones_de_compra(compra.id)
        for fila in reversed(filas):
            if fila.get("estado") != "pendiente" or fila.get("cart_id") != cart_id:
                continue
            if contexto_id is not None and fila.get("contexto_id") != contexto_id:
                continue
            return aprobacion_desde_fila(fila).a_json()
        return None

    # -- servicios sin estado --------------------------------------------------

    @contextlib.contextmanager
    def _cupo_sin_estado(self, integrador: Integrador) -> Iterator[None]:
        """Un lugar de los `MAX_SIN_ESTADO_POR_INTEGRADOR` del integrador, o 429
        sin esperar. Sin await entre mirar y sumar: no hace falta candado."""
        en_curso = self._sin_estado_en_curso.get(integrador.id, 0)
        if en_curso >= MAX_SIN_ESTADO_POR_INTEGRADOR:
            raise ErrorApi(
                429, "demasiadas_solicitudes",
                "Hay demasiadas solicitudes en curso para esta clave en /v1/filtrar y "
                "/v1/evaluar; reintentá cuando terminen.",
                {"maximo": MAX_SIN_ESTADO_POR_INTEGRADOR},
            )
        self._sin_estado_en_curso[integrador.id] = en_curso + 1
        try:
            yield
        finally:
            quedan = self._sin_estado_en_curso.get(integrador.id, 1) - 1
            if quedan > 0:
                self._sin_estado_en_curso[integrador.id] = quedan
            else:
                self._sin_estado_en_curso.pop(integrador.id, None)

    async def evaluar(self, integrador: Integrador, solicitud: SolicitudEvaluar) -> dict[str, Any]:
        with self._cupo_sin_estado(integrador):
            # La barrera: el registro del veredicto en Supabase antes de responder.
            return await self._con_barrera(self._evaluar(integrador, solicitud))

    async def _evaluar(self, integrador: Integrador, solicitud: SolicitudEvaluar) -> dict[str, Any]:
        proxy = self.proxy
        comercio = (
            self._comercio_permitido(integrador, solicitud.comercio_id)
            if solicitud.comercio_id is not None
            else None
        )
        politica = politica_de([comercio] if comercio else self.comercios_de(integrador))
        resultado = await sin_estado.evaluar_sin_estado(
            solicitud,
            evaluador=proxy.evaluador,
            # La clave inyectada (Vercel) o, sin ella, la del archivo: una sola.
            key_path=None if proxy.clave_recibo is not None else proxy.config.guard_key_path,
            clave=proxy.clave_recibo,
            client=proxy.client,
            comercio=comercio,
            politica=politica,
            fabricante=proxy.fabricante,
            ejecutor=proxy.ejecutor_llm,  # las consultas al fabricante
        )
        if proxy.persistencia is not None:
            # Registro (sin bus: no es tráfico de la demo). Solo la vista
            # reducida: nada del texto del comercio. El recibo, con las
            # claves que lee `persistencia.fila_veredicto`.
            recibo = resultado.get("recibo")
            if isinstance(recibo, dict):
                recibo = {"id": recibo.get("id"), "firma": recibo.get("jws"), "kid": recibo.get("kid")}
            proxy.persistencia.guardar_evento(
                Evento(
                    id=uuid.uuid4().hex,
                    ts=datetime.now(timezone.utc),
                    guard_activo=True,
                    tipo="veredicto",
                    datos={
                        "veredicto": {
                            "status": resultado.get("status"),
                            "cart_id_elegido": resultado.get("cart_id"),
                            "motivo": resultado.get("motivo"),
                            "chequeos": resultado.get("chequeos"),
                            "recibo": recibo,
                        },
                        "evaluador": resultado.get("evaluador"),
                        "origen": "api_evaluar",
                        "integrador": integrador.id,
                        "comercio_id": resultado.get("comercio_firmado"),
                    },
                )
            )
        logger.info(
            "/v1/evaluar del integrador %s: %s (%s)",
            integrador.id, resultado.get("status"), resultado.get("cart_id"),
        )
        return resultado

    async def filtrar(self, integrador: Integrador, solicitud: SolicitudFiltrar) -> dict[str, Any]:
        with self._cupo_sin_estado(integrador):
            resultado = await sin_estado.filtrar_contenido(
                self.proxy.filtro, solicitud, ejecutor=self.proxy.ejecutor_filtro
            )
        logger.info(
            "/v1/filtrar del integrador %s (%s): %s",
            integrador.id, solicitud.origen, resultado.get("status"),
        )
        return resultado

    def clave_publica(self) -> dict[str, Any] | None:
        """`{"kid", "jwk", "keys": [jwk]}` del Guard real (None con el stub o
        sin clave: los recibos no se pueden verificar)."""
        proxy = self.proxy
        if proxy.evaluador.nombre != "guard":
            return None
        try:
            datos = proxy.datos_clave_publica()
        except Exception:  # noqa: BLE001
            logger.exception("No se pudo leer la clave del Guard")
            return None
        if not isinstance(datos, dict) or not isinstance(datos.get("jwk"), dict):
            return None
        return {"kid": datos.get("kid"), "jwk": datos["jwk"], "keys": [datos["jwk"]]}

    # -- webhooks --------------------------------------------------------------

    def _sin_webhook(self) -> ErrorApi:
        return ErrorApi(409, "sin_webhook", "Esta clave no tiene un webhook configurado.")

    async def entregas_webhook(self, integrador: Integrador, limite: int) -> list[dict[str, Any]]:
        """Las últimas entregas del integrador. Con varias instancias, del
        almacén (cada instancia tiene en memoria solo las que hizo ella)."""
        despachador = self.proxy.despachador
        if despachador is None or integrador.webhook is None:
            raise self._sin_webhook()
        if self.proxy.varias:
            try:
                return await self.proxy.almacen.listar_entregas(integrador.id, limite=limite)
            except ErrorAlmacen as e:
                logger.error("No se pudieron leer las entregas de %s: %s", integrador.id, e)
                raise _no_disponible() from None
        return despachador.entregas(integrador.id, limite)

    async def probar_webhook(self, integrador: Integrador) -> dict[str, Any]:
        despachador = self.proxy.despachador
        if despachador is None:
            raise self._sin_webhook()
        try:
            return await despachador.probar(integrador.id)
        except LookupError:
            raise self._sin_webhook() from None
