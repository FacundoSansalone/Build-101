"""Agente de intención con juicios tipados (decisiones/: OpenAI Decisions o Jev).

Implementa el protocolo `intencion.agente.Agente` que usa el proxy, más
`evaluar_intake()` para repreguntar hasta tener todo (`intencion.intake`):

- `preguntar()`: corre el intake sobre el pedido. La pregunta es una
  plantilla (el porqué y, si falta algo, qué falta) y los requisitos son
  solo lo que el usuario dijo con confianza alta.
- `evaluar_intake()` y `armar_contexto()`: el intake y el contexto de un
  mismo turno salen de **una sola llamada** (preguntas independientes sobre
  el mismo estado). La necesidad son las palabras del usuario; el
  cronograma y las flexibilidades, juicios sobre candidatos que arma el
  código.
- `detectar_oportunidad()`: se delega en el detector existente
  (`AgenteGemini`), si hay; sin detector no hay oportunidades.
- `clasificar_taxonomia()`: la categoría de la taxonomía de Google para la
  ficha (`taxonomia.clasificar`, nivel por nivel con el mismo cliente). Es
  una propuesta: el Guard no depende de ella.

El modelo nunca escribe nada que lea el usuario ni ningún valor de la ficha:
elige entre opciones y el código decide con umbrales.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from contracts import Contexto, Entrega, Ficha, Flexibilidad, Oferta, Oportunidad, Requisito, Turno
from decisiones import ClienteDecisiones, Eleccion, Predicado, Pregunta, RespuestaPredicado
from intencion import intake
from intencion.agente import LARGO_NECESIDAD
from intencion.agente import Pregunta as PreguntaAgente
from intencion.intake import ResultadoIntake, Umbrales

MAX_CACHE = 64
MAX_CRONOGRAMA = 6

# Flexibilidades que se le consultan al modelo: clave → (lo que se pregunta,
# lo que ve el usuario). Las dos son texto fijo.
FLEXIBILIDADES: dict[str, tuple[str, str]] = {
    "entregas_parciales": (
        "recibir la compra en partes (entregas parciales o escalonadas)",
        "Acepta recibir la compra en partes (entregas escalonadas).",
    ),
    "otra_fecha": ("que la entrega sea en otra fecha que la pedida", "Acepta otra fecha de entrega."),
    "otra_marca": ("otra marca u otro modelo", "Acepta otra marca u otro modelo."),
    "otra_cantidad": ("otra cantidad", "Acepta otra cantidad."),
}
_OPCIONES_FLEX = {
    "dicha": "lo dijo explícitamente",
    "inferida": "no lo dijo, pero se deduce de lo que contó (por ejemplo: si la gente "
    "empieza en días distintos, le sirven entregas escalonadas)",
    "no": "ni lo dijo ni se deduce",
}


@dataclass(frozen=True)
class PreguntaIntake(PreguntaAgente):
    """La `Pregunta` de siempre más el resultado del intake (para el proxy)."""

    intake: ResultadoIntake | None = None


def requisitos_verificables(
    requisitos: Sequence[Requisito],
) -> tuple[list[Requisito], list[Requisito]]:
    """(los que el Guard sabe chequear, los que no).

    Lo que sabe chequear lo publica el Guard (`guard.chequeos.CAMPOS_SOPORTADOS`
    y `OPERADORES_SOPORTADOS`); hoy son todos los del contrato, atributos y
    `minimo` incluidos. Un requisito que el Guard no supiera chequear se
    mostraría en la ficha pero no iría a los parámetros autorizados.
    """
    from guard import chequeos

    campos = chequeos.CAMPOS_SOPORTADOS
    operadores = chequeos.OPERADORES_SOPORTADOS
    si: list[Requisito] = []
    no: list[Requisito] = []
    for r in requisitos:
        (si if r.campo in campos and r.operador in operadores else no).append(r)
    return si, no


def categoria_taxonomia(
    descripcion: str, cliente: ClienteDecisiones | None, taxonomia: Any = None
) -> str | None:
    """El ID de la taxonomía de Google para el pedido, o None.

    `taxonomia.clasificar` baja nivel por nivel mientras la confianza alcance
    y devuelve el último nivel seguro (también si la API falla). Sin cliente
    de decisiones, o si no llega ni al primer nivel, queda vacía. Por
    defecto usa el subconjunto del repo (ramas de la demo, sin red).
    """
    if cliente is None or not (descripcion or "").strip():
        return None
    from taxonomia import cargar_subconjunto, clasificar

    arbol = taxonomia if taxonomia is not None else cargar_subconjunto()
    return clasificar(descripcion.strip()[:intake.LARGO_TEXTO], arbol, cliente).id


# ---------------------------------------------------------------------------
# Contexto: cronograma y flexibilidades
# ---------------------------------------------------------------------------

_SEPARADORES = re.compile(
    r"\s*(?:[,;]|\.(?=\s|$)|\by\b|\be\b|\band\b|\bplus\b|\bpero\b|\bbut\b|\bmientras\b)\s*",
    re.IGNORECASE,
)


def cronograma_candidato(respuestas: Sequence[str], hoy: date) -> list[tuple[int, str, str]]:
    """(cantidad, fecha ISO, frase) que el código encuentra en las respuestas.

    Por cláusula ("10 el lunes y otros 10 el jueves"): una cantidad y una
    fecha en la misma cláusula forman un par. El modelo después juzga cada
    par; nunca inventa uno.
    """
    pares: list[tuple[int, str, str]] = []
    for texto in respuestas:
        for clausula in _SEPARADORES.split(texto or ""):
            if not clausula.strip():
                continue
            c = intake.extraer([clausula], hoy)
            if len(c.cantidades) == 1 and len(c.fechas) == 1:
                par = (c.cantidades[0].valor, c.fechas[0].valor, clausula.strip())
                if (par[0], par[1]) not in {(a, b) for a, b, _ in pares}:
                    pares.append(par)
    return pares[:MAX_CRONOGRAMA]


def preguntas_contexto(cronograma: Sequence[tuple[int, str, str]]) -> list[Pregunta]:
    qs: list[Pregunta] = []
    for i, (cantidad, fecha, frase) in enumerate(cronograma):
        dia = intake._DIAS_LARGOS[date.fromisoformat(fecha).weekday()]
        qs.append(Predicado(
            f"cron_{i}",
            intake._BASE + f"¿El usuario necesita que le lleguen al menos {cantidad} "
            f"unidades a más tardar el {dia} {fecha}, como parte de lo que necesita "
            f'(dice "{frase}")? Respondé que no si es otra cosa (por ejemplo, un precio).',
        ))
    for clave, (pregunta, _) in FLEXIBILIDADES.items():
        qs.append(Eleccion(
            f"flex_{clave}",
            intake._BASE + f"¿El usuario aceptaría {pregunta}?",
            dict(_OPCIONES_FLEX),
        ))
    return qs


def interpretar_contexto(
    respuestas: Mapping[str, Any],
    pedido: str,
    turnos: Sequence[Turno],
    cronograma: Sequence[tuple[int, str, str]],
    hoy: date,
    umbrales: Umbrales,
) -> Contexto:
    primero, *resto = turnos
    entregas = sorted(
        (
            Entrega(cantidad=cantidad, fecha=date.fromisoformat(fecha))
            for i, (cantidad, fecha, _) in enumerate(cronograma)
            if isinstance(r := respuestas.get(f"cron_{i}"), RespuestaPredicado)
            and r.probabilidad >= umbrales.duda
        ),
        key=lambda e: e.fecha,
    )
    flexibilidades = []
    for clave, (_, descripcion) in FLEXIBILIDADES.items():
        opcion, p = intake._elegida(respuestas.get(f"flex_{clave}"))
        if opcion in ("dicha", "inferida") and p >= umbrales.duda:
            flexibilidades.append(Flexibilidad(descripcion=descripcion, origen=opcion))
    return Contexto(
        pedido=pedido,
        pregunta=primero.pregunta,
        respuesta=primero.respuesta,
        historial=list(resto),
        # La necesidad son las palabras del usuario: el modelo no la redacta.
        necesidad=(primero.respuesta or "").strip()[:LARGO_NECESIDAD] or None,
        necesidad_entregas=[e for e in entregas if e.fecha >= hoy],
        flexibilidades=flexibilidades,
    )


# ---------------------------------------------------------------------------
# El agente
# ---------------------------------------------------------------------------


class AgenteDecisiones:
    """`intencion.agente.Agente` con un `ClienteDecisiones` (más el intake)."""

    nombre = "decisiones"

    def __init__(
        self,
        cliente: ClienteDecisiones,
        detector: Any = None,
        umbrales: Umbrales | None = None,
        taxonomia: Any = None,
    ) -> None:
        self.cliente = cliente
        self.detector = detector  # algo con detectar_oportunidad(contexto, ficha, ofertas)
        self.taxonomia = taxonomia  # taxonomia.Taxonomia; None = el subconjunto del repo
        self.umbrales = umbrales or intake.UMBRALES
        self._lock = threading.Lock()
        self._cache: OrderedDict[tuple, tuple[ResultadoIntake, Contexto | None]] = OrderedDict()
        self._categorias: OrderedDict[str, Any] = OrderedDict()

    @property
    def modelo(self) -> str:
        return f"{getattr(self.cliente, 'nombre', 'decisiones')}/{getattr(self.cliente, 'modelo', '?')}"

    # -- protocolo Agente -------------------------------------------------------

    def preguntar(
        self, pedido: str, hoy: date, categorias: list[str] | None = None
    ) -> PreguntaIntake:
        with self._lock:
            self._categorias[pedido] = categorias
            while len(self._categorias) > MAX_CACHE:
                self._categorias.popitem(last=False)
        resultado, _ = self._analizar(pedido, [], hoy, categorias)
        return PreguntaIntake(
            pregunta=resultado.pregunta,
            requisitos=resultado.requisitos_autorizables(confirmada=False),
            intake=resultado,
        )

    def armar_contexto(
        self,
        pedido: str,
        pregunta: str,
        respuesta: str,
        hoy: date,
        historial: Sequence[Turno] | None = None,
    ) -> Contexto:
        if not respuesta or not respuesta.strip():
            raise ValueError("La respuesta está vacía")
        turnos = [Turno(pregunta=pregunta, respuesta=respuesta.strip()), *(historial or [])]
        _, contexto = self._analizar(pedido, turnos, hoy, None)
        assert contexto is not None
        return contexto

    def detectar_oportunidad(
        self, contexto: Contexto, ficha: Ficha, ofertas: list[Oferta]
    ) -> Oportunidad | None:
        if self.detector is None:
            return None
        respuestas = [r for r in (contexto.respuesta, *(t.respuesta for t in contexto.historial)) if r]
        if len(respuestas) > 1:
            # El detector lee `respuesta`: le llega todo lo que contó el usuario.
            contexto = contexto.model_copy(update={"respuesta": " / ".join(respuestas)})
        verificables, _ = requisitos_verificables(ficha.requisitos)
        if len(verificables) != len(ficha.requisitos):
            ficha = ficha.model_copy(update={"requisitos": verificables})
        return self.detector.detectar_oportunidad(contexto, ficha, ofertas)

    def clasificar_taxonomia(self, descripcion: str) -> str | None:
        """`Ficha.categoria_taxonomia` para el pedido (None si no se pudo)."""
        return categoria_taxonomia(descripcion, self.cliente, self.taxonomia)

    # -- intake ----------------------------------------------------------------------

    def evaluar_intake(
        self,
        pedido: str,
        turnos: Sequence[Turno],
        hoy: date,
        categorias: list[str] | None = None,
    ) -> ResultadoIntake:
        resultado, _ = self._analizar(pedido, turnos, hoy, categorias)
        return resultado

    def _analizar(
        self,
        pedido: str,
        turnos: Sequence[Turno],
        hoy: date,
        categorias: Sequence[str] | Mapping[str, str] | None,
    ) -> tuple[ResultadoIntake, Contexto | None]:
        """Una llamada por turno: intake y, si hay respuestas, contexto."""
        if not pedido or not pedido.strip():
            raise ValueError("El pedido está vacío")
        conversacion = tuple((t.pregunta, t.respuesta) for t in turnos)
        clave = (pedido, conversacion, hoy.isoformat())
        with self._lock:
            if clave in self._cache:
                self._cache.move_to_end(clave)
                return self._cache[clave]
            if categorias is None:
                categorias = self._categorias.get(pedido)
            # Memoria: el resultado del turno anterior de esta conversación.
            previo = self._cache.get((pedido, conversacion[:-1], hoy.isoformat())) if turnos else None
        anterior = previo[0] if previo else None
        hechas = intake.respondidas(turnos)
        cands = intake.candidatos_de(pedido, hechas, hoy)
        cronograma = cronograma_candidato([t.respuesta or "" for t in hechas], hoy) if hechas else []
        qs = intake.preguntas(cands, categorias)
        if hechas:
            qs += preguntas_contexto(cronograma)
        respuestas = self.cliente.decidir(intake.estado_para_modelo(pedido, hechas, hoy), qs)
        resultado = intake.interpretar(
            respuestas, cands, categorias, respondidos=len(hechas), umbrales=self.umbrales,
            anterior=anterior,
        )
        contexto = (
            interpretar_contexto(respuestas, pedido, hechas, cronograma, hoy, self.umbrales)
            if hechas
            else None
        )
        with self._lock:
            self._cache[clave] = (resultado, contexto)
            while len(self._cache) > MAX_CACHE:
                self._cache.popitem(last=False)
        return resultado, contexto
