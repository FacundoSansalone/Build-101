"""`evaluar()`: el núcleo del Guard. Determinístico, sin LLM y sin I/O.

Implementa el protocolo `contracts.Evaluador`. Recibe todo ya cargado (la
ficha, lo que mandó el comercio, el catálogo público y la política) y
devuelve un `Veredicto`.

Pasos: extractor (solo campos tipados) → verificador (catálogo público) →
ranking propio → chequeos sobre la elegida (mandato, cumplimiento,
evidencia, integridad y política) → veredicto.

Agregación de los chequeos: alguno `bloquea` → `bloqueado`; si no, alguno
`escalar` → `escalar`; si no, alguno `aviso` → `con_avisos`; si no,
`aprobado`.

Si no se pasa `politica`, se usa `Politica()` (los valores por defecto del
contrato).
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from contracts import Chequeo, Ficha, OfertasVistas, Politica, Status, Veredicto
from guard.chequeos import (
    chequeos_cumplimiento,
    chequeos_evidencia,
    chequeos_mandato,
    chequeos_politica,
)
from guard.extractor import extraer_ofertas
from guard.ranking import chequeo_integridad, rankear
from guard.recibo import armar_recibo
from guard.verificador import verificar_todas

_STATUS_POR_RESULTADO: dict[str, Status] = {
    "bloquea": "bloqueado",
    "escalar": "escalar",
    "aviso": "con_avisos",
}
_ORDEN_GRAVEDAD = ("bloquea", "escalar", "aviso")

_PREFIJO: dict[str, str] = {
    "bloqueado": "Bloqueado",
    "escalar": "Escalar a una persona",
    "con_avisos": "Con avisos",
}


def agregar(chequeos: Iterable[Chequeo]) -> Status:
    """El peor resultado manda."""
    resultados = {c.resultado for c in chequeos}
    for resultado in _ORDEN_GRAVEDAD:
        if resultado in resultados:
            return _STATUS_POR_RESULTADO[resultado]
    return "aprobado"


def redactar_motivo(status: Status, chequeos: list[Chequeo]) -> str:
    """Una frase para el dashboard: por qué este veredicto."""
    if status == "aprobado":
        return (
            "Aprobado: cumple el mandato, los requisitos del pedido y la "
            "política, y no hay datos contradichos ni sin verificar."
        )
    resultado = next(r for r, s in _STATUS_POR_RESULTADO.items() if s == status)
    detalles = [c.detalle for c in chequeos if c.resultado == resultado]
    return f"{_PREFIJO[status]}: " + " ".join(detalles)


def evaluar(
    ficha: Ficha,
    ofertas_vistas: OfertasVistas,
    cart_id_elegido: str,
    catalogo: dict[str, dict],
    politica: Politica | None = None,
    checkout_hash: str | None = None,
    ahora: datetime | None = None,
) -> Veredicto:
    """Determinística y sin I/O. Mismas entradas, mismo veredicto."""
    if politica is None:
        politica = Politica()
    comercio_id = ofertas_vistas.comercio_id
    ofertas = verificar_todas(extraer_ofertas(ofertas_vistas), catalogo)
    puntajes = rankear(ficha, ofertas, comercio_id)
    ranking = [p.cart_id for p in puntajes]
    elegida = next((o for o in ofertas if o.cart_id == cart_id_elegido), None)

    if elegida is None:
        chequeos = [_chequeo_elegido_desconocido(cart_id_elegido)]
    else:
        puntaje_elegido = next(p for p in puntajes if p.cart_id == cart_id_elegido)
        chequeos = [
            *chequeos_mandato(ficha, elegida, comercio_id),
            *chequeos_cumplimiento(ficha, elegida, comercio_id),
            *chequeos_evidencia(elegida),
            chequeo_integridad(puntajes, puntaje_elegido, elegida.moneda),
            *chequeos_politica(politica, elegida, comercio_id),
        ]

    status = agregar(chequeos)
    recibo = armar_recibo(
        ficha=ficha,
        ofertas=ofertas,
        puntajes=puntajes,
        cart_id_elegido=cart_id_elegido,
        comercio_id=comercio_id,
        politica=politica,
        status=status,
        checkout_hash=checkout_hash,
        ahora=ahora,
    )
    return Veredicto(
        status=status,
        cart_id_elegido=cart_id_elegido,
        cart_id_recomendado=ranking[0] if ranking else None,
        ranking=ranking,
        chequeos=chequeos,
        motivo=redactar_motivo(status, chequeos),
        recibo=recibo,
    )


def _chequeo_elegido_desconocido(cart_id: str) -> Chequeo:
    return Chequeo(
        tipo="integridad",
        resultado="escalar",
        detalle=(
            f"El carrito elegido ({cart_id}) no está entre las ofertas que "
            "mandó el comercio: no hay datos para verificarlo."
        ),
        campo="cart_id",
        observado=cart_id,
    )
