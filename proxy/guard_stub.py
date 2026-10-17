"""Stub de `guard.evaluar()` mientras Dev 2 no lo entrega.

Misma firma que `contracts.Evaluador`. Regla fija (ver
docs/onboarding/dev3-proxy-dashboard.md): bloquea si el carrito elegido es
el último que ofreció el comercio (el del atacante en A, B y B2). No es el
Guard: no mira la ficha ni el catálogo.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from contracts import Chequeo, Ficha, OfertasVistas, Politica, Recibo, Veredicto

SIN_VERIFICAR = ["calidad", "entrega", "reputación"]


def evaluar(
    ficha: Ficha,
    ofertas_vistas: OfertasVistas,
    cart_id_elegido: str,
    catalogo: dict[str, dict],
    politica: Politica | None = None,
    checkout_hash: str | None = None,
    ahora: datetime | None = None,
) -> Veredicto:
    cart_ids = [str(c.get("cart_id")) for c in ofertas_vistas.carts]
    ultimo = cart_ids[-1] if cart_ids else None
    ranking = [c for c in cart_ids if c != ultimo]

    if not cart_ids:
        status = "bloqueado"
        motivo = "STUB: no hay ofertas registradas con las que comparar la elección."
    elif cart_id_elegido == ultimo:
        status = "bloqueado"
        motivo = (
            f"STUB: {cart_id_elegido} es el último carrito que ofreció el comercio "
            "(en A, B y B2 es el del atacante)."
        )
    else:
        status = "aprobado"
        motivo = f"STUB: {cart_id_elegido} no es el último carrito ofrecido."

    chequeo = Chequeo(
        tipo="integridad",
        resultado="bloquea" if status == "bloqueado" else "ok",
        detalle=motivo,
        campo="cart_id",
        esperado=ranking[0] if ranking else None,
        observado=cart_id_elegido,
    )
    emitido = ahora or datetime.now(timezone.utc)
    recibo = Recibo(
        id=_id_recibo(cart_ids, cart_id_elegido, checkout_hash, emitido),
        emitido=emitido,
        ficha=ficha,
        consideradas=cart_ids,
        filtradas={ultimo: ["STUB: último carrito ofrecido"]} if ultimo else {},
        verificaciones=[],
        ranking=ranking,
        elegido=cart_id_elegido,
        por_que_gano=motivo,
        sin_verificar=list(SIN_VERIFICAR),
        status=status,
        checkout_hash=checkout_hash,
    )
    return Veredicto(
        status=status,
        cart_id_elegido=cart_id_elegido,
        cart_id_recomendado=ranking[0] if ranking else None,
        ranking=ranking,
        chequeos=[chequeo],
        motivo=motivo,
        recibo=recibo,
    )


def _id_recibo(
    cart_ids: list[str], elegido: str, checkout_hash: str | None, emitido: datetime
) -> str:
    semilla = json.dumps([cart_ids, elegido, checkout_hash, emitido.isoformat()])
    return "stub-" + hashlib.sha256(semilla.encode("utf-8")).hexdigest()[:16]
