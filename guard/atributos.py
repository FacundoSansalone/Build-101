"""Atributos genéricos del producto: nombres, valores, unidades y comparación.

Sin esquema por categoría: un atributo es un nombre (schema.org, como
`weight` o `color`, o el de `additionalProperty`, como `memoria_ram`) y un
`ValorAtributo` (`valor` + `unidad`). El tipo sale del valor pedido:

- número (`igual`, `maximo`, `minimo`), con unidades normalizadas: datos
  (MB/GB/TB decimales; MiB/GiB/TiB binarios), longitud (mm/cm/m/in), masa
  (g/kg/lb/oz) y tiempo (s/min/h), con los nombres comunes y los códigos
  UN/CEFACT de schema.org (`E34` = GB, `CMT` = cm, `KGM` = kg...). Una unidad
  desconocida solo se compara con la misma unidad;
- enumerado (`igual` con texto, sin mayúsculas ni acentos);
- conjunto (`incluye`: todos los pedidos están en lo ofrecido);
- booleano (`igual` con `true`/`false`; acepta Y/N, sí/no, yes/no).

Si no se puede comparar (falta el dato, dimensiones distintas, unidad de un
solo lado, texto que no es número), el resultado es None: el chequeo escala
el obligatorio. Los nombres se canonicalizan con un vocabulario chico de
sinónimos (incluye los nombres de Icecat): no es un mapa de categorías.
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any

from contracts import ValorAtributo
from guard.normalizar import numero

# ---------------------------------------------------------------------------
# Nombres
# ---------------------------------------------------------------------------

# Sinónimos -> nombre canónico. Vale para lo que declara el comercio, la ficha
# y las fuentes (catálogo, página pública, fabricante).
ALIAS: dict[str, str] = {
    # memoria
    "ram": "memoria_ram",
    "memoria": "memoria_ram",
    "internal_memory": "memoria_ram",
    "memory": "memoria_ram",
    # almacenamiento
    "total_storage_capacity": "almacenamiento",
    "storage": "almacenamiento",
    "storage_capacity": "almacenamiento",
    "capacidad_de_almacenamiento": "almacenamiento",
    "disco": "almacenamiento",
    "ssd": "almacenamiento",
    # pantalla
    "display_diagonal": "tamano_pantalla",
    "screen_size": "tamano_pantalla",
    "tamano_de_pantalla": "tamano_pantalla",
    "pantalla": "tamano_pantalla",
    # schema.org
    "peso": "weight",
    "product_weight": "weight",
    "colour": "color",
    "product_colour": "color",
    "modelo": "model",
    # audio
    "active_noise_cancelling_anc": "cancelacion_ruido",
    "active_noise_cancelling": "cancelacion_ruido",
    "noise_cancelling": "cancelacion_ruido",
    "anc": "cancelacion_ruido",
    "cancelacion_de_ruido": "cancelacion_ruido",
    "battery_life": "autonomia_bateria",
    "autonomia": "autonomia_bateria",
    "duracion_de_bateria": "autonomia_bateria",
    "connectivity_technology": "conectividad",
    "connectivity": "conectividad",
    "wireless": "inalambrico",
    "inalambrica": "inalambrico",
    # identificadores (schema.org)
    "gtin8": "gtin",
    "gtin12": "gtin",
    "gtin13": "gtin",
    "gtin14": "gtin",
    "ean": "gtin",
    "upc": "gtin",
}

LARGO_NOMBRE = 64
MAX_ATRIBUTOS = 200
LARGO_TEXTO = 120
LARGO_UNIDAD = 16
MAX_ELEMENTOS = 20


def _sin_acentos(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c)
    )


def clave_atributo(nombre: str) -> str:
    """Nombre canónico: minúsculas, sin acentos, `_` entre palabras y sinónimos."""
    clave = re.sub(r"[^a-z0-9]+", "_", _sin_acentos(nombre).casefold()).strip("_")
    return ALIAS.get(clave, clave)


# ---------------------------------------------------------------------------
# Valores
# ---------------------------------------------------------------------------


def _escalar_valido(valor: Any) -> bool:
    if isinstance(valor, bool):
        return True
    if isinstance(valor, (int, float)):
        return numero(valor) is not None
    return isinstance(valor, str) and 0 < len(valor.strip()) <= LARGO_TEXTO


def valor_atributo(crudo: Any) -> ValorAtributo | None:
    """`{valor, unidad}` (o schema.org `{value, unitCode|unitText}`), un escalar
    o una lista. Lo que no es válido (vacío, NaN, texto largo, anidado) → None."""
    unidad: Any = None
    if isinstance(crudo, ValorAtributo):
        valor, unidad = crudo.valor, crudo.unidad
    elif isinstance(crudo, dict):
        if "valor" in crudo:
            valor, unidad = crudo.get("valor"), crudo.get("unidad")
        elif "value" in crudo:
            valor = crudo.get("value")
            unidad = crudo.get("unitCode", crudo.get("unitText"))
        else:
            return None
    else:
        valor = crudo
    if unidad is not None:
        if not isinstance(unidad, str) or not 0 < len(unidad.strip()) <= LARGO_UNIDAD:
            return None
        unidad = unidad.strip()
    if isinstance(valor, (list, tuple)):
        if not 0 < len(valor) <= MAX_ELEMENTOS or not all(
            _escalar_valido(v) and not isinstance(v, bool) for v in valor
        ):
            return None
        return ValorAtributo(valor=[v.strip() if isinstance(v, str) else v for v in valor], unidad=unidad)
    if not _escalar_valido(valor):
        return None
    return ValorAtributo(valor=valor.strip() if isinstance(valor, str) else valor, unidad=unidad)


def atributos_validos(crudo: Any) -> dict[str, ValorAtributo]:
    """Nombre canónico -> valor. Un nombre que aparece dos veces (p. ej. "RAM"
    y "memoria_ram") con valores distintos es ambiguo: no se toma ninguno."""
    if not isinstance(crudo, dict):
        return {}
    atributos: dict[str, ValorAtributo] = {}
    ambiguos: set[str] = set()
    for nombre, valor in list(crudo.items())[:MAX_ATRIBUTOS]:
        if not isinstance(nombre, str) or not 0 < len(nombre) <= LARGO_NOMBRE:
            continue
        clave = clave_atributo(nombre)
        valido = valor_atributo(valor)
        if not clave or valido is None:
            continue
        if clave in atributos:
            if iguales_atributo(atributos[clave], valido) is not True:
                ambiguos.add(clave)
            continue
        atributos[clave] = valido
    return {k: v for k, v in atributos.items() if k not in ambiguos}


# ---------------------------------------------------------------------------
# Unidades
# ---------------------------------------------------------------------------

_DIMENSIONES: dict[str, dict[str, float]] = {
    "datos": {  # bytes
        "b": 1, "byte": 1, "bytes": 1, "ad": 1,
        "kb": 1e3, "2p": 1e3, "mb": 1e6, "4l": 1e6, "gb": 1e9, "e34": 1e9, "tb": 1e12, "e35": 1e12,
        "kib": 2**10, "mib": 2**20, "gib": 2**30, "tib": 2**40,
    },
    "longitud": {  # metros
        "mm": 1e-3, "mmt": 1e-3, "cm": 1e-2, "cmt": 1e-2, "m": 1.0, "mtr": 1.0,
        "in": 0.0254, "inh": 0.0254, "inch": 0.0254, "inches": 0.0254, '"': 0.0254,
        "″": 0.0254, "pulgada": 0.0254, "pulgadas": 0.0254, "pulg": 0.0254,
    },
    "masa": {  # kilogramos
        "mg": 1e-6, "mgm": 1e-6, "g": 1e-3, "gr": 1e-3, "grm": 1e-3, "kg": 1.0, "kgm": 1.0,
        "lb": 0.45359237, "lbs": 0.45359237, "lbr": 0.45359237, "oz": 0.028349523125, "onz": 0.028349523125,
    },
    "tiempo": {  # segundos
        "s": 1.0, "sec": 1.0, "seg": 1.0, "min": 60.0,
        "h": 3600.0, "hr": 3600.0, "hrs": 3600.0, "hur": 3600.0, "hora": 3600.0,
        "horas": 3600.0, "hour": 3600.0, "hours": 3600.0,
    },
}
_UNIDADES: dict[str, tuple[str, float]] = {
    unidad: (dimension, factor)
    for dimension, unidades in _DIMENSIONES.items()
    for unidad, factor in unidades.items()
}

# Tolerancia relativa cuando hay que convertir (35,6 cm publicados ≈ 14").
TOLERANCIA_CONVERSION = 0.005
_TOLERANCIA_MISMA_UNIDAD = 1e-9
_NUMERO_CON_UNIDAD = re.compile(r"^\s*(-?\d+(?:[.,]\d+)?)\s*([^\d\s][^\d]{0,15})?\s*$")


def unidad_conocida(unidad: str | None) -> bool:
    return unidad is not None and unidad.strip().casefold().rstrip(".") in _UNIDADES


def _unidad(unidad: str | None) -> tuple[str, float] | None:
    """(dimensión, factor a la unidad base). Una desconocida es su propia dimensión."""
    if unidad is None:
        return None
    clave = unidad.strip().casefold().rstrip(".")
    return _UNIDADES.get(clave, (f"unidad:{clave}", 1.0))


def _cantidad(valor: Any, unidad: str | None) -> tuple[float, tuple[str, float] | None] | None:
    """(número, unidad normalizada) o None si no es un número."""
    if isinstance(valor, bool):
        return None
    como_numero = numero(valor)
    if como_numero is None and isinstance(valor, str) and unidad is None:
        coincidencia = _NUMERO_CON_UNIDAD.match(valor)
        if coincidencia:
            como_numero = float(coincidencia.group(1).replace(",", "."))
            unidad = coincidencia.group(2)
    if como_numero is None or not math.isfinite(como_numero):
        return None
    return como_numero, _unidad(unidad)


def _numeros(
    observado: ValorAtributo, valor: Any, unidad: str | None
) -> tuple[float, float, float] | None:
    """(observado, pedido, tolerancia) en la misma unidad, o None si no se puede."""
    a = _cantidad(observado.valor, observado.unidad)
    b = _cantidad(valor, unidad)
    if a is None or b is None:
        return None
    (na, ua), (nb, ub) = a, b
    if ua is None and ub is None:
        x, y, relativa = na, nb, _TOLERANCIA_MISMA_UNIDAD
    elif ua is None or ub is None or ua[0] != ub[0]:
        return None  # unidad de un solo lado o dimensiones distintas
    else:
        x, y = na * ua[1], nb * ub[1]
        relativa = _TOLERANCIA_MISMA_UNIDAD if ua[1] == ub[1] else TOLERANCIA_CONVERSION
    return x, y, relativa * max(abs(x), abs(y)) + 1e-12


# ---------------------------------------------------------------------------
# Comparación
# ---------------------------------------------------------------------------

_VERDADEROS = {"y", "yes", "si", "true", "verdadero"}
_FALSOS = {"n", "no", "false", "falso"}


def como_booleano(valor: Any) -> bool | None:
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, str):
        texto = _sin_acentos(valor.strip()).casefold()
        if texto in _VERDADEROS:
            return True
        if texto in _FALSOS:
            return False
    return None


def _texto(valor: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _sin_acentos(str(valor)).casefold()).strip()


def _iguales_escalares(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return como_booleano(a) is not None and como_booleano(a) == como_booleano(b)
    na, nb = numero(a), numero(b)
    if na is not None and nb is not None:
        return abs(na - nb) <= _TOLERANCIA_MISMA_UNIDAD * max(abs(na), abs(nb)) + 1e-12
    return _texto(a) == _texto(b)


def _lista(valor: Any) -> list:
    return list(valor) if isinstance(valor, (list, tuple)) else [valor]


def _contiene_todos(elementos: list, pedidos: list) -> bool:
    return all(any(_iguales_escalares(e, p) for e in elementos) for p in pedidos)


def comparar(
    observado: ValorAtributo | None, operador: str, valor: Any, unidad: str | None = None
) -> bool | None:
    """¿El valor observado cumple `operador valor [unidad]`? None = no se puede decir."""
    if observado is None:
        return None
    if operador in ("maximo", "minimo"):
        numeros = _numeros(observado, valor, unidad)
        if numeros is None:
            return None
        x, y, tolerancia = numeros
        return x <= y + tolerancia if operador == "maximo" else x >= y - tolerancia
    if operador == "incluye":
        pedidos = _lista(valor)
        if not pedidos or any(isinstance(p, (dict, list, tuple)) for p in pedidos):
            return None
        return _contiene_todos(_lista(observado.valor), pedidos)
    if operador != "igual":
        return None
    if isinstance(valor, bool):
        observado_bool = como_booleano(observado.valor)
        return None if observado_bool is None else observado_bool == valor
    if isinstance(valor, (list, tuple)):
        elementos = _lista(observado.valor)
        return _contiene_todos(elementos, list(valor)) and _contiene_todos(list(valor), elementos)
    if isinstance(valor, dict):
        return None
    if _cantidad(valor, unidad) is not None:
        numeros = _numeros(observado, valor, unidad)
        if numeros is not None:
            x, y, tolerancia = numeros
            return abs(x - y) <= tolerancia
        if not (isinstance(valor, str) and isinstance(observado.valor, str)):
            return None  # p. ej. 16 contra "16 GB": unidad de un solo lado
        # dos textos ("5G" contra "LTE"): se comparan como enumerados
    if isinstance(observado.valor, bool):
        pedido_bool = como_booleano(valor)
        return None if pedido_bool is None else observado.valor == pedido_bool
    if isinstance(observado.valor, (list, tuple)):
        return _contiene_todos(list(observado.valor), [valor]) and len(observado.valor) == 1
    return _texto(observado.valor) == _texto(valor)


def iguales_atributo(a: ValorAtributo, b: ValorAtributo) -> bool | None:
    """¿Dos valores del mismo atributo coinciden? (evidencia: declarado vs fuente)."""
    ba, bb = como_booleano(a.valor), como_booleano(b.valor)
    if ba is not None and bb is not None:
        return ba == bb
    if isinstance(a.valor, (list, tuple)) or isinstance(b.valor, (list, tuple)):
        la, lb = _lista(a.valor), _lista(b.valor)
        return _contiene_todos(la, lb) and _contiene_todos(lb, la)
    if _cantidad(a.valor, a.unidad) is not None or _cantidad(b.valor, b.unidad) is not None:
        numeros = _numeros(a, b.valor, b.unidad)
        if numeros is None:
            return None
        x, y, tolerancia = numeros
        return abs(x - y) <= tolerancia
    return _texto(a.valor) == _texto(b.valor)


# ---------------------------------------------------------------------------
# Textos
# ---------------------------------------------------------------------------


def _numero_legible(valor: float) -> str:
    return str(int(valor)) if float(valor).is_integer() else f"{valor:g}"


def formatear_atributo(valor: ValorAtributo | dict | None) -> str:
    """"16 GB", "1.5 kg", "sí", "bluetooth, usb-c". Sin caracteres de control."""
    from guard.textos import LARGO_MAXIMO, sin_control  # evita un import circular

    if isinstance(valor, dict):
        valor = valor_atributo(valor)
    if valor is None:
        return "sin dato"
    booleano = como_booleano(valor.valor) if valor.unidad is None else None
    if booleano is not None:  # también "Y"/"N" de Icecat
        texto = "sí" if booleano else "no"
    elif isinstance(valor.valor, (list, tuple)):
        texto = ", ".join(str(v) for v in valor.valor)
    else:
        como_numero = numero(valor.valor)
        texto = _numero_legible(como_numero) if como_numero is not None else str(valor.valor)
    if valor.unidad:
        texto = f"{texto} {valor.unidad}"
    return sin_control(texto)[:LARGO_MAXIMO]


def mostrable_declarado(valor: ValorAtributo | None) -> bool:
    """¿Se puede escribir en un `detalle` lo que declaró el comercio? Solo un
    booleano o un número con una unidad conocida (o sin unidad): nunca texto."""
    if valor is None:
        return False
    if isinstance(valor.valor, bool):
        return valor.unidad is None
    if isinstance(valor.valor, (int, float)) and numero(valor.valor) is not None:
        return valor.unidad is None or unidad_conocida(valor.unidad)
    return False
