"""Chequeos explicables y alertas del veredicto (panel del cliente, fase 1).

Del `Veredicto` completo del Guard sale lo que se guarda en la compra junto a
la vista reducida (`tipos_gateway.vista_veredicto`), para explicar en
lenguaje claro qué pasó sin leer los eventos:

- `chequeos_detalle`: cada chequeo con `tipo`, `resultado`, `caso` (qué
  pasó, en un código fijo nuestro), `campo`, `origen`, `esperado` y
  `observado`, **saneados**. Nunca el `detalle` del Guard: puede llevar
  valores del catálogo o del comercio.
- `alertas`: qué encontró el Guard, en códigos fijos (tabla abajo).
- `monto` y `moneda` del cart evaluado (los del chequeo de mandato).

**Saneamiento (lista blanca).** De `esperado`/`observado` pasan los números
finitos, los booleanos, las fechas ISO, los códigos de moneda ISO, los
dígitos (GTIN, IDs de la taxonomía), los cronogramas `[{cantidad, fecha}]`,
los atributos `{valor, unidad}` (con una unidad conocida) y listas cortas de
eso. Un texto pasa solo si viene de una fuente que el comercio no controla y
es corto y sin signos (`TEXTO_SEGURO_RE`):

| chequeo | `esperado` | `observado` |
|---|---|---|
| cumplimiento, mandato | la ficha (confiable) | la fuente de `origen` (confiable si es `catalogo_publico`, `fabricante` o `ficha`) |
| evidencia | la fuente de `origen` | lo que declaró el comercio (nunca) |
| política | la del operador (confiable) | el id del comercio (nunca) |
| integridad, checkout firmado | nunca | nunca |

Un `cart_id` con formato seguro pasa en los chequeos de `campo="cart_id"`,
como en el resto de la API. Lo que no pasa sale `null` y el chequeo lleva
`"omitido": true`. El `campo` sale solo si es uno que usa el Guard
(`CAMPOS_FIJOS`) o `atributo:<nombre>` con `ATRIBUTO_RE`.

**Alertas** (solo los chequeos que escalan o bloquean; un `ok` o un `aviso`
nunca alertan):

| Qué pasó (`caso`) | alerta |
|---|---|
| un dato declarado contradice una fuente independiente (`contradicho`) | `dato_falso` |
| un atributo que el Guard marcó contradicho pero no pudo comparar (sin unidad, bits contra bytes, miles ambiguos, otra dimensión: `iguales_atributo` da None) | `sin_verificar` (`sin_dato`) |
| el precio o la moneda que se cobra no coinciden con los publicados (`precio_distinto`) | `precio_distinto` |
| el checkout firmado no coincide con lo evaluado o no se puede leer (`checkout_distinto`, `checkout_ilegible`, `checkout_comercio_ajeno`) | `checkout_alterado` |
| no cumple un obligatorio sin contradicción (`no_cumple`, `cantidad_sin_autorizar`, `comercio_no_aprobado`) | `no_cumple` |
| un obligatorio que no se pudo verificar (`solo_declarado`, `sin_dato`, `fabricante_sin_atar`, `categoria_general`, `checkout_sin_hash`, `checkout_sin_moneda`, `checkout_sin_cart`, `checkout_sin_monto`, `comercio_sin_identificar`, `elegido_desconocido`) | `sin_verificar` |

`manipulacion` (el filtro de seguridad cortó la conversación) no sale de un
veredicto: la agrega la explicación de la compra (`proxy/explicacion.py`).
`manipulacion`, `dato_falso`, `precio_distinto` y `checkout_alterado` son
**intento de estafa**; `no_cumple` y `sin_verificar`, no.

Sin I/O, sin entorno y sin reloj. El Guard se importa recién al usarlo
(unidades y taxonomía): importar la vista no carga el Guard.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable
from datetime import date, datetime
from functools import lru_cache
from typing import Any, get_args

from contracts import Chequeo, Entrega, Origen, ValorAtributo

# -- alertas -----------------------------------------------------------------

ALERTAS_ESTAFA: tuple[str, ...] = ("manipulacion", "dato_falso", "precio_distinto", "checkout_alterado")
ALERTAS: tuple[str, ...] = (*ALERTAS_ESTAFA, "no_cumple", "sin_verificar")

ALERTA_POR_CASO: dict[str, str] = {
    "contradicho": "dato_falso",
    "precio_distinto": "precio_distinto",
    "checkout_distinto": "checkout_alterado",
    "checkout_ilegible": "checkout_alterado",
    "checkout_comercio_ajeno": "checkout_alterado",
    "no_cumple": "no_cumple",
    "cantidad_sin_autorizar": "no_cumple",
    "comercio_no_aprobado": "no_cumple",
    "solo_declarado": "sin_verificar",
    "sin_dato": "sin_verificar",
    "fabricante_sin_atar": "sin_verificar",
    "categoria_general": "sin_verificar",
    "checkout_sin_hash": "sin_verificar",
    "checkout_sin_moneda": "sin_verificar",
    "checkout_sin_cart": "sin_verificar",
    "checkout_sin_monto": "sin_verificar",
    "comercio_sin_identificar": "sin_verificar",
    "elegido_desconocido": "sin_verificar",
}

# Todos los `caso` que puede dar `caso_de` (cada uno tiene su texto en
# `proxy/explicacion.py`).
CASOS = frozenset({
    *ALERTA_POR_CASO,
    "cumple", "verificado", "no_declarado", "preferencia_no_cumple", "preferencia_sin_dato",
    "categoria_solo_declarada", "autorizacion_incompleta", "checkout_coincide",
    "checkout_vencido", "checkout_futuro", "monto_ok", "monto_requiere_aprobacion",
    "comercio_aprobado", "eleccion_ok", "eleccion_mejorable", "carrito_ambiguo",
})

# -- valores conocidos -------------------------------------------------------

TIPOS = frozenset({"cumplimiento", "evidencia", "integridad", "politica", "mandato"})
RESULTADOS = frozenset({"ok", "aviso", "escalar", "bloquea"})
ORIGENES = frozenset(get_args(Origen))
# Fuentes que el comercio no controla: sus textos pueden salir (cortos).
ORIGENES_CONFIABLES = frozenset({"catalogo_publico", "fabricante", "ficha"})
CHECKOUT = "checkout_firmado"

# Los campos que usa el Guard en sus chequeos: los de la ficha
# (`guard.chequeos.CAMPOS_SOPORTADOS`, sin "atributo", que va con el nombre),
# las claves de evidencia, el mandato, la política y el checkout firmado.
CAMPOS_FIJOS = frozenset({
    "precio", "moneda", "categoria", "uso", "talle", "marca", "comercio", "cantidad", "entregas",
    "titulo", "talles", "sku", "gtin", "categoria_taxonomia",
    "ficha", "monto",
    "checkout_jwt", "checkout_hash", "cart_id", "total", "amount", "amount_cents",
    "line_items", "exp", "iat",
})
PREFIJO_ATRIBUTO = "atributo:"  # = guard.verificador.PREFIJO_ATRIBUTO
ATRIBUTO_RE = re.compile(r"[a-z][a-z0-9_]{0,40}")
# Un texto que pasa: corto y sin signos que sirvan para dar órdenes
# (comillas, dos puntos, saltos de línea…). Como `vistas.TEXTO_PUBLICO_RE`.
TEXTO_SEGURO_RE = re.compile(r"[\w .,/+-]{1,40}")
MONEDA_RE = re.compile(r"[A-Z]{3}")
DIGITOS_RE = re.compile(r"[0-9]{1,14}")
FECHA_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
FECHA_HORA_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})?"
)
CART_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")  # = a2a.cart_id_seguro
UNIDAD_RE = re.compile(r"[^\s\x00-\x1f]{1,12}")
MAX_ELEMENTOS = 20
# El detalle de un veredicto, en JSON compacto: 20 veredictos (el máximo de la
# compra) entran holgados en un webhook (1 MB) y en una respuesta de Vercel.
PRESUPUESTO_DETALLE_BYTES = 12_000
_GRAVEDAD = {"bloquea": 0, "escalar": 1, "aviso": 2, "ok": 3}

# Los montos que el checkout firmado compara con lo evaluado.
_MONTOS_CHECKOUT = frozenset({"total", "amount", "amount_cents"})


# -- valores -----------------------------------------------------------------


def _numero(valor: Any) -> float | int | None:
    """Un número finito (no bool) que entra en un float; si no, None."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    try:
        return valor if math.isfinite(float(valor)) else None
    except OverflowError:  # un entero de más de 308 dígitos
        return None


def _fecha_valida(texto: str) -> bool:
    try:
        if FECHA_RE.fullmatch(texto):
            date.fromisoformat(texto)
            return True
        if FECHA_HORA_RE.fullmatch(texto):
            datetime.fromisoformat(texto.replace("Z", "+00:00"))
            return True
    except ValueError:
        return False
    return False


def _texto(valor: str, confiable: bool) -> str | None:
    if MONEDA_RE.fullmatch(valor) or DIGITOS_RE.fullmatch(valor) or _fecha_valida(valor):
        return valor
    if confiable and TEXTO_SEGURO_RE.fullmatch(valor):
        return valor
    return None


@lru_cache(maxsize=1)
def _unidad_conocida():  # pragma: no cover - trivial
    from guard.atributos import unidad_conocida

    return unidad_conocida


def _unidad(valor: Any, confiable: bool) -> tuple[str | None, bool]:
    if valor is None:
        return None, False
    if not isinstance(valor, str) or not UNIDAD_RE.fullmatch(valor):
        return None, True
    if _unidad_conocida()(valor) or (confiable and TEXTO_SEGURO_RE.fullmatch(valor)):
        return valor, False
    return None, True


def _entrega(valor: dict[str, Any]) -> dict[str, Any] | None:
    try:
        entrega = Entrega.model_validate(valor)
    except ValueError:
        return None
    return {"cantidad": entrega.cantidad, "fecha": entrega.fecha.isoformat()}


def valor_saneado(valor: Any, *, confiable: bool) -> tuple[Any, bool]:
    """(valor que puede salir, omitido). Ver la tabla del módulo."""
    if valor is None or isinstance(valor, bool):
        return valor, False
    if isinstance(valor, (int, float)):
        numero = _numero(valor)
        return numero, numero is None
    if isinstance(valor, str):
        texto = _texto(valor, confiable)
        return texto, texto is None
    if isinstance(valor, dict):
        claves = set(valor)
        if claves == {"cantidad", "fecha"}:
            entrega = _entrega(valor)
            return entrega, entrega is None
        if claves == {"valor", "unidad"}:
            interno = valor["valor"]
            if isinstance(interno, (list, tuple, dict)):
                seguro, falta = None, True
            else:
                seguro, falta = valor_saneado(interno, confiable=confiable)
            unidad, falta_unidad = _unidad(valor["unidad"], confiable)
            return {"valor": seguro, "unidad": unidad}, falta or falta_unidad
        return None, True
    if isinstance(valor, (list, tuple)):
        if len(valor) > MAX_ELEMENTOS:
            return None, True
        salida: list[Any] = []
        omitido = False
        for elemento in valor:
            if isinstance(elemento, (list, tuple)):
                salida.append(None)  # sin listas anidadas: ningún chequeo las usa
                omitido = True
                continue
            seguro, falta = valor_saneado(elemento, confiable=confiable)
            salida.append(seguro)
            omitido = omitido or falta
        return salida, omitido
    return None, True


# -- chequeos ------------------------------------------------------------------


def _como_chequeo(crudo: Any) -> tuple[Chequeo, bool] | None:
    """(chequeo, tipo desconocido) de un `Chequeo` o de su JSON; lo que no
    valida queda afuera. Un tipo, resultado u origen que no es de los del
    contrato no se inventa: el tipo desconocido sale en null y sin caso."""
    if isinstance(crudo, Chequeo):
        return crudo, False
    if not isinstance(crudo, dict):
        return None
    tipo, resultado, origen = crudo.get("tipo"), crudo.get("resultado"), crudo.get("origen")
    datos = {
        "tipo": tipo if tipo in TIPOS else "integridad",
        "resultado": resultado if resultado in RESULTADOS else "escalar",
        "detalle": "",
        "campo": crudo.get("campo") if isinstance(crudo.get("campo"), str) else None,
        "esperado": crudo.get("esperado"),
        "observado": crudo.get("observado"),
        "origen": origen if origen in ORIGENES else None,
    }
    try:
        return Chequeo.model_validate(datos), tipo not in TIPOS
    except ValueError:
        return None


def _campo(campo: Any) -> str | None:
    if not isinstance(campo, str):
        return None
    if campo in CAMPOS_FIJOS:
        return campo
    if campo.startswith(PREFIJO_ATRIBUTO) and ATRIBUTO_RE.fullmatch(campo[len(PREFIJO_ATRIBUTO):]):
        return campo
    return None


def clave_evidencia(campo: str | None) -> str | None:
    """La clave de evidencia que respalda un campo de la ficha (`talle` →
    `talles`; el resto, igual), como `guard.chequeos.clave_evidencia`."""
    return "talles" if campo == "talle" else campo


def _confianza(tipo: str | None, origen: str | None) -> tuple[bool, bool]:
    """(¿`esperado` es de una fuente confiable?, ¿`observado`?)."""
    if origen == CHECKOUT:
        return False, False
    if tipo in ("cumplimiento", "mandato"):
        return True, origen in ORIGENES_CONFIABLES
    if tipo == "evidencia":
        return origen in ORIGENES_CONFIABLES, False
    if tipo == "politica":
        return True, False
    return False, False


def _es_cobro(c: Chequeo) -> bool:
    """Precio y moneda de lo que se cobra (`ap2.cart`): su contradicción con
    lo publicado es `precio_distinto`, no un dato declarado falso."""
    return c.campo in ("precio", "moneda") and c.origen == "cart_ap2"


@lru_cache(maxsize=1)
def _iguales_atributo():  # pragma: no cover - trivial
    from guard.atributos import iguales_atributo

    return iguales_atributo


def _atributo_incomparable(c: Chequeo) -> bool:
    """La evidencia de un atributo que el Guard marcó `contradicho` porque no
    la pudo comparar: `guard.verificador` contradice cuando
    `iguales_atributo(declarado, publico)` no da True, también cuando da None
    ("16" sin unidad contra 16 GB, 16 Gb contra 16 GB, "1,500 g"). Se vuelve a
    comparar con los `{valor, unidad}` del chequeo: None no es un dato falso."""
    if c.tipo != "evidencia" or c.resultado != "escalar":
        return False
    if not (isinstance(c.campo, str) and c.campo.startswith(PREFIJO_ATRIBUTO)):
        return False
    publico, declarado = c.esperado, c.observado
    forma = {"valor", "unidad"}
    if not (isinstance(publico, dict) and isinstance(declarado, dict)
            and set(publico) == forma == set(declarado)):
        return False
    try:
        return _iguales_atributo()(ValorAtributo(**declarado), ValorAtributo(**publico)) is None
    except Exception:  # noqa: BLE001 - lo que no se puede volver a comparar no acusa a nadie
        return True


def _contradichos(chequeos: list[Chequeo]) -> frozenset[str]:
    """Claves de evidencia contradichas (la evidencia escala solo así), sin
    las del cobro (un precio sobre el tope sigue siendo "no cumple" aunque
    además no coincida con el publicado: eso ya es `precio_distinto`) y sin
    los atributos que no se pudieron comparar."""
    return frozenset(
        c.campo for c in chequeos
        if c.tipo == "evidencia" and c.resultado == "escalar" and isinstance(c.campo, str)
        and not _es_cobro(c) and not _atributo_incomparable(c)
    )


def _texto_presente(valor: Any) -> bool:
    return isinstance(valor, str) and bool(valor.strip())


def _distintos(a: Any, b: Any) -> bool:
    """Dos valores presentes y distintos (un monto, una moneda, un id)."""
    if a is None or b is None:
        return False
    na, nb = _numero(a), _numero(b)
    if na is not None and nb is not None:
        return abs(na - nb) > 0.005
    return a != b


def _caso_checkout(c: Chequeo) -> str:
    campo, resultado = c.campo, c.resultado
    if resultado == "ok":
        return "checkout_coincide"
    if campo == "checkout_jwt" or (campo in ("exp", "iat") and resultado == "bloquea"):
        return "checkout_ilegible"
    if campo == "exp":
        return "checkout_vencido"
    if campo == "iat":
        return "checkout_futuro"
    if c.tipo == "politica":
        return "checkout_comercio_ajeno" if resultado == "bloquea" else "comercio_sin_identificar"
    if resultado == "bloquea":
        return "checkout_distinto"
    if campo == "checkout_hash":
        return "checkout_sin_hash"
    if campo == "cart_id":
        return "checkout_sin_cart"
    if campo == "moneda":
        return "checkout_distinto" if _texto_presente(c.observado) and _texto_presente(c.esperado) \
            else "checkout_sin_moneda"
    if campo in _MONTOS_CHECKOUT:
        if _numero(c.observado) is None:
            return "checkout_ilegible"  # un monto del data part que no se puede leer
        return "checkout_distinto" if _distintos(c.esperado, c.observado) else "checkout_sin_monto"
    # El comercio que firmó no es el que se verificó, u otra diferencia.
    return "checkout_distinto"


@lru_cache(maxsize=1)
def _arbol():  # pragma: no cover - trivial
    from guard.categorias import ARBOL, id_valido

    return ARBOL, id_valido


def _caso_categoria(c: Chequeo, contradichos: frozenset[str]) -> str:
    """Una categoría de la taxonomía que escala: fuera de la rama (no cumple,
    o dato falso si la fuente contradice lo declarado), más general o sin dato."""
    arbol, id_valido = _arbol()
    pedida, ofrecida = id_valido(c.esperado), id_valido(c.observado)
    if arbol is None or pedida is None or ofrecida is None:
        return "sin_dato"
    if arbol.get(pedida) is None or arbol.get(ofrecida) is None:
        return "sin_dato"
    if arbol.es_descendiente(pedida, ofrecida, incluir_igual=False):
        return "categoria_general"
    if "categoria_taxonomia" in contradichos:
        return "contradicho"
    return "no_cumple"


def caso_de(c: Chequeo, contradichos: frozenset[str] = frozenset()) -> str:
    """Qué pasó en un chequeo, en un código fijo (ver la tabla del módulo).

    Se decide por `tipo`, `resultado`, `campo`, `origen` y el tipo de los
    valores (nunca por el texto del `detalle`)."""
    tipo, resultado, campo, origen = c.tipo, c.resultado, c.campo, c.origen
    if origen == CHECKOUT:
        return _caso_checkout(c)
    if tipo == "evidencia":
        if resultado == "ok":
            return "verificado"
        if resultado == "aviso":
            return "no_declarado" if c.observado is None else "solo_declarado"
        if _es_cobro(c):
            return "precio_distinto"
        return "sin_dato" if _atributo_incomparable(c) else "contradicho"
    if tipo in ("cumplimiento", "mandato"):
        if tipo == "mandato" and origen == "ficha":
            return "autorizacion_incompleta" if resultado != "ok" else "cumple"
        if resultado == "ok":
            return "cumple"
        clave = clave_evidencia(campo)
        if resultado == "bloquea":
            return "contradicho" if clave in contradichos else "no_cumple"
        if resultado == "aviso":
            if campo == "categoria_taxonomia":
                return "categoria_solo_declarada"
            return "preferencia_sin_dato" if c.observado is None else "preferencia_no_cumple"
        # escalar
        if campo == "cantidad" and isinstance(c.observado, int) and not isinstance(c.observado, bool) \
                and c.observado > 1 and c.esperado == 1:
            return "cantidad_sin_autorizar"
        if campo == "categoria_taxonomia":
            return _caso_categoria(c, contradichos)
        if clave in contradichos:
            return "contradicho"
        if c.observado is None:
            return "sin_dato"
        if origen == "fabricante":
            return "fabricante_sin_atar"
        return "solo_declarado"
    if tipo == "politica":
        if campo == "monto":
            return "monto_ok" if resultado == "ok" else "monto_requiere_aprobacion"
        if resultado == "ok":
            return "comercio_aprobado"
        return "comercio_no_aprobado" if resultado == "bloquea" else "comercio_sin_identificar"
    # integridad fuera del checkout: la elección (ranking) y el extractor.
    if origen in ("cart_ap2", "declarado"):
        return "carrito_ambiguo"
    if resultado == "ok":
        return "eleccion_ok"
    if c.esperado is None:
        return "elegido_desconocido"
    return "eleccion_mejorable"


def _chequeos(chequeos: Any) -> list[tuple[Chequeo, bool]]:
    if not isinstance(chequeos, (list, tuple)):
        return []
    return [par for par in (_como_chequeo(crudo) for crudo in chequeos) if par is not None]


def _valores_cart_id(valor: Any) -> tuple[Any, bool]:
    """En un chequeo de `cart_id`: un id con formato seguro (como en toda la
    API) o un número; lo demás, omitido."""
    if isinstance(valor, str):
        seguro = valor if CART_ID_RE.fullmatch(valor) else None
        return seguro, seguro is None
    return valor_saneado(valor, confiable=False)


def _bytes(valor: Any) -> int:
    return len(json.dumps(valor, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _omitir(item: dict[str, Any], tipos: tuple[type, ...]) -> None:
    for clave in ("esperado", "observado"):
        if item[clave] is not None and isinstance(item[clave], tipos):
            item[clave] = None
            item["omitido"] = True


def _dentro_del_presupuesto(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """El detalle de un veredicto en `PRESUPUESTO_DETALLE_BYTES` como mucho: la
    compra guarda hasta 20 veredictos y va entera en cada webhook (1 MB). Si
    no entra, se sacan los valores más pesados (listas, después objetos,
    después todos, con `omitido`) y, al final, los chequeos menos graves. Las
    `alertas` no cambian: salen de los chequeos completos."""
    if _bytes(items) <= PRESUPUESTO_DETALLE_BYTES:
        return items
    for tipos in ((list, tuple), (dict,), (str, int, float, bool)):
        for item in items:
            _omitir(item, tipos)
        if _bytes(items) <= PRESUPUESTO_DETALLE_BYTES:
            return items
    orden = sorted(range(len(items)), key=lambda i: _GRAVEDAD.get(items[i]["resultado"], 9))
    quedan: set[int] = set()
    total = 2  # los corchetes
    for i in orden:
        peso = _bytes(items[i]) + 1
        if total + peso > PRESUPUESTO_DETALLE_BYTES:
            break
        quedan.add(i)
        total += peso
    return [items[i] for i in sorted(quedan)]


def chequeos_detalle(chequeos: Any) -> list[dict[str, Any]]:
    """Los chequeos del veredicto, saneados y con su `caso` (sin `detalle`),
    en `PRESUPUESTO_DETALLE_BYTES` como mucho."""
    pares = _chequeos(chequeos)
    contradichos = _contradichos([c for c, _ in pares])
    salida: list[dict[str, Any]] = []
    for c, desconocido in pares:
        tipo = None if desconocido else c.tipo
        origen = c.origen if c.origen in ORIGENES else None
        campo = _campo(c.campo)
        if campo == "cart_id":
            esperado, falta_e = _valores_cart_id(c.esperado)
            observado, falta_o = _valores_cart_id(c.observado)
        else:
            confia_esperado, confia_observado = _confianza(tipo, origen)
            esperado, falta_e = valor_saneado(c.esperado, confiable=confia_esperado)
            observado, falta_o = valor_saneado(c.observado, confiable=confia_observado)
        item: dict[str, Any] = {
            "tipo": tipo,
            "resultado": c.resultado,
            "caso": None if desconocido else caso_de(c, contradichos),
            "campo": campo,
            "origen": origen,
            "esperado": esperado,
            "observado": observado,
        }
        if (c.campo is not None and campo is None) or falta_e or falta_o:
            item["omitido"] = True
        salida.append(item)
    return _dentro_del_presupuesto(salida)


def alertas_de(chequeos: Any) -> list[str]:
    """Las alertas de un veredicto, sin repetir y en el orden de `ALERTAS`.
    Solo las dan los chequeos que escalan o bloquean."""
    pares = _chequeos(chequeos)
    contradichos = _contradichos([c for c, _ in pares])
    encontradas = {
        ALERTA_POR_CASO[caso]
        for c, desconocido in pares
        if c.resultado in ("escalar", "bloquea")
        and not desconocido
        and (caso := caso_de(c, contradichos)) in ALERTA_POR_CASO
    }
    return [a for a in ALERTAS if a in encontradas]


def ordenar_alertas(alertas: Iterable[Any]) -> list[str]:
    """Las alertas conocidas de `alertas`, sin repetir y en el orden de `ALERTAS`."""
    vistas = {a for a in alertas if isinstance(a, str)}
    return [a for a in ALERTAS if a in vistas]


def es_intento_de_estafa(alertas: Iterable[Any]) -> bool:
    return any(a in ALERTAS_ESTAFA for a in alertas)


def monto_evaluado(chequeos: Any) -> tuple[float | int | None, str | None]:
    """(monto, moneda) del cart evaluado: lo que se cobra (`ap2.cart`), del
    chequeo de mandato de precio y de moneda (no los del checkout firmado ni
    los de la ficha). Un monto que no es un número finito, None; una moneda
    que no es ISO, None."""
    monto: float | int | None = None
    moneda: str | None = None
    for c, desconocido in _chequeos(chequeos):
        if desconocido or c.tipo != "mandato" or c.origen != "cart_ap2":
            continue
        if c.campo == "precio" and monto is None:
            monto = _numero(c.observado)
        elif c.campo == "moneda" and moneda is None:
            moneda = c.observado if isinstance(c.observado, str) and MONEDA_RE.fullmatch(c.observado) else None
    return monto, moneda
