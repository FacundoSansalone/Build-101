"""Webhooks firmados: el gateway le avisa al backend del integrador.

Cuando una compra de integrador necesita al usuario (el porqué, una
oportunidad, una aprobación) o termina (veredicto, bloqueo), el gateway le
manda un POST al webhook que el integrador configuró. Este módulo hace solo la
entrega; qué se manda (la `VistaCompra` ya saneada) lo decide el núcleo.

Reglas (diseño del gateway, §1.6):

- **Firma HMAC con timestamp.** `X-Guard-Firma: t=<unix>,v1=<hex>` sobre
  `f"{t}." + cuerpo`. `t` es el momento de cada intento, así un receptor puede
  rechazar lo viejo (anti-replay) y deduplicar por `X-Guard-Entrega`, que es
  igual en todos los reintentos.
- **SSRF.** Solo `https`, sin userinfo. En cada intento se resuelve el host y,
  si alguna IP no es global, la entrega se descarta sin reintento. El pedido
  sale a la IP ya resuelta (con `Host` y SNI del nombre original): entre la
  verificación y la conexión nadie puede cambiar el DNS (rebinding).
- **Reintentos.** Hasta 6 intentos con backoff y jitter ante red, timeout,
  5xx, 408 y 429. No sigue redirecciones: un 3xx es una falla, porque seguirla
  saltearía el chequeo de destino.
- **En memoria.** La cola no sobrevive a un reinicio (al-menos-una-vez
  mientras el proceso vive). El integrador reconstruye con
  `GET /v1/compras/{id}`. El registro de entregas guarda las últimas 500 por
  integrador y, si hay almacén, una copia en segundo plano.

No lee el entorno: todo llega por parámetros (`GATEWAY_WEBHOOKS_INSEGUROS` lo
traduce la integración a `permitir_inseguro`).
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import ipaddress
import json
import logging
import random
import secrets
import socket
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, get_args
from urllib.parse import urlsplit

import httpx

from contracts import Evento
from proxy.tipos_gateway import (
    HEADER_ENTREGA_WEBHOOK,
    HEADER_EVENTO_WEBHOOK,
    HEADER_FIRMA_WEBHOOK,
    ID_COMPRA_RE,
    AlmacenEstado,
    EntregaWebhook,
    Integrador,
    TipoWebhook,
    es_id,
)

logger = logging.getLogger("proxy.webhooks")

TIPOS_WEBHOOK: tuple[str, ...] = get_args(TipoWebhook)
BACKOFF_S: tuple[float, ...] = (1, 4, 16, 60, 300)
MAX_INTENTOS = 6
TOLERANCIA_S = 300

USER_AGENT = "KaiAI-Webhooks/1"
MAX_REGISTRO_POR_INTEGRADOR = 500
MAX_LECTURA_RESPUESTA = 4096        # bytes de la respuesta que se leen, solo para el log
MAX_GUARDADOS_EN_VUELO = 100        # copias al almacén; si se cuelga no crece sin tope
MAX_PENDIENTES = 1000               # entregas en vuelo, entre todos los integradores
# La parte de la cola de cada integrador: uno cuyo webhook no contesta (o
# falla y se reintenta ~7 min) no deja sin lugar a los demás.
MAX_PENDIENTES_POR_INTEGRADOR = 200
JITTER = (0.8, 1.2)
STATUS_REINTENTABLES_4XX = frozenset({408, 429})

Resolver = Callable[[str, int], Awaitable[list[str]]]


class DestinoNoPermitido(ValueError):
    """La URL del webhook o la IP a la que resuelve no se pueden usar (SSRF)."""


# -- firma -------------------------------------------------------------------


def firmar(secreto: bytes, cuerpo: bytes, t: int) -> str:
    """El valor de `X-Guard-Firma` para un intento hecho en el instante `t`."""
    mac = hmac.new(secreto, f"{t}.".encode() + cuerpo, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def verificar_firma(
    secreto: bytes,
    cuerpo: bytes,
    cabecera: str,
    *,
    ahora: int,
    tolerancia: int = TOLERANCIA_S,
) -> bool:
    """Lo que tiene que hacer el receptor (y la doc lo muestra así).

    Rechaza si `t` está a más de `tolerancia` segundos de `ahora` (en los dos
    sentidos: un reloj adelantado también es sospechoso). Acepta varios `v1`
    en la misma cabecera, por si un día se rota el secreto con dos firmas.
    Nunca lanza: cualquier formato raro es una firma inválida.
    """
    if not isinstance(cabecera, str) or not isinstance(cuerpo, bytes):
        return False
    t: int | None = None
    firmas: list[str] = []
    for parte in cabecera.split(","):
        clave, sep, valor = parte.strip().partition("=")
        if not sep:
            return False
        if clave == "t":
            if t is not None or not valor.isascii() or not valor.isdigit():
                return False
            t = int(valor)
        elif clave == "v1":
            firmas.append(valor)
    if t is None or not firmas:
        return False
    if abs(ahora - t) > tolerancia:
        return False
    esperada = firmar(secreto, cuerpo, t).partition(",v1=")[2]
    return any(hmac.compare_digest(esperada.encode(), f.encode("utf-8", "replace")) for f in firmas)


def cuerpo_webhook(
    *,
    entrega_id: str,
    tipo: str,
    integrador_id: str,
    creado: datetime,
    compra: dict[str, Any] | None,
    datos: dict[str, Any] | None = None,
) -> bytes:
    """JSON compacto y ordenado: los mismos bytes en cada reintento.

    `compra` y `datos` van tal cual: sanearlos (nada del texto del vendedor)
    es tarea del núcleo, que manda la misma `VistaCompra` que da el GET.
    """
    return json.dumps(
        {
            "id": entrega_id,
            "tipo": tipo,
            "creado": creado.isoformat(),
            "integrador": integrador_id,
            "compra": compra,
            "datos": datos or {},
        },
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")


def tipo_webhook(evento: Evento) -> str | None:
    """Qué webhook corresponde a un `Evento` del proxy (None = ninguno).

    Un `bloqueado` del Guard ya sale como `compra.veredicto`; por eso solo se
    avisa `compra.bloqueada` cuando el bloqueo viene de una etapa (filtro,
    intención) o del proxy sin veredicto (`sin_veredicto`, lo agrega la
    integración).
    """
    datos = evento.datos if isinstance(evento.datos, dict) else {}
    if evento.tipo == "pedido_detectado":
        return "compra.pregunta"
    if evento.tipo == "oportunidad":
        return "compra.oportunidad"
    if evento.tipo == "veredicto":
        return "compra.veredicto"
    if evento.tipo == "bloqueado":
        if datos.get("requiere_aprobacion") is True:
            return "compra.requiere_aprobacion"
        if "etapa" in datos or datos.get("sin_veredicto") is True:
            return "compra.bloqueada"
    return None


# -- destino (SSRF) ------------------------------------------------------------


def _partes(url: str, *, permitir_inseguro: bool) -> tuple[str, str, int, str]:
    """(esquema, host, puerto, ruta+query) de una URL de webhook válida."""
    if not isinstance(url, str) or not url or any(ord(c) < 0x21 for c in url):
        raise DestinoNoPermitido("URL de webhook vacía o con espacios")
    try:
        partes = urlsplit(url)
        puerto = partes.port
    except ValueError as e:
        raise DestinoNoPermitido("URL de webhook inválida") from e
    esquema = partes.scheme.lower()
    permitidos = ("https", "http") if permitir_inseguro else ("https",)
    if esquema not in permitidos:
        raise DestinoNoPermitido("el webhook tiene que ser https")
    if "@" in partes.netloc:
        raise DestinoNoPermitido("la URL del webhook no puede llevar usuario ni clave")
    host = (partes.hostname or "").rstrip(".")
    if not host:
        raise DestinoNoPermitido("la URL del webhook no tiene host")
    if puerto is None:
        puerto = 443 if esquema == "https" else 80
    ruta = partes.path or "/"
    if partes.query:
        ruta = f"{ruta}?{partes.query}"
    return esquema, host, puerto, ruta


def _ip(texto: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(texto.split("%", 1)[0])
    except ValueError:
        return None


def ip_permitida(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Solo direcciones globales de unicast (ni loopback, ni privadas, ni metadata).

    Una IPv4 mapeada en IPv6 (`::ffff:127.0.0.1`) se juzga por la IPv4 de
    adentro: es la que termina usando el sistema operativo. Lo mismo con el
    prefijo NAT64 conocido (`64:ff9b::/96`): en una red con NAT64 la conexión
    sale hacia la IPv4 de adentro, y `is_global` lo acepta aunque esa IPv4
    sea privada o la de metadata. Las IPv4-compatibles (`::/96`, obsoletas)
    y las IPv4-traducidas (`::ffff:0:0:0/96`) no se usan para un webhook: se
    rechazan. 6to4 y Teredo ya no son globales para `ipaddress`.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip_permitida(ip.ipv4_mapped)
        if ip in _NAT64:
            return ip_permitida(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if any(ip in red for red in _IPV4_EN_IPV6_RECHAZADAS):
            return False
    return ip.is_global and not ip.is_multicast


_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
_IPV4_EN_IPV6_RECHAZADAS = (
    ipaddress.IPv6Network("::/96"),            # IPv4-compatible
    ipaddress.IPv6Network("::ffff:0:0:0/96"),  # IPv4-traducida (SIIT)
)


def validar_url_estatica(url: str, *, permitir_inseguro: bool) -> None:
    """Chequeo al arrancar, sin DNS: esquema, userinfo, host e IP literal.

    El chequeo que cuenta es el de cada intento (`resolver_destino`); este
    corta antes una configuración que nunca podría funcionar.
    """
    _, host, _, _ = _partes(url, permitir_inseguro=permitir_inseguro)
    if permitir_inseguro:
        return
    literal = _ip(host)
    if literal is not None and not ip_permitida(literal):
        raise DestinoNoPermitido("el webhook apunta a una IP no pública")
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        raise DestinoNoPermitido("el webhook apunta a localhost")


async def _resolver_por_defecto(host: str, puerto: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, puerto, type=socket.SOCK_STREAM
    )
    return [str(info[4][0]) for info in infos]


async def resolver_destino(
    url: str,
    *,
    permitir_inseguro: bool,
    resolver: Resolver | None = None,
) -> tuple[str, int, list[str]]:
    """(host, puerto, IPs permitidas). `DestinoNoPermitido` si alguna no es global.

    Basta una IP no global para descartar: si el receptor resuelve a una
    pública y a una interna, no hay forma de saber a cuál conectaría otro
    cliente. Con `permitir_inseguro` (solo desarrollo) no se resuelve ni se
    fija la IP, y devuelve una lista vacía. Un error de DNS (`OSError`) se
    propaga: es una falla de red y se reintenta.
    """
    _, host, puerto, _ = _partes(url, permitir_inseguro=permitir_inseguro)
    if permitir_inseguro:
        return host, puerto, []
    literal = _ip(host)
    if literal is not None:
        textos = [host]
    else:
        textos = await (resolver or _resolver_por_defecto)(host, puerto)
    ips: list[str] = []
    for texto in textos:
        ip = _ip(str(texto))
        if ip is None or not ip_permitida(ip):
            raise DestinoNoPermitido("el webhook resuelve a una IP no pública")
        if str(ip) not in ips:
            ips.append(str(ip))
    if not ips:
        raise OSError(f"{host} no resolvió a ninguna IP")
    return host, puerto, ips


def _url_fijada(ip: str, puerto: int, ruta: str) -> str:
    host = f"[{ip}]" if ":" in ip else ip
    return f"https://{host}:{puerto}{ruta}"


def _cabecera_host(host: str, puerto: int) -> str:
    nombre = f"[{host}]" if ":" in host else host
    return nombre if puerto == 443 else f"{nombre}:{puerto}"


# -- despachador ---------------------------------------------------------------


class _Reintentar(Exception):
    """Un intento falló de una forma que vale la pena reintentar."""

    def __init__(self, error: str, status_http: int | None = None) -> None:
        super().__init__(error)
        self.error = error
        self.status_http = status_http


class _Final(Exception):
    """Un intento falló de una forma que no se reintenta."""

    def __init__(self, estado: str, error: str, status_http: int | None = None) -> None:
        super().__init__(error)
        self.estado = estado
        self.error = error
        self.status_http = status_http


class Despachador:
    """Cola de entregas en memoria: una tarea asyncio por entrega."""

    def __init__(
        self,
        integradores: Callable[[str], Integrador | None],
        *,
        almacen: AlmacenEstado | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        permitir_inseguro: bool = False,
        resolver: Resolver | None = None,
        reloj: Callable[[], float] = time.time,
        dormir: Callable[[float], Awaitable[None]] = asyncio.sleep,
        azar: Callable[[], float] = random.random,
        max_intentos: int = MAX_INTENTOS,
        backoff_s: tuple[float, ...] = BACKOFF_S,
        max_pendientes: int = MAX_PENDIENTES,
        max_pendientes_por_integrador: int = MAX_PENDIENTES_POR_INTEGRADOR,
        timeout_s: float = 10.0,
    ) -> None:
        self._integradores = integradores
        self._almacen = almacen
        self._permitir_inseguro = permitir_inseguro
        self._resolver = resolver
        self._reloj = reloj
        self._dormir = dormir
        self._azar = azar
        self._max_intentos = max(1, max_intentos)
        self._backoff_s = tuple(backoff_s) or (0.0,)
        self._max_pendientes = max_pendientes
        self._max_por_integrador = max_pendientes_por_integrador
        # Tope total de cada intento: los timeouts de httpx son por operación
        # (cada byte que llega reinicia el de lectura), así que un receptor que
        # gotea un byte cada 9 s retenía la entrega horas.
        self._timeout_s = timeout_s
        # Cliente propio: sin proxies del entorno (podrían ver o redirigir el
        # tráfico firmado) y sin seguir redirecciones (saltearían el chequeo
        # de destino). Sin keep-alive: cada POST va a `https://<ip>` con el
        # SNI del host, y el pool de httpcore reutiliza conexiones por
        # (esquema, IP, puerto) sin mirar el SNI. Con keep-alive, el webhook
        # de un integrador podía salir por una conexión abierta (y validada)
        # con el certificado de otro que apunta su dominio a la misma IP.
        self._client = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_s),
            limits=httpx.Limits(max_keepalive_connections=0),
        )
        self._registro: dict[str, deque[EntregaWebhook]] = {}
        self._tareas: dict[asyncio.Task[None], EntregaWebhook] = {}
        self._guardados: set[asyncio.Task[None]] = set()
        self._cerrado = False

    # -- lo que llama el núcleo ------------------------------------------------

    def notificar(
        self,
        integrador_id: str,
        tipo: str,
        compra: dict[str, Any] | None,
        datos: dict[str, Any] | None = None,
    ) -> str | None:
        """Programa la entrega y devuelve su id (None si no hay webhook).

        Nunca lanza: un webhook que no sale no puede cortar la compra, que
        sigue su curso y se puede consultar por GET.
        """
        try:
            integrador = self._integradores(integrador_id)
            webhook = integrador.webhook if integrador is not None else None
            if webhook is None:
                return None
            entrega = self._nueva(integrador_id, tipo, compra)
            cuerpo = cuerpo_webhook(
                entrega_id=entrega.id,
                tipo=tipo,
                integrador_id=integrador_id,
                creado=entrega.creada,
                compra=compra,
                datos=datos,
            )
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self._terminar(entrega, "descartada", "sin_loop", guardar=False)
                return entrega.id
            if self._cerrado:
                self._terminar(entrega, "descartada", "cerrado")
                return entrega.id
            if len(self._tareas) >= self._max_pendientes or (
                self._pendientes_de(integrador_id) >= self._max_por_integrador
            ):
                logger.warning("Cola de webhooks llena: se descarta %s (%s)", entrega.id, tipo)
                self._terminar(entrega, "descartada", "cola_llena")
                return entrega.id
            tarea = loop.create_task(
                self._entregar(entrega, webhook.url, webhook.secreto, cuerpo, self._max_intentos)
            )
            self._tareas[tarea] = entrega
            tarea.add_done_callback(self._tarea_terminada)
            return entrega.id
        except Exception:
            logger.exception("No se pudo programar el webhook %s para %s", tipo, integrador_id)
            return None

    def entregas(self, integrador_id: str, limite: int = 50) -> list[dict[str, Any]]:
        """Las últimas entregas del integrador, la más reciente primero."""
        registro = self._registro.get(integrador_id)
        if not registro:
            return []
        limite = max(0, min(int(limite), MAX_REGISTRO_POR_INTEGRADOR))
        return [e.a_json() for e in list(reversed(registro))[:limite]]

    async def probar(self, integrador_id: str) -> dict[str, Any]:
        """Manda un `ping` en un solo intento y devuelve cómo le fue.

        Se espera acá mismo (no pasa por la cola): el integrador quiere ver
        el resultado en la respuesta de `POST /v1/webhooks/prueba`.
        """
        integrador = self._integradores(integrador_id)
        webhook = integrador.webhook if integrador is not None else None
        if webhook is None:
            raise LookupError(f"El integrador {integrador_id} no tiene webhook")
        entrega = self._nueva(integrador_id, "ping", None)
        if self._cerrado:
            self._terminar(entrega, "descartada", "cerrado")
            return entrega.a_json()
        cuerpo = cuerpo_webhook(
            entrega_id=entrega.id,
            tipo="ping",
            integrador_id=integrador_id,
            creado=entrega.creada,
            compra=None,
        )
        await self._entregar(entrega, webhook.url, webhook.secreto, cuerpo, 1)
        return entrega.a_json()

    async def esperar(self) -> None:
        """Espera hasta vaciar la cola y las copias al almacén (tests)."""
        while self._tareas or self._guardados:
            await asyncio.gather(*list(self._tareas), *list(self._guardados), return_exceptions=True)
            # Ceder una vuelta al loop: si todas ya terminaron, gather (3.12)
            # resuelve sin pasar por el loop y el `discard` del done callback
            # no llega a correr (el while no terminaba nunca).
            await asyncio.sleep(0)

    async def cerrar(self, timeout_s: float = 5.0) -> None:
        """Cancela lo pendiente (queda `descartada`/`cerrado`) y cierra el cliente."""
        self._cerrado = True
        pendientes = list(self._tareas)
        for tarea in pendientes:
            tarea.cancel()
        if pendientes:
            await asyncio.gather(*pendientes, return_exceptions=True)
        # Por si alguna tarea terminó cancelada antes de llegar a marcarse.
        for entrega in list(self._tareas.values()):
            if entrega.estado == "pendiente":
                self._terminar(entrega, "descartada", "cerrado")
        if self._guardados:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*list(self._guardados), return_exceptions=True), timeout_s
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Quedaron %d entregas de webhook sin guardar en el almacén",
                    len(self._guardados),
                )
        await self._client.aclose()

    # -- interno ---------------------------------------------------------------

    def _ahora(self) -> datetime:
        return datetime.fromtimestamp(self._reloj(), timezone.utc)

    def _pendientes_de(self, integrador_id: str) -> int:
        return sum(1 for e in self._tareas.values() if e.integrador_id == integrador_id)

    def _nueva(self, integrador_id: str, tipo: str, compra: dict[str, Any] | None) -> EntregaWebhook:
        compra_id = compra.get("compra_id") if isinstance(compra, dict) else None
        entrega = EntregaWebhook(
            id=f"whe-{secrets.token_hex(16)}",
            integrador_id=integrador_id,
            tipo=tipo,
            compra_id=compra_id if es_id(ID_COMPRA_RE, compra_id) else None,
            creada=self._ahora(),
        )
        registro = self._registro.setdefault(
            integrador_id, deque(maxlen=MAX_REGISTRO_POR_INTEGRADOR)
        )
        registro.append(entrega)
        return entrega

    def _tarea_terminada(self, tarea: asyncio.Task[None]) -> None:
        entrega = self._tareas.pop(tarea, None)
        if entrega is not None and entrega.estado == "pendiente":
            # Cancelada antes de empezar (la corrutina nunca corrió).
            self._terminar(entrega, "descartada", "cerrado")

    def _terminar(
        self,
        entrega: EntregaWebhook,
        estado: str,
        error: str | None,
        *,
        status_http: int | None = None,
        guardar: bool = True,
    ) -> None:
        entrega.estado = estado  # type: ignore[assignment]
        entrega.error = error
        if status_http is not None:
            entrega.status_http = status_http
        entrega.actualizada = self._ahora()
        if estado != "entregada":
            logger.warning(
                "Webhook %s (%s) para %s: %s, error=%s, intentos=%d",
                entrega.id, entrega.tipo, entrega.integrador_id, estado, error, entrega.intentos,
            )
        if guardar:
            self._guardar(entrega)

    def _guardar(self, entrega: EntregaWebhook) -> None:
        """Copia al almacén en segundo plano; si falla, solo queda en el log."""
        if self._almacen is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if len(self._guardados) >= MAX_GUARDADOS_EN_VUELO:
            logger.warning("El almacén no da abasto: la entrega %s no se guarda", entrega.id)
            return
        tarea = loop.create_task(self._guardar_ya(dataclasses.replace(entrega)))
        self._guardados.add(tarea)
        tarea.add_done_callback(self._guardados.discard)

    async def _guardar_ya(self, foto: EntregaWebhook) -> None:
        try:
            await self._almacen.guardar_entrega(foto)  # type: ignore[union-attr]
        except Exception as e:  # el registro no puede romper la entrega
            logger.warning("No se pudo guardar la entrega %s: %s", foto.id, type(e).__name__)

    async def _entregar(
        self,
        entrega: EntregaWebhook,
        url: str,
        secreto: bytes,
        cuerpo: bytes,
        max_intentos: int,
    ) -> None:
        try:
            while True:
                entrega.intentos += 1
                entrega.actualizada = self._ahora()
                try:
                    status = await self._intento(entrega, url, secreto, cuerpo)
                except _Final as f:
                    self._terminar(entrega, f.estado, f.error, status_http=f.status_http)
                    return
                except _Reintentar as r:
                    if r.status_http is not None:
                        entrega.status_http = r.status_http
                    entrega.error = r.error
                    if entrega.intentos >= max_intentos:
                        self._terminar(entrega, "fallida", r.error, status_http=r.status_http)
                        return
                    indice = min(entrega.intentos - 1, len(self._backoff_s) - 1)
                    bajo, alto = JITTER
                    espera = self._backoff_s[indice] * (bajo + (alto - bajo) * self._azar())
                    await self._dormir(espera)
                    continue
                entrega.error = None
                self._terminar(entrega, "entregada", None, status_http=status)
                return
        except asyncio.CancelledError:
            if entrega.estado == "pendiente":
                self._terminar(entrega, "descartada", "cerrado")
            raise
        except Exception:
            # Un error inesperado (bug) no deja la entrega colgada en "pendiente".
            logger.exception("Error inesperado entregando el webhook %s", entrega.id)
            if entrega.estado == "pendiente":
                self._terminar(entrega, "fallida", "red")

    async def _intento(
        self, entrega: EntregaWebhook, url: str, secreto: bytes, cuerpo: bytes
    ) -> int:
        """Un POST. Devuelve el status 2xx o lanza `_Reintentar` / `_Final`."""
        try:
            host, puerto, ips = await resolver_destino(
                url, permitir_inseguro=self._permitir_inseguro, resolver=self._resolver
            )
        except DestinoNoPermitido as e:
            raise _Final("descartada", "destino_no_permitido") from e
        except OSError as e:  # DNS caído o sin respuesta: es red, se reintenta
            raise _Reintentar("red") from e
        entrega.destino_host = host

        headers = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            HEADER_EVENTO_WEBHOOK: entrega.tipo,
            HEADER_ENTREGA_WEBHOOK: entrega.id,
            HEADER_FIRMA_WEBHOOK: firmar(secreto, cuerpo, int(self._reloj())),
        }
        extensions: dict[str, Any] = {}
        destino = url
        if ips:
            # IP fijada: se conecta a la IP que se acaba de verificar; el
            # certificado se valida contra el nombre (SNI) y el `Host` lo lleva.
            _, _, _, ruta = _partes(url, permitir_inseguro=self._permitir_inseguro)
            destino = _url_fijada(ips[0], puerto, ruta)
            headers["Host"] = _cabecera_host(host, puerto)
            extensions["sni_hostname"] = host

        status: int | None = None
        leido = b""
        try:
            # Tope total del intento (conexión, headers y lo que se lea).
            async with asyncio.timeout(self._timeout_s):
                async with self._client.stream(
                    "POST", destino, content=cuerpo, headers=headers, extensions=extensions
                ) as resp:
                    status = resp.status_code
                    if not 200 <= status < 300:
                        # El cuerpo solo sirve para el log de un error: con un 2xx
                        # no se lee (un cuerpo que gotea no retiene la entrega).
                        leido = await _leer_para_log(resp)
        except TimeoutError as e:
            if status is None:
                raise _Reintentar("timeout") from e
            # Se cortó leyendo el cuerpo de un error: vale el status que llegó.
        except httpx.TimeoutException as e:
            raise _Reintentar("timeout") from e
        except httpx.HTTPError as e:
            raise _Reintentar("red") from e
        assert status is not None

        if not (200 <= status < 300):
            logger.debug(
                "Webhook %s → HTTP %d: %r", entrega.id, status, leido[:MAX_LECTURA_RESPUESTA]
            )
        if 200 <= status < 300:
            return status
        if 300 <= status < 400:
            raise _Final("fallida", "redireccion", status)
        if status >= 500:
            raise _Reintentar("http_5xx", status)
        if status in STATUS_REINTENTABLES_4XX:
            raise _Reintentar("http_4xx", status)
        raise _Final("fallida", "http_4xx" if status >= 400 else "red", status)


async def _leer_para_log(resp: httpx.Response) -> bytes:
    """Bytes crudos (sin descomprimir: un gzip chico no se vuelve megas) y como
    mucho `MAX_LECTURA_RESPUESTA`: es solo para el log."""
    leido = b""
    try:
        async for trozo in resp.aiter_raw():
            leido += trozo
            if len(leido) >= MAX_LECTURA_RESPUESTA:
                break
    except httpx.StreamConsumed:  # respuesta ya leída (transportes en memoria)
        leido = resp.content[:MAX_LECTURA_RESPUESTA]
    return leido
