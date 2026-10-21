"""La explicación de una compra en lenguaje claro (panel del cliente, fase 1).

Funciones puras sobre la `VistaCompra` (`proxy/vistas.py`, con los
veredictos explicables de `proxy/alertas.py`) y los eventos saneados del
historial (`proxy/historial.py`): qué pidió el usuario, qué pasó, si lo
intentaron estafar y por qué KaiAI aprobó o frenó la compra. Sin LLM, sin
I/O, sin entorno y sin reloj (el `ahora` lo pasa quien llama).

**Resultado** (`resultado_de`, el mismo en el listado y en la explicación):

| resultado | cuándo |
|---|---|
| `cortada_por_seguridad` | la compra está `bloqueada`: la cortó el filtro de seguridad |
| `vencida` | nadie decidió a tiempo una alternativa |
| `aprobada` | el último checkout evaluado llegó a la firma (`aprobado`, `con_avisos`, o `escalar` aprobado por una persona) |
| `retenida` | el último quedó en `escalar`, sin aprobación usada |
| `frenada` | el último quedó `bloqueado` (por el Guard, o el proxy no pudo evaluarlo) |
| `sin_ofertas` | sin checkout y la última búsqueda volvió sin ofertas (la marca `sin_ofertas` de la compra) |
| `en_curso` | todavía no hubo un checkout y la compra está viva |
| `sin_compra` | pasó la vida de la compra sin ningún checkout |

**Alertas de la compra:** las de todos sus veredictos y `manipulacion` si el
filtro la cortó por lo que detectó (`corte="manipulacion"`). Un corte porque
el filtro no pudo revisar (`corte="sin_revisar"`) o una compra de antes sin
`corte` (no se sabe por qué) no acusan a nadie.

**Textos:** plantillas nuestras con valores ya saneados (números, montos,
monedas, fechas, unidades y textos cortos de fuentes que el comercio no
controla). Nunca un id del comercio (`cart_id`, `sku`, contexto), el
`detalle` o el `motivo` del Guard, ni el JWS del recibo: un id con forma de
id igual puede traer una orden. Los valores de cada evento se vuelven a
validar acá (una fila del historial es un dato, no una orden).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from typing import Any

from proxy import alertas
from proxy.alertas import (
    ALERTAS,
    ALERTAS_ESTAFA,
    ATRIBUTO_RE,
    MONEDA_RE,
    PREFIJO_ATRIBUTO,
    es_intento_de_estafa,
    ordenar_alertas,
)

RESULTADOS: tuple[str, ...] = (
    "aprobada", "frenada", "retenida", "cortada_por_seguridad", "en_curso", "vencida", "sin_compra",
    "sin_ofertas",
)
ETAPAS = frozenset({
    "pedido", "contexto", "busqueda", "ofertas", "seguridad", "intencion", "checkout",
    "verificacion", "aprobacion", "resultado", "omitido",
})
ESTADOS_ACTIVOS = frozenset({"creada", "esperando_contexto", "negociando", "esperando_decision"})
_GRAVEDAD = {"bloquea": 0, "escalar": 1, "aviso": 2, "ok": 3}
_CHECKOUT = "checkout_firmado"
LARGO_PORQUE = 200
# La línea de tiempo: como mucho `MAX_LINEA` entradas, con el principio
# (`LINEA_PRINCIPIO`) y el final de la compra y una marca de lo omitido. Se
# leen los últimos `MAX_EVENTOS` eventos: si llegan todos, hay más viejos.
MAX_LINEA = 200
LINEA_PRINCIPIO = 50
MAX_EVENTOS = 1000
_RECIBO_RE = re.compile(r"rcb-[0-9a-f]{8,64}")
# Un valor técnico (`basketball_shoes`): en los textos va con espacios.
_ENUM_RE = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)+")
# Una categoría del catálogo en minúsculas ("televisions", "basketball_shoes").
_SLUG_RE = re.compile(r"[a-z0-9]+(?:[_ ][a-z0-9]+)*")
_KID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
_COMERCIO_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")

# -- nombres ---------------------------------------------------------------------

# (texto con artículo, género: m, f, mp, fp).
_NOMBRES: dict[str, tuple[str, str]] = {
    "precio": ("el precio", "m"), "moneda": ("la moneda", "f"),
    "categoria": ("la categoría", "f"), "uso": ("el uso", "m"), "talle": ("el talle", "m"),
    "talles": ("los talles", "mp"), "marca": ("la marca", "f"), "comercio": ("el comercio", "m"),
    "cantidad": ("la cantidad", "f"), "entregas": ("las entregas", "fp"),
    "titulo": ("el título", "m"), "sku": ("el producto", "m"), "gtin": ("el código GTIN", "m"),
    "categoria_taxonomia": ("la categoría del producto", "f"), "monto": ("el monto", "m"),
}
_ATRIBUTOS: dict[str, tuple[str, str]] = {
    "memoria_ram": ("la RAM", "f"), "almacenamiento": ("el almacenamiento", "m"),
    "tamano_pantalla": ("el tamaño de pantalla", "m"), "color": ("el color", "m"),
    "weight": ("el peso", "m"), "peso": ("el peso", "m"),
    "inalambrico": ("la conexión inalámbrica", "f"), "conectividad": ("la conectividad", "f"),
    "display_resolution": ("la resolución de pantalla", "f"),
    "autonomia_bateria": ("la autonomía de la batería", "f"), "power": ("la potencia", "f"),
    "capacity": ("la capacidad", "f"), "cancelacion_ruido": ("la cancelación de ruido", "f"),
    "brightness": ("el brillo", "m"), "colour_printing": ("la impresión a color", "f"),
    "wifi_standard": ("el estándar de wifi", "m"),
    "maximum_video_resolution": ("la resolución máxima de video", "f"),
    "megapixel": ("los megapíxeles", "mp"), "maximum_user_weight": ("el peso máximo admitido", "m"),
    "width": ("el ancho", "m"), "gtin": ("el código GTIN", "m"),
}
_PRONOMBRE = {"m": "lo", "f": "la", "mp": "los", "fp": "las"}
_FUENTES = {
    "catalogo_publico": "el catálogo público", "fabricante": "el fabricante",
    "ficha": "tus datos", "pagina_publica": "la página del comercio",
    "declarado": "lo que declaró el comercio", "cart_ap2": "el carrito",
}
# En el resumen de una compra aprobada, de la que más se reconoce a la que menos.
_ORDEN_FUENTES = ("catalogo_publico", "fabricante", "ficha", "pagina_publica")
_SENALES = {
    "heuristica:instruccion_ia": "instrucciones dirigidas a asistentes de IA",
    "heuristica:anula_instrucciones": "un intento de anular las instrucciones de tu agente",
    "heuristica:marcador_rol": "mensajes que se hacían pasar por el sistema",
    "heuristica:manipulacion": "frases para manipular la elección",
    "heuristica:exfiltracion": "pedidos de datos que el comercio no necesita",
    "heuristica:herramientas": "órdenes para usar herramientas",
    "heuristica:texto_oculto": "texto oculto",
    "heuristica:homoglifos": "caracteres disfrazados",
    "modelo:instruccion_ia": "instrucciones dirigidas a asistentes de IA",
    "modelo:manipulacion": "intentos de manipular la elección",
    "modelo:anula_instrucciones": "un intento de anular las instrucciones de tu agente",
}
_FRASE_ALERTA = {
    "dato_falso": "declaró datos que no coinciden con fuentes independientes",
    "precio_distinto": "quiso cobrar un precio distinto del publicado",
    "checkout_alterado": "mandó un checkout firmado que no coincidía con lo evaluado",
    "manipulacion": "intentó manipular a tu agente",
}


def _nombre(campo: Any) -> tuple[str, str]:
    if isinstance(campo, str) and campo.startswith(PREFIJO_ATRIBUTO):
        atributo = campo[len(PREFIJO_ATRIBUTO):]
        if ATRIBUTO_RE.fullmatch(atributo):
            return _ATRIBUTOS.get(atributo, (f"el atributo {atributo.replace('_', ' ')}", "m"))
    if isinstance(campo, str) and campo in _NOMBRES:
        return _NOMBRES[campo]
    return "un dato", "m"


def _tema(c: dict[str, Any]) -> str:
    campo = c.get("campo")
    if c.get("origen") == _CHECKOUT:
        return "checkout"
    if c.get("tipo") == "integridad":
        return "ofertas" if c.get("caso") == "carrito_ambiguo" else "eleccion"
    if c.get("tipo") == "politica":
        return "politica" if campo == "monto" else "comercio"
    if c.get("caso") == "autorizacion_incompleta":
        return "autorizacion"
    if isinstance(campo, str) and campo.startswith(PREFIJO_ATRIBUTO):
        return campo[len(PREFIJO_ATRIBUTO):]
    if campo == "talles":
        return "talle"
    if campo == "sku":
        return "producto"
    return campo if isinstance(campo, str) else "otro"


def _mayus(texto: str) -> str:
    return texto[:1].upper() + texto[1:]


def _minus(texto: str) -> str:
    return texto[:1].lower() + texto[1:]


def _n(genero: str) -> str:
    """La "n" del plural del verbo: "coincide" / "coinciden"."""
    return "n" if genero.endswith("p") else ""


def _de(nombre: str) -> str:
    """"de" + el nombre, con la contracción: "del color", "de la RAM"."""
    return "del " + nombre[3:] if nombre.startswith("el ") else "de " + nombre


# -- valores -----------------------------------------------------------------------


def _numero(valor: Any) -> float | int | None:
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    try:
        return valor if math.isfinite(float(valor)) else None
    except OverflowError:
        return None


def _cifra(numero: float | int, decimales: int) -> str:
    """Con punto de miles y coma decimal: 17.000, 1.450,00."""
    texto = f"{numero:,.{decimales}f}"
    return texto.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _cantidad(numero: float | int) -> str:
    """Una cantidad sin decimales de más: 20, 13,3, 0,75."""
    if float(numero).is_integer():
        return _cifra(numero, 0)
    return _cifra(round(float(numero), 2), 2).rstrip("0").rstrip(",")


def _monto(valor: Any, moneda: str | None) -> str | None:
    numero = _numero(valor)
    if numero is None:
        return None
    cifra = _cifra(numero, 2)
    return f"{moneda} {cifra}" if moneda else cifra


def _fecha(texto: str) -> str | None:
    try:
        return date.fromisoformat(texto).strftime("%d/%m/%Y")
    except ValueError:
        return None


def _valor(valor: Any) -> str | None:
    """Un valor ya saneado, legible; None si no hay nada que mostrar."""
    if valor is None:
        return None
    if isinstance(valor, bool):
        return "sí" if valor else "no"
    numero = _numero(valor)
    if numero is not None:
        return _cantidad(numero)
    if isinstance(valor, str):
        if alertas.FECHA_RE.fullmatch(valor):
            return _fecha(valor)
        if alertas.FECHA_HORA_RE.fullmatch(valor) or not valor.strip():
            return None
        if _ENUM_RE.fullmatch(valor):
            return valor.replace("_", " ")  # "lifestyle_sneakers" → "lifestyle sneakers"
        return valor
    if isinstance(valor, dict):
        if set(valor) == {"cantidad", "fecha"}:
            cantidad, fecha = _numero(valor.get("cantidad")), valor.get("fecha")
            dia = _fecha(fecha) if isinstance(fecha, str) else None
            return f"{_cantidad(cantidad)} el {dia}" if cantidad is not None and dia else None
        if set(valor) == {"valor", "unidad"}:
            base = _valor(valor.get("valor"))
            unidad = valor.get("unidad")
            if base is None:
                return None
            return f"{base} {unidad}" if isinstance(unidad, str) and unidad else base
        return None
    if isinstance(valor, list):
        partes = [p for p in (_valor(v) for v in valor[:8] if not isinstance(v, list)) if p]
        if not partes:
            return None
        if len(partes) == 1:
            return partes[0]
        return ", ".join(partes[:-1]) + " y " + partes[-1]
    return None


def _valor_de(campo: Any, valor: Any) -> str | None:
    """`_valor` del campo. Una categoría del catálogo en minúsculas va como
    los nombres de la taxonomía ("Televisions", "Basketball Shoes"), igual que
    en la línea de la categoría del producto."""
    texto = _valor(valor)
    if campo == "categoria" and texto and isinstance(valor, str) and _SLUG_RE.fullmatch(valor):
        return " ".join(p[:1].upper() + p[1:] for p in texto.split(" "))
    return texto


def _categoria(valor: Any) -> str | None:
    """"Laptops": el nombre de la taxonomía de Google (el árbol del motor, es
    nuestro); si no está en el árbol (o no hay árbol), el ID."""
    if not isinstance(valor, str) or not alertas.DIGITOS_RE.fullmatch(valor):
        return None
    arbol, _ = alertas._arbol()
    categoria = arbol.get(valor) if arbol is not None else None
    if categoria is None:
        return valor
    nombre = "".join(c for c in categoria.nombre if c.isprintable())[:60].strip()
    return nombre or valor


def _fuente(origen: Any) -> str:
    return _FUENTES.get(origen, "una fuente independiente") if isinstance(origen, str) else "una fuente independiente"


def _independiente(origen: Any) -> bool:
    return origen in alertas.ORIGENES_CONFIABLES or origen == "pagina_publica"


# -- lo pedido ---------------------------------------------------------------------


def _requisito(ficha: Any, campo: Any) -> dict[str, Any] | None:
    """El requisito de la ficha (de la vista) que corresponde al campo de un
    chequeo: para el operador. Los valores salen del chequeo (saneados)."""
    if not isinstance(ficha, dict) or not isinstance(ficha.get("requisitos"), list):
        return None
    for r in ficha["requisitos"]:
        if not isinstance(r, dict):
            continue
        if isinstance(campo, str) and campo.startswith(PREFIJO_ATRIBUTO):
            if r.get("campo") == "atributo" and r.get("atributo") == campo[len(PREFIJO_ATRIBUTO):]:
                return r
        elif r.get("campo") == ("talle" if campo == "talles" else campo):
            return r
    return None


def _pedido(c: dict[str, Any], ficha: Any) -> str | None:
    """Lo pedido, legible: "al menos 16 GB", "como máximo USD 1.500,00", "27 in"."""
    valor = _valor_de(c.get("campo"), c.get("esperado"))
    if valor is None:
        return None
    operador = (_requisito(ficha, c.get("campo")) or {}).get("operador")
    return {
        "minimo": f"al menos {valor}", "maximo": f"como máximo {valor}",
        "incluye": f"que incluya {valor}",
    }.get(operador, valor)


# -- un chequeo ------------------------------------------------------------------


def _texto_checkout(c: dict[str, Any], moneda: str | None) -> str:
    caso, campo = c.get("caso"), c.get("campo")
    esp, obs = c.get("esperado"), c.get("observado")
    if caso == "checkout_coincide":
        return "El checkout firmado coincide con lo que evaluamos."
    if caso == "checkout_ilegible":
        return "No pudimos leer el checkout firmado: no se puede atar a lo evaluado."
    if caso == "checkout_vencido":
        return "El checkout firmado venció antes de la verificación: el comercio tiene que emitir uno nuevo."
    if caso == "checkout_futuro":
        return "El checkout firmado dice haberse emitido en el futuro."
    if caso == "checkout_comercio_ajeno":
        return "El checkout lo firmó un comercio que no está entre los aprobados."
    if caso == "comercio_sin_identificar":
        return "El checkout firmado no identifica al comercio."
    if caso == "checkout_sin_hash":
        return "El comercio no declaró el hash del checkout: no se puede confirmar qué firmarías."
    if caso == "checkout_sin_moneda":
        return "El checkout firmado no indica una moneda legible."
    if caso in ("checkout_sin_cart", "checkout_sin_monto"):
        return "El checkout no trae datos para compararlo con el carrito elegido."
    # checkout_distinto
    if c.get("tipo") == "mandato" and campo == "precio":
        return (f"El total firmado ({_monto(obs, moneda) or 'otro'}) supera el tope que "
                f"autorizaste ({_monto(esp, moneda) or 'el tuyo'}).")
    if c.get("tipo") == "mandato" and campo == "moneda":
        return "La moneda firmada no es la que autorizaste."
    if campo == "checkout_hash":
        return ("El hash del checkout firmado no coincide con el que declaró el comercio, así "
                "que firmarías algo distinto de lo evaluado.")
    if campo == "cart_id":
        return "El checkout firmado es de otro carrito que el que eligió tu agente."
    if campo == "total":
        return (f"El total firmado ({_monto(obs, moneda) or 'otro'}) no coincide con el monto "
                f"evaluado ({_monto(esp, moneda) or 'sin dato'}).")
    if campo in ("amount", "amount_cents"):
        return (f"El monto del checkout ({_monto(obs, moneda) or 'otro'}) no coincide con el "
                f"total firmado ({_monto(esp, moneda) or 'sin dato'}).")
    if campo == "line_items":
        return "Las líneas del checkout firmado no suman el total firmado."
    if campo == "moneda":
        return (f"La moneda firmada ({_valor(obs) or 'otra'}) no coincide con la evaluada "
                f"({_valor(esp) or 'sin dato'}).")
    if campo == "comercio":
        return "El checkout lo firmó un comercio distinto del que verificamos."
    return "El checkout firmado no coincide con lo que evaluamos."


def _texto_precio(c: dict[str, Any], moneda: str | None) -> str | None:
    """Mandato y evidencia del precio y la moneda que se cobran."""
    caso, tipo, campo = c.get("caso"), c.get("tipo"), c.get("campo")
    esp, obs = c.get("esperado"), c.get("observado")
    if tipo == "mandato" and campo == "precio":
        if caso == "cumple":
            return (f"El monto ({_monto(obs, moneda)}) está dentro del tope que autorizaste "
                    f"({_monto(esp, moneda)}).")
        if caso == "no_cumple":
            return (f"El monto ({_monto(obs, moneda) or 'del carrito'}) supera el tope que "
                    f"autorizaste ({_monto(esp, moneda) or 'el tuyo'}).")
        return "No pudimos leer el monto del carrito para compararlo con tu tope."
    if tipo == "mandato" and campo == "moneda":
        if caso == "cumple":
            return f"La moneda ({_valor(obs) or _valor(esp)}) es la que autorizaste."
        if caso == "no_cumple":
            return (f"La moneda del carrito ({_valor(obs) or 'otra'}) no es la que autorizaste "
                    f"({_valor(esp) or 'la tuya'}).")
        return "No pudimos leer la moneda del carrito."
    if tipo == "evidencia" and campo == "precio":
        if caso == "verificado":
            return f"El precio coincide con el publicado ({_monto(esp, moneda)})."
        if caso == "precio_distinto":
            return (f"El precio cobrado ({_monto(obs, moneda) or 'otro'}) no coincide con el "
                    f"publicado ({_monto(esp, moneda) or 'otro'}).")
        return "No pudimos comparar el precio: el producto no está en el catálogo público."
    if tipo == "evidencia" and campo == "moneda":
        if caso == "verificado":
            return "La moneda coincide con la del catálogo público."
        if caso == "precio_distinto":
            return (f"La moneda cobrada ({_valor(obs) or 'otra'}) no coincide con la publicada "
                    f"({_valor(esp) or 'otra'}).")
        return "No pudimos comparar la moneda con el catálogo público."
    return None


def _texto_categoria(c: dict[str, Any]) -> str:
    caso, fuente = c.get("caso"), _fuente(c.get("origen"))
    pedida, ofrecida = _categoria(c.get("esperado")), _categoria(c.get("observado"))
    if caso == "cumple":
        return f"El producto está dentro de la categoría que pediste ({ofrecida or pedida}), según {fuente}."
    if caso == "categoria_solo_declarada":
        return "La categoría del producto está dentro de la que pediste, pero solo la declaró el comercio."
    if caso == "categoria_general":
        return (f"La categoría del producto ({ofrecida}) es más general que la que pediste "
                f"({pedida}): no alcanza para saber si es lo pedido.")
    if caso == "no_cumple":
        return f"El producto es de otra categoría ({ofrecida}) que la que pediste ({pedida}), según {fuente}."
    if caso == "contradicho":
        return (f"El comercio declaró una categoría que no coincide con {fuente}: según esa fuente "
                f"es {ofrecida} y pediste {pedida}.")
    if pedida and ofrecida:
        return (f"No pudimos ubicar la categoría del producto ({ofrecida}) respecto de la que "
                f"pediste ({pedida}).")
    return "No pudimos verificar la categoría del producto."


def _texto_cumplimiento(c: dict[str, Any], ficha: Any, evidencia: dict[str, Any] | None) -> str:
    caso = c.get("caso")
    nombre, genero = _nombre(c.get("campo"))
    pronombre = _PRONOMBRE[genero]
    fuente = _fuente(c.get("origen"))
    pedido = _pedido(c, ficha)
    obs = _valor_de(c.get("campo"), c.get("observado"))
    if caso == "cumple":
        if obs and c.get("origen") != "declarado":
            return f"{_mayus(nombre)} cumple{_n(genero)} lo que pediste ({obs}, según {fuente})."
        return f"{_mayus(nombre)} cumple{_n(genero)} lo que pediste."
    if caso == "no_cumple":
        if pedido and obs:
            return (f"{_mayus(nombre)} no cumple{_n(genero)} lo que pediste: pediste {pedido} y "
                    f"{fuente} indica {obs}.")
        return f"{_mayus(nombre)} no cumple{_n(genero)} lo que pediste, según {fuente}."
    if caso == "contradicho":
        declarado = _valor_de(c.get("campo"), evidencia.get("observado")) if evidencia else None
        declaro = f"el comercio declaró {declarado}" if declarado else "el comercio declaró otra cosa"
        es = "son" if genero.endswith("p") else "es"
        return (f"{_mayus(nombre)} no {es} lo que pediste: según {fuente} {es} {obs or 'otro'} "
                f"({declaro}) y pediste {pedido or 'otra cosa'}.")
    if caso == "solo_declarado":
        return f"No pudimos verificar {nombre}: solo {pronombre} declaró el comercio."
    if caso == "fabricante_sin_atar":
        return (f"No pudimos verificar {nombre}: el dato es de la ficha del fabricante de un código "
                "que declaró el comercio, sin una fuente que lo ate al producto.")
    if caso == "sin_dato":
        return f"No pudimos verificar {nombre}: no hay datos para comparar con lo que pediste."
    if caso == "cantidad_sin_autorizar":
        return (f"La oferta es por {obs or 'varias'} unidades y no autorizaste una cantidad: "
                "comprar más de una no está autorizado.")
    if caso == "preferencia_no_cumple":
        segun = f", según {fuente}" if c.get("origen") != "declarado" else ", según el comercio"
        return (f"{_mayus(nombre)} no cumple{_n(genero)} tu preferencia ({pedido or 'la tuya'})"
                f"{segun}: no frena la compra.")
    if caso == "preferencia_sin_dato":
        return f"No hay datos {_de(nombre)} para comparar con tu preferencia: no frena la compra."
    if caso == "autorizacion_incompleta":
        return ("La autorización no tenía un tope y una moneda obligatorios, o no estaba "
                "confirmada: sin eso no hay contra qué aprobar.")
    return f"{_mayus(nombre)}: lo revisamos."


def _texto_evidencia(c: dict[str, Any]) -> str:
    caso, campo = c.get("caso"), c.get("campo")
    nombre, genero = _nombre(campo)
    pronombre = _PRONOMBRE[genero]
    fuente = _fuente(c.get("origen"))
    if campo == "sku":
        if caso == "verificado":
            return "El producto está en el catálogo público."
        return "El producto no está en el catálogo público: sus datos solo los declaró el comercio."
    if caso == "verificado":
        return f"{_mayus(nombre)} coincide{_n(genero)} con {fuente}."
    if caso == "contradicho":
        # Como la línea de la categoría: "… no coincide con X: según esa fuente es …".
        if campo == "categoria_taxonomia":
            publico = _categoria(c.get("esperado"))
        else:
            publico = _valor_de(campo, c.get("esperado"))
        plural = genero.endswith("p")
        base = (f"{_mayus(nombre)} que declaró el comercio no coincide{'n' if plural else ''} "
                f"con {fuente}")
        if publico:
            return f"{base}: según esa fuente {'son' if plural else 'es'} {publico}."
        return base + "."
    if caso == "no_declarado":
        return f"{_mayus(nombre)}: el comercio no {pronombre} informó."
    if caso == "sin_dato":
        return (f"No pudimos comparar {nombre} con {fuente}: lo que declaró el comercio no se "
                "puede comparar (otra unidad o un valor ambiguo).")
    return (f"{_mayus(nombre)}: solo {pronombre} declaró el comercio; no hay una fuente "
            f"independiente para verificar{pronombre}.")


def _texto_politica(c: dict[str, Any], moneda: str | None) -> str:
    caso = c.get("caso")
    if caso == "monto_ok":
        return "El monto está dentro del máximo sin aprobación."
    if caso == "monto_requiere_aprobacion":
        return (f"El monto ({_monto(c.get('observado'), moneda) or 'del carrito'}) supera el máximo "
                f"sin aprobación ({_monto(c.get('esperado'), moneda) or 'de la política'}): "
                "requiere aprobación.")
    if caso == "comercio_aprobado":
        return "El comercio está entre los aprobados."
    if caso == "comercio_no_aprobado":
        return "El comercio no está entre los aprobados."
    return "No pudimos identificar al comercio."


def _texto_integridad(c: dict[str, Any]) -> str:
    caso = c.get("caso")
    if caso == "eleccion_ok":
        return "Ninguna otra oferta del comercio era igual o mejor en todo que la elegida."
    if caso == "eleccion_mejorable":
        return "Había otra oferta igual o mejor en todo que la que eligió tu agente."
    if caso == "elegido_desconocido":
        return "El carrito que eligió tu agente no estaba entre las ofertas del comercio."
    return "El comercio mandó carritos repetidos o sin identificador."


def texto_chequeo(c: dict[str, Any], *, ficha: Any = None, moneda: str | None = None,
                  evidencia: dict[str, Any] | None = None) -> str:
    """El texto de un chequeo saneado (`alertas.chequeos_detalle`). `evidencia`:
    la evidencia del mismo campo (para decir qué declaró el comercio)."""
    if c.get("origen") == _CHECKOUT:
        return _texto_checkout(c, moneda)
    precio = _texto_precio(c, moneda) if c.get("origen") == "cart_ap2" else None
    if precio is not None:
        return precio
    tipo = c.get("tipo")
    if tipo in ("cumplimiento", "mandato"):
        if c.get("campo") == "categoria_taxonomia":
            return _texto_categoria(c)
        return _texto_cumplimiento(c, ficha, evidencia)
    if tipo == "evidencia":
        return _texto_evidencia(c)
    if tipo == "politica":
        return _texto_politica(c, moneda)
    if tipo == "integridad":
        return _texto_integridad(c)
    return "Un chequeo que no sabemos explicar: lo revisó el Guard."


# Los casos de `alertas.caso_de` que tienen texto propio (`texto_chequeo`).
CASOS_CON_TEXTO = frozenset({
    "cumple", "no_cumple", "contradicho", "solo_declarado", "sin_dato", "fabricante_sin_atar",
    "cantidad_sin_autorizar", "preferencia_no_cumple", "preferencia_sin_dato",
    "categoria_general", "categoria_solo_declarada", "autorizacion_incompleta",
    "verificado", "precio_distinto", "no_declarado",
    "checkout_coincide", "checkout_distinto", "checkout_ilegible", "checkout_comercio_ajeno",
    "checkout_sin_hash", "checkout_sin_moneda", "checkout_sin_cart", "checkout_sin_monto",
    "checkout_vencido", "checkout_futuro", "comercio_sin_identificar",
    "monto_ok", "monto_requiere_aprobacion", "comercio_aprobado", "comercio_no_aprobado",
    "eleccion_ok", "eleccion_mejorable", "elegido_desconocido", "carrito_ambiguo",
})


def _chequeos_de(veredicto: dict[str, Any]) -> list[dict[str, Any]]:
    """Los chequeos saneados de un veredicto guardado. Se vuelven a sanear:
    una fila vieja o rara no trae nada que no pase la lista blanca."""
    crudos = veredicto.get("chequeos_detalle")
    if not isinstance(crudos, list):
        return []
    salida = []
    for c in crudos:
        if not isinstance(c, dict):
            continue
        tipo = c.get("tipo") if c.get("tipo") in alertas.TIPOS else None
        caso = c.get("caso") if c.get("caso") in CASOS_CON_TEXTO else None
        if tipo is None or caso is None or c.get("resultado") not in _GRAVEDAD:
            continue
        campo = c.get("campo")
        if campo is not None and not (
            campo in alertas.CAMPOS_FIJOS
            or (isinstance(campo, str) and campo.startswith(PREFIJO_ATRIBUTO)
                and ATRIBUTO_RE.fullmatch(campo[len(PREFIJO_ATRIBUTO):]))
        ):
            campo = None
        origen = c.get("origen") if c.get("origen") in alertas.ORIGENES else None
        esperado, _ = alertas.valor_saneado(c.get("esperado"), confiable=True)
        observado, _ = alertas.valor_saneado(c.get("observado"), confiable=True)
        salida.append({"tipo": tipo, "resultado": c["resultado"], "caso": caso, "campo": campo,
                       "origen": origen, "esperado": esperado, "observado": observado})
    return salida


def _moneda_de(veredicto: dict[str, Any]) -> str | None:
    moneda = veredicto.get("moneda")
    return moneda if isinstance(moneda, str) and MONEDA_RE.fullmatch(moneda) else None


# La conclusión de un chequeo sobre un campo, para no contar dos veces lo
# mismo: un dato falso o algo que no se pudo verificar.
_CONCLUSION = {
    "contradicho": "contradicho",
    "solo_declarado": "sin_verificar", "no_declarado": "sin_verificar", "sin_dato": "sin_verificar",
    "fabricante_sin_atar": "sin_verificar", "categoria_solo_declarada": "sin_verificar",
}
# La evidencia de un campo que respalda el cumplimiento de otro: la categoría
# que declaró el comercio (la del catálogo) y la de la taxonomía.
_FAMILIA = {"categoria": "categoria_taxonomia"}


def _es_cumplimiento(c: dict[str, Any]) -> bool:
    return c["tipo"] in ("cumplimiento", "mandato") and c["campo"] is not None \
        and c["origen"] not in (_CHECKOUT, "cart_ap2")


def _es_evidencia(c: dict[str, Any]) -> bool:
    return c["tipo"] == "evidencia" and c["campo"] is not None \
        and c["origen"] not in (_CHECKOUT, "cart_ap2")


def _juntar_evidencias(
    chequeos: list[dict[str, Any]],
) -> tuple[set[int], dict[int, str], dict[int, list[str]]]:
    """Las evidencias que ya explica el cumplimiento de su campo (o, para la
    categoría, el de la taxonomía): (las que no van en una línea propia, el
    resultado más grave que toma cada cumplimiento, las otras fuentes que lo
    contradicen). Cada chequeo, por su `id()`.

    - Del mismo campo, tan o menos grave: va en el texto del cumplimiento.
    - Más grave, o de la categoría, con la misma conclusión que un
      cumplimiento que no está bien: va en esa línea, con el resultado más
      grave y, si es un dato falso según otra fuente, esa fuente.
    - Si no, en su línea (dice otra cosa)."""
    cumplimientos: dict[Any, list[dict[str, Any]]] = {}
    for c in chequeos:
        if _es_cumplimiento(c):
            cumplimientos.setdefault(alertas.clave_evidencia(c["campo"]), []).append(c)
    omitidas: set[int] = set()
    resultado: dict[int, str] = {}
    fuentes: dict[int, list[str]] = {}
    for e in chequeos:
        if not _es_evidencia(e):
            continue
        clave = alertas.clave_evidencia(e["campo"])
        familia = clave not in cumplimientos and _FAMILIA.get(clave) in cumplimientos
        if familia:
            clave = _FAMILIA[clave]
        if clave not in cumplimientos:
            continue
        gravedad = _GRAVEDAD[e["resultado"]]
        minima = min(_GRAVEDAD[c["resultado"]] for c in cumplimientos[clave])
        if gravedad >= minima and (not familia or e["resultado"] == "ok"):
            omitidas.add(id(e))
            continue
        conclusion = _CONCLUSION.get(e["caso"])
        iguales = [c for c in cumplimientos[clave] if conclusion is not None
                   and c["resultado"] != "ok" and _CONCLUSION.get(c["caso"]) == conclusion]
        if not iguales:
            continue
        omitidas.add(id(e))
        for c in iguales:
            if gravedad < _GRAVEDAD[resultado.get(id(c), c["resultado"])]:
                resultado[id(c)] = e["resultado"]
            if conclusion == "contradicho" and e["origen"] != c["origen"] \
                    and _independiente(e["origen"]):
                fuentes.setdefault(id(c), []).append(_fuente(e["origen"]))
    return omitidas, resultado, fuentes


def explicar_chequeos(veredicto: dict[str, Any], ficha: Any = None) -> list[dict[str, Any]]:
    """`[{"tema", "resultado", "texto"}]` de un veredicto, del más grave al
    menos. La evidencia que repite lo que explica el cumplimiento de su campo
    va en esa línea (`_juntar_evidencias`); los del checkout firmado, si están
    todos bien, en uno solo."""
    chequeos = _chequeos_de(veredicto)
    moneda = _moneda_de(veredicto)
    evidencias = {
        alertas.clave_evidencia(c["campo"]): c
        for c in chequeos
        if c["tipo"] == "evidencia" and c["campo"] is not None and c["origen"] != "cart_ap2"
    }
    omitidas, mas_grave, otras_fuentes = _juntar_evidencias(chequeos)
    checkout = [c for c in chequeos if c["origen"] == _CHECKOUT]
    salida: list[dict[str, Any]] = []
    vistos: set[str] = set()

    def sumar(tema: str, resultado: str, texto: str) -> None:
        if texto not in vistos:
            vistos.add(texto)
            salida.append({"tema": tema, "resultado": resultado, "texto": texto})

    for c in chequeos:
        if c["origen"] == _CHECKOUT or id(c) in omitidas:
            continue
        clave = alertas.clave_evidencia(c["campo"])
        evidencia = evidencias.get(clave) if c["tipo"] in ("cumplimiento", "mandato") else None
        texto = texto_chequeo(c, ficha=ficha, moneda=moneda, evidencia=evidencia)
        fuentes = otras_fuentes.get(id(c))
        if fuentes:
            texto += f" Tampoco coincide con {_y(list(dict.fromkeys(fuentes)))}."
        sumar(_tema(c), mas_grave.get(id(c), c["resultado"]), texto)
    if checkout and all(c["resultado"] == "ok" for c in checkout):
        sumar("checkout", "ok", "El checkout firmado coincide con lo que evaluamos.")
    else:
        for c in checkout:
            if c["resultado"] != "ok":
                sumar("checkout", c["resultado"], texto_chequeo(c, ficha=ficha, moneda=moneda))
    salida.sort(key=lambda item: _GRAVEDAD[item["resultado"]])
    return salida


# -- la compra ---------------------------------------------------------------------


def _fecha_hora(valor: Any) -> datetime | None:
    if isinstance(valor, datetime):
        fecha = valor
    elif isinstance(valor, str):
        try:
            fecha = datetime.fromisoformat(valor)
        except ValueError:
            return None
    else:
        return None
    return fecha if fecha.tzinfo is not None else fecha.replace(tzinfo=timezone.utc)


def _veredictos(vista: dict[str, Any]) -> list[dict[str, Any]]:
    veredictos = vista.get("veredictos")
    return [v for v in veredictos if isinstance(v, dict)] if isinstance(veredictos, list) else []


def _ultimo(vista: dict[str, Any], bloqueo_proxy: Any) -> dict[str, Any] | None:
    """El último checkout que decidió algo: el último veredicto o, si es
    posterior, el bloqueo del proxy (`{"ts", ...}`, sin veredicto)."""
    veredictos = _veredictos(vista)
    ultimo = veredictos[-1] if veredictos else None
    if isinstance(bloqueo_proxy, dict):
        ts_bloqueo = _fecha_hora(bloqueo_proxy.get("ts"))
        ts_ultimo = _fecha_hora(ultimo.get("ts")) if ultimo else None
        if ts_bloqueo is not None and (ts_ultimo is None or ts_bloqueo > ts_ultimo):
            return {"status": "bloqueado", "proxy": True, "ts": ts_bloqueo.isoformat()}
    return ultimo


def _aprobado_por_persona(vista: dict[str, Any], veredicto: dict[str, Any]) -> bool:
    """Un `escalar` cuyo checkout pasó con una aprobación humana usada (de
    ese contexto y cart)."""
    aprobaciones = vista.get("aprobaciones")
    if not isinstance(aprobaciones, list):
        return False
    return any(
        isinstance(a, dict) and a.get("estado") == "usada"
        and a.get("cart_id") is not None and a.get("cart_id") == veredicto.get("cart_id")
        and a.get("contexto_id") == veredicto.get("contexto_id")
        for a in aprobaciones
    )


def resultado_de(vista: dict[str, Any], *, ahora: datetime, ttl_s: float,
                 corte: str | None = None, bloqueo_proxy: Any = None,
                 sin_ofertas: bool = False) -> str:
    """El resultado de la compra (tabla del módulo). `ttl_s`: la vida de una
    compra (`GATEWAY_COMPRA_TTL_S`). `sin_ofertas`: la marca de la compra (la
    última búsqueda volvió sin ofertas)."""
    estado = vista.get("estado")
    if estado == "bloqueada":
        return "cortada_por_seguridad"
    if estado == "vencida":
        return "vencida"
    ultimo = _ultimo(vista, bloqueo_proxy)
    if ultimo is not None:
        status = ultimo.get("status")
        if status in ("aprobado", "con_avisos"):
            return "aprobada"
        if status == "escalar":
            return "aprobada" if _aprobado_por_persona(vista, ultimo) else "retenida"
        return "frenada"
    if sin_ofertas is True:
        return "sin_ofertas"
    creada = _fecha_hora(vista.get("creada"))
    if creada is not None and (ahora - creada).total_seconds() > ttl_s:
        return "sin_compra"
    return "en_curso"


def alertas_de_compra(vista: dict[str, Any], *, corte: str | None = None) -> list[str]:
    """Las alertas de todos los veredictos de la compra y `manipulacion` si
    el filtro la cortó por lo que detectó."""
    todas: list[Any] = []
    for veredicto in _veredictos(vista):
        if isinstance(veredicto.get("alertas"), list):
            todas.extend(veredicto["alertas"])
    if vista.get("estado") == "bloqueada" and corte == "manipulacion":
        todas.append("manipulacion")
    return ordenar_alertas(todas)


def resumen_de_compra(vista: dict[str, Any], *, ahora: datetime, ttl_s: float,
                      corte: str | None = None, bloqueo_proxy: Any = None,
                      sin_ofertas: bool = False) -> dict[str, Any]:
    """Lo que suma cada compra del listado: `resultado`, `alertas` e
    `intento_de_estafa` (sin leer los eventos)."""
    lista = alertas_de_compra(vista, corte=corte)
    return {
        "resultado": resultado_de(vista, ahora=ahora, ttl_s=ttl_s, corte=corte,
                                  bloqueo_proxy=bloqueo_proxy, sin_ofertas=sin_ofertas),
        "alertas": lista,
        "intento_de_estafa": es_intento_de_estafa(lista),
    }


def monto_de_compra(vista: dict[str, Any], bloqueo_proxy: Any = None) -> tuple[Any, str | None]:
    """El monto y la moneda del último veredicto (None si no hay)."""
    ultimo = _ultimo(vista, bloqueo_proxy)
    if not ultimo or ultimo.get("proxy"):
        return None, None
    return _numero(ultimo.get("monto")), _moneda_de(ultimo)


# -- el resumen en una frase --------------------------------------------------------

_PRIORIDAD_RAZON = {
    "checkout_distinto": 0, "checkout_ilegible": 0, "checkout_comercio_ajeno": 0,
    "contradicho": 1, "precio_distinto": 2, "no_cumple": 3, "cantidad_sin_autorizar": 3,
    "comercio_no_aprobado": 3,
}


def _evidencia_de(veredicto: dict[str, Any], c: dict[str, Any]) -> dict[str, Any] | None:
    """La evidencia del mismo campo que un chequeo de cumplimiento (o None)."""
    if c["tipo"] not in ("cumplimiento", "mandato"):
        return None
    clave = alertas.clave_evidencia(c["campo"])
    return next(
        (e for e in _chequeos_de(veredicto) if e["tipo"] == "evidencia" and e["campo"] == clave),
        None,
    )


def _razon(veredicto: dict[str, Any], ficha: Any, resultado_buscado: str) -> tuple[str, bool] | None:
    """La razón principal de un `bloqueado` o un `escalar`: (texto, es una
    frase que sigue a la anterior con dos puntos). Si no es una frase, es una
    oración entera con su mayúscula (va después de un punto)."""
    chequeos = [c for c in _chequeos_de(veredicto) if c["resultado"] == resultado_buscado]
    if not chequeos:
        return None
    chequeos.sort(key=lambda c: _PRIORIDAD_RAZON.get(c["caso"], 9))
    c = chequeos[0]
    moneda = _moneda_de(veredicto)
    if c["caso"] in ("contradicho", "no_cumple") and c["tipo"] == "cumplimiento"             and c["campo"] != "categoria_taxonomia":
        nombre, _ = _nombre(c["campo"])
        obs, pedido = _valor_de(c["campo"], c.get("observado")), _pedido(c, ficha)
        if obs and pedido:
            es = "son" if _nombre(c["campo"])[1].endswith("p") else "es"
            frase = (f"{nombre} del producto que eligió tu agente {es} {obs} y pediste {pedido}, "
                     f"según {_fuente(c.get('origen'))}")
            evidencia = _evidencia_de(veredicto, c) if c["caso"] == "contradicho" else None
            declarado = _valor_de(c["campo"], evidencia.get("observado")) if evidencia else None
            if declarado:
                frase += f" (el comercio había declarado {declarado})"
            return frase + ".", True
    if c["origen"] == _CHECKOUT and c["caso"] in ("checkout_distinto", "checkout_ilegible",
                                                   "checkout_comercio_ajeno"):
        return "el checkout firmado no coincide con lo que evaluamos.", True
    texto = texto_chequeo(c, ficha=ficha, moneda=moneda, evidencia=_evidencia_de(veredicto, c))
    if c["caso"] in ("precio_distinto", "no_cumple") and ":" not in texto:
        return _minus(texto), True
    return texto, False


def _seguir(base: str, razon: tuple[str, bool] | None) -> str:
    """"Frenamos…: <frase>" o "Frenamos…. <Oración>" (sin dos puntos dobles)."""
    if razon is None:
        return base + "."
    texto, es_frase = razon
    return f"{base}: {texto}" if es_frase else f"{base}. {texto}"


def _fuentes_ok(veredicto: dict[str, Any]) -> list[str]:
    origenes = set()
    for c in _chequeos_de(veredicto):
        if c["tipo"] == "evidencia" and c["resultado"] == "ok" and c["campo"] != "sku":
            origenes.add("catalogo_publico" if c["origen"] == "cart_ap2" else c["origen"])
    return [_FUENTES[o] for o in _ORDEN_FUENTES if o in origenes]


def _y(partes: list[str]) -> str:
    if len(partes) <= 1:
        return "".join(partes)
    return ", ".join(partes[:-1]) + " y " + partes[-1]


def _resumen_aprobada(veredicto: dict[str, Any], vista: dict[str, Any]) -> str:
    chequeos = _chequeos_de(veredicto)
    if veredicto.get("status") == "escalar":
        return _seguir(
            "Aprobada por una persona de tu equipo después de revisar por qué la retuvimos",
            _razon(veredicto, vista.get("ficha"), "escalar"),
        )
    mandato = {c["campo"] for c in chequeos
               if c["tipo"] == "mandato" and c["origen"] == "cart_ap2" and c["resultado"] == "ok"}
    partes = []
    if {"precio", "moneda"} <= mandato:
        partes.append("el monto y la moneda cumplen lo autorizado")
    elif "precio" in mandato:
        partes.append("el monto cumple el tope autorizado")
    fuentes = _fuentes_ok(veredicto)
    if fuentes:
        partes.append(f"los datos coinciden con {_y(fuentes)}")
    elif any(c["tipo"] == "cumplimiento" and c["resultado"] == "ok" for c in chequeos):
        partes.append("cumple lo que pediste")
    cuerpo = " y ".join(partes) if partes else "ningún chequeo objetó la compra"
    if veredicto.get("status") == "con_avisos":
        aviso = _razon(veredicto, vista.get("ficha"), "aviso")
        texto = f"Aprobada con avisos: {cuerpo}."
        return f"{texto} {_mayus(aviso[0])}" if aviso else texto
    return f"Aprobada: {cuerpo}."


def _resumen_en_curso(vista: dict[str, Any]) -> str:
    estado = vista.get("estado")
    if estado == "creada":
        return "La compra está creada: tu agente todavía no buscó en el comercio."
    if estado == "esperando_contexto":
        return "La compra espera que nos cuentes para qué es."
    if estado == "esperando_decision":
        return "La compra espera tu decisión sobre una alternativa que te propusimos."
    return "Tu agente está negociando con el comercio: todavía no pidió un checkout."


def _sin_ofertas(eventos: list[dict[str, Any]]) -> bool:
    ofertas = [e for e in eventos if e["tipo"] == "ofertas_recibidas"]
    return bool(ofertas) and ofertas[-1]["datos"].get("ofertas") == 0


def _resumen(resultado: str, vista: dict[str, Any], ultimo: dict[str, Any] | None,
             corte: str | None, eventos: list[dict[str, Any]]) -> str:
    ficha = vista.get("ficha")
    if resultado == "cortada_por_seguridad":
        if corte == "manipulacion":
            return ("El comercio intentó manipular a tu agente con instrucciones ocultas en sus "
                    "respuestas: cortamos la conversación y no se compró nada.")
        if corte == "sin_revisar":
            return ("No pudimos revisar a tiempo una respuesta del comercio: por seguridad "
                    "cortamos la conversación y no se compró nada.")
        # Una compra de antes de guardar el `corte`: no se sabe por qué.
        return "El filtro de seguridad cortó la conversación con el comercio y no se compró nada."
    if resultado == "vencida":
        return ("Te propusimos una alternativa y nadie la decidió a tiempo: sin tu autorización "
                "no se compró nada.")
    if resultado == "en_curso":
        return _resumen_en_curso(vista)
    if resultado == "sin_ofertas":
        return "El comercio no tuvo ofertas para tu pedido: no hubo compra."
    if resultado == "sin_compra":
        # Una compra de antes de guardar la marca `sin_ofertas`: lo dicen los eventos.
        base = "La compra terminó sin que tu agente pidiera un checkout"
        return f"{base}: el comercio no tuvo ofertas para tu pedido." if _sin_ofertas(eventos) else base + "."
    assert ultimo is not None
    if resultado == "frenada":
        base = "Frenamos la compra antes de que la firmaras"
        if ultimo.get("proxy"):
            return f"{base}: no pudimos evaluar el checkout con seguridad, así que no se presentó para firma."
        return _seguir(base, _razon(ultimo, ficha, "bloquea"))
    if resultado == "retenida":
        base = "Retuvimos el checkout hasta que una persona lo apruebe"
        return _seguir(base, _razon(ultimo, ficha, "escalar"))
    return _resumen_aprobada(ultimo, vista)


def _nota_de_estafa(alertas_compra: list[str], ultimo: dict[str, Any] | None,
                    resultado: str) -> str:
    """Si el comercio intentó algo en un checkout anterior (o en este, y el
    resumen no lo dice), una frase más."""
    if resultado in ("frenada", "cortada_por_seguridad"):
        return ""
    del_ultimo = set(ultimo.get("alertas") or []) if ultimo and not ultimo.get("proxy") else set()
    if resultado == "retenida":
        otras = [a for a in alertas_compra if a in ALERTAS_ESTAFA and a not in del_ultimo]
    else:
        otras = [a for a in alertas_compra if a in ALERTAS_ESTAFA]
    if not otras:
        return ""
    return " Ojo: en esta compra el comercio " + _y([_FRASE_ALERTA[a] for a in otras]) + "."


# -- la línea de tiempo ---------------------------------------------------------------


def _eventos(eventos: Iterable[Any]) -> list[dict[str, Any]]:
    """Los eventos del historial que se pueden leer, en el orden en que se
    guardaron (`orden`: la identidad de `gw_eventos` o el de inserción en
    memoria). No el `ts`: dos eventos del mismo momento, o de dos instancias
    con relojes distintos, no se dan vuelta. El `ts` es para mostrar."""
    salida = []
    for e in eventos:
        if not isinstance(e, dict) or not isinstance(e.get("datos"), dict):
            continue
        ts = _fecha_hora(e.get("ts"))
        if ts is None or not isinstance(e.get("tipo"), str):
            continue
        orden = e.get("orden") if isinstance(e.get("orden"), int) else 0
        salida.append({"ts": ts, "orden": orden, "tipo": e["tipo"], "datos": e["datos"]})
    salida.sort(key=lambda e: e["orden"])
    return salida


def _precio_y_moneda(ficha: Any) -> tuple[float | int | None, str | None]:
    """El precio máximo y la moneda obligatorios de la ficha (cada uno None
    si falta)."""
    if not isinstance(ficha, dict) or not isinstance(ficha.get("requisitos"), list):
        return None, None
    tope = moneda = None
    for r in ficha["requisitos"]:
        if not isinstance(r, dict) or r.get("obligatorio") is not True:
            continue
        if r.get("campo") == "precio" and r.get("operador") == "maximo":
            tope = _numero(r.get("valor"))
        elif r.get("campo") == "moneda" and isinstance(r.get("valor"), str) \
                and MONEDA_RE.fullmatch(r["valor"]):
            moneda = r["valor"]
    return tope, moneda


def _tope(ficha: Any) -> str | None:
    tope, moneda = _precio_y_moneda(ficha)
    return _monto(tope, moneda) if tope is not None else None


def tope_de_ficha(ficha: Any) -> dict[str, Any] | None:
    """El tope que autorizó el usuario, `{"precio", "moneda"}` (el precio
    máximo y la moneda obligatorios de la ficha), o None si falta alguno."""
    tope, moneda = _precio_y_moneda(ficha)
    return {"precio": tope, "moneda": moneda} if tope is not None and moneda is not None else None


def _texto_bloqueado(datos: dict[str, Any]) -> tuple[str, str]:
    etapa, causa = datos.get("etapa"), datos.get("causa")
    if etapa == "intencion" or causa == "sin_decision":
        return "intencion", "Nadie decidió a tiempo la alternativa: no se autorizó y no se compró nada."
    if etapa == "seguridad":
        if causa == "filtro":
            senales = [s for s in datos.get("senales") or [] if isinstance(s, str) and s in _SENALES]
            if senales:
                que = _y(list(dict.fromkeys(_SENALES[s] for s in senales)))
                return "seguridad", (f"El filtro de seguridad cortó la conversación: encontró {que} "
                                     "en la respuesta del comercio.")
            if any(isinstance(s, str) and s.startswith("filtro:") for s in datos.get("senales") or []):
                return "seguridad", ("No pudimos revisar la respuesta del comercio: por seguridad no "
                                     "se la pasamos a tu agente y cortamos la conversación.")
            return "seguridad", ("El filtro de seguridad detectó contenido peligroso en la respuesta "
                                 "del comercio y cortó la conversación.")
        if causa == "filtro_fallo":
            return "seguridad", ("No pudimos revisar la respuesta del comercio: por seguridad no se "
                                 "la pasamos a tu agente y cortamos la conversación.")
        if causa == "ilegible":
            return "seguridad", ("La respuesta del comercio no se pudo leer: por seguridad no se la "
                                 "pasamos a tu agente.")
        if causa == "contexto_ajeno":
            return "seguridad", ("El comercio respondió con el contexto de otra compra: no se lo "
                                 "pasamos a tu agente.")
        if causa == "ya_bloqueada":
            return "seguridad", ("La conversación ya estaba cortada por seguridad: no pasó nada más "
                                 "del comercio.")
        return "seguridad", "Cortamos la conversación con el comercio por seguridad."
    if datos.get("sin_veredicto") is True:
        return "resultado", "No pudimos evaluar el checkout: no se presentó para firma."
    if datos.get("requiere_aprobacion") is True:
        return "resultado", "El checkout quedó retenido hasta que lo apruebe una persona."
    return "resultado", "El checkout no se presentó para firma."


def _cambios(datos: dict[str, Any]) -> str:
    partes = []
    for cambio in datos.get("cambios") or []:
        if not isinstance(cambio, dict):
            continue
        campo = cambio.get("campo")
        if campo not in ("precio", "moneda", "categoria", "uso", "talle", "marca", "comercio",
                         "cantidad", "entregas"):
            continue
        nombre, genero = _nombre(campo)
        pedido = _valor_de(campo, alertas.valor_saneado(cambio.get("pedido"), confiable=True)[0])
        ofrecido = _valor_de(campo, alertas.valor_saneado(cambio.get("ofrecido"), confiable=False)[0])
        if pedido and ofrecido:
            partes.append(f"{nombre} pasa{_n(genero)} de {pedido} a {ofrecido}")
        else:
            partes.append(f"cambia{_n(genero)} {nombre}")
    return _y(partes[:5])


def _texto_evento(evento: dict[str, Any], *, hubo_ofertas: bool = False) -> tuple[str, str] | None:
    """`hubo_ofertas`: la búsqueda de este reenvío trajo ofertas (lo sabe
    `linea_de_tiempo`)."""
    tipo, datos = evento["tipo"], evento["datos"]
    if tipo == "pedido_detectado":
        return "contexto", "Te preguntamos para qué es la compra."
    if tipo == "contexto_registrado":
        if datos.get("vencido") is True:
            return "contexto", "No llegó tu respuesta a tiempo: seguimos con tu pedido literal."
        respuesta = datos.get("respuesta")
        if isinstance(respuesta, str) and respuesta.strip():
            limpio = "".join(c for c in respuesta if c.isprintable())[:LARGO_PORQUE].strip()
            limpio = limpio.replace("«", "\"").replace("»", "\"")
            # Si la cita ya cierra la frase ("…variar.", "¿…?", "…"), no lleva otro punto.
            punto = "" if limpio.endswith((".", "?", "!", "…")) else "."
            return "contexto", f"Nos contaste para qué es: «{limpio}»{punto}"
        return "contexto", "Elegiste no contar para qué es la compra: seguimos con tu pedido literal."
    if tipo == "busqueda_enviada":
        return "busqueda", "Tu agente buscó productos en el comercio."
    if tipo == "ofertas_recibidas":
        n = datos.get("ofertas")
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            return "ofertas", "El comercio mandó ofertas."
        if n == 0:
            return "ofertas", "El comercio no tuvo ofertas para tu pedido."
        return "ofertas", f"El comercio mandó {n} oferta{'s' if n != 1 else ''}."
    if tipo == "oportunidad":
        cambios = _cambios(datos)
        detalle = f": {cambios}" if cambios else ""
        return "intencion", ("Te propusimos una alternativa que no cumple el pedido literal pero sí "
                             f"la necesidad{detalle}.")
    if tipo == "oportunidad_decidida":
        decision = datos.get("decision")
        if decision == "aceptada":
            return "intencion", "Aceptaste la alternativa: los parámetros nuevos pasaron a ser los autorizados."
        if decision == "rechazada":
            return "intencion", "Rechazaste la alternativa: seguimos con tu pedido original."
        return "intencion", "La alternativa venció sin respuesta: no se autorizó."
    if tipo == "checkout_pedido":
        return "checkout", "Tu agente pidió el checkout de un carrito."
    if tipo == "checkout_recibido":
        if datos.get("cart_distinto") is True:
            return "checkout", "El comercio mandó un checkout firmado de otro carrito."
        return "checkout", "El comercio mandó el checkout firmado."
    if tipo == "veredicto":
        status = datos.get("status")
        textos = {
            "aprobado": "Verificamos el checkout: está aprobado.",
            "con_avisos": "Verificamos el checkout: está aprobado, con avisos.",
            "escalar": "Verificamos el checkout: lo retuvimos porque requiere la aprobación de una persona.",
            "bloqueado": "Verificamos el checkout y lo frenamos antes de la firma.",
        }
        return "verificacion", textos.get(status, "Verificamos el checkout.")
    if tipo == "reenviado":
        metodo = datos.get("metodo")
        if metodo == "create_checkout":
            if datos.get("aprobacion_humana") is True:
                return "resultado", "Con la aprobación, le pasamos el checkout a tu agente para que lo firme."
            return "resultado", "Le pasamos el checkout a tu agente para que lo firme."
        if metodo == "find_products":
            if hubo_ofertas:
                return "ofertas", "Le pasamos las ofertas a tu agente."
            return "ofertas", "Le pasamos la respuesta a tu agente."
        return None
    if tipo == "bloqueado":
        return _texto_bloqueado(datos)
    return None


def linea_de_tiempo(vista: dict[str, Any], eventos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`[{"ts", "etapa", "texto"}]`: la creación, cada evento del historial
    que se puede contar (en el orden de `_eventos`) y las aprobaciones de una
    persona (no son eventos: van antes del primer evento posterior a su
    momento)."""
    linea: list[tuple[datetime, str, str]] = []
    creada = _fecha_hora(vista.get("creada"))
    if creada is not None:
        tope = _tope(vista.get("ficha"))
        texto = f"Creaste la compra con un tope de {tope}." if tope else "Creaste la compra."
        linea.append((creada, "pedido", texto))
        if len(eventos) >= MAX_EVENTOS:
            linea.append((creada, "omitido",
                          "Hay pasos anteriores que no mostramos: mostramos los últimos "
                          f"{_cantidad(MAX_EVENTOS)}."))
    aprobaciones = sorted(
        momento
        for aprobacion in vista.get("aprobaciones") or [] if isinstance(aprobacion, dict)
        for momento in [_fecha_hora(aprobacion.get("creada"))] if momento is not None
    )
    texto_aprobacion = "Una persona de tu equipo aprobó el checkout retenido."
    # ¿La búsqueda en curso trajo ofertas? Lo dice su `ofertas_recibidas`; sin
    # él (una búsqueda sin ofertas que cuenten, o el historial ya no guarda más
    # búsquedas) el reenvío dice "la respuesta".
    hubo_ofertas = False
    for evento in eventos:
        tipo = evento["tipo"]
        if tipo == "busqueda_enviada":
            hubo_ofertas = False
        elif tipo == "ofertas_recibidas":
            hubo_ofertas = evento["datos"].get("ofertas") != 0
        contado = _texto_evento(evento, hubo_ofertas=hubo_ofertas)
        if tipo == "reenviado" and evento["datos"].get("metodo") == "find_products":
            hubo_ofertas = False
        if contado is None:
            continue
        while aprobaciones and aprobaciones[0] <= evento["ts"]:
            linea.append((aprobaciones.pop(0), "aprobacion", texto_aprobacion))
        linea.append((evento["ts"], *contado))
    linea.extend((momento, "aprobacion", texto_aprobacion) for momento in aprobaciones)
    if len(linea) > MAX_LINEA:
        final = MAX_LINEA - LINEA_PRINCIPIO - 1
        omitidas = len(linea) - LINEA_PRINCIPIO - final
        marca = (linea[LINEA_PRINCIPIO][0], "omitido",
                 f"Omitimos {_cantidad(omitidas)} pasos del medio de la compra.")
        linea = [*linea[:LINEA_PRINCIPIO], marca, *linea[-final:]]
    return [{"ts": ts.isoformat(), "etapa": etapa, "texto": texto}
            for ts, etapa, texto in linea]


# -- la explicación ---------------------------------------------------------------------


def _recibo(ultimo: dict[str, Any] | None) -> dict[str, Any] | None:
    """El recibo del último veredicto: su id, el kid y si está firmado. El
    JWS no (su payload puede traer valores que declaró el comercio): está en
    `GET /v1/compras/{id}`."""
    if not ultimo or ultimo.get("proxy") or not isinstance(ultimo.get("recibo"), dict):
        return None
    recibo = ultimo["recibo"]
    recibo_id, kid = recibo.get("id"), recibo.get("kid")
    if not (isinstance(recibo_id, str) and _RECIBO_RE.fullmatch(recibo_id)):
        return None
    return {
        "id": recibo_id,
        "kid": kid if isinstance(kid, str) and _KID_RE.fullmatch(kid) else None,
        "firmado": isinstance(recibo.get("jws"), str) and bool(recibo["jws"]),
    }


def explicar(vista: dict[str, Any], eventos: Iterable[Any], *, ahora: datetime, ttl_s: float,
             corte: str | None = None, bloqueo_proxy: Any = None,
             sin_ofertas: bool = False) -> dict[str, Any]:
    """La explicación de la compra (`GET /v1/compras/{id}/explicacion`)."""
    lista_eventos = _eventos(eventos)
    resultado = resultado_de(vista, ahora=ahora, ttl_s=ttl_s, corte=corte,
                             bloqueo_proxy=bloqueo_proxy, sin_ofertas=sin_ofertas)
    lista = alertas_de_compra(vista, corte=corte)
    ultimo = _ultimo(vista, bloqueo_proxy)
    resumen = _resumen(resultado, vista, ultimo, corte, lista_eventos)
    resumen += _nota_de_estafa(lista, ultimo, resultado)
    monto, moneda = monto_de_compra(vista, bloqueo_proxy)
    chequeos = explicar_chequeos(ultimo, vista.get("ficha")) if ultimo and not ultimo.get("proxy") else []
    compra_id = vista.get("compra_id")
    comercio = vista.get("comercio")
    creada = vista.get("creada")
    return {
        "compra_id": compra_id if isinstance(compra_id, str) else None,
        # Los datos de la compra (para mostrarla con un solo pedido): el pedido
        # y la referencia son de la empresa, el comercio es el id de su ruta.
        "pedido": vista.get("pedido") if isinstance(vista.get("pedido"), str) else None,
        "referencia": vista.get("referencia") if isinstance(vista.get("referencia"), str) else None,
        "comercio": comercio if isinstance(comercio, str) and _COMERCIO_RE.fullmatch(comercio) else None,
        "creada": creada if _fecha_hora(creada) is not None else None,
        "tope": tope_de_ficha(vista.get("ficha")),
        "resultado": resultado,
        "intento_de_estafa": es_intento_de_estafa(lista),
        "alertas": lista,
        "resumen": resumen,
        "linea_de_tiempo": linea_de_tiempo(vista, lista_eventos),
        "chequeos": chequeos,
        "recibo": _recibo(ultimo),
        "monto": monto,
        "moneda": moneda,
    }


# -- muchas compras ---------------------------------------------------------------------


def resumir(entradas: Iterable[dict[str, Any]], *, desde: datetime, hasta: datetime) -> dict[str, Any]:
    """`GET /v1/resumen`: cuántas compras por resultado y por alerta, cuántos
    intentos de estafa y, por moneda, el monto frenado (las `frenada`) y el
    retenido (las `retenida`: un `escalar` que aprobó una persona es
    `aprobada` y no cuenta). Cada entrada es la de `resumen_de_compra` con
    `monto` y `moneda` (`monto_de_compra`)."""
    por_resultado = {r: 0 for r in RESULTADOS}
    por_alerta = {a: 0 for a in ALERTAS}
    estafas = 0
    montos: dict[str, dict[str, float]] = {"frenada": {}, "retenida": {}}
    total = 0
    for entrada in entradas:
        total += 1
        resultado = entrada.get("resultado")
        if resultado in por_resultado:
            por_resultado[resultado] += 1
        for alerta in ordenar_alertas(entrada.get("alertas") or []):
            por_alerta[alerta] += 1
        if entrada.get("intento_de_estafa") is True:
            estafas += 1
        monto, moneda = _numero(entrada.get("monto")), entrada.get("moneda")
        if resultado in montos and monto is not None and isinstance(moneda, str) \
                and MONEDA_RE.fullmatch(moneda):
            suma = montos[resultado]
            suma[moneda] = suma.get(moneda, 0.0) + float(monto)
    return {
        "desde": desde.isoformat(),
        "hasta": hasta.isoformat(),
        "compras": total,
        "por_resultado": por_resultado,
        "intentos_de_estafa": estafas,
        "por_alerta": por_alerta,
        "monto_frenado": _por_moneda(montos["frenada"]),
        "monto_retenido": _por_moneda(montos["retenida"]),
    }


def _por_moneda(sumas: dict[str, float]) -> list[dict[str, Any]]:
    return [{"moneda": m, "total": round(t, 2)} for m, t in sorted(sumas.items())]


def ventana_por_defecto(ahora: datetime, dias: int = 30) -> tuple[datetime, datetime]:
    """Los últimos `dias` días hasta `ahora` (el resumen sin fechas)."""
    return ahora - timedelta(days=dias), ahora
