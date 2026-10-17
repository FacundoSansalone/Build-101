"""Chequeos del Guard sobre la oferta elegida. Código determinístico.

Ningún chequeo lee texto libre: solo los campos tipados de `Oferta` (y el
`comercio_id` de `OfertasVistas`). `Oferta.texto_libre_descartado` no se
toca acá.

- **mandato**: los requisitos obligatorios de precio y moneda (lo que el
  usuario firma en AP2). Si fallan, `bloquea`.
- **cumplimiento**: el resto de los requisitos de la ficha. Un obligatorio
  que falla `bloquea`; un preferido que falla es `aviso`. Si falta el dato,
  no se aprueba en silencio: `escalar` (obligatorio) o `aviso` (preferido).
- **evidencia**: cada campo verificado contra el catálogo público. Un dato
  `contradicho` se escala (nunca se bloquea: puede haber razones legítimas);
  uno `solo_declarado` es `aviso`.
- **política**: comercio aprobado (fuera de la lista `bloquea`; sin
  identificar es `aviso`) y monto máximo sin aprobación (si se supera,
  `escalar`).

La integridad de la elección (ranking propio) está en `guard.ranking`.
"""

from __future__ import annotations

from typing import Any

from contracts import Chequeo, Evidencia, Ficha, Oferta, Origen, Politica, Requisito
from guard.normalizar import EPSILON_MONTO, normalizar, numero

# Campo de la ficha -> atributo de Oferta. "comercio" sale de OfertasVistas.
ATRIBUTO_POR_CAMPO: dict[str, str] = {
    "precio": "precio",
    "moneda": "moneda",
    "categoria": "categoria",
    "uso": "uso",
    "talle": "talles",
    "marca": "marca",
}

# Requisitos obligatorios que forman parte del mandato que firma el usuario.
CAMPOS_MANDATO = frozenset({"precio", "moneda"})

_NOMBRES = {
    "precio": "Precio",
    "moneda": "Moneda",
    "categoria": "Categoría",
    "uso": "Uso",
    "talle": "Talle",
    "talles": "Talles",
    "marca": "Marca",
    "titulo": "Título",
    "comercio": "Comercio",
}


def nombre_campo(campo: str) -> str:
    return _NOMBRES.get(campo, campo)


# ---------------------------------------------------------------------------
# Comparación genérica de un requisito contra una oferta
# ---------------------------------------------------------------------------


def valor_observado(campo: str, oferta: Oferta, comercio_id: str | None = None) -> Any:
    """Valor de la oferta para un campo de la ficha. None = sin dato."""
    if campo == "comercio":
        return comercio_id or None
    valor = getattr(oferta, ATRIBUTO_POR_CAMPO[campo])
    if valor in ("", None) or valor == []:
        return None
    if isinstance(valor, float) and valor != valor:  # NaN: monto ilegible
        return None
    return valor


def cumple(
    requisito: Requisito, oferta: Oferta, comercio_id: str | None = None
) -> bool | None:
    """True/False si se puede decidir; None si falta el dato para comparar."""
    observado = valor_observado(requisito.campo, oferta, comercio_id)
    if observado is None:
        return None
    if requisito.operador == "maximo":
        valor, tope = numero(observado), numero(requisito.valor)
        if valor is None or tope is None:
            return None
        return valor <= tope + EPSILON_MONTO
    if requisito.operador == "incluye":
        elementos = observado if isinstance(observado, list) else [observado]
        return normalizar(requisito.valor) in {normalizar(e) for e in elementos}
    return normalizar(observado) == normalizar(requisito.valor)


# ---------------------------------------------------------------------------
# Mandato y cumplimiento
# ---------------------------------------------------------------------------


def es_de_mandato(requisito: Requisito) -> bool:
    return requisito.obligatorio and requisito.campo in CAMPOS_MANDATO


def chequeos_mandato(
    ficha: Ficha, oferta: Oferta, comercio_id: str | None = None
) -> list[Chequeo]:
    """Precio y moneda obligatorios contra lo que se va a cobrar (ap2.cart)."""
    return [
        _chequeo_requisito(r, oferta, comercio_id, tipo="mandato")
        for r in ficha.requisitos
        if es_de_mandato(r)
    ]


def chequeos_cumplimiento(
    ficha: Ficha, oferta: Oferta, comercio_id: str | None = None
) -> list[Chequeo]:
    """El resto de los requisitos (obligatorios y preferidos) de la ficha."""
    return [
        _chequeo_requisito(r, oferta, comercio_id, tipo="cumplimiento")
        for r in ficha.requisitos
        if not es_de_mandato(r)
    ]


def _chequeo_requisito(
    requisito: Requisito, oferta: Oferta, comercio_id: str | None, tipo: str
) -> Chequeo:
    resultado_cumple = cumple(requisito, oferta, comercio_id)
    if resultado_cumple is True:
        resultado = "ok"
    elif resultado_cumple is None:
        resultado = "escalar" if requisito.obligatorio else "aviso"
    else:
        resultado = "bloquea" if requisito.obligatorio else "aviso"
    observado = valor_observado(requisito.campo, oferta, comercio_id)
    return Chequeo(
        tipo=tipo,
        resultado=resultado,
        detalle=_detalle_requisito(requisito, oferta, observado, resultado_cumple, tipo),
        campo=requisito.campo,
        esperado=requisito.valor,
        observado=observado,
        origen=origen_del_campo(requisito.campo, oferta),
    )


def origen_del_campo(campo: str, oferta: Oferta) -> Origen | None:
    """Qué fuente usó el chequeo para este campo."""
    if campo in ("precio", "moneda"):
        return "cart_ap2"
    if campo == "comercio":
        return None
    evidencia = oferta.evidencia.get(ATRIBUTO_POR_CAMPO[campo])
    return evidencia.origen_usado if evidencia else "declarado"


# ---------------------------------------------------------------------------
# Evidencia
# ---------------------------------------------------------------------------


def chequeos_evidencia(oferta: Oferta) -> list[Chequeo]:
    """Un chequeo por campo verificable de la oferta (ya verificada)."""
    chequeos = []
    for evidencia in oferta.evidencia.values():
        if evidencia.estado == "no_verificable":
            continue  # va al recibo (sin_verificar), no cambia el veredicto
        resultado = {
            "verificado": "ok",
            "solo_declarado": "aviso",
            "contradicho": "escalar",
        }[evidencia.estado]
        chequeos.append(
            Chequeo(
                tipo="evidencia",
                resultado=resultado,
                detalle=_detalle_evidencia(evidencia, oferta.moneda),
                campo=evidencia.campo,
                esperado=evidencia.publico,
                observado=evidencia.declarado,
                origen=evidencia.origen_usado,
            )
        )
    return chequeos


def _detalle_evidencia(evidencia: Evidencia, moneda: str) -> str:
    nombre = nombre_campo(evidencia.campo)
    if evidencia.campo == "precio":
        cobrado = monto(evidencia.declarado, moneda)
        publico = monto(evidencia.publico, moneda)
        if evidencia.estado == "contradicho":
            return f"Precio: se cobra {cobrado} pero el catálogo público dice {publico}."
        if evidencia.estado == "solo_declarado":
            return f"Precio {cobrado}: no está en el catálogo público para compararlo."
        return f"Precio {cobrado} coincide con el catálogo público."

    declarado = formatear(evidencia.declarado)
    publico = formatear(evidencia.publico)
    if evidencia.estado == "contradicho":
        return (
            f"{nombre}: el comercio declara {declarado} pero el catálogo "
            f"público dice {publico}."
        )
    if evidencia.estado == "solo_declarado":
        return (
            f"{nombre} {declarado}: solo lo declara el comercio, no hay fuente "
            "independiente para verificarlo."
        )
    return f"{nombre} {publico} verificado contra el catálogo público."


# ---------------------------------------------------------------------------
# Política de compras
# ---------------------------------------------------------------------------


def chequeos_politica(
    politica: Politica, oferta: Oferta, comercio_id: str | None
) -> list[Chequeo]:
    """Comercio aprobado y monto máximo sin aprobación."""
    chequeos = [_chequeo_comercio(politica, comercio_id)]
    if politica.monto_maximo_sin_aprobacion is not None:
        chequeos.append(_chequeo_monto(politica.monto_maximo_sin_aprobacion, oferta))
    return chequeos


def _chequeo_comercio(politica: Politica, comercio_id: str | None) -> Chequeo:
    aprobados = politica.comercios_aprobados
    if not comercio_id:
        resultado = "aviso"
        detalle = (
            "Comercio no identificado: no se puede confirmar que esté entre "
            "los aprobados por la política."
        )
    elif normalizar(comercio_id) in {normalizar(c) for c in aprobados}:
        resultado = "ok"
        detalle = f"Comercio {comercio_id} aprobado por la política."
    else:
        resultado = "bloquea"
        detalle = (
            f"Comercio {comercio_id} fuera de los aprobados por la política "
            f"({formatear(list(aprobados))})."
        )
    return Chequeo(
        tipo="politica",
        resultado=resultado,
        detalle=detalle,
        campo="comercio",
        esperado=list(aprobados),
        observado=comercio_id,
    )


def _chequeo_monto(maximo: float, oferta: Oferta) -> Chequeo:
    cobrado = valor_observado("precio", oferta)
    tope = monto(maximo, oferta.moneda)
    if cobrado is None:
        resultado = "escalar"
        detalle = f"Monto ilegible: no se puede comparar con el máximo sin aprobación ({tope})."
    elif cobrado > maximo + EPSILON_MONTO:
        resultado = "escalar"
        detalle = (
            f"Monto {monto(cobrado, oferta.moneda)} supera el máximo sin "
            f"aprobación de la política ({tope}): requiere aprobación."
        )
    else:
        resultado = "ok"
        detalle = (
            f"Monto {monto(cobrado, oferta.moneda)} dentro del máximo sin "
            f"aprobación ({tope})."
        )
    return Chequeo(
        tipo="politica",
        resultado=resultado,
        detalle=detalle,
        campo="monto",
        esperado=maximo,
        observado=cobrado,
        origen="cart_ap2",
    )


# ---------------------------------------------------------------------------
# Textos para el dashboard
# ---------------------------------------------------------------------------


def formatear(valor: Any) -> str:
    """Valor legible: listas separadas por comas y floats enteros sin decimales."""
    if valor is None:
        return "sin dato"
    if isinstance(valor, list):
        return ", ".join(formatear(v) for v in valor) or "ninguno"
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))
    return str(valor)


def monto(precio: Any, moneda: str) -> str:
    valor = numero(precio)
    return f"{moneda} {valor:.2f}".strip() if valor is not None else "ilegible"


def _detalle_requisito(
    requisito: Requisito,
    oferta: Oferta,
    observado: Any,
    resultado_cumple: bool | None,
    tipo: str,
) -> str:
    nombre = nombre_campo(requisito.campo)
    if tipo == "mandato":
        referencia = "el mandato"
    elif requisito.obligatorio:
        referencia = "el obligatorio"
    else:
        referencia = "el preferido"

    if requisito.campo == "precio" and requisito.operador == "maximo":
        tope = f"{referencia} ({monto(requisito.valor, oferta.moneda)})"
        cobrado = monto(oferta.precio, oferta.moneda)
        if resultado_cumple is None:
            return f"Precio {cobrado}: no se puede comparar con el tope de {tope}."
        if resultado_cumple:
            return f"Precio {cobrado} dentro del tope de {tope}."
        return f"Precio {cobrado} supera el tope de {tope}."

    pedido = formatear(requisito.valor)
    if resultado_cumple is None:
        return f"{nombre}: sin dato para comparar con {referencia} ({pedido})."

    if requisito.operador == "incluye":
        ofrecidos = formatear(observado)
        if resultado_cumple:
            return f"{nombre} {pedido} disponible (ofrecidos: {ofrecidos})."
        return (
            f"{nombre} {pedido} no disponible (ofrecidos: {ofrecidos}): "
            f"no cumple {referencia}."
        )

    if resultado_cumple:
        return f"{nombre} {formatear(observado)} cumple {referencia} ({pedido})."
    return f"{nombre} {formatear(observado)} no cumple {referencia} ({pedido})."
