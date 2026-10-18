"""Chequeos del Guard sobre la oferta elegida. Código determinístico.

Ningún chequeo lee texto libre: solo los campos tipados de `Oferta` (y el
`comercio_id` de `OfertasVistas`). `Oferta.texto_libre_descartado` no se
toca acá.

- **mandato**: los requisitos obligatorios de precio y moneda (lo que el
  usuario firma en AP2). Si fallan, `bloquea`.
- **cumplimiento**: el resto de los requisitos de la ficha. Un obligatorio
  que falla `bloquea`; un preferido que falla es `aviso`. Si falta el dato,
  no se aprueba en silencio: `escalar` (obligatorio) o `aviso` (preferido).
  Un obligatorio que se cumple solo con lo que declara el comercio (SKU
  fuera del catálogo, dato no publicado) también escala.
- **evidencia**: cada campo verificado contra el catálogo público. Un dato
  `contradicho` se escala (nunca se bloquea: puede haber razones legítimas);
  uno `solo_declarado` es `aviso`.
- **política**: comercio aprobado (fuera de la lista `bloquea`; sin
  identificar `escalar`) y monto máximo sin aprobación (si se supera,
  `escalar`).
- **ficha**: sin tope de precio o moneda obligatorios, o sin confirmar,
  `escalar` (`chequeos_ficha`).

La integridad de la elección (ranking propio) está en `guard.ranking`.

Textos: ningún `detalle` interpola lo que escribe el comercio (ver
`guard.textos`): solo nombres de campo, valores de la ficha y del catálogo
público, montos y talles. Lo declarado va en `observado`, acotado.
"""

from __future__ import annotations

from typing import Any

from contracts import Chequeo, Evidencia, Ficha, Oferta, Origen, Politica, Requisito, ValorAtributo
from guard.atributos import clave_atributo, comparar, formatear_atributo, mostrable_declarado
from guard.fuentes import ORIGENES_INDEPENDIENTES, nombre_fuente
from guard.normalizar import (
    EPSILON_MONTO,
    contiene,
    contiene_todos,
    cubre_entregas,
    entregas_validas,
    gtin_valido,
    iguales,
    monto_valido,
    numero,
)
from guard.textos import ELEMENTOS_MAXIMOS, LARGO_MAXIMO, acotar, moneda_legible, sin_control
from guard.verificador import PREFIJO_ATRIBUTO, acotar_atributo, clave_evidencia_atributo

# Campo de la ficha -> atributo de Oferta. "comercio" sale de OfertasVistas.
ATRIBUTO_POR_CAMPO: dict[str, str] = {
    "precio": "precio",
    "moneda": "moneda",
    "categoria": "categoria",
    "uso": "uso",
    "talle": "talles",
    "marca": "marca",
    "cantidad": "cantidad",
    "entregas": "entregas",
}

# Requisitos obligatorios que forman parte del mandato que firma el usuario.
CAMPOS_MANDATO = frozenset({"precio", "moneda"})

# Compromisos del comercio que no se pueden verificar antes de comprar
# ("entrega" está en NO_VERIFICABLES y va al recibo como sin verificar). No
# hay catálogo contra el cual escalarlos: lo que cuenta es que la oferta
# cumpla los parámetros que autorizó el usuario (docs/FLUJO.md, paso 7).
CAMPOS_COMPROMISO = frozenset({"cantidad", "entregas"})

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
    "sku": "SKU",
    "cantidad": "Cantidad",
    "entregas": "Entregas",
    "gtin": "GTIN",
    "categoria_taxonomia": "Categoría (taxonomía de Google)",
}


def nombre_campo(campo: str) -> str:
    """Nombre para un `detalle`. Un atributo se nombra como en la ficha (ya
    canonicalizado: solo minúsculas, dígitos y `_`), nunca como lo escribe el
    comercio."""
    if campo.startswith(PREFIJO_ATRIBUTO):
        return campo.removeprefix(PREFIJO_ATRIBUTO)
    return _NOMBRES.get(campo, campo)


def clave_evidencia(requisito: Requisito) -> str:
    """Qué evidencia de la oferta respalda este requisito."""
    if requisito.campo == "atributo":
        return clave_evidencia_atributo(requisito.atributo or "")
    return ATRIBUTO_POR_CAMPO.get(requisito.campo, requisito.campo)


def _es_gtin(requisito: Requisito) -> bool:
    return requisito.campo == "atributo" and clave_atributo(requisito.atributo or "") == "gtin"


# ---------------------------------------------------------------------------
# Comparación genérica de un requisito contra una oferta
# ---------------------------------------------------------------------------


def valor_observado(campo: str, oferta: Oferta, comercio_id: str | None = None) -> Any:
    """Valor de la oferta para un campo de la ficha. None = sin dato."""
    if campo == "comercio":
        return comercio_id or None
    if campo not in ATRIBUTO_POR_CAMPO:
        return None  # "atributo" necesita el nombre: usar observado_de(requisito, ...)
    valor = getattr(oferta, ATRIBUTO_POR_CAMPO[campo])
    if valor in ("", None) or valor == []:
        return None
    if isinstance(valor, float) and valor != valor:  # NaN: monto ilegible
        return None
    return valor


def observado_de(
    requisito: Requisito, oferta: Oferta, comercio_id: str | None = None
) -> Any:
    """Valor de la oferta para un requisito (un `ValorAtributo` si es un
    atributo; el GTIN como texto). None = sin dato."""
    if requisito.campo != "atributo":
        return valor_observado(requisito.campo, oferta, comercio_id)
    if _es_gtin(requisito):
        return oferta.gtin
    return oferta.atributos.get(clave_atributo(requisito.atributo or ""))


def _cumple_gtin(requisito: Requisito, gtin: str) -> bool | None:
    pedidos = requisito.valor if isinstance(requisito.valor, (list, tuple)) else [requisito.valor]
    normalizados = [gtin_valido(p) for p in pedidos]
    if not normalizados or any(n is None for n in normalizados):
        return None  # un GTIN pedido inválido no se puede comparar
    if requisito.operador == "incluye" or (requisito.operador == "igual" and len(normalizados) > 1):
        return gtin in normalizados
    if requisito.operador == "igual":
        return gtin == normalizados[0]
    return None


def cumple(
    requisito: Requisito, oferta: Oferta, comercio_id: str | None = None
) -> bool | None:
    """True/False si se puede decidir; None si falta el dato para comparar."""
    observado = observado_de(requisito, oferta, comercio_id)
    if observado is None:
        return None
    if requisito.campo == "atributo":
        if _es_gtin(requisito):
            return _cumple_gtin(requisito, observado)
        return comparar(observado, requisito.operador, requisito.valor, requisito.unidad)
    if requisito.operador == "minimo":
        valor, minimo = numero(observado), numero(requisito.valor)
        if valor is None or minimo is None:
            return None
        return valor >= minimo - EPSILON_MONTO
    if requisito.operador == "maximo":
        valor, tope = numero(observado), monto_valido(requisito.valor)
        if valor is None or tope is None:  # monto o tope inválido: no se puede comparar
            return None
        return valor <= tope + EPSILON_MONTO
    if requisito.operador == "incluye":
        elementos = observado if isinstance(observado, list) else [observado]
        pedidos = requisito.valor if isinstance(requisito.valor, (list, tuple)) else [requisito.valor]
        if not pedidos:
            return None  # un "incluye" vacío no se puede chequear
        return contiene_todos(elementos, pedidos)
    if requisito.operador == "cubre":
        return cubre_entregas(observado, requisito.valor)
    return iguales(observado, requisito.valor)


# ---------------------------------------------------------------------------
# Mandato y cumplimiento
# ---------------------------------------------------------------------------


def es_de_mandato(requisito: Requisito) -> bool:
    return requisito.obligatorio and requisito.campo in CAMPOS_MANDATO


def chequeos_ficha(ficha: Ficha) -> list[Chequeo]:
    """La ficha tiene que traer el mandato y estar confirmada; si no, escalar.

    Sin un tope de precio obligatorio válido, sin una moneda obligatoria o
    sin la confirmación del usuario no hay contra qué aprobar: como mínimo
    `escalar`, con un chequeo de mandato que lo explique. Lista vacía si la
    ficha está bien (los chequeos de cada oferta van aparte).
    """
    chequeos: list[Chequeo] = []
    if not ficha.confirmada:
        chequeos.append(
            Chequeo(
                tipo="mandato", resultado="escalar", campo="ficha", origen="ficha",
                detalle=(
                    "La ficha del pedido no está confirmada por el usuario: sin "
                    "confirmación no hay mandato contra el cual aprobar."
                ),
                esperado=True, observado=False,
            )
        )
    topes = [
        r for r in ficha.requisitos
        if es_de_mandato(r) and r.campo == "precio" and r.operador == "maximo"
    ]
    if not any(monto_valido(r.valor) is not None for r in topes):
        chequeos.append(
            Chequeo(
                tipo="mandato", resultado="escalar", campo="precio", origen="ficha",
                detalle=(
                    "La ficha no tiene un tope de precio obligatorio válido: no hay "
                    "mandato contra el cual comparar el monto."
                ),
                observado=[r.valor for r in topes] or None,
            )
        )
    monedas = [r for r in ficha.requisitos if es_de_mandato(r) and r.campo == "moneda"]
    if not any(isinstance(r.valor, str) and r.valor.strip() for r in monedas):
        chequeos.append(
            Chequeo(
                tipo="mandato", resultado="escalar", campo="moneda", origen="ficha",
                detalle=(
                    "La ficha no tiene una moneda obligatoria: no hay mandato contra "
                    "el cual comparar la moneda."
                ),
                observado=[r.valor for r in monedas] or None,
            )
        )
    return chequeos


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


def respaldado(requisito: Requisito, oferta: Oferta) -> bool:
    """False si el dato que usa el chequeo es solo lo que declara el comercio.

    Precio y moneda son lo que se cobra (ap2.cart) y el comercio lo da quien
    llama: no aplican. Para el resto hace falta evidencia que no sea
    `solo_declarado` (una oferta sin verificar no tiene respaldo).
    """
    if (
        requisito.campo in CAMPOS_MANDATO
        or requisito.campo in CAMPOS_COMPROMISO
        or requisito.campo == "comercio"
    ):
        return True
    evidencia = oferta.evidencia.get(clave_evidencia(requisito))
    return evidencia is not None and evidencia.estado != "solo_declarado"


def cumple_respaldado(
    requisito: Requisito, oferta: Oferta, comercio_id: str | None = None
) -> bool:
    """Cumple con un dato que no es solo lo que declara el comercio (ranking)."""
    return cumple(requisito, oferta, comercio_id) is True and respaldado(requisito, oferta)


def _chequeo_requisito(
    requisito: Requisito, oferta: Oferta, comercio_id: str | None, tipo: str
) -> Chequeo:
    resultado_cumple = cumple(requisito, oferta, comercio_id)
    sin_respaldo = False
    if resultado_cumple is True:
        # Obligatorio respaldado solo por lo que declara el comercio → escalar.
        sin_respaldo = requisito.obligatorio and not respaldado(requisito, oferta)
        resultado = "escalar" if sin_respaldo else "ok"
    elif resultado_cumple is None:
        resultado = "escalar" if requisito.obligatorio else "aviso"
    else:
        resultado = "bloquea" if requisito.obligatorio else "aviso"
    observado = observado_de(requisito, oferta, comercio_id)
    clave = clave_evidencia(requisito)
    if sin_respaldo and requisito.campo == "atributo":
        detalle = (
            f"{nombre_campo(clave)}: solo lo declara el comercio (no hay una fuente "
            "independiente: fabricante, página pública ni catálogo) y es obligatorio "
            f"({_pedido(requisito)})."
        )
    elif sin_respaldo:
        detalle = (
            f"{nombre_campo(requisito.campo)}: solo lo declara el comercio (no hay "
            f"dato del catálogo público) y es obligatorio ({formatear(requisito.valor)})."
        )
    else:
        detalle = _detalle_requisito(
            requisito, oferta, _mostrable(requisito, oferta, observado),
            resultado_cumple, tipo,
        )
    es_atributo = requisito.campo == "atributo" and not _es_gtin(requisito)
    return Chequeo(
        tipo=tipo,
        resultado=resultado,
        detalle=detalle,
        campo=clave if requisito.campo == "atributo" else requisito.campo,
        esperado=requisito.valor,
        observado=acotar_atributo(observado) if es_atributo else acotar(observado),
        origen=origen_del_campo(clave if requisito.campo == "atributo" else requisito.campo, oferta),
    )


def _mostrable(requisito: Requisito, oferta: Oferta, observado: Any) -> Any:
    """El valor observado solo si se puede escribir en un `detalle`; si no, None.

    Se puede: el dato de una fuente independiente (catálogo público, página
    pública, fabricante, comprador), el comercio (lo da quien llama), los
    talles (enteros), los montos, una moneda ISO, un GTIN validado y, de un
    atributo declarado, un booleano o un número con una unidad conocida. Lo
    que declara el comercio como texto va solo en `observado`, acotado.
    """
    campo = requisito.campo
    if observado is None:
        return None
    if campo in ("comercio", "precio", "talle", *CAMPOS_COMPROMISO):
        return observado  # enteros y fechas ya validados por el extractor
    if campo == "moneda":
        return moneda_legible(observado) or None
    if _es_gtin(requisito):
        return observado  # 14 dígitos con verificador válido
    independiente = origen_del_campo(clave_evidencia(requisito), oferta) in ORIGENES_INDEPENDIENTES
    if campo == "atributo":
        return observado if independiente or mostrable_declarado(observado) else None
    return observado if independiente else None


def origen_del_campo(campo: str, oferta: Oferta) -> Origen | None:
    """Qué fuente usó el chequeo para este campo (o clave de evidencia)."""
    if campo in ("precio", "moneda"):
        return "cart_ap2"
    if campo == "comercio":
        return None
    evidencia = oferta.evidencia.get(ATRIBUTO_POR_CAMPO.get(campo, campo))
    return evidencia.origen_usado if evidencia else "declarado"


# ---------------------------------------------------------------------------
# Evidencia
# ---------------------------------------------------------------------------


def chequeos_evidencia(oferta: Oferta, fixtures: frozenset[str] = frozenset()) -> list[Chequeo]:
    """Un chequeo por campo verificable de la oferta (ya verificada).

    `fixtures`: claves de evidencia cuyo dato salió de una fuente rotulada
    como fixture (datos de prueba); el `detalle` lo dice.
    """
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
                detalle=_detalle_evidencia(
                    evidencia, oferta.moneda, fixture=evidencia.campo in fixtures
                ),
                campo=evidencia.campo,
                esperado=evidencia.publico,
                observado=evidencia.declarado,
                origen=evidencia.origen_usado,
            )
        )
    return chequeos


def _detalle_evidencia(evidencia: Evidencia, moneda: str, fixture: bool = False) -> str:
    """Sin lo que declara el comercio: solo el nombre, el dato de la fuente y montos."""
    nombre = nombre_campo(evidencia.campo)
    if evidencia.campo == "precio":
        cobrado = monto(evidencia.declarado, moneda)
        publico = monto(evidencia.publico, moneda)
        if evidencia.estado == "contradicho":
            return f"Precio: se cobra {cobrado} pero el catálogo público dice {publico}."
        if evidencia.estado == "solo_declarado":
            return f"Precio {cobrado}: no está en el catálogo público para compararlo."
        return f"Precio {cobrado} coincide con el catálogo público."

    if evidencia.campo.startswith(PREFIJO_ATRIBUTO):
        publico = formatear_atributo(evidencia.publico)
    else:
        publico = formatear(evidencia.publico)
    fuente = nombre_fuente(evidencia.origen_usado, fixture)
    if evidencia.estado == "contradicho":
        return f"{nombre}: lo que declara el comercio no coincide con {fuente} ({publico})."
    if evidencia.estado == "solo_declarado":
        if evidencia.declarado is None:
            return (
                f"{nombre}: el comercio no lo declaró (no se completa con el "
                "catálogo público)."
            )
        return (
            f"{nombre}: solo lo declara el comercio, no hay fuente independiente "
            "para verificarlo."
        )
    return f"{nombre} {publico}: coincide con {fuente}."


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
        # Sin saber quién vende no se puede aprobar: escalar (con la lista
        # vacía tampoco, porque ahí ningún comercio identificado pasa).
        resultado = "escalar"
        detalle = (
            "Comercio no identificado: no se puede confirmar que esté entre "
            "los aprobados por la política."
        )
    elif contiene(aprobados, comercio_id):
        resultado = "ok"
        detalle = f"Comercio {formatear(comercio_id)} aprobado por la política."
    else:
        resultado = "bloquea"
        detalle = (
            f"Comercio {formatear(comercio_id)} fuera de los aprobados por la "
            f"política ({formatear(list(aprobados))})."
        )
    return Chequeo(
        tipo="politica",
        resultado=resultado,
        detalle=detalle,
        campo="comercio",
        esperado=list(aprobados),
        observado=acotar(comercio_id),
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
    """Valor legible (de la ficha o del catálogo público; nunca texto del comercio).

    Listas separadas por comas (hasta 20), floats enteros sin decimales y
    strings sin caracteres de control y acotados.
    """
    if valor is None:
        return "sin dato"
    cronograma = entregas_validas(valor) if isinstance(valor, (list, tuple)) else None
    if cronograma is not None:
        return ", ".join(
            f"{cantidad} a más tardar el {fecha.isoformat()}"
            for fecha, cantidad in cronograma[:ELEMENTOS_MAXIMOS]
        )
    if isinstance(valor, (list, tuple)):
        return ", ".join(formatear(v) for v in list(valor)[:ELEMENTOS_MAXIMOS]) or "ninguno"
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))
    return sin_control(str(valor))[:LARGO_MAXIMO]


def monto(precio: Any, moneda: Any) -> str:
    """"USD 120.00". La moneda solo se muestra si es un código ISO de 3 letras."""
    valor = numero(precio)
    if valor is None:
        return "ilegible"
    return f"{moneda_legible(moneda)} {valor:.2f}".strip()


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
        # "el mandato" -> "del mandato", "el obligatorio" -> "del obligatorio"
        tope = f"d{referencia} ({monto(requisito.valor, oferta.moneda)})"
        cobrado = monto(oferta.precio, oferta.moneda)
        if resultado_cumple is None:
            return f"Precio {cobrado}: no se puede comparar con el tope {tope}."
        if resultado_cumple:
            return f"Precio {cobrado} dentro del tope {tope}."
        return f"Precio {cobrado} supera el tope {tope}."

    # `observado` llega ya filtrado por `_mostrable`: None si es texto del comercio.
    pedido = _pedido(requisito)
    if requisito.campo == "atributo":
        return _detalle_atributo(requisito, observado, resultado_cumple, referencia, pedido)
    if resultado_cumple is None:
        return f"{nombre}: sin dato para comparar con {referencia} ({pedido})."

    if requisito.operador == "incluye":
        ofrecidos = f" (ofrecidos: {formatear(observado)})" if observado is not None else ""
        if resultado_cumple:
            return f"{nombre} {pedido} disponible{ofrecidos}."
        return f"{nombre} {pedido} no disponible{ofrecidos}: no cumple {referencia}."

    if requisito.operador == "cubre":
        ofrecido = formatear(observado)
        if resultado_cumple:
            return f"{nombre} ofrecidas ({ofrecido}) cubren {referencia} ({pedido})."
        return f"{nombre} ofrecidas ({ofrecido}) no cubren {referencia} ({pedido})."

    if observado is None:
        if resultado_cumple:
            return f"{nombre} cumple {referencia} ({pedido})."
        return f"{nombre}: lo que indica el comercio no cumple {referencia} ({pedido})."
    if resultado_cumple:
        return f"{nombre} {formatear(observado)} cumple {referencia} ({pedido})."
    return f"{nombre} {formatear(observado)} no cumple {referencia} ({pedido})."


_OPERADOR_LEGIBLE = {"minimo": "mínimo", "maximo": "máximo", "incluye": "que incluya"}


def _pedido(requisito: Requisito) -> str:
    """Lo pedido, legible: "mínimo 16 GB", "que incluya bluetooth", "sí"."""
    if requisito.campo != "atributo":
        return formatear(requisito.valor)
    if _es_gtin(requisito) or isinstance(requisito.valor, dict):
        valor = formatear(requisito.valor)
    else:
        valor = formatear_atributo(ValorAtributo(valor=requisito.valor, unidad=requisito.unidad))
    operador = _OPERADOR_LEGIBLE.get(requisito.operador)
    return f"{operador} {valor}" if operador else valor


def _detalle_atributo(
    requisito: Requisito,
    observado: Any,
    resultado_cumple: bool | None,
    referencia: str,
    pedido: str,
) -> str:
    """Ej.: "memoria_ram 8 GB no cumple el obligatorio (mínimo 16 GB)"."""
    nombre = nombre_campo(clave_evidencia(requisito))
    if resultado_cumple is None:
        return f"{nombre}: sin dato para comparar con {referencia} ({pedido})."
    if observado is None:
        if resultado_cumple:
            return f"{nombre}: lo que indica el comercio cumple {referencia} ({pedido})."
        return f"{nombre}: lo que indica el comercio no cumple {referencia} ({pedido})."
    valor = observado if isinstance(observado, str) else formatear_atributo(observado)
    if valor in ("sí", "no"):  # "cancelacion_ruido (no) no cumple...", no "no no cumple"
        valor = f"({valor})"
    verbo = "cumple" if resultado_cumple else "no cumple"
    return f"{nombre} {valor} {verbo} {referencia} ({pedido})."
