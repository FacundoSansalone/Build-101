"""Clasificación jerárquica en la taxonomía de Google, nivel por nivel.

En cada nivel se le pregunta al adaptador `decisiones` una `Eleccion` entre
las subcategorías del nivel actual (como mucho 255 opciones, con "ninguna").
Se baja mientras la probabilidad de la opción elegida alcance el umbral; si
no alcanza (o el modelo elige "ninguna", no contesta o falla), se devuelve el
**último nivel seguro**. El LLM propone una categoría: no autoriza nada (la
ficha la confirma el usuario y el Guard no depende de esto).
"""

from __future__ import annotations

from dataclasses import dataclass

from decisiones import ClienteDecisiones, DecisionesError, Eleccion, RespuestaEleccion
from taxonomia.arbol import Categoria, Taxonomia

NINGUNA = "ninguna"
MAX_OPCIONES = 255  # límite de opciones de una Eleccion (incluida "ninguna")
UMBRAL_POR_DEFECTO = 0.7
NOMBRE_PREGUNTA = "categoria"
HIJOS_EN_DESCRIPCION = 12  # subcategorías que se nombran en cada opción

_INSTRUCCIONES = (
    "Elegí la subcategoría de la taxonomía de productos de Google que mejor "
    "describe el producto. Las opciones son las subcategorías directas de "
    "`categoria_actual` (o el primer nivel si es null). Si ninguna describe el "
    f"producto, elegí '{NINGUNA}'."
)


@dataclass(frozen=True)
class Paso:
    """Una pregunta: qué se ofreció, qué eligió el modelo y con qué probabilidad."""

    nivel: int
    opciones: tuple[str, ...]  # IDs ofrecidos (sin "ninguna")
    eleccion: str | None       # ID, "ninguna" o None (no contestó o falló)
    confianza: float | None


@dataclass(frozen=True)
class Clasificacion:
    categoria: Categoria | None  # último nivel seguro; None = ni el primer nivel
    pasos: tuple[Paso, ...]
    completa: bool               # llegó a una hoja
    # "hoja", "umbral", "ninguna", "rechazo", "error", "eleccion_invalida",
    # "max_niveles" o "demasiadas_opciones"
    motivo: str
    confianza: float | None      # producto de las probabilidades aceptadas

    @property
    def id(self) -> str | None:
        return self.categoria.id if self.categoria else None

    @property
    def ruta(self) -> tuple[str, ...]:
        return self.categoria.ruta if self.categoria else ()


def _descripcion(taxonomia: Taxonomia, categoria: Categoria) -> str:
    """La ruta y, si tiene, sus subcategorías: "Audio Components" no dice que
    adentro están los auriculares, y sin eso el modelo elige "ninguna"."""
    hijos = [c.nombre for c in taxonomia.hijos(categoria.id)]
    if not hijos:
        return categoria.ruta_texto
    resto = f" y {len(hijos) - HIJOS_EN_DESCRIPCION} más" if len(hijos) > HIJOS_EN_DESCRIPCION else ""
    return f"{categoria.ruta_texto} (incluye: {', '.join(hijos[:HIJOS_EN_DESCRIPCION])}{resto})"


def clasificar(
    descripcion: str,
    taxonomia: Taxonomia,
    cliente: ClienteDecisiones,
    *,
    umbral: float = UMBRAL_POR_DEFECTO,
    max_niveles: int = 10,
    desde: str | None = None,
) -> Clasificacion:
    """Baja por la taxonomía desde `desde` (None = la raíz) hasta una hoja o
    hasta donde la confianza alcance. Nunca lanza por la API: una falla
    (`DecisionesError`) corta y devuelve el último nivel seguro."""
    if not 0.0 < umbral <= 1.0:
        raise ValueError("El umbral tiene que estar en (0, 1]")
    actual = taxonomia[desde] if desde is not None else None
    pasos: list[Paso] = []
    confianza: float | None = None

    def fin(motivo: str, completa: bool = False) -> Clasificacion:
        return Clasificacion(actual, tuple(pasos), completa, motivo, confianza)

    while True:
        hijos = taxonomia.hijos(actual.id if actual else None)
        if not hijos:
            return fin("hoja", completa=actual is not None)
        if len(pasos) >= max_niveles:
            return fin("max_niveles")
        if len(hijos) + 1 > MAX_OPCIONES:
            return fin("demasiadas_opciones")
        opciones = {c.id: _descripcion(taxonomia, c) for c in hijos}
        opciones[NINGUNA] = "Ninguna de estas categorías describe el producto"
        pregunta = Eleccion(NOMBRE_PREGUNTA, _INSTRUCCIONES, opciones)
        estado = {
            "producto": descripcion,
            "categoria_actual": actual.ruta_texto if actual else None,
        }
        ofrecidas = tuple(c.id for c in hijos)
        nivel = (actual.nivel if actual else 0) + 1
        try:
            respuesta = cliente.decidir(estado, [pregunta]).get(NOMBRE_PREGUNTA)
        except DecisionesError:
            pasos.append(Paso(nivel, ofrecidas, None, None))
            return fin("error")
        if not isinstance(respuesta, RespuestaEleccion):
            pasos.append(Paso(nivel, ofrecidas, None, None))
            return fin("rechazo")
        probabilidad = respuesta.probabilidades.get(respuesta.eleccion, respuesta.confianza)
        pasos.append(Paso(nivel, ofrecidas, respuesta.eleccion, probabilidad))
        if respuesta.eleccion == NINGUNA:
            return fin("ninguna")
        if respuesta.eleccion not in ofrecidas:
            return fin("eleccion_invalida")
        if probabilidad < umbral:
            return fin("umbral")
        actual = taxonomia[respuesta.eleccion]
        confianza = probabilidad if confianza is None else confianza * probabilidad
