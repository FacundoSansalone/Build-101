"""Categoría de la taxonomía de Google: la oferta tiene que caer en la rama pedida.

Determinístico, sin LLM y sin I/O dentro de `evaluar()`: el árbol (el
subconjunto del repo, `taxonomia.cargar.cargar_subconjunto()`) se carga una
sola vez, al importar este módulo. Si no se puede cargar, `ARBOL` queda en
None y una ficha con categoría escala (falla cerrado).

Con `Ficha.categoria_taxonomia` (un ID), cada oferta suma un chequeo de
cumplimiento (`campo="categoria_taxonomia"`) con la categoría ya verificada
de la oferta: la de la primera fuente independiente que la tiene, si el
comercio la declaró (ver `guard.verificador`; un campo omitido no se
completa con el dato público).

| Caso | Resultado |
|---|---|
| ficha sin categoría | sin chequeo |
| sin árbol, o el ID de la ficha no es un ID o no está en el árbol | `escalar` |
| la oferta no declara la categoría | `escalar` |
| el ID de la oferta no está en el árbol | `escalar` |
| la oferta es más general (un ancestro de la pedida) | `escalar` |
| fuera de la rama | `escalar` |
| dentro de la rama (igual o descendiente), con fuente independiente | `ok` |
| dentro de la rama, solo declarado | `aviso` |

Fuera de la rama escala y no bloquea: una clasificación puede discutirse
(una 2 en 1 entre laptops y tablets); que decida una persona.

En el ranking cuenta como un obligatorio más, cumplido solo dentro de la
rama con fuente independiente (`cumple_categoria`): unas tablets más
baratas no "dominan" a una laptop correcta, y una oferta inverificable no
desplaza a una verificada.

Textos: los nombres salen del árbol (son nuestros) y los IDs son dígitos
validados. Nada de lo que escribe el comercio llega al `detalle`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from contracts import Chequeo, Ficha, Oferta, Origen
from guard.fuentes import ORIGENES_INDEPENDIENTES, nombre_fuente
from guard.textos import LARGO_MAXIMO, acotar, sin_control

if TYPE_CHECKING:
    from taxonomia.arbol import Categoria, Taxonomia

logger = logging.getLogger(__name__)

CAMPO = "categoria_taxonomia"
LARGO_MAXIMO_ID = 12  # como `/v1` (`proxy/api_v1.py`); la taxonomía usa hasta 6

Caso = Literal[
    "sin_arbol", "pedido_invalido", "pedido_desconocido", "sin_dato",
    "oferta_desconocida", "mas_general", "fuera", "dentro",
]


def cargar_arbol() -> Taxonomia | None:
    """El subconjunto del repo, o None si no se puede cargar (falla cerrado).

    El import va adentro: si `taxonomia` no carga, el Guard igual arranca y
    la categoría escala.
    """
    try:
        import taxonomia.cargar

        return taxonomia.cargar.cargar_subconjunto()
    except Exception:  # noqa: BLE001 - sin árbol, toda ficha con categoría escala
        logger.exception("No se pudo cargar la taxonomía: la categoría del pedido va a escalar")
        return None


# Una sola vez por proceso, al importar: `evaluar()` nunca lee el archivo.
ARBOL: Taxonomia | None = cargar_arbol()


@dataclass(frozen=True)
class Ubicacion:
    """Dónde cae la oferta respecto de la categoría pedida."""

    caso: Caso
    id_pedido: str | None = None      # dígitos validados (o None)
    id_ofrecido: str | None = None    # dígitos validados (o None)
    pedida: Categoria | None = None
    ofrecida: Categoria | None = None
    independiente: bool = False       # la categoría de la oferta salió de una fuente independiente
    origen: Origen = "declarado"


def exige_categoria(ficha: Ficha) -> bool:
    """La ficha pide una categoría (un texto vacío es no pedirla)."""
    return isinstance(ficha.categoria_taxonomia, str) and bool(ficha.categoria_taxonomia.strip())


def id_valido(valor: object) -> str | None:
    """Un ID de la taxonomía: solo dígitos ASCII, hasta 12. Si no, None."""
    if isinstance(valor, bool) or not isinstance(valor, (str, int)):
        return None
    texto = str(valor).strip()
    if texto.isascii() and texto.isdigit() and len(texto) <= LARGO_MAXIMO_ID:
        return texto
    return None


def ubicar(ficha: Ficha, oferta: Oferta) -> Ubicacion:
    """Compara la categoría (ya verificada) de la oferta con la de la ficha."""
    id_pedido = id_valido(ficha.categoria_taxonomia)
    arbol = ARBOL  # se lee al llamar: los tests lo cambian
    if arbol is None:
        return Ubicacion("sin_arbol", id_pedido=id_pedido)
    if id_pedido is None:
        return Ubicacion("pedido_invalido")
    pedida = arbol.get(id_pedido)
    if pedida is None:
        return Ubicacion("pedido_desconocido", id_pedido=id_pedido)

    evidencia = oferta.evidencia.get(CAMPO)
    independiente = (
        evidencia is not None
        and evidencia.estado in ("verificado", "contradicho")
        and evidencia.origen_usado in ORIGENES_INDEPENDIENTES
    )
    origen: Origen = evidencia.origen_usado if independiente else "declarado"
    base = {
        "id_pedido": id_pedido, "pedida": pedida, "independiente": independiente, "origen": origen,
    }
    if oferta.categoria_taxonomia is None:
        return Ubicacion("sin_dato", **base)
    id_ofrecido = id_valido(oferta.categoria_taxonomia)
    ofrecida = arbol.get(id_ofrecido)
    if ofrecida is None:
        return Ubicacion("oferta_desconocida", id_ofrecido=id_ofrecido, **base)
    if arbol.es_descendiente(id_ofrecido, id_pedido):
        caso: Caso = "dentro"
    elif arbol.es_descendiente(id_pedido, id_ofrecido, incluir_igual=False):
        caso = "mas_general"
    else:
        caso = "fuera"
    return Ubicacion(caso, id_ofrecido=id_ofrecido, ofrecida=ofrecida, **base)


def cumple_categoria(ficha: Ficha, oferta: Oferta) -> bool:
    """Para el ranking: dentro de la rama según una fuente independiente."""
    if not exige_categoria(ficha):
        return False
    ubicacion = ubicar(ficha, oferta)
    return ubicacion.caso == "dentro" and ubicacion.independiente


def chequeos_categoria(
    ficha: Ficha, oferta: Oferta, fixtures: frozenset[str] = frozenset()
) -> list[Chequeo]:
    """El chequeo de categoría de la oferta (lista vacía si la ficha no la pide).

    `fixtures`: claves de evidencia cuyo dato salió de una fuente rotulada
    como fixture (datos de prueba); el `detalle` lo dice.
    """
    if not exige_categoria(ficha):
        return []
    ubicacion = ubicar(ficha, oferta)
    if ubicacion.caso == "dentro":
        resultado = "ok" if ubicacion.independiente else "aviso"
    else:
        resultado = "escalar"
    return [
        Chequeo(
            tipo="cumplimiento",
            resultado=resultado,
            detalle=_detalle(ubicacion, fixture=CAMPO in fixtures),
            campo=CAMPO,
            esperado=acotar(ficha.categoria_taxonomia),
            observado=acotar(oferta.categoria_taxonomia),
            origen=ubicacion.origen,
        )
    ]


def _partes(categoria: Categoria | None) -> tuple[str, str]:
    """El nombre del árbol y el ID ("" si falta: `ubicar()` siempre los trae)."""
    if categoria is None:
        return "", ""
    return sin_control(categoria.nombre)[:LARGO_MAXIMO], categoria.id


def _nombre(categoria: Categoria | None) -> str:
    """"Laptops (328)"."""
    nombre, id_categoria = _partes(categoria)
    return f"{nombre} ({id_categoria})"


def _detalle(ubicacion: Ubicacion, fixture: bool = False) -> str:
    caso = ubicacion.caso
    if caso == "sin_arbol":
        pedido = f" (pedido: {ubicacion.id_pedido})" if ubicacion.id_pedido else ""
        return (
            "No se pudo cargar la taxonomía de productos: no se puede chequear la "
            f"categoría de la oferta{pedido}."
        )
    if caso == "pedido_invalido":
        return (
            "La categoría del pedido no es un ID de la taxonomía de Google: no se "
            "puede chequear la categoría de la oferta."
        )
    if caso == "pedido_desconocido":
        return (
            f"La categoría del pedido ({ubicacion.id_pedido}) no está en la taxonomía "
            "cargada: no se puede chequear la categoría de la oferta."
        )
    pedida = ", ".join(_partes(ubicacion.pedida))  # "Laptops, 328"
    if caso == "sin_dato":
        return (
            "Categoría (taxonomía de Google): la oferta no la declara; no se puede "
            f"comprobar que sea de la pedida ({pedida})."
        )
    if caso == "oferta_desconocida":
        ofrecida = "La categoría de la oferta"
        if ubicacion.id_ofrecido:  # sin ID válido (no son dígitos) no se repite
            ofrecida = f"Categoría {ubicacion.id_ofrecido} de la oferta"
        return (
            f"{ofrecida}: no está en la taxonomía cargada; no se puede ubicar "
            f"respecto de la pedida ({pedida})."
        )
    ofrecida = _nombre(ubicacion.ofrecida)
    if caso == "mas_general":
        return (
            f"Categoría {ofrecida} es más general que la pedida ({pedida}): no "
            "alcanza para saber si es lo pedido."
        )
    fuente = (
        nombre_fuente(ubicacion.origen, fixture) if ubicacion.independiente
        else "lo que declara el comercio"
    )
    if caso == "fuera":
        return f"Categoría {ofrecida} fuera de la pedida ({pedida}), según {fuente}."
    if ubicacion.independiente:
        return f"Categoría {ofrecida} dentro de la pedida ({pedida}), según {fuente}."
    return (
        f"Categoría {ofrecida}: dentro de la pedida ({pedida}), pero solo lo declara "
        "el comercio (no hay una fuente independiente que lo confirme)."
    )
