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
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal

from contracts import Evento
from proxy import sin_estado
from proxy.aprobaciones import SinEscalar
from proxy.comercios import politica_de
from proxy.compras import Compra, EstadoInvalido
from proxy.estado import fila_aprobacion_gw
from proxy.evaluador import clave_publica
from proxy.tipos_gateway import (
    Comercio,
    ErrorAlmacen,
    ErrorApi,
    FotoCompra,
    Integrador,
    SolicitudCompra,
    SolicitudEvaluar,
    SolicitudFiltrar,
)

if TYPE_CHECKING:
    from proxy.app import ProxyGuard

logger = logging.getLogger("proxy.gateway")


# Estados que `Compra.desde_fila` cierra después de un reinicio: una fila
# en el estado de la derecha se lee en el de la izquierda.
_NORMALIZADA_DESDE = {"negociando": "esperando_contexto", "vencida": "esperando_decision"}

# Cuánto espera `POST /decision` a que la búsqueda retenida aplique la
# decisión antes de armar la vista (en la práctica, unas vueltas del loop).
ESPERA_RESOLUCION_S = 2.0
_PASO_RESOLUCION_S = 0.01


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
        self.espera_resolucion_s = ESPERA_RESOLUCION_S

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
            # creación de este integrador ya la cuenta.
            return await self._crear_compra(integrador, solicitud, comercio)

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
        return self.proxy.foto(await self._compra_de(integrador, compra_id))

    async def listar_compras(
        self, integrador: Integrador, *, limite: int, estado: str | None
    ) -> list[FotoCompra]:
        """Del almacén (la lista completa), con la instancia de memoria cuando
        existe (tiene las esperas vivas y puede estar más al día)."""
        proxy = self.proxy
        try:
            filas = await proxy.almacen.listar_compras(integrador.id, limite=limite, estado=estado)
            desde = _NORMALIZADA_DESDE.get(estado) if estado is not None else None
            if desde is not None:
                # Una fila que quedó esperando antes de un reinicio se lee con
                # la espera cerrada (`Compra.desde_fila`): también es de este
                # estado aunque la fila todavía no lo diga.
                filas = [
                    *filas,
                    *await proxy.almacen.listar_compras(
                        integrador.id, limite=limite, estado=desde
                    ),
                ]
            compras: list[Compra] = []
            for fila in filas:
                compra = proxy.compras.get(fila.get("id")) if isinstance(fila, dict) else None
                if compra is None:
                    # Sin restaurarla en memoria: un listado no desplaza a las
                    # compras en curso.
                    compra = Compra.desde_fila(fila)
                    if compra.estado != fila.get("estado"):
                        proxy.guardar_compra(compra)  # la fila, con la espera cerrada
                if compra.integrador_id != integrador.id:
                    continue
                if estado is not None and compra.estado != estado:
                    continue
                compras.append(compra)
        except ErrorAlmacen as e:
            logger.error("No se pudieron listar las compras de %s: %s", integrador.id, e)
            raise _no_disponible() from None
        compras.sort(key=lambda c: c.creada, reverse=True)
        return [proxy.foto(c) for c in compras[:limite]]

    async def responder_porque(
        self, integrador: Integrador, compra_id: str, respuesta: str | None
    ) -> FotoCompra:
        compra = await self._compra_de(integrador, compra_id)
        try:
            self.proxy.compras.responder(compra.id, respuesta)
        except EstadoInvalido as e:
            raise _estado(e.estado) from None
        logger.info("Compra %s: el integrador %s respondió el porqué", compra.id, integrador.id)
        return self.proxy.foto(compra)

    async def decidir_oportunidad(
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
        """
        proxy = self.proxy
        compra = await self._compra_de(integrador, compra_id)
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

    # -- servicios sin estado --------------------------------------------------

    async def evaluar(self, integrador: Integrador, solicitud: SolicitudEvaluar) -> dict[str, Any]:
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
            key_path=proxy.config.guard_key_path,
            client=proxy.client,
            comercio=comercio,
            politica=politica,
            fabricante=proxy.fabricante,
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
        resultado = await sin_estado.filtrar_contenido(self.proxy.filtro, solicitud)
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
            datos = clave_publica(proxy.config.guard_key_path)
        except Exception:  # noqa: BLE001
            logger.exception("No se pudo leer la clave del Guard")
            return None
        if not isinstance(datos, dict) or not isinstance(datos.get("jwk"), dict):
            return None
        return {"kid": datos.get("kid"), "jwk": datos["jwk"], "keys": [datos["jwk"]]}

    # -- webhooks --------------------------------------------------------------

    def _sin_webhook(self) -> ErrorApi:
        return ErrorApi(409, "sin_webhook", "Esta clave no tiene un webhook configurado.")

    def entregas_webhook(self, integrador: Integrador, limite: int) -> list[dict[str, Any]]:
        despachador = self.proxy.despachador
        if despachador is None or integrador.webhook is None:
            raise self._sin_webhook()
        return despachador.entregas(integrador.id, limite)

    async def probar_webhook(self, integrador: Integrador) -> dict[str, Any]:
        despachador = self.proxy.despachador
        if despachador is None:
            raise self._sin_webhook()
        try:
            return await despachador.probar(integrador.id)
        except LookupError:
            raise self._sin_webhook() from None
