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

Con varias instancias (`Config.instancias="varias"`) la espera de una
compra de integrador vive en el almacén (`proxy/esperas.py`) y la retiene
otra instancia: `desde_fila(fila, viva_hasta=...)` no la cierra mientras
siga viva, y la fila lleva el `espera_id` (`a_fila(espera=True)`).
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
from proxy.tipos_gateway import ID_COMPRA_RE, ID_ESPERA_RE, ErrorAlmacen, FotoCompra, es_id

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
# Una compra en estos estados tiene una espera abierta (el porqué o la decisión).
ESTADOS_ESPERANDO = frozenset({"esperando_contexto", "esperando_decision"})


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
    marca es `max(ahora, última + 1 µs)` (y, si se pasa, después de `minimo`).

    `minimo` es la marca previa de una compra, que puede venir de una fila
    que escribió otro proceso con otro reloj: vale solo para la marca que se
    devuelve y no mueve la del proceso (si no, una fila adelantada dejaba en
    el futuro `creada` y `actualizada` de todas las compras nuevas)."""

    def __init__(self) -> None:
        self._ultima: datetime | None = None
        self._candado = threading.Lock()

    def siguiente(self, minimo: datetime | None = None) -> datetime:
        with self._candado:
            marca = _ahora()
            if self._ultima is not None and marca <= self._ultima:
                marca = self._ultima + _UN_MICRO
            self._ultima = marca
        if minimo is not None and marca <= minimo:
            marca = minimo + _UN_MICRO  # solo para esa compra
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
    ficha_llm: Ficha | None              # la del usuario: la que armó el intake (o el agente)
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
    # -- varias instancias (proxy/esperas.py) ---------------------------------
    # `espera_id`: la espera abierta de la compra (`gw_esperas`), la que va en
    # la fila con `a_fila(espera=True)`. `espera_guardada`: la que esta
    # instancia sabe que tiene la fila (la condición de la próxima escritura).
    espera_id: str | None = None
    espera_guardada: str | None = None
    # Las dos fotos de la fila para escribir con varias instancias sin pisar
    # cambios de otras (`ProxyGuard._escribir_fusionada`), las dos en el
    # formato de `a_fila(espera=True)`: `fila_base`, cómo era esta compra en
    # memoria la última vez que se sincronizó con la base (lo que cambió
    # desde ahí es de este pedido), y `fila_db`, la última fila de la base
    # que se conoce (la condición de la próxima escritura).
    fila_base: dict[str, Any] | None = field(default=None, repr=False, compare=False)
    fila_db: dict[str, Any] | None = field(default=None, repr=False, compare=False)

    @property
    def requisitos(self) -> list[Requisito]:
        """Los parámetros vigentes: los resueltos o, antes, los del pedido."""
        ficha = self.ficha if self.ficha_resuelta else self.ficha_llm
        return list(ficha.requisitos) if ficha else []

    def resolver_ficha(self, confirmada: Ficha | None) -> Ficha | None:
        """Una sola vez, los parámetros autorizados de la compra: los del
        usuario (`ficha_llm`, lo que confirmó en el intake o, sin intake, lo
        que armó el agente sin confirmar, con lo que el Guard escala).

        `confirmada` es la ficha de un escenario fijo de la demo: quien llama
        solo la pasa con `PROXY_FICHA_DEMO=1` (grabaciones, benchmark por el
        proxy) y entonces manda sobre la del usuario. Por defecto (etapa 3)
        llega None y manda la del usuario. Después de aceptar una oportunidad
        no se toca."""
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

    def a_fila(self, *, espera: bool = False) -> dict[str, Any]:
        """La fila de `gw_compras` (proxy/sql/003_gateway.sql): las columnas por
        las que se filtra van aparte y el resto, en `datos` (jsonb). Los
        `Future` y los `Event` no se guardan: son del proceso.

        `espera=True` (varias instancias) suma la columna `espera_id` de
        004_serverless.sql, también en None (así se borra la de la fila). Sin
        eso la fila es la de siempre: con una sola instancia la base puede
        tener solo 003, sin esa columna."""

        def _json(modelo: Any) -> Any:
            return modelo.model_dump(mode="json") if modelo is not None else None

        fila: dict[str, Any] = {
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
        if espera:
            fila["espera_id"] = self.espera_id
        return fila

    @classmethod
    def desde_fila(
        cls,
        fila: dict[str, Any],
        *,
        viva_hasta: datetime | None = None,
        ahora: datetime | None = None,
    ) -> Compra:
        """La compra de una fila de `gw_compras`, después de un reinicio.

        `viva_hasta` (varias instancias): hasta cuándo sigue viva la espera
        de la fila (su vencimiento más la gracia, `proxy/esperas.py`). Si la
        compra está esperando y todavía no llegó ese instante, la espera la
        retiene otra instancia: se lee tal cual (estado, pregunta u
        oportunidad, `vence` y `espera_id`) y sin poner `resuelta`. Sin
        `viva_hasta` (una sola instancia), lo de siempre:

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
        compra = cls._leer(fila)
        # La fila tal cual (antes de cerrar nada): lo que tiene la base.
        compra.fila_base = compra.fila_db = compra.a_fila(espera=True)
        viva = (
            viva_hasta is not None
            and compra.estado in ESTADOS_ESPERANDO
            and (ahora or _ahora()) < viva_hasta
        )
        if viva:
            # La retiene otra instancia: nada se cierra. El porqué ya pasó si
            # se está esperando la decisión.
            if compra.estado == "esperando_decision":
                compra.contexto_listo.set()
            return compra
        compra.cerrar_espera_al_leer()
        return compra

    @classmethod
    def fila_normalizada(cls, fila: dict[str, Any]) -> dict[str, Any]:
        """Una fila de la base en el formato de `a_fila(espera=True)` (las
        fechas y los modelos como los escribe este módulo), sin cerrar nada:
        para compararla campo a campo con la de memoria. `ErrorAlmacen` si no
        valida."""
        return cls._leer(fila).a_fila(espera=True)

    def sincronizar(self, base: dict[str, Any], db: dict[str, Any]) -> None:
        """Después de escribir (o de ver que no hacía falta): `base` es cómo
        estaba esta compra en memoria y `db`, la fila que quedó en la base."""
        self.fila_base, self.fila_db = base, db
        self.espera_guardada = db.get("espera_id")

    @classmethod
    def _leer(cls, fila: dict[str, Any]) -> Compra:
        try:
            return cls._desde_fila(fila)
        except ErrorAlmacen:
            raise
        except (KeyError, TypeError, ValueError, ValidationError) as e:
            # Sin el contenido de la fila en el mensaje: puede traer texto del pedido.
            raise ErrorAlmacen(f"fila de compra inválida ({type(e).__name__})") from None

    def cerrar_espera_al_leer(self) -> None:
        """Cierra del lado seguro la espera de una compra que nadie retiene
        (un reinicio con una sola instancia; con varias, una huérfana).

        Deja `espera_guardada` como estaba: la próxima escritura se
        condiciona a lo que tiene la fila y borra el `espera_id`."""
        if self.estado == "esperando_contexto":
            self.cerrar_porque()
        elif self.estado == "esperando_decision":
            self.estado = self.decision = "vencida"
            self.vence = None
        else:
            self.vence = None
        self.espera_id = None
        if self.estado != "creada":
            self.contexto_listo.set()
        self.resuelta.set()

    def cerrar_porque(self) -> None:
        """El porqué que nadie contestó (o que no se va a procesar): sigue el
        pedido literal, sin contexto ni pregunta (docs/FLUJO.md)."""
        self.estado = "negociando"
        self.contexto = None
        self.pregunta = ""
        self.vence = None

    def aplicar_decision(self, decision: Decision | None) -> None:
        """La decisión del usuario sobre la oportunidad consultada.

        - `aceptar`: los parámetros autorizados pasan a ser los propuestos.
          Los que cambian salen de la oferta del comercio: van marcados
          `aceptado` para que la vista de /v1 y los webhooks los filtren
          (`vistas._ficha_publica`); los que no cambian siguen siendo del
          pedido;
        - `rechazar`: siguen los parámetros originales;
        - None (venció, o no se pudo esperar): `vencida`. El silencio no es
          autorización."""
        oportunidad = self.oportunidad
        if decision == "aceptar" and oportunidad is not None:
            originales = list(self.ficha.requisitos) if self.ficha else []
            self.requisitos_autorizados = [
                r if r in originales else r.model_copy(update={"origen": "aceptado"})
                for r in oportunidad.requisitos_propuestos
            ]
            self.ficha = Ficha(
                pedido_original=self.pedido,
                requisitos=self.requisitos_autorizados,
                confirmada=True,
                categoria_taxonomia=self.ficha.categoria_taxonomia if self.ficha else None,
            )
            self.estado = self.decision = "aceptada"
        elif decision == "rechazar":
            self.estado = self.decision = "rechazada"
        else:
            self.estado = self.decision = "vencida"

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
        espera_id = fila.get("espera_id")  # 004: no está en una fila de solo 003
        if espera_id is not None and not es_id(ID_ESPERA_RE, espera_id):
            raise ValueError("espera_id inválido")

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
            espera_id=espera_id,
            espera_guardada=espera_id,
        )
        # Las esperas no cruzan un reinicio (fail-closed): las cierra
        # `desde_fila`, salvo que siga viva en otra instancia.
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

    def responder_en_memoria(self, respuesta: str | None) -> None:
        """Resuelve el `Future` del porqué (una sola instancia). None = omitir."""
        if self.estado != "esperando_contexto" or self._respuesta.done():
            raise EstadoInvalido(self.estado)
        self._respuesta.set_result(respuesta)
        self.estado = "negociando"

    def decidir_en_memoria(self, decision: Decision) -> None:
        """Resuelve el `Future` de la decisión (una sola instancia)."""
        if self.estado != "esperando_decision" or self._decision.done():
            raise EstadoInvalido(self.estado)
        self._decision.set_result(decision)


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


# -- fusión de filas (varias instancias) ---------------------------------------

_CONFLICTO = object()
# Las columnas que pueden cambiar (el resto, como el id o `creada`, no) y
# `actualizada`, que la pone quien escribe.
_COLUMNAS_FUSION = ("estado", "espera_id", "referencia")


def _fusionar_valor(base: Any, nuestro: Any, suyo: Any) -> Any:
    if nuestro == base:
        return suyo  # este pedido no lo cambió: lo de la base
    if suyo == base or suyo == nuestro:
        return nuestro  # solo lo cambió este pedido
    return _CONFLICTO  # lo cambiaron los dos, distinto


def _fusionar_veredictos(base: Any, nuestros: Any, suyos: Any) -> list[Any]:
    """Los veredictos se agregan: los de la base más los nuevos de este pedido."""
    base, nuestros, suyos = base or [], nuestros or [], suyos or []
    nuevos = [v for v in nuestros if v not in base and v not in suyos]
    return [*suyos, *nuevos][-MAX_VEREDICTOS_POR_COMPRA:]


def fusionar_filas(
    base: dict[str, Any], nuestra: dict[str, Any], suya: dict[str, Any]
) -> dict[str, Any] | None:
    """La fila a escribir con varias instancias: la de la base (`suya`) con lo
    que cambió este pedido (de `base` a `nuestra`), campo a campo.

    None si los dos cambiaron el mismo campo de forma distinta (otra
    instancia ya movió la compra: decidió, se bloqueó, cerró una espera): el
    cambio de este pedido ya no aplica y no se pisa lo de la base. Los
    veredictos no chocan: se juntan. Las tres filas, en el formato de
    `Compra.a_fila(espera=True)`; `actualizada` y `creada` quedan las de la
    base (quien escribe pone su marca)."""
    resultado = copy.deepcopy(suya)
    for columna in _COLUMNAS_FUSION:
        valor = _fusionar_valor(base.get(columna), nuestra.get(columna), suya.get(columna))
        if valor is _CONFLICTO:
            return None
        resultado[columna] = copy.deepcopy(valor)
    datos_base, datos_nuestros, datos_suyos = base["datos"], nuestra["datos"], suya["datos"]
    datos = resultado["datos"]
    for clave in {*datos_base, *datos_nuestros, *datos_suyos}:
        b, n, s = datos_base.get(clave), datos_nuestros.get(clave), datos_suyos.get(clave)
        if clave == "veredictos":
            datos[clave] = copy.deepcopy(_fusionar_veredictos(b, n, s))
            continue
        valor = _fusionar_valor(b, n, s)
        if valor is _CONFLICTO:
            return None
        datos[clave] = copy.deepcopy(valor)
    return resultado


def misma_fila(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """¿Las dos filas tienen lo mismo, sin mirar `actualizada`?"""
    return {**a, "actualizada": None} == {**b, "actualizada": None}


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
        return self.reemplazar(compra)

    def reemplazar(self, compra: Compra) -> Compra:
        """Pone en memoria la compra recién leída del almacén, aunque ya haya
        otra con ese id (varias instancias: la fila manda, salvo que esta
        instancia retenga su espera, y eso lo decide el núcleo antes).

        Las ataduras son por id, así que las de memoria siguen valiendo. Un
        pedido en curso que tenga la anterior la sigue usando: sus
        escrituras van con su propia copia."""
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
        compra.responder_en_memoria(respuesta)
        return compra

    def decidir(self, compra_id: str, decision: Decision) -> Compra:
        compra = self._buscar(compra_id)
        compra.decidir_en_memoria(decision)
        return compra

    def _buscar(self, compra_id: str) -> Compra:
        compra = self._compras.get(compra_id)
        if compra is None:
            raise CompraDesconocida(compra_id)
        return compra
