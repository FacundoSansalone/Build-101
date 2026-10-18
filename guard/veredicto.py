"""`evaluar()`: el núcleo del Guard. Determinístico, sin LLM y sin I/O.

Implementa el protocolo `contracts.Evaluador`. Recibe todo ya cargado (la
ficha, lo que mandó el comercio, el catálogo público y la política) y
devuelve un `Veredicto`.

Pasos: extractor (solo campos tipados; lo descartado escala) → verificador
(catálogo público) → ranking propio → chequeos de la ficha (mandato y
confirmación) y de la elegida (mandato, cumplimiento, evidencia, integridad
y política) → si llegó, el ap2.checkout firmado → veredicto.

Agregación de los chequeos: alguno `bloquea` → `bloqueado`; si no, alguno
`escalar` → `escalar`; si no, alguno `aviso` → `con_avisos`; si no,
`aprobado`.

Si no se pasa `politica`, se usa `Politica()` (los valores por defecto del
contrato).
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from contracts import (
    CheckoutVisto,
    Chequeo,
    Ficha,
    OfertasVistas,
    Politica,
    Status,
    Veredicto,
)
from guard.checkout import chequeos_checkout
from guard.chequeos import (
    chequeos_cumplimiento,
    chequeos_evidencia,
    chequeos_ficha,
    chequeos_mandato,
    chequeos_politica,
)
from guard.extractor import extraer
from guard.ranking import chequeo_integridad, rankear
from guard.recibo import armar_recibo
from guard.fuentes import ORDEN_FUENTES, nombre_fuente
from guard.textos import acotar, sin_control
from guard.verificador import verificar_con_rotulos

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


def redactar_motivo(
    status: Status, chequeos: list[Chequeo], fixtures: frozenset[str] = frozenset()
) -> str:
    """Una frase para el dashboard (y el agente): por qué este veredicto.

    Plantilla fija: solo dice lo que efectivamente se chequeó. Los `detalle`
    que junta no interpolan texto del comercio (ver `guard.textos`).
    `fixtures`: orígenes cuyos datos fueron de prueba (se rotulan).
    """
    if status == "aprobado":
        return _motivo_aprobado(chequeos, fixtures)
    resultado = next(r for r, s in _STATUS_POR_RESULTADO.items() if s == status)
    detalles = [c.detalle for c in chequeos if c.resultado == resultado]
    return sin_control(f"{_PREFIJO[status]}: " + " ".join(detalles))


def _motivo_aprobado(chequeos: list[Chequeo], fixtures: frozenset[str] = frozenset()) -> str:
    ok = [c for c in chequeos if c.resultado == "ok"]
    mandato = {c.campo for c in ok if c.tipo == "mandato" and c.origen != "checkout_firmado"}
    partes: list[str] = []
    if {"precio", "moneda"} <= mandato:
        partes.append("el monto y la moneda cumplen el mandato")
    elif "precio" in mandato:
        partes.append("el monto cumple el tope del mandato")
    elif "moneda" in mandato:
        partes.append("la moneda cumple el mandato")
    cumplidos = sum(c.tipo == "cumplimiento" for c in ok)
    if cumplidos:
        plural = "s" if cumplidos != 1 else ""
        partes.append(f"{cumplidos} requisito{plural} del pedido cumplido{plural}")
    origenes = {c.origen for c in ok if c.tipo == "evidencia"}
    if origenes:
        fuentes = [nombre_fuente(o, o in fixtures) for o in ORDEN_FUENTES if o in origenes]
        if len(fuentes) > 1:
            fuentes = [", ".join(fuentes[:-1]) + " y " + fuentes[-1]]
        partes.append(f"los datos coinciden con {fuentes[0] if fuentes else 'el catálogo público'}")
    if any(c.tipo == "politica" and c.origen != "checkout_firmado" for c in ok):
        partes.append("la política de compras se cumple")
    if any(c.tipo == "integridad" and c.origen is None for c in ok):
        partes.append("ninguna otra opción la supera en todo")
    if any(c.origen == "checkout_firmado" for c in ok):
        partes.append("el checkout firmado coincide con lo evaluado")
    if not partes:
        return "Aprobado: ningún chequeo objetó la compra."
    return "Aprobado: " + "; ".join(partes) + "."


def evaluar(
    ficha: Ficha,
    ofertas_vistas: OfertasVistas,
    cart_id_elegido: str,
    catalogo: dict[str, dict],
    politica: Politica | None = None,
    checkout_hash: str | None = None,
    ahora: datetime | None = None,
    checkout: CheckoutVisto | None = None,
) -> Veredicto:
    """Determinística y sin I/O. Mismas entradas, mismo veredicto.

    Con `checkout` (el data part ap2.checkout tal cual), además ata lo que
    firmó el comercio al cart evaluado (chequeos con origen
    "checkout_firmado") y el recibo lleva el hash recalculado del JWT. Sin
    `checkout` no se agrega ningún chequeo de checkout.
    """
    if politica is None:
        politica = Politica()
    comercio_id = ofertas_vistas.comercio_id
    extraccion = extraer(ofertas_vistas)
    nombres = [r.atributo for r in ficha.requisitos if r.campo == "atributo" and r.atributo]
    verificadas = [verificar_con_rotulos(o, catalogo, nombres) for o in extraccion.ofertas]
    ofertas = [oferta for oferta, _ in verificadas]
    fixtures = {oferta.cart_id: rotulos for oferta, rotulos in verificadas}
    puntajes = rankear(ficha, ofertas, comercio_id)
    ranking = [p.cart_id for p in puntajes]
    elegida = next((o for o in ofertas if o.cart_id == cart_id_elegido), None)
    # Mandato y cumplimiento de cada oferta, una sola vez: los de la elegida
    # deciden el veredicto y los de todas arman `recibo.filtradas`.
    requisitos_por_oferta = {
        o.cart_id: [
            *chequeos_mandato(ficha, o, comercio_id),
            *chequeos_cumplimiento(ficha, o, comercio_id),
        ]
        for o in ofertas
    }

    chequeos = chequeos_ficha(ficha)
    chequeos += [descarte.chequeo for descarte in extraccion.descartes]
    if elegida is None:
        chequeos.append(_chequeo_elegido_desconocido(cart_id_elegido))
    else:
        puntaje_elegido = next(p for p in puntajes if p.cart_id == cart_id_elegido)
        chequeos += [
            *requisitos_por_oferta[elegida.cart_id],
            *chequeos_evidencia(elegida, fixtures[elegida.cart_id]),
            chequeo_integridad(puntajes, puntaje_elegido, elegida.moneda),
            *chequeos_politica(politica, elegida, comercio_id),
        ]

    hash_recibo = checkout_hash
    if checkout is not None:
        del_checkout, hash_recibo = chequeos_checkout(
            checkout,
            cart_id_elegido=cart_id_elegido,
            elegida=elegida,
            ficha=ficha,
            politica=politica,
            comercio_id=comercio_id,
            checkout_hash=checkout_hash,
            ahora=ahora,
        )
        chequeos += del_checkout

    status = agregar(chequeos)
    origenes_fixture = frozenset(
        elegida.evidencia[clave].origen_usado
        for clave in (fixtures[elegida.cart_id] if elegida is not None else ())
    )
    recibo = armar_recibo(
        ficha=ficha,
        ofertas=ofertas,
        requisitos_por_oferta=requisitos_por_oferta,
        descartes=extraccion.descartes,
        puntajes=puntajes,
        cart_id_elegido=cart_id_elegido,
        comercio_id=comercio_id,
        politica=politica,
        status=status,
        checkout_hash=hash_recibo,
        ahora=ahora,
    )
    return Veredicto(
        status=status,
        cart_id_elegido=cart_id_elegido,
        cart_id_recomendado=ranking[0] if ranking else None,
        ranking=ranking,
        chequeos=chequeos,
        motivo=redactar_motivo(status, chequeos, origenes_fixture),
        recibo=recibo,
    )


def _chequeo_elegido_desconocido(cart_id: str) -> Chequeo:
    return Chequeo(
        tipo="integridad",
        resultado="escalar",
        detalle=(
            "El carrito elegido no está entre las ofertas que mandó el "
            "comercio: no hay datos para verificarlo."
        ),
        campo="cart_id",
        observado=acotar(cart_id),
    )
