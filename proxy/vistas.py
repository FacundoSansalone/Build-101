"""Vistas de la API `/v1` y de los webhooks: lo que ve el integrador de una compra.

Todo lo que sale por `/v1` o por un webhook pasa por acá (diseño del gateway,
§1.11). La regla es la misma que con el agente del cliente: **nada del texto
del vendedor sale del proxy.** El veredicto ya viene reducido
(`tipos_gateway.vista_veredicto`); acá se arman la compra, lo que está
pendiente, la oportunidad y las aprobaciones, y se filtra cada valor que
pudo escribir el comercio:

- los identificadores (`cart_id`, `sku`, `task_id`…) solo si cumplen
  `ID_SEGURO_RE`; si no, `null`;
- los valores tipados de una oportunidad (`cambios`, `requisitos_propuestos`)
  pasan si son números, bools, fechas o cronogramas de `Entrega`; un texto
  pasa solo si es corto y sin signos raros, y si no, `null` con
  `"omitido": true` en ese elemento (así el integrador sabe que había algo);
- el título de la oportunidad es el público que fija el proxy, recortado y
  sin caracteres de control.

Funciones puras: sin I/O, sin entorno y sin reloj.
"""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import date, datetime
from typing import Any

from contracts import Entrega, Oportunidad, Requisito
from proxy.tipos_gateway import ID_SEGURO_RE, FotoCompra, es_id

# Un texto tipado de la oportunidad pasa solo si es corto y sin signos que
# sirvan para dar órdenes (comillas, dos puntos, saltos de línea…): "Dell",
# "16 GB", "USD", "2026-10-19". Una frase del vendedor no entra.
TEXTO_PUBLICO_RE = re.compile(r"[\w .,/+-]{1,40}")
MONEDA_RE = re.compile(r"[A-Z]{3}")

LARGO_TITULO = 200
LARGO_TEXTO_FIJO = 1000   # pregunta del porqué, motivo y por_que (son nuestros)
MAX_ELEMENTOS_LISTA = 100

ESTADOS_APROBACION = frozenset({"pendiente", "usada"})


# -- valores sueltos ---------------------------------------------------------


def _limpio(texto: Any, largo: int) -> str:
    """Sin caracteres de control ni de formato (incluye los bidi que dan
    vuelta el texto en pantalla), recortado a `largo`."""
    if not isinstance(texto, str):
        return ""
    sin_control = "".join(
        c for c in texto if unicodedata.category(c) not in ("Cc", "Cf")
    )
    return sin_control[:largo]


def _id(valor: Any) -> str | None:
    return valor if es_id(ID_SEGURO_RE, valor) else None


def _moneda(valor: Any) -> str | None:
    return valor if isinstance(valor, str) and MONEDA_RE.fullmatch(valor) else None


def _numero(valor: Any) -> float | int | None:
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    return valor if math.isfinite(valor) else None


def _iso(valor: Any) -> str | None:
    if isinstance(valor, datetime):
        return valor.isoformat()
    return valor if isinstance(valor, str) else None


def _entrega(valor: Any) -> dict[str, Any] | None:
    """Una `Entrega` (o un dict que valida como una) en JSON; si no, None."""
    if isinstance(valor, dict):
        if set(valor) != {"cantidad", "fecha"}:
            return None
        try:
            valor = Entrega.model_validate(valor)
        except ValueError:
            return None
    if isinstance(valor, Entrega):
        return {"cantidad": valor.cantidad, "fecha": valor.fecha.isoformat()}
    return None


def valor_publico(valor: Any) -> tuple[Any, bool]:
    """(valor seguro, omitido).

    Pasan números finitos, bools, fechas, `Entrega` y listas de eso. Un str
    pasa solo si cumple `TEXTO_PUBLICO_RE`. Todo lo demás (texto largo,
    objetos arbitrarios, NaN) sale como None con `omitido=True`: el
    integrador ve que había un valor, pero no el texto del vendedor.
    """
    if valor is None:
        return None, False
    if isinstance(valor, bool):
        return valor, False
    if isinstance(valor, (int, float)):
        numero = _numero(valor)
        return numero, numero is None
    if isinstance(valor, datetime):
        return valor.isoformat(), False
    if isinstance(valor, date):
        return valor.isoformat(), False
    if isinstance(valor, str):
        return (valor, False) if TEXTO_PUBLICO_RE.fullmatch(valor) else (None, True)
    entrega = _entrega(valor)
    if entrega is not None:
        return entrega, False
    if isinstance(valor, (list, tuple)):
        if len(valor) > MAX_ELEMENTOS_LISTA:
            return None, True
        salida: list[Any] = []
        omitido = False
        for elemento in valor:
            if isinstance(elemento, (list, tuple)):
                # Sin listas anidadas: ningún campo de la ficha las usa.
                salida.append(None)
                omitido = True
                continue
            seguro, falta = valor_publico(elemento)
            salida.append(seguro)
            omitido = omitido or falta
        return salida, omitido
    return None, True


def _requisito_publico(requisito: Requisito) -> dict[str, Any]:
    """Un requisito con el valor (y el nombre del atributo y la unidad)
    filtrados: en una oportunidad salen de la oferta del comercio."""
    valor, omitido = valor_publico(requisito.valor)
    salida: dict[str, Any] = {
        "campo": requisito.campo,
        "operador": requisito.operador,
        "valor": valor,
        "obligatorio": requisito.obligatorio,
        "origen": requisito.origen,
    }
    for clave in ("atributo", "unidad"):
        original = getattr(requisito, clave)
        seguro, falta = valor_publico(original) if isinstance(original, str) else (None, False)
        salida[clave] = seguro
        omitido = omitido or falta
    if omitido:
        salida["omitido"] = True
    return salida


# -- piezas de la compra -----------------------------------------------------


def vista_oportunidad(op: Oportunidad) -> dict[str, Any]:
    """La oportunidad sin texto del vendedor (§1.11).

    `titulo` es el título público que fija el proxy (regla del equipo para
    `Oportunidad.titulo`), igual se recorta y se limpia. `por_que` es una
    plantilla nuestra.
    """
    cambios: list[dict[str, Any]] = []
    for cambio in op.cambios:
        pedido, falta_pedido = valor_publico(cambio.pedido)
        ofrecido, falta_ofrecido = valor_publico(cambio.ofrecido)
        item: dict[str, Any] = {"campo": cambio.campo, "pedido": pedido, "ofrecido": ofrecido}
        if falta_pedido or falta_ofrecido:
            item["omitido"] = True
        cambios.append(item)
    return {
        "id": _id(op.id),
        "cart_id": _id(op.cart_id),
        "sku": _id(op.sku),
        "titulo": _limpio(op.titulo, LARGO_TITULO),
        "precio": _numero(op.precio),
        "moneda": _moneda(op.moneda),
        "por_que": _limpio(op.por_que, LARGO_TEXTO_FIJO),
        "cambios": cambios,
        "requisitos_propuestos": [_requisito_publico(r) for r in op.requisitos_propuestos],
    }


def _chequeos(chequeos: Any) -> list[dict[str, Any]]:
    """Solo `tipo` y `resultado`, como en `a2a.veredicto_para_agente`."""
    if not isinstance(chequeos, list):
        return []
    return [
        {"tipo": c.get("tipo"), "resultado": c.get("resultado")}
        for c in chequeos
        if isinstance(c, dict)
    ]


def _aprobacion_pendiente(datos: dict[str, Any]) -> dict[str, Any]:
    centavos = datos.get("centavos")
    centavos = centavos if isinstance(centavos, int) and not isinstance(centavos, bool) else None
    return {
        "tipo": "aprobacion",
        "contexto_id": _id(datos.get("contexto_id")),
        "cart_id": _id(datos.get("cart_id")),
        "monto": centavos / 100 if centavos is not None else None,
        "centavos": centavos,
        # La moneda firmada la escribe el comercio: solo un código ISO.
        "moneda": _moneda(datos.get("moneda")),
        "motivo": _limpio(datos.get("motivo"), LARGO_TEXTO_FIJO),
        "chequeos": _chequeos(datos.get("chequeos")),
    }


def vista_pendiente(foto: FotoCompra) -> dict[str, Any] | None:
    """Lo que la compra espera del integrador (como mucho una cosa a la vez).

    El orden sigue al flujo: primero el porqué, después la oportunidad y
    al final la aprobación de un `escalar`. Las dos primeras dependen del
    estado de la compra (si ya no espera, no hay nada que contestar); la
    aprobación, de que haya un checkout retenido.
    """
    vence = _iso(foto.vence)
    if foto.estado == "esperando_contexto" and foto.pregunta:
        return {
            "tipo": "porque",
            "pregunta": _limpio(foto.pregunta, LARGO_TEXTO_FIJO),
            "vence": vence,
        }
    if foto.estado == "esperando_decision" and foto.oportunidad is not None:
        return {
            "tipo": "oportunidad",
            "vence": vence,
            "oportunidad": vista_oportunidad(foto.oportunidad),
        }
    if isinstance(foto.aprobacion_pendiente, dict):
        return _aprobacion_pendiente(foto.aprobacion_pendiente)
    return None


def vista_aprobacion(datos: dict[str, Any]) -> dict[str, Any]:
    """`Aprobacion.a_json()` con cada campo controlado.

    El `task_id` lo puso el comercio y la moneda viene del JWT firmado por
    él: los dos pasan solo con el formato esperado. `monto` se recalcula de
    los centavos (la fuente es el entero).
    """
    centavos = datos.get("centavos")
    centavos = centavos if isinstance(centavos, int) and not isinstance(centavos, bool) else None
    estado = datos.get("estado")
    return {
        "id": _id(datos.get("id")),
        "contexto_id": _id(datos.get("contexto_id")),
        "cart_id": _id(datos.get("cart_id")),
        "centavos": centavos,
        "monto": centavos / 100 if centavos is not None else None,
        "moneda": _moneda(datos.get("moneda")),
        "creada": _iso(datos.get("creada")),
        "estado": estado if estado in ESTADOS_APROBACION else None,
        "usada": _iso(datos.get("usada")),
        "task_id": _id(datos.get("task_id")),
    }


def _ficha_publica(foto: FotoCompra) -> dict[str, Any] | None:
    """La ficha vigente. Los requisitos que puso el integrador salen tal
    cual (son suyos); los aceptados en una oportunidad salen de la oferta
    del comercio y se filtran como en `vista_oportunidad`.

    Después de aceptar una oportunidad se filtran todos, no solo los
    marcados `aceptado`: la marca la pone el proxy y, si faltara, el texto
    del vendedor no tiene que llegar al integrador igual. Un valor del
    integrador que es seguro sale idéntico."""
    if foto.ficha is None:
        return None
    ficha = foto.ficha.model_dump(mode="json")
    aceptada = foto.decision == "aceptada"
    ficha["requisitos"] = [
        _requisito_publico(r) if aceptada or r.origen == "aceptado" else r.model_dump(mode="json")
        for r in foto.ficha.requisitos
    ]
    return ficha


def vista_compra(foto: FotoCompra, *, a2a_url: str | None) -> dict[str, Any]:
    """`VistaCompra` (§1.5): la misma para el GET y para el cuerpo de los
    webhooks, así un webhook nunca muestra algo que el GET no."""
    return {
        "compra_id": foto.compra_id,
        "referencia": foto.referencia,
        "comercio": foto.comercio_id,
        "a2a_url": a2a_url,
        "estado": foto.estado,
        "creada": foto.creada.isoformat(),
        "actualizada": foto.actualizada.isoformat(),
        "pedido": foto.pedido,
        # Los contextId los manda el agente o el comercio: el proxy corta los
        # que no cumplen el formato, pero acá se vuelve a mirar.
        "contextos": [c for c in foto.contextos if es_id(ID_SEGURO_RE, c)],
        "ficha": _ficha_publica(foto),
        "intencion_activa": foto.intencion_activa,
        "pendiente": vista_pendiente(foto),
        "veredictos": [dict(v) for v in foto.veredictos],
        "aprobaciones": [vista_aprobacion(a) for a in foto.aprobaciones],
    }
