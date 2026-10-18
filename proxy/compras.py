"""Compras de la etapa 2 (docs/FLUJO.md): contexto, oportunidad y parámetros.

Una compra empieza con el primer `find_products` de un contexto, con el
Guard prendido y el agente de intención disponible. Guarda:

- el pedido y los **parámetros autorizados** (`ficha`): la ficha confirmada
  de la demo si el escenario la tiene o, si no, la que armó el agente con el
  pedido (`ficha_llm`), hasta que el usuario acepte otros;
- el **contexto** (el porqué que contó el usuario);
- la **oportunidad** que se le consultó y la decisión.

Las dos esperas (la respuesta del porqué y la decisión) son `Future`s que
resuelve la API de control; cada repregunta del intake abre otra espera con
`Compra.nueva_espera()`. Si llega otra búsqueda de la misma compra
mientras tanto, espera el mismo resultado (`contexto_listo`, `resuelta`).
El silencio no es autorización: si vence la decisión, la compra queda
`vencida` y no se entrega nada.

Todo vive en memoria, como las aprobaciones: un reinicio del proxy corta las
compras en curso (el agente del cliente recibe un error y no compra).

Gateway de producción: las compras que crea un integrador por `/v1`
(`origen="api"`, `RegistroCompras.crear_api`) además se pueden guardar en un
almacén (`proxy/estado.py`) con `Compra.a_fila()` y volver con
`Compra.desde_fila()`. Las esperas no sobreviven a un reinicio, así que
`desde_fila` las cierra del lado seguro: el porqué pendiente se da por
omitido (sigue el pedido literal) y la oportunidad pendiente queda `vencida`.
Este módulo no persiste nada por su cuenta: decide el núcleo.
"""

from __future__ import annotations

import asyncio
import copy
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, get_args

from pydantic import ValidationError

from contracts import Contexto, Ficha, Oportunidad, Requisito
from proxy.tipos_gateway import ID_COMPRA_RE, ErrorAlmacen, FotoCompra, es_id

EstadoCompra = Literal[
    "creada",              # compra de API: todavía no llegó el primer find_products
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
MAX_VEREDICTOS_POR_COMPRA = 20
MAX_CONTEXTOS_POR_COMPRA = 20
# Una compra de API en estos estados ya no acepta tráfico A2A ni cuenta como
# activa para el límite del integrador.
ESTADOS_TERMINALES_API = frozenset({"bloqueada", "vencida"})
ESTADOS_COMPRA: frozenset[str] = frozenset(get_args(EstadoCompra))


class CompraDesconocida(LookupError):
    pass


class EstadoInvalido(RuntimeError):
    def __init__(self, estado: str) -> None:
        super().__init__(f"La compra está en estado {estado}")
        self.estado = estado


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


_UN_MICRO = timedelta(microseconds=1)


class MarcasMonotonas:
    """Marcas de tiempo estrictamente crecientes en el proceso, para `creada`
    y `actualizada`: el integrador ordena los webhooks por `actualizada` y en
    Windows dos `datetime.now()` seguidos suelen dar el mismo instante. Cada
    marca es `max(ahora, última + 1 µs)` (y, si se pasa, después de `minimo`)."""

    def __init__(self) -> None:
        self._ultima: datetime | None = None
        self._candado = threading.Lock()

    def siguiente(self, minimo: datetime | None = None) -> datetime:
        with self._candado:
            marca = _ahora()
            for previa in (self._ultima, minimo):
                if previa is not None and marca <= previa:
                    marca = previa + _UN_MICRO
            self._ultima = marca
            return marca


_marcas = MarcasMonotonas()


def _marca(minimo: datetime | None = None) -> datetime:
    return _marcas.siguiente(minimo)


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
    # -- gateway de producción (compras de integrador por /v1) ---------------
    integrador_id: str | None = None
    comercio_id: str | None = None
    referencia: str | None = None        # del integrador, se devuelve tal cual
    origen: Literal["agente", "api"] = "agente"
    preguntar_porque: bool = False
    porque: str | None = None            # el porqué que mandó el integrador
    intencion_activa: bool = False
    actualizada: datetime | None = None
    veredictos: list[dict[str, Any]] = field(default_factory=list)  # vista_veredicto
    aprobacion_pendiente: dict[str, Any] | None = None

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
            "origen": self.origen,
            "integrador_id": self.integrador_id,
            "comercio_id": self.comercio_id,
        }

    # -- gateway: estado persistente y foto para /v1 --------------------------

    def tocar(self) -> None:
        """Marca un cambio (la API y los webhooks ordenan por `actualizada`):
        siempre después de la marca anterior, también la de una fila que
        escribió otro proceso con otro reloj."""
        self.actualizada = _marca(self.actualizada)

    def registrar_veredicto(self, vista: dict[str, Any]) -> None:
        """Agrega un veredicto en vista reducida (`tipos_gateway.vista_veredicto`:
        sin texto del comercio). Se guardan los últimos
        `MAX_VEREDICTOS_POR_COMPRA`: un agente que reintenta sin fin no hace
        crecer la fila."""
        self.veredictos.append(vista)
        del self.veredictos[:-MAX_VEREDICTOS_POR_COMPRA]
        self.tocar()

    def a_fila(self) -> dict[str, Any]:
        """La fila de `gw_compras` (proxy/sql/003_gateway.sql): las columnas por
        las que se filtra van aparte y el resto, en `datos` (jsonb). Los
        `Future` y los `Event` no se guardan: son del proceso."""

        def _json(modelo: Any) -> Any:
            return modelo.model_dump(mode="json") if modelo is not None else None

        return {
            "id": self.id,
            "integrador_id": self.integrador_id,
            "comercio_id": self.comercio_id,
            "referencia": self.referencia,
            "estado": self.estado,
            "creada": self.creada.isoformat(),
            "actualizada": (self.actualizada or self.creada).isoformat(),
            "datos": {
                "pedido": self.pedido,
                "pregunta": self.pregunta,
                "origen": self.origen,
                "preguntar_porque": self.preguntar_porque,
                "porque": self.porque,
                "intencion_activa": self.intencion_activa,
                "ficha_llm": _json(self.ficha_llm),
                "ficha": _json(self.ficha),
                "ficha_resuelta": self.ficha_resuelta,
                "contexto_id": self.contexto_id,
                "contexto": _json(self.contexto),
                "oportunidad": _json(self.oportunidad),
                "vence": self.vence.isoformat() if self.vence else None,
                "decision": self.decision,
                "requisitos_autorizados": [
                    r.model_dump(mode="json") for r in self.requisitos_autorizados
                ]
                if self.requisitos_autorizados is not None
                else None,
                "veredictos": copy.deepcopy(self.veredictos),
                "aprobacion_pendiente": copy.deepcopy(self.aprobacion_pendiente),
            },
        }

    @classmethod
    def desde_fila(cls, fila: dict[str, Any]) -> Compra:
        """La compra de una fila de `gw_compras`, después de un reinicio.

        Requiere un loop corriendo (crea los `Future`). Nadie quedó esperando
        del otro lado del reinicio, así que las esperas se cierran del lado
        seguro (diseño §1.7):

        - `esperando_contexto` → `negociando`, sin contexto ni pregunta: si el
          usuario no contesta el porqué, se sigue el pedido literal y sin
          oportunidades (docs/FLUJO.md);
        - `esperando_decision` → `vencida`: el silencio no es autorización y
          la respuesta retenida ya no existe;
        - `contexto_listo` y `resuelta` quedan puestos, para que ninguna
          búsqueda espere a alguien que no va a llegar. La excepción es
          `creada`: la etapa de contexto todavía no corrió y la va a correr la
          primera búsqueda (como en una compra recién creada).

        Una fila que no valida (corrupta o de otra versión) da `ErrorAlmacen`:
        el núcleo la trata como estado no disponible (503), nunca como una
        compra vacía.
        """
        try:
            return cls._desde_fila(fila)
        except ErrorAlmacen:
            raise
        except (KeyError, TypeError, ValueError, ValidationError) as e:
            # Sin el contenido de la fila en el mensaje: puede traer texto del pedido.
            raise ErrorAlmacen(f"fila de compra inválida ({type(e).__name__})") from None

    @classmethod
    def _desde_fila(cls, fila: dict[str, Any]) -> Compra:
        if not isinstance(fila, dict):
            raise TypeError("la fila no es un objeto")
        datos = fila["datos"]
        if not isinstance(datos, dict):
            raise TypeError("datos no es un objeto")
        compra_id = fila["id"]
        if not es_id(ID_COMPRA_RE, compra_id):
            raise ValueError("id de compra inválido")
        estado = fila["estado"]
        if estado not in ESTADOS_COMPRA:
            raise ValueError("estado desconocido")
        origen = datos.get("origen", "agente")
        if origen not in ("agente", "api"):
            raise ValueError("origen desconocido")
        decision = datos.get("decision")
        if decision not in (None, "aceptada", "rechazada", "vencida"):
            raise ValueError("decisión desconocida")
        pedido = datos["pedido"]
        if not isinstance(pedido, str):
            raise TypeError("pedido no es texto")
        veredictos = datos.get("veredictos") or []
        if not isinstance(veredictos, list) or not all(isinstance(v, dict) for v in veredictos):
            raise TypeError("veredictos inválidos")
        pendiente = datos.get("aprobacion_pendiente")
        if pendiente is not None and not isinstance(pendiente, dict):
            raise TypeError("aprobación pendiente inválida")
        requisitos = datos.get("requisitos_autorizados")
        if requisitos is not None and not isinstance(requisitos, list):
            raise TypeError("requisitos autorizados inválidos")

        loop = asyncio.get_running_loop()
        compra = cls(
            id=compra_id,
            creada=_fecha(fila["creada"]),
            pedido=pedido,
            pregunta=_texto_o_none(datos.get("pregunta")) or "",
            ficha_llm=_modelo(Ficha, datos.get("ficha_llm")),
            ficha=_modelo(Ficha, datos.get("ficha")),
            contexto_id=_texto_o_none(datos.get("contexto_id")),
            estado=estado,
            contexto=_modelo(Contexto, datos.get("contexto")),
            oportunidad=_modelo(Oportunidad, datos.get("oportunidad")),
            vence=_fecha(datos["vence"]) if datos.get("vence") else None,
            decision=decision,
            requisitos_autorizados=[Requisito.model_validate(r) for r in requisitos]
            if requisitos is not None
            else None,
            ficha_resuelta=bool(datos.get("ficha_resuelta", False)),
            _respuesta=loop.create_future(),
            _decision=loop.create_future(),
            integrador_id=_texto_o_none(fila.get("integrador_id")),
            comercio_id=_texto_o_none(fila.get("comercio_id")),
            referencia=_texto_o_none(fila.get("referencia")),
            origen=origen,
            preguntar_porque=bool(datos.get("preguntar_porque", False)),
            porque=_texto_o_none(datos.get("porque")),
            intencion_activa=bool(datos.get("intencion_activa", False)),
            actualizada=_fecha(fila["actualizada"]) if fila.get("actualizada") else None,
            veredictos=copy.deepcopy(veredictos[-MAX_VEREDICTOS_POR_COMPRA:]),
            aprobacion_pendiente=copy.deepcopy(pendiente),
        )
        # Las esperas no cruzan un reinicio (fail-closed).
        if compra.estado == "esperando_contexto":
            compra.estado = "negociando"
            compra.contexto = None
            compra.pregunta = ""
            compra.vence = None
        elif compra.estado == "esperando_decision":
            compra.estado = compra.decision = "vencida"
            compra.vence = None
        else:
            compra.vence = None
        if compra.estado != "creada":
            compra.contexto_listo.set()
        compra.resuelta.set()
        return compra

    def foto(
        self, *, contextos: list[str], aprobaciones: list[dict[str, Any]]
    ) -> FotoCompra:
        """La foto inmutable que consumen la API `/v1` y los webhooks.

        `ficha` es la autorizada (la resuelta); antes de resolverla no hay
        ficha autorizada todavía, así que va None y no la del LLM. La
        pregunta solo sale mientras se la está esperando."""
        esperando = self.estado == "esperando_contexto"
        return FotoCompra(
            compra_id=self.id,
            integrador_id=self.integrador_id,
            comercio_id=self.comercio_id,
            referencia=self.referencia,
            estado=self.estado,
            creada=self.creada,
            actualizada=self.actualizada or self.creada,
            pedido=self.pedido,
            contextos=tuple(contextos),
            ficha=self.ficha if self.ficha_resuelta else None,
            intencion_activa=self.intencion_activa,
            pregunta=self.pregunta if esperando else None,
            vence=self.vence,
            oportunidad=self.oportunidad,
            decision=self.decision,
            aprobacion_pendiente=copy.deepcopy(self.aprobacion_pendiente),
            veredictos=tuple(copy.deepcopy(self.veredictos)),
            aprobaciones=tuple(copy.deepcopy(aprobaciones)),
        )

    # -- esperas -------------------------------------------------------------

    def fijar_plazo(self, segundos: float) -> datetime:
        """El vencimiento de la espera que sigue (va en el evento al dashboard)."""
        self.vence = _ahora() + timedelta(seconds=max(0.0, segundos))
        return self.vence

    def restante(self) -> float:
        if self.vence is None:
            return 0.0
        return max(0.0, (self.vence - _ahora()).total_seconds())

    def nueva_espera(self, pregunta: str) -> None:
        """Otro turno (una repregunta del intake): la compra vuelve a esperar
        una respuesta a `pregunta`. Una espera anterior que quedó sin
        resolver (venció) se cancela: nadie la va a resolver."""
        if not self._respuesta.done():
            self._respuesta.cancel()
        self._respuesta = asyncio.get_running_loop().create_future()
        self.pregunta = pregunta
        self.estado = "esperando_contexto"

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


def _fecha(valor: Any) -> datetime:
    """Una fecha ISO 8601 de la fila. PostgREST devuelve `timestamptz` con
    zona; una fecha sin zona se toma como UTC (así se compara con `_ahora`)."""
    if not isinstance(valor, str):
        raise TypeError("fecha no es texto")
    fecha = datetime.fromisoformat(valor)
    return fecha if fecha.tzinfo is not None else fecha.replace(tzinfo=timezone.utc)


def _texto_o_none(valor: Any) -> str | None:
    if valor is None:
        return None
    if not isinstance(valor, str):
        raise TypeError("se esperaba texto")
    return valor


def _modelo(tipo: Any, valor: Any) -> Any:
    return tipo.model_validate(valor) if valor is not None else None


class RegistroCompras:
    def __init__(self, maximo: int = MAX_COMPRAS, ttl_s: float | None = None) -> None:
        self._compras: OrderedDict[str, Compra] = OrderedDict()
        self._por_contexto: dict[str, str] = {}
        self._maximo = maximo
        # Vida de una compra de integrador (GATEWAY_COMPRA_TTL_S): pasada,
        # el tráfico A2A ya no la acepta y se puede sacar de memoria.
        self._ttl_s = ttl_s

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
            creada=_marca(),
            pedido=pedido,
            pregunta=pregunta,
            ficha_llm=ficha_llm,
            _respuesta=loop.create_future(),
            _decision=loop.create_future(),
        )
        self._compras[compra.id] = compra
        if contexto_id:
            self.atar(compra, contexto_id)
        self._recortar()
        return compra

    def _recortar(self) -> None:
        """Acota la memoria sin sacar nunca una compra de integrador viva.

        - Las de la demo (`origen="agente"`): las más viejas, cuando pasan de
          `maximo` (como siempre).
        - Las de integrador: solo las que ya no pueden recibir tráfico
          (bloqueadas, vencidas o pasadas de su TTL) y sin una espera abierta.
          Una viva no sale nunca: con ella se irían las ataduras
          contexto → compra, que son las que dicen que es de un integrador
          (el dashboard no la puede aprobar) y qué ficha autorizó, y su
          espera se cerraría al hidratarla de nuevo (`desde_fila`). Cuántas
          hay lo acota el límite de compras activas de cada integrador.
        """
        demo = [cid for cid, c in self._compras.items() if c.origen != "api"]
        for compra_id in demo[: max(0, len(demo) - self._maximo)]:
            self._sacar(compra_id)
        api = [c for c in self._compras.values() if c.origen == "api"]
        sobran = len(api) - self._maximo
        for compra in api:
            if sobran <= 0:
                break
            if self._terminada(compra):
                self._sacar(compra.id)
                sobran -= 1

    def _terminada(self, compra: Compra) -> bool:
        """Una compra de integrador que ya no recibe tráfico A2A ni espera
        nada del integrador (se puede sacar de memoria)."""
        if compra.estado in ("esperando_contexto", "esperando_decision"):
            return False
        if compra.estado in ESTADOS_TERMINALES_API:
            return True
        if self._ttl_s is None:
            return False
        return (_ahora() - compra.creada).total_seconds() > self._ttl_s

    def _sacar(self, compra_id: str) -> None:
        self._compras.pop(compra_id, None)
        for ctx in [c for c, cid in self._por_contexto.items() if cid == compra_id]:
            del self._por_contexto[ctx]

    # -- gateway: compras de integrador ----------------------------------------

    def crear_api(
        self,
        *,
        integrador_id: str,
        comercio_id: str,
        pedido: str,
        ficha: Ficha,
        referencia: str | None,
        porque: str | None,
        preguntar_porque: bool,
        intencion_activa: bool,
    ) -> Compra:
        """Una compra que crea un integrador por `POST /v1/compras`.

        La ficha la autorizó el integrador y queda resuelta desde el inicio:
        `resolver_ficha` (la ficha de la demo) no la pisa, y el tráfico del
        agente no la cambia. Solo la cambia el usuario al aceptar una
        oportunidad. Empieza en `creada`, sin contexto: el `contextId` lo ata
        el primer pedido A2A que trae su `X-Compra-Id`. Requiere un loop.
        """
        loop = asyncio.get_running_loop()
        ahora = _marca()
        compra = Compra(
            id=f"cmp-{uuid.uuid4().hex[:16]}",
            creada=ahora,
            pedido=pedido,
            pregunta="",
            ficha_llm=None,
            ficha=ficha,
            estado="creada",
            ficha_resuelta=True,
            _respuesta=loop.create_future(),
            _decision=loop.create_future(),
            integrador_id=integrador_id,
            comercio_id=comercio_id,
            referencia=referencia,
            origen="api",
            preguntar_porque=preguntar_porque,
            porque=porque,
            intencion_activa=intencion_activa,
            actualizada=ahora,
        )
        self._compras[compra.id] = compra
        self._recortar()
        return compra

    def restaurar(self, compra: Compra) -> Compra:
        """Vuelve a poner en memoria una compra hidratada del almacén.

        Si ya está en memoria, gana la de memoria (tiene los `Future` vivos y
        puede estar más al día que la fila, que se escribe en segundo plano).
        Su `contexto_id` se ata solo si nadie lo tiene: las demás ataduras
        las hidrata el núcleo desde el almacén, contexto por contexto."""
        actual = self._compras.get(compra.id)
        if actual is not None:
            return actual
        self._compras[compra.id] = compra
        if compra.contexto_id and compra.contexto_id not in self._por_contexto:
            self._por_contexto[compra.contexto_id] = compra.id
        self._recortar()
        return compra

    def quitar(self, compra_id: str) -> Compra | None:
        """Saca una compra y sus ataduras (rollback: el almacén no la pudo
        guardar al crearla, así que no existe para nadie)."""
        compra = self._compras.get(compra_id)
        self._sacar(compra_id)
        return compra

    def contextos_de(self, compra_id: str) -> list[str]:
        """Los `contextId` atados en memoria a esa compra (orden de atadura)."""
        return [c for c, cid in self._por_contexto.items() if cid == compra_id]

    def de_integrador(self, integrador_id: str) -> list[Compra]:
        """Las compras de ese integrador que están en memoria, la más reciente
        primero (por fecha de creación: una restaurada no se adelanta)."""
        propias = [c for c in self._compras.values() if c.integrador_id == integrador_id]
        return sorted(propias, key=lambda c: c.creada, reverse=True)

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
