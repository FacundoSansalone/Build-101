"""Chequeos del Guard sobre la oferta elegida. Código determinístico.

Ningún chequeo lee texto libre: solo los campos tipados de `Oferta` (y el
`comercio_id` de `OfertasVistas`). `Oferta.texto_libre_descartado` no se
toca acá.

- **mandato**: los requisitos obligatorios de precio y moneda (lo que el
  usuario firma en AP2). Si fallan, `bloquea`.
- **cumplimiento**: el resto de los requisitos de la ficha. Un obligatorio
  que falla `bloquea`; un preferido que falla es `aviso`. Si falta el dato,
  no se aprueba en silencio: `escalar` (obligatorio) o `aviso` (preferido).
"""

from __future__ import annotations

import math
import re
from typing import Any

from contracts import Chequeo, Ficha, Oferta, Origen, Requisito

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

# Tolerancia para comparar montos (centavos).
_EPSILON_MONTO = 0.005

_NOMBRES = {
    "precio": "Precio",
    "moneda": "Moneda",
    "categoria": "Categoría",
    "uso": "Uso",
    "talle": "Talle",
    "marca": "Marca",
    "comercio": "Comercio",
}

_NUMERO = re.compile(r"^-?\d+(\.\d+)?$")


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
    if isinstance(valor, float) and math.isnan(valor):
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
        monto, tope = _numero(observado), _numero(requisito.valor)
        if monto is None or tope is None:
            return None
        return monto <= tope + _EPSILON_MONTO
    if requisito.operador == "incluye":
        elementos = observado if isinstance(observado, list) else [observado]
        return _normalizar(requisito.valor) in {_normalizar(e) for e in elementos}
    return _normalizar(observado) == _normalizar(requisito.valor)


def _normalizar(valor: Any) -> Any:
    """Compara sin importar mayúsculas, espacios ni "42" frente a 42."""
    numero = _numero(valor)
    if numero is not None:
        return numero
    if isinstance(valor, str):
        return valor.strip().casefold()
    return valor


def _numero(valor: Any) -> float | None:
    if isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return None if math.isnan(float(valor)) else float(valor)
    if isinstance(valor, str) and _NUMERO.match(valor.strip()):
        return float(valor.strip())
    return None


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
        detalle=_detalle(requisito, oferta, observado, resultado_cumple, tipo),
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
# Textos para el dashboard
# ---------------------------------------------------------------------------


def formatear(valor: Any) -> str:
    """Valor legible: listas separadas por comas y floats enteros sin decimales."""
    if isinstance(valor, list):
        return ", ".join(formatear(v) for v in valor) or "ninguno"
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))
    return str(valor)


def monto(precio: Any, moneda: str) -> str:
    numero = _numero(precio)
    return f"{moneda} {numero:.2f}".strip() if numero is not None else "ilegible"


def _detalle(
    requisito: Requisito,
    oferta: Oferta,
    observado: Any,
    resultado_cumple: bool | None,
    tipo: str,
) -> str:
    nombre = _NOMBRES.get(requisito.campo, requisito.campo)
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
