"""Estado persistente del gateway de producción (diseño §1.7 y §1.10).

Guarda solo lo de las **compras de integrador** (`origen="api"`): la compra,
las ataduras `contextId` → compra, las ofertas de sus contextos, el último
veredicto por (contexto, cart), sus aprobaciones y las entregas de webhooks.
El tráfico anónimo de la demo no pasa por acá y el registro de veredictos
de `proxy/persistencia.py` no cambia.

Dos almacenes con la misma interfaz (`tipos_gateway.AlmacenEstado`):

- `EstadoMemoria`: el default. Con `durable=False` equivale a hoy (nada
  sobrevive a un reinicio). En los tests, con `durable=True` y la misma
  instancia en dos `create_app`, simula un reinicio.
- `EstadoSupabase`: PostgREST con httpx y timeout corto. Solo con
  `GATEWAY_ESTADO=supabase` explícito: las credenciales `SUPABASE_*` solas no
  lo activan, así la demo (que las tiene en `.env`) no escribe en tablas que
  todavía no existen. Las tablas están en `proxy/sql/003_gateway.sql`, que
  se aplica a mano.

Fail-closed: cualquier falla del almacén es `ErrorAlmacen` y quien llama
bloquea (503 o corte); nunca se toma una falla por "no hay nada". Lo que se
escribe en segundo plano va por `TareasEnFondo`, que nunca lanza: si una
escritura se pierde y hay un reinicio, falta el dato y el proxy bloquea.

Este módulo no decide cuándo persistir: lo decide el núcleo del proxy.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from typing import Any

import httpx

from proxy.aprobaciones import Aprobacion
from proxy.persistencia import cabeceras, credenciales, fila_aprobacion
from proxy.tipos_gateway import (
    ID_COMPRA_RE,
    ID_SEGURO_RE,
    ID_SLUG_RE,
    AlmacenEstado,
    EntregaWebhook,
    ErrorAlmacen,
    es_id,
)

logger = logging.getLogger("proxy.estado")

TIMEOUT_SEGUNDOS = 5.0
MAX_PENDIENTES_FONDO = 100
LIMITE_LISTADO_MAXIMO = 200

# Mismos valores que los `check` de 003_gateway.sql: un estado que la base
# rechazaría se rechaza antes (y en memoria también, para que los tests vean
# lo mismo que producción).
ESTADOS_COMPRA_SQL = frozenset(
    {
        "creada", "esperando_contexto", "negociando", "esperando_decision",
        "sin_oportunidad", "bloqueada", "aceptada", "rechazada", "vencida",
    }
)
ESTADOS_TERMINALES = ("bloqueada", "vencida")
STATUS_VEREDICTO_SQL = frozenset({"aprobado", "con_avisos", "escalar", "bloqueado"})

TABLA_COMPRAS = "gw_compras"
TABLA_CONTEXTOS = "gw_contextos"
TABLA_OFERTAS = "gw_ofertas"
TABLA_VEREDICTOS = "gw_ultimos_veredictos"
TABLA_APROBACIONES = "gw_aprobaciones"
TABLA_ENTREGAS = "gw_entregas_webhook"


# -- validación de ids (antes de cada operación, en los dos almacenes) --------


def _id(regex: Any, valor: Any, nombre: str) -> str:
    """El id si cumple el formato; si no, `ErrorAlmacen` y no se hace nada.

    Los ids van en filtros de PostgREST (`?id=eq.<id>`): un id con `&`, `,` o
    paréntesis podría cambiar el filtro. El mensaje no incluye el valor."""
    if not es_id(regex, valor):
        raise ErrorAlmacen(f"{nombre} con formato inválido")
    return valor


def _id_compra(valor: Any) -> str:
    return _id(ID_COMPRA_RE, valor, "compra_id")


def _id_seguro(valor: Any, nombre: str) -> str:
    return _id(ID_SEGURO_RE, valor, nombre)


def _id_slug(valor: Any, nombre: str) -> str:
    return _id(ID_SLUG_RE, valor, nombre)


def _limite(limite: Any) -> int:
    if not isinstance(limite, int) or isinstance(limite, bool) or limite < 1:
        raise ErrorAlmacen("límite inválido")
    return min(limite, LIMITE_LISTADO_MAXIMO)


def _estado_compra(estado: Any) -> str:
    if estado not in ESTADOS_COMPRA_SQL:
        raise ErrorAlmacen("estado de compra desconocido")
    return estado


def _iso(fecha: datetime) -> str:
    """ISO 8601 en UTC con `Z`: un `+00:00` en la query string se lee como
    espacio si alguien no lo codifica."""
    if fecha.tzinfo is None:
        fecha = fecha.replace(tzinfo=timezone.utc)
    return fecha.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _fecha(valor: Any) -> datetime:
    if isinstance(valor, datetime):
        fecha = valor
    elif isinstance(valor, str):
        try:
            fecha = datetime.fromisoformat(valor)
        except ValueError:
            raise ErrorAlmacen("fecha inválida") from None
    else:
        raise ErrorAlmacen("fecha inválida")
    return fecha if fecha.tzinfo is not None else fecha.replace(tzinfo=timezone.utc)


def _validar_fila_compra(fila: Any) -> dict[str, Any]:
    if not isinstance(fila, dict) or not isinstance(fila.get("datos"), dict):
        raise ErrorAlmacen("fila de compra inválida")
    _id_compra(fila.get("id"))
    _id_slug(fila.get("integrador_id"), "integrador_id")
    _id_slug(fila.get("comercio_id"), "comercio_id")
    _estado_compra(fila.get("estado"))
    _fecha(fila.get("creada"))
    _fecha(fila.get("actualizada"))
    return fila


# -- filas -------------------------------------------------------------------


def fila_ofertas(
    contexto_id: str,
    comercio_id: str | None,
    carts: list[dict],
    offer_details: list[dict],
    escenario: str | None,
    registradas: datetime,
) -> dict[str, Any]:
    """La fila de `gw_ofertas`: lo que vio el proxy en `find_products`, para
    evaluar el `create_checkout` aunque haya un reinicio en el medio."""
    return {
        "contexto_id": contexto_id,
        "comercio_id": comercio_id,
        "carts": copy.deepcopy(carts),
        "offer_details": copy.deepcopy(offer_details),
        "escenario": escenario,
        "registradas": registradas.isoformat(),
    }


def fila_ultimo_veredicto(
    contexto_id: str,
    cart_id: str,
    status: str,
    centavos: int | None,
    moneda: str | None,
    ts: datetime,
) -> dict[str, Any]:
    """La fila de `gw_ultimos_veredictos`: lo que necesita
    `RegistroAprobaciones.aprobar` (status y total firmado) después de un
    reinicio."""
    return {
        "contexto_id": contexto_id,
        "cart_id": cart_id,
        "status": status,
        "centavos": centavos,
        "moneda": moneda,
        "ts": ts.isoformat(),
    }


def fila_aprobacion_gw(
    aprobacion: Aprobacion, *, integrador_id: str | None, compra_id: str | None
) -> dict[str, Any]:
    """La fila de `gw_aprobaciones`: la de auditoría de `persistencia` más el
    dueño (integrador y compra)."""
    return {
        **fila_aprobacion(aprobacion),
        "integrador_id": integrador_id,
        "compra_id": compra_id,
    }


# -- tareas en segundo plano --------------------------------------------------


class TareasEnFondo:
    """Escrituras que no demoran la respuesta (actualizar la compra, las
    ofertas, el último veredicto y las entregas).

    Tope de tareas en vuelo: si el almacén se cuelga, no crece sin límite;
    lo que no entra se descarta y se avisa. `programar` nunca lanza: una
    falla se loguea y va a `al_fallar` (el proxy la convierte en un evento
    `error`)."""

    def __init__(
        self,
        *,
        max_pendientes: int = MAX_PENDIENTES_FONDO,
        al_fallar: Callable[[str], None] | None = None,
    ) -> None:
        self._max = max_pendientes
        self.al_fallar = al_fallar
        self._tareas: set[asyncio.Task[None]] = set()
        self.errores = 0
        self.descartadas = 0

    def _avisar(self, mensaje: str) -> None:
        logger.warning("%s", mensaje)
        if self.al_fallar is not None:
            try:
                self.al_fallar(mensaje)
            except Exception:  # noqa: BLE001 — el aviso nunca rompe al que escribe
                logger.exception("al_fallar lanzó")

    def programar(
        self, coro: Awaitable[None], descripcion: str, *, obligatoria: bool = False
    ) -> None:
        """`obligatoria`: no se descarta por el tope de tareas en vuelo (el
        paso de una compra a un estado terminal: si se perdiera, la compra
        cortada por seguridad reviviría después de un reinicio)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _cerrar_coro(coro)
            self.descartadas += 1
            self._avisar(f"Estado: no se guardó {descripcion} (sin loop)")
            return
        if len(self._tareas) >= self._max and not obligatoria:
            _cerrar_coro(coro)
            self.descartadas += 1
            self._avisar(f"Estado: no se guardó {descripcion} (demasiadas escrituras en vuelo)")
            return
        tarea = loop.create_task(self._correr(coro, descripcion))
        self._tareas.add(tarea)
        tarea.add_done_callback(self._tareas.discard)

    async def _correr(self, coro: Awaitable[None], descripcion: str) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except ErrorAlmacen as e:
            self.errores += 1
            self._avisar(f"Estado: no se guardó {descripcion} ({e})")
        except Exception as e:  # noqa: BLE001 — nunca lanza
            self.errores += 1
            logger.exception("Estado: error inesperado guardando %s", descripcion)
            self._avisar(f"Estado: no se guardó {descripcion} ({type(e).__name__})")

    @property
    def pendientes(self) -> int:
        return len(self._tareas)

    async def esperar(self) -> None:
        """Espera las escrituras en vuelo (tests y cierre)."""
        while self._tareas:
            await asyncio.gather(*list(self._tareas), return_exceptions=True)
            # Ceder una vuelta al loop: si todas ya terminaron, gather (3.12)
            # resuelve sin pasar por el loop y el `discard` del done callback
            # no llega a correr (el while no terminaba nunca).
            await asyncio.sleep(0)

    async def cerrar(self, timeout_s: float = 5.0) -> None:
        """Espera hasta `timeout_s` y cancela lo que quede."""
        if not self._tareas:
            return
        _, pendientes = await asyncio.wait(list(self._tareas), timeout=timeout_s)
        for tarea in pendientes:
            tarea.cancel()
        if pendientes:
            await asyncio.gather(*pendientes, return_exceptions=True)
            self._avisar(f"Estado: {len(pendientes)} escrituras canceladas al cerrar")


def _cerrar_coro(coro: Awaitable[None]) -> None:
    """Una corrutina que no se va a correr se cierra (sin el warning de
    "never awaited")."""
    cerrar = getattr(coro, "close", None)
    if callable(cerrar):
        cerrar()


# -- almacén en memoria ---------------------------------------------------------


class EstadoMemoria:
    """Almacén en memoria con la misma semántica que el de Supabase.

    Guarda y devuelve copias (`copy.deepcopy`): nadie muta el estado por
    referencia, igual que si viniera de la base. Valida los ids igual que
    `EstadoSupabase` y respeta la clave foránea de `gw_contextos`, para que
    un test en memoria vea los mismos errores que producción.
    """

    nombre = "memoria"

    def __init__(self, *, durable: bool = False, max_compras: int = 10_000) -> None:
        self.durable = durable
        self._max_compras = max_compras
        self._compras: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._contextos: dict[str, dict[str, Any]] = {}
        self._ofertas: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._veredictos: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        self._aprobaciones: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._entregas: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def _recortar(self, tabla: OrderedDict[Any, Any], maximo: int) -> None:
        while len(tabla) > maximo:
            tabla.popitem(last=False)

    async def guardar_compra(self, fila: dict[str, Any]) -> None:
        fila = _validar_fila_compra(fila)
        self._compras[fila["id"]] = copy.deepcopy(fila)
        while len(self._compras) > self._max_compras:
            vieja, _ = self._compras.popitem(last=False)
            # Como `on delete cascade`: las ataduras de una compra borrada se van.
            for ctx in [c for c, f in self._contextos.items() if f["compra_id"] == vieja]:
                del self._contextos[ctx]

    async def cargar_compra(self, compra_id: str) -> dict[str, Any] | None:
        fila = self._compras.get(_id_compra(compra_id))
        return copy.deepcopy(fila) if fila is not None else None

    async def listar_compras(
        self, integrador_id: str, *, limite: int = 50, estado: str | None = None
    ) -> list[dict[str, Any]]:
        _id_slug(integrador_id, "integrador_id")
        limite = _limite(limite)
        if estado is not None:
            _estado_compra(estado)
        filas = [
            f
            for f in self._compras.values()
            if f["integrador_id"] == integrador_id and (estado is None or f["estado"] == estado)
        ]
        filas.sort(key=lambda f: _fecha(f["creada"]), reverse=True)
        return copy.deepcopy(filas[:limite])

    async def contar_activas(self, integrador_id: str, desde: datetime) -> int:
        """Compras creadas desde `desde` (el TTL) que no están bloqueadas ni
        vencidas (diseño §1.3)."""
        _id_slug(integrador_id, "integrador_id")
        desde = _fecha(desde)
        return sum(
            1
            for f in self._compras.values()
            if f["integrador_id"] == integrador_id
            and _fecha(f["creada"]) >= desde
            and f["estado"] not in ESTADOS_TERMINALES
        )

    async def atar_contexto(self, contexto_id: str, compra_id: str, comercio_id: str) -> str:
        """Ata el contexto si nadie lo tiene; devuelve el dueño (gana el primero)."""
        _id_seguro(contexto_id, "contexto_id")
        _id_compra(compra_id)
        _id_slug(comercio_id, "comercio_id")
        actual = self._contextos.get(contexto_id)
        if actual is not None:
            return actual["compra_id"]
        if compra_id not in self._compras:
            # La clave foránea de gw_contextos: la compra tiene que estar guardada.
            raise ErrorAlmacen("la compra no está en el almacén")
        self._contextos[contexto_id] = {
            "contexto_id": contexto_id,
            "compra_id": compra_id,
            "comercio_id": comercio_id,
            "creado": _iso(datetime.now(timezone.utc)),
        }
        return compra_id

    async def compra_de_contexto(self, contexto_id: str) -> str | None:
        fila = self._contextos.get(_id_seguro(contexto_id, "contexto_id"))
        return fila["compra_id"] if fila is not None else None

    async def guardar_ofertas(self, fila: dict[str, Any]) -> None:
        contexto_id = _id_seguro(fila.get("contexto_id"), "contexto_id")
        self._ofertas[contexto_id] = copy.deepcopy(fila)
        self._ofertas.move_to_end(contexto_id)
        self._recortar(self._ofertas, self._max_compras * 4)

    async def cargar_ofertas(self, contexto_id: str) -> dict[str, Any] | None:
        fila = self._ofertas.get(_id_seguro(contexto_id, "contexto_id"))
        return copy.deepcopy(fila) if fila is not None else None

    async def guardar_ultimo_veredicto(self, fila: dict[str, Any]) -> None:
        clave = (
            _id_seguro(fila.get("contexto_id"), "contexto_id"),
            _id_seguro(fila.get("cart_id"), "cart_id"),
        )
        if fila.get("status") not in STATUS_VEREDICTO_SQL:
            raise ErrorAlmacen("status de veredicto desconocido")
        self._veredictos[clave] = copy.deepcopy(fila)
        self._veredictos.move_to_end(clave)
        self._recortar(self._veredictos, self._max_compras * 4)

    async def cargar_ultimo_veredicto(
        self, contexto_id: str, cart_id: str
    ) -> dict[str, Any] | None:
        clave = (_id_seguro(contexto_id, "contexto_id"), _id_seguro(cart_id, "cart_id"))
        fila = self._veredictos.get(clave)
        return copy.deepcopy(fila) if fila is not None else None

    async def guardar_aprobacion(self, fila: dict[str, Any]) -> None:
        aprobacion_id = _id_seguro(fila.get("id"), "aprobacion_id")
        _id_seguro(fila.get("contexto_id"), "contexto_id")
        _id_seguro(fila.get("cart_id"), "cart_id")
        if fila.get("estado") not in ("pendiente", "usada"):
            raise ErrorAlmacen("estado de aprobación desconocido")
        self._aprobaciones[aprobacion_id] = copy.deepcopy(fila)
        self._recortar(self._aprobaciones, self._max_compras * 4)

    async def cargar_aprobacion_pendiente(
        self, contexto_id: str, cart_id: str
    ) -> dict[str, Any] | None:
        _id_seguro(contexto_id, "contexto_id")
        _id_seguro(cart_id, "cart_id")
        pendientes = [
            f
            for f in self._aprobaciones.values()
            if f["contexto_id"] == contexto_id
            and f["cart_id"] == cart_id
            and f["estado"] == "pendiente"
        ]
        if not pendientes:
            return None
        return copy.deepcopy(max(pendientes, key=lambda f: _fecha(f["creada"])))

    async def reclamar_aprobacion(
        self, aprobacion_id: str, *, usada: str, task_id: str | None
    ) -> bool:
        """`pendiente` → `usada` una sola vez: True solo para quien la cambió."""
        fila = self._aprobaciones.get(_id_seguro(aprobacion_id, "aprobacion_id"))
        if task_id is not None:
            _id_seguro(task_id, "task_id")
        if fila is None or fila["estado"] != "pendiente":
            return False
        fila.update({"estado": "usada", "usada": usada, "task_id": task_id})
        return True

    async def guardar_entrega(self, entrega: EntregaWebhook) -> None:
        entrega_id = _id_seguro(entrega.id, "entrega_id")
        self._entregas[entrega_id] = entrega.a_json()
        self._entregas.move_to_end(entrega_id)
        self._recortar(self._entregas, self._max_compras)

    async def cerrar(self) -> None:
        return None


# -- almacén en Supabase (PostgREST) --------------------------------------------


class EstadoSupabase:
    """Almacén durable sobre PostgREST (tablas de `proxy/sql/003_gateway.sql`).

    Cada id que va en un filtro se valida antes de mandar el pedido. Toda
    falla (red, timeout, status ≥ 300, respuesta que no es lo esperado) es
    `ErrorAlmacen` con un mensaje sin la clave ni el cuerpo de la respuesta.
    Las dos operaciones que deciden algo son atómicas en la base: la
    atadura de un contexto (`ignore-duplicates`, gana el primero) y el
    reclamo de una aprobación (PATCH condicionado a `estado=eq.pendiente`).
    """

    nombre = "supabase"
    durable = True

    def __init__(
        self,
        url: str,
        clave: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = TIMEOUT_SEGUNDOS,
    ) -> None:
        self._rest = f"{url.rstrip('/')}/rest/v1"
        self._clave = clave
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(timeout_s),
            trust_env=False,
            follow_redirects=False,
        )

    def __repr__(self) -> str:  # sin la clave
        return f"EstadoSupabase({self._rest!r})"

    def _cabeceras(self, prefer: str | None) -> dict[str, str]:
        headers = cabeceras(self._clave, prefer or "")
        if not prefer:
            del headers["Prefer"]
        return headers

    async def _pedir(
        self,
        metodo: str,
        tabla: str,
        *,
        params: dict[str, str],
        prefer: str | None,
        cuerpo: Any = None,
    ) -> httpx.Response:
        try:
            respuesta = await self._client.request(
                metodo,
                f"{self._rest}/{tabla}",
                params=params,
                headers=self._cabeceras(prefer),
                json=cuerpo,
            )
        except httpx.HTTPError as e:
            raise ErrorAlmacen(f"{metodo} {tabla}: {type(e).__name__}") from None
        if respuesta.status_code >= 300:
            raise ErrorAlmacen(f"{metodo} {tabla}: HTTP {respuesta.status_code}")
        return respuesta

    async def _filas(self, tabla: str, params: dict[str, str]) -> list[dict[str, Any]]:
        respuesta = await self._pedir("GET", tabla, params=params, prefer=None)
        try:
            filas = respuesta.json()
        except ValueError:
            raise ErrorAlmacen(f"GET {tabla}: respuesta no es JSON") from None
        if not isinstance(filas, list) or not all(isinstance(f, dict) for f in filas):
            raise ErrorAlmacen(f"GET {tabla}: respuesta inesperada")
        return filas

    async def _una(self, tabla: str, params: dict[str, str]) -> dict[str, Any] | None:
        filas = await self._filas(tabla, params)
        return filas[0] if filas else None

    async def _upsert(self, tabla: str, conflicto: str, fila: dict[str, Any]) -> None:
        await self._pedir(
            "POST",
            tabla,
            params={"on_conflict": conflicto},
            prefer="resolution=merge-duplicates,return=minimal",
            cuerpo=fila,
        )

    # -- compras ----------------------------------------------------------------

    async def guardar_compra(self, fila: dict[str, Any]) -> None:
        await self._upsert(TABLA_COMPRAS, "id", _validar_fila_compra(fila))

    async def cargar_compra(self, compra_id: str) -> dict[str, Any] | None:
        return await self._una(
            TABLA_COMPRAS, {"id": f"eq.{_id_compra(compra_id)}", "select": "*"}
        )

    async def listar_compras(
        self, integrador_id: str, *, limite: int = 50, estado: str | None = None
    ) -> list[dict[str, Any]]:
        params = {
            "integrador_id": f"eq.{_id_slug(integrador_id, 'integrador_id')}",
            "order": "creada.desc",
            "limit": str(_limite(limite)),
            "select": "*",
        }
        if estado is not None:
            params["estado"] = f"eq.{_estado_compra(estado)}"
        return await self._filas(TABLA_COMPRAS, params)

    async def contar_activas(self, integrador_id: str, desde: datetime) -> int:
        """Cuenta con `Prefer: count=exact` y lee el total de `Content-Range`
        (`0-0/57` o `*/0`); con `limit=1` no baja todas las filas."""
        respuesta = await self._pedir(
            "GET",
            TABLA_COMPRAS,
            params={
                "integrador_id": f"eq.{_id_slug(integrador_id, 'integrador_id')}",
                "creada": f"gte.{_iso(_fecha(desde))}",
                "estado": f"not.in.({','.join(ESTADOS_TERMINALES)})",
                "select": "id",
                "limit": "1",
            },
            prefer="count=exact",
        )
        rango = respuesta.headers.get("content-range", "")
        _, _, total = rango.rpartition("/")
        if not total.isdigit():
            raise ErrorAlmacen("GET gw_compras: sin total en Content-Range")
        return int(total)

    # -- contextos ----------------------------------------------------------------

    async def atar_contexto(self, contexto_id: str, compra_id: str, comercio_id: str) -> str:
        """Inserta la atadura ignorando duplicados y lee el dueño: si otro la
        ató antes (en este proceso o en otro), devuelve ese."""
        contexto_id = _id_seguro(contexto_id, "contexto_id")
        fila = {
            "contexto_id": contexto_id,
            "compra_id": _id_compra(compra_id),
            "comercio_id": _id_slug(comercio_id, "comercio_id"),
        }
        await self._pedir(
            "POST",
            TABLA_CONTEXTOS,
            params={"on_conflict": "contexto_id"},
            prefer="resolution=ignore-duplicates,return=minimal",
            cuerpo=fila,
        )
        dueno = await self.compra_de_contexto(contexto_id)
        if dueno is None:
            raise ErrorAlmacen("POST gw_contextos: la atadura no quedó guardada")
        return dueno

    async def compra_de_contexto(self, contexto_id: str) -> str | None:
        fila = await self._una(
            TABLA_CONTEXTOS,
            {
                "contexto_id": f"eq.{_id_seguro(contexto_id, 'contexto_id')}",
                "select": "compra_id",
            },
        )
        if fila is None:
            return None
        return _id_compra(fila.get("compra_id"))

    # -- ofertas y último veredicto ---------------------------------------------------

    async def guardar_ofertas(self, fila: dict[str, Any]) -> None:
        _id_seguro(fila.get("contexto_id"), "contexto_id")
        await self._upsert(TABLA_OFERTAS, "contexto_id", fila)

    async def cargar_ofertas(self, contexto_id: str) -> dict[str, Any] | None:
        return await self._una(
            TABLA_OFERTAS,
            {"contexto_id": f"eq.{_id_seguro(contexto_id, 'contexto_id')}", "select": "*"},
        )

    async def guardar_ultimo_veredicto(self, fila: dict[str, Any]) -> None:
        _id_seguro(fila.get("contexto_id"), "contexto_id")
        _id_seguro(fila.get("cart_id"), "cart_id")
        if fila.get("status") not in STATUS_VEREDICTO_SQL:
            raise ErrorAlmacen("status de veredicto desconocido")
        await self._upsert(TABLA_VEREDICTOS, "contexto_id,cart_id", fila)

    async def cargar_ultimo_veredicto(
        self, contexto_id: str, cart_id: str
    ) -> dict[str, Any] | None:
        return await self._una(
            TABLA_VEREDICTOS,
            {
                "contexto_id": f"eq.{_id_seguro(contexto_id, 'contexto_id')}",
                "cart_id": f"eq.{_id_seguro(cart_id, 'cart_id')}",
                "select": "*",
            },
        )

    # -- aprobaciones -------------------------------------------------------------

    async def guardar_aprobacion(self, fila: dict[str, Any]) -> None:
        _id_seguro(fila.get("id"), "aprobacion_id")
        _id_seguro(fila.get("contexto_id"), "contexto_id")
        _id_seguro(fila.get("cart_id"), "cart_id")
        if fila.get("estado") not in ("pendiente", "usada"):
            raise ErrorAlmacen("estado de aprobación desconocido")
        await self._upsert(TABLA_APROBACIONES, "id", fila)

    async def cargar_aprobacion_pendiente(
        self, contexto_id: str, cart_id: str
    ) -> dict[str, Any] | None:
        return await self._una(
            TABLA_APROBACIONES,
            {
                "contexto_id": f"eq.{_id_seguro(contexto_id, 'contexto_id')}",
                "cart_id": f"eq.{_id_seguro(cart_id, 'cart_id')}",
                "estado": "eq.pendiente",
                "order": "creada.desc",
                "limit": "1",
                "select": "*",
            },
        )

    async def reclamar_aprobacion(
        self, aprobacion_id: str, *, usada: str, task_id: str | None
    ) -> bool:
        """PATCH condicionado a `estado=eq.pendiente`: la base hace el
        "un solo uso" atómico. True solo si cambió exactamente una fila."""
        params = {
            "id": f"eq.{_id_seguro(aprobacion_id, 'aprobacion_id')}",
            "estado": "eq.pendiente",
        }
        if task_id is not None:
            _id_seguro(task_id, "task_id")
        respuesta = await self._pedir(
            "PATCH",
            TABLA_APROBACIONES,
            params=params,
            prefer="return=representation",
            cuerpo={"estado": "usada", "usada": usada, "task_id": task_id},
        )
        try:
            filas = respuesta.json()
        except ValueError:
            raise ErrorAlmacen("PATCH gw_aprobaciones: respuesta no es JSON") from None
        if not isinstance(filas, list):
            raise ErrorAlmacen("PATCH gw_aprobaciones: respuesta inesperada")
        return len(filas) == 1

    # -- entregas de webhooks ------------------------------------------------------

    async def guardar_entrega(self, entrega: EntregaWebhook) -> None:
        _id_seguro(entrega.id, "entrega_id")
        await self._upsert(TABLA_ENTREGAS, "id", entrega.a_json())

    async def cerrar(self) -> None:
        await self._client.aclose()


# -- desde el entorno ---------------------------------------------------------------


def estado_desde_entorno(env: Mapping[str, str]) -> AlmacenEstado:
    """El almacén según `GATEWAY_ESTADO` (`memoria` por defecto o `supabase`).

    `supabase` se pide explícito y exige credenciales: sin ellas es un error
    de configuración (`ValueError`, el proxy no arranca), no una vuelta
    silenciosa a memoria, porque el operador pidió durabilidad. Un valor
    desconocido también es `ValueError`."""
    valor = (env.get("GATEWAY_ESTADO") or "memoria").strip().lower()
    if valor == "memoria":
        return EstadoMemoria()
    if valor == "supabase":
        datos = credenciales(env)
        if datos is None:
            raise ValueError(
                "GATEWAY_ESTADO=supabase exige SUPABASE_URL y "
                "SUPABASE_SERVICE_ROLE_KEY (o SUPABASE_SECRET_KEY)"
            )
        return EstadoSupabase(*datos)
    raise ValueError("GATEWAY_ESTADO tiene que ser 'memoria' o 'supabase'")
