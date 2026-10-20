"""Intake: ¿tenemos todo lo necesario para comprar? (etapa 2, docs/FLUJO.md).

Antes de negociar, nuestro agente se asegura de tener los parámetros
**necesarios** para la compra: qué producto (categoría), cuántas unidades,
el tope de precio y su moneda, y para cuándo. Además, los atributos que el
usuario mencionó (marca, talle, memoria…), con si los exige o los prefiere.
Si falta algo, se le vuelve a preguntar, hasta `MAX_REPREGUNTAS` veces.

Patrón "seleccionar, no generar" (juicios tipados con probabilidad,
`decisiones/`):

1. **El código encuentra los candidatos** en las palabras del usuario
   (montos, cantidades, fechas, monedas, marcas, talles…): `extraer()`.
2. **El modelo elige** entre esos candidatos, con una opción "no dicho" en
   cada pregunta: `preguntas()`. Nunca escribe valores ni texto.
3. **El código decide con umbrales** sobre la probabilidad de lo elegido
   (`interpretar()`): resuelto, a confirmar (lo dedujimos o hay dudas) o
   faltante (se repregunta). Los valores salen del candidato, no del modelo.
4. **La siguiente pregunta es una plantilla** con lo que falta: el modelo no
   redacta nada que vea el usuario.

Lo inferido (una sola unidad, la moneda de un "$" suelto) queda `inferido`
y no se autoriza hasta que el usuario confirma la ficha. Todo es código puro
salvo `evaluar()`, que hace una llamada al cliente de decisiones (y la
lectura del subconjunto de la taxonomía, una vez por proceso).

Entre turnos, lo que el usuario dijo y quedó resuelto **no se pierde** porque
una respuesta posterior no lo mencione: solo cambia si lo corrige (ver
`interpretar`). Un tipo general de producto ("computadoras", "cámaras") es
un producto: queda resuelto con su categoría de la taxonomía de Google.
"""

from __future__ import annotations

import calendar
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Literal

from contracts import Requisito, Turno
from decisiones import (
    ClienteDecisiones,
    Eleccion,
    Predicado,
    Pregunta,
    RespuestaEleccion,
    RespuestaPredicado,
)

if TYPE_CHECKING:
    from taxonomia.arbol import Taxonomia

# ---------------------------------------------------------------------------
# Umbrales y límites (calibrados con `python -m intencion.evaluar_intake`)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Umbrales:
    """Sobre la probabilidad de la opción elegida.

    - `>= resuelto` y dicho por el usuario: resuelto.
    - entre `duda` y `resuelto`, o inferido: a confirmar.
    - `< duda` (o "no dicho"): faltante, se repregunta.

    Calibrado con el set (OpenAI Decisions, gpt-6-luna, 2026-10-18): de 0,70
    a 0,85 en `resuelto` da lo mismo (0,80, en el medio). `duda` 0,60 y no
    0,50: con 0,50 pasaba una moneda al aire ("notebooks para el equipo":
    una 0,51 contra no dicha 0,49) y se proponía 1 en vez de preguntar.
    Preguntar de más es más barato que comprar mal.
    """

    resuelto: float = 0.80
    duda: float = 0.60


UMBRALES = Umbrales()
MAX_REPREGUNTAS = 3  # preguntas por lo que falta, después de la primera
MAX_TURNOS = MAX_REPREGUNTAS + 2  # + la primera (el porqué) y la confirmación
MAX_CANDIDATOS = 8  # opciones por pregunta (sin contar "no dicho")
MAX_MENCIONES = 8
MAX_CATEGORIAS = 40
LARGO_TEXTO = 2000  # lo que se le pasa al modelo de cada texto del usuario

NECESARIOS = ("categoria", "cantidad", "precio", "moneda", "plazo")
SIN_APURO = "sin_apuro"  # la opción del plazo sin fecha (y su elección)

EstadoParametro = Literal["resuelto", "a_confirmar", "faltante"]
Siguiente = Literal["porque", "repreguntar", "confirmar", "fin"]
Idioma = Literal["es", "en"]

# ---------------------------------------------------------------------------
# Plantillas (lo único que lee el usuario: nada sale del modelo)
# ---------------------------------------------------------------------------

PORQUE: dict[str, str] = {
    "es": "¿Para qué lo necesitás? ¿Hay algo que pueda variar, como la cantidad, "
    "las fechas o la marca?",
    "en": "What do you need it for? Could anything change, like the quantity, "
    "the dates or the brand?",
}
_FALTAN = {
    "es": "Para hacer la compra me falta saber {lista}.",
    "en": "To make the purchase I still need to know {lista}.",
}
# Desde la segunda repregunta: la anterior no alcanzó, así que se dan ejemplos
# de lo que sí se entiende en vez de repetir la misma pregunta.
_FALTAN_OTRA_VEZ = {
    "es": "No te entendí: todavía me falta saber {lista}. Podés escribirlo así: {ejemplos}.",
    "en": "I didn't get that: I still need to know {lista}. You can write it like this: {ejemplos}.",
}
_FALTAN_INICIAL = {
    "es": "Además, para hacer la compra me falta saber {lista}.",
    "en": "Also, to make the purchase I need to know {lista}.",
}
CONFIRMAR: dict[str, str] = {
    "es": "Armé la ficha de la compra. ¿La confirmás? Si algo está mal, "
    "escribime la corrección.",
    "en": "I put together the order sheet. Do you confirm it? If something is "
    "wrong, write me the correction.",
}
_A_CONFIRMAR = {
    "es": "Esto lo deduje yo o no me quedó claro, revisalo: {lista}.",
    "en": "I inferred these or I'm not sure about them, please check: {lista}.",
}
_CORTADO = {
    "es": "Ya pregunté {n} veces y todavía me falta saber {lista}: sigo sin eso, "
    "y lo que no está en la ficha no se autoriza.",
    "en": "I already asked {n} times and I still don't know {lista}: I'll go on "
    "without it, and nothing outside the sheet is authorized.",
}
# Que no figure en el catálogo público no es un error ni un ataque: el Guard
# verifica lo que declare el comercio con otras fuentes (el fabricante, una
# página pública) y lo obligatorio que no pueda verificar escala al usuario.
_FUERA_DE_CATALOGO = {
    "es": "Lo que pediste no figura en el catálogo público del comercio: antes de la "
    "firma verificamos lo que declare con otras fuentes (como el fabricante), y si "
    "algo de lo que pediste no se puede verificar, te lo consultamos.",
    "en": "What you asked for isn't in the merchant's public catalog: before signing "
    "we check what the merchant declares against other sources (like the "
    "manufacturer), and if something you asked for can't be verified, we ask you.",
}
_PIEZAS = {
    "es": {
        "categoria": "qué producto es exactamente",
        "cantidad": "cuántas unidades necesitás",
        "precio": "el tope de precio (total)",
        "moneda": "en qué moneda",
        "plazo": "para cuándo lo necesitás",
        "precio+moneda": "el tope de precio y la moneda",
    },
    "en": {
        "categoria": "what product exactly",
        "cantidad": "how many units you need",
        "precio": "your maximum budget (total)",
        "moneda": "the currency",
        "plazo": "by when you need it",
        "precio+moneda": "your maximum budget and the currency",
    },
}
_EJEMPLOS = {
    "es": {
        "categoria": "«una notebook»",
        "cantidad": "«2 unidades»",
        "precio": "«hasta 150»",
        "moneda": "«dólares» o «pesos»",
        "plazo": "«el viernes», «30/10» o «esta semana»",
        "precio+moneda": "«hasta 150 dólares»",
    },
    "en": {
        "categoria": "\"a laptop\"",
        "cantidad": "\"2 units\"",
        "precio": "\"up to 150\"",
        "moneda": "\"dollars\" or \"pesos\"",
        "plazo": "\"by Friday\", \"10/30\" or \"this week\"",
        "precio+moneda": "\"up to 150 dollars\"",
    },
}
_ETIQUETAS = {
    "es": {"categoria": "categoría", "cantidad": "cantidad", "precio": "tope",
           "moneda": "moneda", "plazo": "plazo"},
    "en": {"categoria": "category", "cantidad": "quantity", "precio": "budget",
           "moneda": "currency", "plazo": "deadline"},
}
_TEXTOS = {
    "es": {"sin_apuro": "sin apuro", "fuera": "fuera del catálogo público", "dicha": "dicha",
           "por_unidad": "{monto} por unidad"},
    "en": {"sin_apuro": "no rush", "fuera": "outside the public catalog", "dicha": "given",
           "por_unidad": "{monto} per unit"},
}
_DIAS_CORTOS = {
    "es": ("lun", "mar", "mié", "jue", "vie", "sáb", "dom"),
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
}
_DIAS_LARGOS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")


def _unir(items: Sequence[str], idioma: Idioma) -> str:
    conj = " y " if idioma == "es" else " and "
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + conj + items[-1]


def fecha_corta(iso: str, idioma: Idioma = "es") -> str:
    """"2026-10-19" → "lun 19/10" (es) o "Mon 10/19" (en)."""
    try:
        f = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return str(iso)
    dia = _DIAS_CORTOS[idioma][f.weekday()]
    return f"{dia} {f.day:02d}/{f.month:02d}" if idioma == "es" else f"{dia} {f.month:02d}/{f.day:02d}"


def _numero_texto(valor: float) -> str:
    return str(int(valor)) if float(valor).is_integer() else f"{valor:.2f}"


# ---------------------------------------------------------------------------
# 1. Candidatos (código)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidato:
    """Un valor que el código encontró en el texto del usuario."""

    clave: str   # opción de la pregunta ("m0", "n1", "f0")
    valor: Any   # tipado: float (monto), int (cantidad), "YYYY-MM-DD" (fecha)
    frase: str   # lo que dice el texto
    dicho: bool = True
    # Un número de cosas ("20 computadoras", "10 unidades"): nunca es el tope.
    de_cosas: bool = False


@dataclass(frozen=True)
class Mencion:
    """Un atributo del producto que el usuario mencionó (candidato a requisito)."""

    clave: str  # "marca:nike", "talle:42", "atributo:memoria_ram"
    campo: Literal["marca", "talle", "atributo"]
    operador: Literal["igual", "minimo", "incluye"]
    valor: Any
    frase: str
    unidad: str | None = None
    atributo: str | None = None

    def etiqueta(self, idioma: Idioma = "es") -> str:
        """Plantilla con el valor tipado (nunca el texto libre del usuario)."""
        e = _ETIQUETAS_ATRIBUTO[idioma]
        if self.campo == "marca":
            return f"{e['marca']} {self.valor}"
        if self.campo == "talle":
            return f"{e['talle']} {_valor_texto(self.valor)}"
        nombre = e.get(self.atributo or "", self.atributo or "")
        if isinstance(self.valor, bool):
            return nombre
        valor = _valor_texto(self.valor) + (f" {self.unidad}" if self.unidad else "")
        if self.operador == "minimo":
            return f"{nombre} ≥ {valor}"
        return f"{nombre} {valor}"


_ETIQUETAS_ATRIBUTO = {
    "es": {"marca": "marca", "talle": "talle", "memoria_ram": "memoria RAM",
           "almacenamiento": "almacenamiento", "pantalla": "pantalla", "color": "color",
           "inalambrico": "inalámbrico", "cancelacion_ruido": "cancelación de ruido"},
    "en": {"marca": "brand", "talle": "size", "memoria_ram": "RAM",
           "almacenamiento": "storage", "pantalla": "screen", "color": "color",
           "inalambrico": "wireless", "cancelacion_ruido": "noise cancelling"},
}


def _valor_texto(valor: Any) -> str:
    if isinstance(valor, bool):
        return "sí" if valor else "no"
    if isinstance(valor, (int, float)):
        return _numero_texto(float(valor))
    return str(valor)


@dataclass(frozen=True)
class Nombrada:
    """La categoría de la taxonomía de Google que nombra el usuario como lo que
    compra (`taxonomia.palabras.nombradas`, sin modelo)."""

    id: str
    frase: str  # la de la tabla de `taxonomia.palabras` (nuestra): va en las plantillas


@dataclass(frozen=True)
class Candidatos:
    montos: list[Candidato] = field(default_factory=list)
    cantidades: list[Candidato] = field(default_factory=list)
    fechas: list[Candidato] = field(default_factory=list)
    monedas_dichas: dict[str, str] = field(default_factory=dict)  # ISO → frase
    simbolo_ambiguo: bool = False  # "$" sin decir de qué país
    menciones: list[Mencion] = field(default_factory=list)
    idioma: Idioma = "es"
    # La del primer texto que nombra una (el pedido antes que las respuestas),
    # o una de su rama que nombre una respuesta después ("computadoras" →
    # "notebooks"). `mencionadas`: todas las que nombra cualquier texto.
    # `ultima`: la que nombra la última respuesta (si corrige el producto).
    categoria: Nombrada | None = None
    mencionadas: frozenset[str] = frozenset()
    ultima: Nombrada | None = None


def _sin_tildes(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn"
    )


# -- idioma ------------------------------------------------------------------

_PALABRAS_EN = frozenset(
    "the an for by to with and of up me buy need want some get i my under before "
    "delivered please something looking order deliver within until next".split()
)
_PALABRAS_ES = frozenset(
    "el la los las un una unos unas para de con y hasta comprame compra necesito quiero "
    "que mi antes del algo por menos busco entregar lo".split()
)


def _idioma(texto: str) -> Idioma:
    palabras = re.findall(r"[a-záéíóúñü]+", texto.lower())
    en = sum(p in _PALABRAS_EN for p in palabras)
    es = sum(p in _PALABRAS_ES for p in palabras)
    return "en" if en > es else "es"


# -- fechas ------------------------------------------------------------------

_DIAS = {
    "lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3, "viernes": 4, "sabado": 5,
    "domingo": 6, "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11,
    "diciembre": 12, "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_NUMEROS = {
    # es
    "un": 1, "una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6,
    "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12, "quince": 15,
    "veinte": 20, "treinta": 30, "cuarenta": 40, "cincuenta": 50, "cien": 100,
    # en
    "a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "hundred": 100,
}
_MESES_RE = "|".join(sorted(_MESES, key=len, reverse=True))
_DIAS_RE = "|".join(sorted(_DIAS, key=len, reverse=True))
_CANT_RE = r"\d{1,3}|" + "|".join(sorted(_NUMEROS, key=len, reverse=True))

_P_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_P_BARRA = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?![\d/])")
_P_DE_MES = re.compile(rf"\b(\d{{1,2}})\s+de\s+({_MESES_RE})\b(?:\s+(?:de|del)\s+(\d{{4}}))?")
_P_MES_DIA = re.compile(rf"\b({_MESES_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?")
_P_DIA_MES = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MESES_RE})\b")
_P_RELATIVO = re.compile(
    rf"\b(?:en|dentro\s+de|in|within)\s+({_CANT_RE})\s+(dias?|days?|semanas?|weeks?)\b"
)
_P_ESTA_SEMANA = re.compile(r"\b(?:esta\s+(?:misma\s+)?semana|this\s+week)\b")
_P_SEMANA_PROX = re.compile(
    r"\b(?:la\s+semana\s+que\s+viene|la\s+proxima\s+semana|la\s+semana\s+proxima|next\s+week)\b"
)
_P_FIN_MES = re.compile(r"\b(?:fin(?:es|ales)?\s+de(?:l)?\s+mes|end\s+of\s+(?:the\s+)?month)\b")
_P_PASADO = re.compile(r"\b(?:pasado\s+manana|day\s+after\s+tomorrow)\b")
_P_MANANA = re.compile(r"\b(?:manana|tomorrow)\b")
# "por la mañana", "a las 9 de la mañana": una franja del día, no mañana.
_P_FRANJA = re.compile(r"\b(?:por|a|de|en)\s+la\s+$")
_P_HOY = re.compile(r"\b(?:hoy|today)\b")
_P_DIA = re.compile(rf"\b({_DIAS_RE})\b")
# Un día de la semana con el número del día ("el jueves 29", "Monday the 2nd").
_P_DIA_NUMERO = re.compile(
    rf"\b({_DIAS_RE}),?\s+(?:the\s+)?(\d{{1,2}})(?:st|nd|rd|th)?\b(?![/:.,]\d)"
)
# Lo que dice que ese número no es el día: una hora o algo que se cuenta.
_P_NO_ES_DIA = re.compile(r"^\s*(?:hs|h|hrs|horas|am|pm|mas|otros|otras|more)\b")
# El día de la semana que viene ("el miércoles de la semana que viene").
_P_DIA_SEMANA_PROX = re.compile(
    rf"\b({_DIAS_RE})\s+(?:de\s+)?(?:la\s+semana\s+que\s+viene|la\s+proxima\s+semana"
    rf"|la\s+semana\s+proxima|(?:of\s+)?next\s+week)\b"
)
# Entre un día de la semana y su fecha ("el viernes 30 de octubre", "Friday, October 30").
_P_HASTA_LA_FECHA = re.compile(r"[\s,]*(?:el\s+|the\s+)?")


def _fecha_sin_anio(hoy: date, mes: int, dia: int) -> date | None:
    """Sin año: este año; si ya pasó y cae dentro de 6 meses, el que viene."""
    try:
        f = date(hoy.year, mes, dia)
    except ValueError:
        return None
    if f >= hoy:
        return f
    try:
        siguiente = date(hoy.year + 1, mes, dia)
    except ValueError:
        return None
    return siguiente if (siguiente - hoy).days <= 183 else None


def _fecha_con_anio(anio: str | None, mes: int, dia: int, hoy: date) -> date | None:
    if not anio:
        return _fecha_sin_anio(hoy, mes, dia)
    a = int(anio)
    a = a + 2000 if a < 100 else a
    try:
        return date(a, mes, dia)
    except ValueError:
        return None


def _proximo_dia(hoy: date, dia_semana: int) -> date:
    """El próximo día de la semana, nunca hoy: dicho un lunes, "el lunes" es
    el de la semana que viene (como el comercio, `demo_world.consulta`)."""
    return hoy + timedelta(days=((dia_semana - hoy.weekday() - 1) % 7) + 1)


def _dia_con_numero(hoy: date, dia_semana: int, numero: int) -> date | None:
    """"el jueves 29": el primer jueves 29 desde hoy, este mes o el que viene.
    None si no hay (el número no es el día: "el viernes 29" con el 29 jueves)."""
    anio, mes = hoy.year, hoy.month
    for _ in range(2):
        try:
            f = date(anio, mes, numero)
        except ValueError:
            f = None
        if f is not None and f >= hoy and f.weekday() == dia_semana:
            return f
        anio, mes = (anio + 1, 1) if mes == 12 else (anio, mes + 1)
    return None


# Entre dos fechas, lo que corta la secuencia: el fin de una oración o una
# alternativa ("el viernes, si no el miércoles"). "O sea" aclara, no es otra.
_P_CORTA_SECUENCIA = re.compile(
    r"([.;!?\n])(?:\s|$)|\b(?:o(?!\s+sea\b)|u|or|si\s+no|sino|otherwise)\b"
)
# Una oración que sigue la cuenta de la anterior no la corta: "Entran 10 el
# lunes. Los otros 10 entran el jueves."
_P_SIGUE_LA_CUENTA = re.compile(
    rf"\s*(?:los\s+otros|las\s+otras|el\s+resto|los\s+demas|las\s+demas"
    rf"|(?:otros|otras)\s+(?:\d+|{_CANT_RE})|the\s+other|the\s+rest)\b"
)


def _corta_secuencia(texto: str, desde: int, hasta: int) -> bool:
    """¿Entre `desde` y `hasta` termina la oración o hay una alternativa?"""
    for m in _P_CORTA_SECUENCIA.finditer(texto[desde:hasta]):
        if not (m.group(1) and _P_SIGUE_LA_CUENTA.match(texto, desde + m.end())):
            return True
    return False


def _fechas(texto: str, hoy: date, idioma: Idioma) -> list[tuple[tuple[int, int], date | None]]:
    """(span, fecha) en `texto` (ya en minúsculas y sin tildes). Fecha None si
    no es válida o ya pasó: el span igual se marca (sus números no son
    cantidades).

    Un día de la semana es el próximo (`_proximo_dia`), con una regla más: en
    una misma oración, si viene después de otra fecha y caería hasta 6 días
    antes que ella, es el de la semana siguiente. El usuario cuenta en orden:
    dicho un lunes, "10 el lunes y otros 10 el jueves" es el lunes 26 y el
    jueves 29, no el 22, que pondría la segunda tanda antes que la primera.
    No se corre entre oraciones, ante una alternativa ("si no") ni por una
    fecha lejana ("el pago vence el 15/12. Lo necesito el lunes."): ahí queda
    el próximo, del lado estricto (la fecha más temprana). Una oración que
    sigue la cuenta de la anterior ("Los otros 10…", "El resto…") no corta.

    Un día de la semana con su fecha es una sola fecha: "el viernes 30 de
    octubre" no suma el próximo viernes (si el 30 es viernes), "el jueves 29"
    es el primer jueves 29 desde hoy (y el 29 no es una cantidad) y "el
    miércoles de la semana que viene" es el de esa semana, no el próximo.
    "Por la mañana" (o "a la", "de la", "en la") no es mañana.
    """
    hallazgos: list[tuple[tuple[int, int], date | None]] = []
    ocupado: list[tuple[int, int]] = []
    spans_dia: set[tuple[int, int]] = set()  # los de un día de la semana

    def libre(span: tuple[int, int]) -> bool:
        return all(span[1] <= a or span[0] >= b for a, b in ocupado)

    def agregar(m: re.Match, f: date | None) -> bool:
        if libre(m.span()):
            ocupado.append(m.span())
            hallazgos.append((m.span(), f))
            return True
        return False

    for m in _P_ISO.finditer(texto):
        agregar(m, _fecha_con_anio(m.group(1), int(m.group(2)), int(m.group(3)), hoy))
    for m in _P_BARRA.finditer(texto):
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12 or (idioma == "es" and b <= 12):
            dia, mes = a, b
        else:
            mes, dia = a, b
        agregar(m, _fecha_con_anio(m.group(3), mes, dia, hoy) if 1 <= mes <= 12 else None)
    for m in _P_DE_MES.finditer(texto):
        agregar(m, _fecha_con_anio(m.group(3), _MESES[m.group(2)], int(m.group(1)), hoy))
    for m in _P_MES_DIA.finditer(texto):
        agregar(m, _fecha_con_anio(m.group(3), _MESES[m.group(1)], int(m.group(2)), hoy))
    for m in _P_DIA_MES.finditer(texto):
        agregar(m, _fecha_con_anio(None, _MESES[m.group(2)], int(m.group(1)), hoy))
    explicitas = {span[0]: f for span, f in hallazgos}  # dónde empieza cada fecha con número
    for m in _P_DIA_NUMERO.finditer(texto):
        despues = texto[m.end():m.end() + 40]
        if _P_NO_ES_DIA.match(despues) or _de_cosas(despues):
            continue  # "el lunes 10 personas": el número cuenta otra cosa
        f = _dia_con_numero(hoy, _DIAS[m.group(1)], int(m.group(2)))
        if f is not None:
            agregar(m, f)
    for m in _P_DIA_SEMANA_PROX.finditer(texto):
        agregar(m, _proximo_dia(hoy, 0) + timedelta(days=_DIAS[m.group(1)]))
    for m in _P_RELATIVO.finditer(texto):
        n = _a_entero(m.group(1))
        if n is not None and n > 0:
            dias = n * (7 if m.group(2).startswith(("semana", "week")) else 1)
            agregar(m, hoy + timedelta(days=dias))
    for m in _P_ESTA_SEMANA.finditer(texto):
        # Hasta el viernes; el sábado o el domingo, "esta semana" es la que empieza.
        viernes = hoy + timedelta(days=4 - hoy.weekday())
        agregar(m, viernes if viernes >= hoy else _proximo_dia(hoy, 4))
    for m in _P_SEMANA_PROX.finditer(texto):
        agregar(m, _proximo_dia(hoy, 0) + timedelta(days=4))
    for m in _P_FIN_MES.finditer(texto):
        agregar(m, date(hoy.year, hoy.month, calendar.monthrange(hoy.year, hoy.month)[1]))
    for m in _P_PASADO.finditer(texto):
        agregar(m, hoy + timedelta(days=2))
    for m in _P_MANANA.finditer(texto):
        if not _P_FRANJA.search(texto[max(0, m.start() - 12):m.start()]):
            agregar(m, hoy + timedelta(days=1))
    for m in _P_HOY.finditer(texto):
        agregar(m, hoy)
    for m in _P_DIA.finditer(texto):
        dia = _DIAS[m.group(1)]
        pegada = explicitas.get(_P_HASTA_LA_FECHA.match(texto, m.end()).end())
        if pegada is not None and pegada.weekday() == dia:
            continue  # "el viernes 30 de octubre": ya está la fecha
        if agregar(m, _proximo_dia(hoy, dia)):
            spans_dia.add(m.span())
    # En el orden del texto: un día de la semana no cae antes que lo anterior.
    ajustadas: dict[tuple[int, int], date | None] = {}
    limite: date | None = None
    fin_limite = 0  # dónde termina la fecha que puso el límite
    for span, f in sorted(hallazgos, key=lambda h: h[0][0]):
        if (f is not None and span in spans_dia and limite is not None and f < limite
                and (limite - f).days <= 6
                and not _corta_secuencia(texto, fin_limite, span[0])):
            f += timedelta(days=7)
        ajustadas[span] = f
        if f is not None and f >= hoy and (limite is None or f >= limite):
            limite, fin_limite = f, span[1]
    return [(s, f if f is not None and f >= hoy else None) for s, f in
            ((s, ajustadas[s]) for s, _ in hallazgos)]


# -- números, montos y monedas ----------------------------------------------------

_P_NUMERO = re.compile(r"(?<![\w.,])(\d[\d.,]*\d|\d)(?![\d])")
_P_MULTIPLICADOR = re.compile(
    r"^\s*(k|mil|lucas|thousand|millon(?:es)?|millions?)\b"
)
_MULTIPLICADORES = {"k": 1e3, "mil": 1e3, "lucas": 1e3, "thousand": 1e3}
_P_MONEDA_ANTES = re.compile(r"(us\$|u\$s|u\$d|usd|ars|eur|brl|gbp|r\$|€|£|\$)\s*$")
_P_MONEDA_DESPUES = re.compile(
    r"^\s*(?:(?:k|mil|lucas|thousand|millon(?:es)?|millions?)\s*)?(?:de\s+)?"
    r"(usd|dolares?|dollars?|bucks|pesos?|ars|euros?|eur|€|reales|brl|libras|pounds?|gbp|£)\b"
)
# Lo que va después de un número de cosas: un producto de la tabla de
# `taxonomia.palabras` o una de estas palabras.
_P_SIGUE = re.compile(r"^\s*([a-z]+)(?:\s+([a-z]+))?")
_COSAS = frozenset(
    "unidad unidades unit units par pares pair pairs pieza piezas piece pieces item items "
    "articulo articulos persona personas people trabajadores workers empleados employees".split()
)


# Entre el número y la cosa: es un precio por cosa ("800 cada notebook", "100
# por teclado", "150 el par"), no un número de cosas.
_POR_COSA = frozenset("cada por c per each la el".split())


def _de_cosas(despues: str) -> bool:
    """¿El número cuenta cosas? ("20 computadoras", "10 unidades")."""
    m = _P_SIGUE.match(despues)
    if m is None or m.group(1) in _POR_COSA:
        return False
    if m.group(1) in _COSAS:
        return True
    from taxonomia.palabras import categoria_por_palabras

    if categoria_por_palabras(m.group(1)) is not None:
        return True
    return m.group(2) is not None and categoria_por_palabras(f"{m.group(1)} {m.group(2)}") is not None
_MONEDAS: dict[str, re.Pattern] = {
    "USD": re.compile(r"\busd\b|us\$|u\$s|u\$d|\bdolar(?:es)?\b|\bdollars?\b|\bbucks\b"),
    "ARS": re.compile(r"\bars\b|\bpesos?\b(?!\s+(?:mexicanos|chilenos|colombianos|uruguayos))"),
    "EUR": re.compile(r"\beur\b|€|\beuros?\b"),
    "BRL": re.compile(r"\bbrl\b|r\$|\breales\b"),
    "MXN": re.compile(r"\bmxn\b|pesos\s+mexicanos"),
    "GBP": re.compile(r"\bgbp\b|£|\blibras\b|\bpounds?\b"),
}
DESCRIPCION_MONEDAS = {
    "USD": "dólares estadounidenses (USD, US$, u$s, dólares, dollars, bucks)",
    "ARS": "pesos argentinos (ARS, pesos)",
    "EUR": "euros (EUR, €)",
    "BRL": "reales brasileños (BRL, R$)",
    "MXN": "pesos mexicanos (MXN)",
    "GBP": "libras esterlinas (GBP, £)",
}
MONEDA_DEL_SIMBOLO = "USD"  # "$" suelto: se propone USD y se pide confirmar


def _a_numero(s: str) -> float | None:
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?", s):  # 20.000 / 1.200,50
        entero, _, dec = s.partition(",")
        return float(entero.replace(".", "") + ("." + dec if dec else ""))
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?", s):  # 20,000 / 1,200.50
        return float(s.replace(",", ""))
    if re.fullmatch(r"\d+,\d{1,2}", s):  # 1,5
        return float(s.replace(",", "."))
    if re.fullmatch(r"\d+(?:\.\d{1,2})?", s):  # 150 / 149.99
        return float(s)
    return None


def _a_entero(s: str) -> int | None:
    if s.isdigit():
        return int(s)
    return _NUMEROS.get(s)


# -- menciones ---------------------------------------------------------------------

MARCAS = (
    "Nike", "Adidas", "Puma", "Reebok", "New Balance", "Asics", "Converse", "Vans",
    "Under Armour", "Jordan", "Fila", "Topper", "Velo", "Apex", "Apple", "Lenovo", "Dell",
    "HP", "Asus", "Acer", "MSI", "Samsung", "Sony", "Bose", "JBL", "Sennheiser", "Beats",
    "Xiaomi", "Motorola", "LG", "Microsoft", "Logitech", "Huawei", "Philips", "Nespresso",
    "Lumen", "Orion",
)
_P_MARCAS = [(m, re.compile(rf"\b{re.escape(m.lower())}\b")) for m in MARCAS]
_P_TALLE = re.compile(r"\b(?:talle|talla|size|numero|nro\.?|n°)\s*:?\s*(\d{1,2}(?:[.,]5)?)\b")
_P_RAM = re.compile(
    r"\b(\d{1,3})\s*(?:gb|gigas?|g)\s+(?:de\s+)?(?:memoria\s+)?(?:of\s+)?ram\b"
    r"|\bram\s*(?:de\s+)?(\d{1,3})\s*(?:gb|gigas?)\b"
)
_P_DISCO = re.compile(
    r"\b(\d{1,4})\s*(gb|tb)\s+(?:de\s+)?(?:disco\s+)?(?:ssd|hdd|disco|almacenamiento|storage)\b"
)
_P_PANTALLA = re.compile(r"\b(\d{1,2}(?:[.,]\d)?)\s*(?:\"|”|''|pulgadas|pulg\b|-?inch(?:es)?\b)")
_COLORES = {
    "negro": "negro", "negra": "negro", "negros": "negro", "negras": "negro",
    "blanco": "blanco", "blanca": "blanco", "blancos": "blanco", "blancas": "blanco",
    "rojo": "rojo", "roja": "rojo", "rojos": "rojo", "rojas": "rojo",
    "azul": "azul", "azules": "azul", "verde": "verde", "verdes": "verde",
    "gris": "gris", "grises": "gris", "rosa": "rosa", "rosas": "rosa",
    "amarillo": "amarillo", "amarilla": "amarillo", "amarillos": "amarillo",
    "black": "black", "white": "white", "red": "red", "blue": "blue", "green": "green",
    "grey": "gray", "gray": "gray", "pink": "pink", "yellow": "yellow",
}
_P_COLOR = re.compile(r"\b(" + "|".join(sorted(_COLORES, key=len, reverse=True)) + r")\b")
_P_INALAMBRICO = re.compile(r"\binalambric\w*|\bbluetooth\b|\bwireless\b|\bsin\s+cables?\b")
_P_RUIDO = re.compile(
    r"\bcancelacion\s+(?:activa\s+)?de(?:l)?\s+ruido\b|\bnoise[- ]cancel\w*|\banc\b"
)


def _menciones(texto: str, original: str) -> tuple[list[Mencion], list[tuple[int, int]]]:
    """Menciones de atributos y los spans de sus números (no son cantidades)."""
    menciones: list[Mencion] = []
    spans: list[tuple[int, int]] = []

    def frase(m: re.Match) -> str:
        return original[m.start():m.end()]

    for marca, patron in _P_MARCAS:
        for m in patron.finditer(texto):
            menciones.append(Mencion(f"marca:{marca.lower()}", "marca", "igual", marca, frase(m)))
    for m in _P_TALLE.finditer(texto):
        n = float(m.group(1).replace(",", "."))
        valor: Any = int(n) if n.is_integer() else n
        menciones.append(Mencion(f"talle:{_valor_texto(valor)}", "talle", "incluye", valor, frase(m)))
        spans.append(m.span())
    for m in _P_RAM.finditer(texto):
        menciones.append(Mencion("atributo:memoria_ram", "atributo", "minimo",
                                 int(m.group(1) or m.group(2)), frase(m), "GB", "memoria_ram"))
        spans.append(m.span())
    for m in _P_DISCO.finditer(texto):
        menciones.append(Mencion("atributo:almacenamiento", "atributo", "minimo", int(m.group(1)),
                                 frase(m), m.group(2).upper(), "almacenamiento"))
        spans.append(m.span())
    for m in _P_PANTALLA.finditer(texto):
        n = float(m.group(1).replace(",", "."))
        menciones.append(Mencion("atributo:pantalla", "atributo", "igual",
                                 int(n) if n.is_integer() else n, frase(m), "in", "pantalla"))
        spans.append(m.span())
    for m in _P_COLOR.finditer(texto):
        menciones.append(Mencion("atributo:color", "atributo", "igual", _COLORES[m.group(1)],
                                 frase(m), None, "color"))
    for m in _P_INALAMBRICO.finditer(texto):
        menciones.append(Mencion("atributo:inalambrico", "atributo", "igual", True, frase(m),
                                 None, "inalambrico"))
    for m in _P_RUIDO.finditer(texto):
        menciones.append(Mencion("atributo:cancelacion_ruido", "atributo", "igual", True,
                                 frase(m), None, "cancelacion_ruido"))
    return menciones, spans


def _dedup_menciones(menciones: list[Mencion]) -> list[Mencion]:
    """Una por (clave, valor). Si un atributo aparece con valores distintos
    (una corrección: "16 GB… mejor 32 GB"), la clave lleva el valor."""
    unicas: dict[tuple[str, str], Mencion] = {}
    for m in menciones:
        unicas.setdefault((m.clave, _valor_texto(m.valor)), m)
    por_clave: dict[str, int] = {}
    for clave, _ in unicas:
        por_clave[clave] = por_clave.get(clave, 0) + 1
    salida = []
    for (clave, valor), m in unicas.items():
        if por_clave[clave] > 1 and m.campo == "atributo":
            m = Mencion(f"{clave}:{valor}", m.campo, m.operador, m.valor, m.frase, m.unidad, m.atributo)
        salida.append(m)
    return salida[:MAX_MENCIONES]


# -- extraer -------------------------------------------------------------------------


def extraer(textos: Sequence[str], hoy: date) -> Candidatos:
    """Los candidatos de las palabras del usuario (el pedido y sus respuestas).

    El idioma lo da el primer texto (el pedido). Fechas pasadas se descartan;
    los números de una fecha, un talle o una unidad (GB, pulgadas) no son
    cantidades ni montos. Cada texto resuelve sus fechas por su lado ("el
    lunes" es el mismo en el pedido y en una respuesta; ver `_fechas`).
    """
    idioma = _idioma(textos[0]) if textos else "es"
    montos: list[Candidato] = []
    cantidades: list[Candidato] = []
    fechas: list[Candidato] = []
    monedas: dict[str, str] = {}
    simbolo = False
    menciones: list[Mencion] = []
    vistos_monto: set[float] = set()
    vistos_cant: set[int] = set()
    vistas_fecha: set[str] = set()

    for crudo in textos:
        original = _sin_tildes((crudo or "")[:LARGO_TEXTO])
        texto = original.lower()

        # Fechas primero: sus números no son cantidades ni montos.
        ocupados: list[tuple[int, int]] = []
        for span, f in _fechas(texto, hoy, idioma):
            ocupados.append(span)
            if f is not None and f.isoformat() not in vistas_fecha:
                vistas_fecha.add(f.isoformat())
                fechas.append(Candidato(f"f{len(fechas)}", f.isoformat(), original[span[0]:span[1]]))

        nuevas, spans = _menciones(texto, original)
        menciones.extend(nuevas)
        ocupados.extend(spans)

        for codigo, patron in _MONEDAS.items():
            m = patron.search(texto)
            if m and codigo not in monedas:
                monedas[codigo] = original[m.start():m.end()]
        if "$" in re.sub(r"us\$|u\$s|u\$d|r\$", " ", texto):
            simbolo = True

        def libre(a: int, b: int) -> bool:
            return all(b <= x or a >= y for x, y in ocupados)

        for m in _P_NUMERO.finditer(texto):
            if not libre(*m.span()):
                continue
            n = _a_numero(m.group(1))
            if n is None:
                continue
            despues = texto[m.end():m.end() + 40]
            mult = _P_MULTIPLICADOR.match(despues)
            fin = m.end()
            if mult:
                palabra = mult.group(1)
                n *= _MULTIPLICADORES.get(palabra, 1e6)
                fin += mult.end()
            antes = texto[max(0, m.start() - 6):m.start()]
            pre = _P_MONEDA_ANTES.search(antes)
            es_dinero = bool(mult or pre or _P_MONEDA_DESPUES.match(despues))
            de_cosas = not es_dinero and _de_cosas(despues)
            # La frase incluye el signo de moneda de adelante ("$150", "US$ 150").
            frase = original[m.start() - (len(pre.group(0)) if pre else 0):fin]
            if n > 0 and n not in vistos_monto and len(montos) < MAX_CANDIDATOS:
                vistos_monto.add(n)
                montos.append(Candidato(f"m{len(montos)}", n, frase.strip(), de_cosas=de_cosas))
            elif not de_cosas:
                # El mismo número otra vez, ahora no como cosas ("20 notebooks… 20 dólares").
                montos = [replace(c, de_cosas=False) if c.valor == n else c for c in montos]
            if not es_dinero and n.is_integer() and 0 < n < 100000:
                entero = int(n)
                if entero not in vistos_cant and len(cantidades) < MAX_CANDIDATOS:
                    vistos_cant.add(entero)
                    cantidades.append(Candidato(f"n{len(cantidades)}", entero, frase.strip()))

        # Cantidades en palabras ("dos auriculares", "a dozen laptops").
        for m in re.finditer(
            r"\b(?:(media\s+docena|half\s+a\s+dozen)|(un\s+par|a\s+couple|a\s+pair)|"
            r"(?:una\s+|a\s+)?(docena|dozen)|(" + "|".join(
                k for k in sorted(_NUMEROS, key=len, reverse=True) if _NUMEROS[k] > 1
            ) + r"))\b",
            texto,
        ):
            if not libre(*m.span()):
                continue
            if m.group(1):
                n = 6
            elif m.group(2):
                n = 2
            elif m.group(3):
                n = 12
            else:
                n = _NUMEROS[m.group(4)]
            if n not in vistos_cant and len(cantidades) < MAX_CANDIDATOS:
                vistos_cant.add(n)
                cantidades.append(Candidato(f"n{len(cantidades)}", n, original[m.start():m.end()]))

    categoria, mencionadas, ultima = _categoria_nombrada(textos)
    return Candidatos(
        montos=montos,
        cantidades=cantidades,
        fechas=fechas[:MAX_CANDIDATOS],
        monedas_dichas=monedas,
        simbolo_ambiguo=simbolo,
        menciones=_dedup_menciones(menciones),
        idioma=idioma,
        categoria=categoria,
        mencionadas=mencionadas,
        ultima=ultima,
    )


# -- categoría de la taxonomía -------------------------------------------------------


@lru_cache(maxsize=1)
def _arbol() -> Taxonomia | None:
    """El subconjunto del repo (el mismo que usa el Guard), o None si no carga:
    sin árbol no hay categoría general y todo queda como antes."""
    try:
        from taxonomia import cargar_subconjunto

        return cargar_subconjunto()
    except Exception:  # noqa: BLE001 - sin taxonomía, sin categoría general
        return None


def _categoria_nombrada(
    textos: Sequence[str],
) -> tuple[Nombrada | None, frozenset[str], Nombrada | None]:
    """(la categoría, todas las mencionadas, la de la última respuesta)."""
    arbol = _arbol()
    if arbol is None:
        return None, frozenset(), None
    from taxonomia.palabras import nombradas

    elegida: Nombrada | None = None
    ultima: Nombrada | None = None
    todas: set[str] = set()
    for n, texto in enumerate(textos):
        encontradas = nombradas((texto or "")[:LARGO_TEXTO], arbol)
        todas.update(i for i, _ in encontradas)
        if n and n == len(textos) - 1 and encontradas:
            ultima = Nombrada(*encontradas[0])
        if not encontradas:
            continue
        if elegida is None:
            elegida = Nombrada(*encontradas[0])
            continue
        # Una respuesta la precisa solo dentro de su rama.
        rama = [(i, f) for i, f in encontradas if arbol.es_descendiente(i, elegida.id, False)]
        if rama:
            elegida = Nombrada(*rama[0])
    return elegida, frozenset(todas), ultima


def _palabras_de(texto: str) -> tuple[str, ...]:
    """Palabras sin tildes ni plural ("Mobile Phones" → ("mobile", "phone"))."""
    from taxonomia.palabras import normalizar

    return tuple(p[:-1] if len(p) > 3 and p.endswith("s") else p for p in normalizar(texto).split())


@lru_cache(maxsize=512)
def taxonomia_de_categoria(categoria: str) -> str | None:
    """El ID de la taxonomía de una categoría del catálogo ("laptops" → "328").

    El intake recibe solo los nombres del catálogo: en orden, el nombre de una
    categoría del árbol ("digital_cameras" = "Digital Cameras"), la tabla de
    palabras ("notebooks", "zapatillas") y un nombre del árbol que tenga
    todas sus palabras en orden ("usb_drives" ⊂ "USB Flash Drives"; si hay
    más de uno igual de cerca, ninguno). None si no se sabe.
    """
    arbol = _arbol()
    if arbol is None or not isinstance(categoria, str):
        return None
    palabras = _palabras_de(categoria.replace("_", " ").replace("-", " "))
    if not palabras:
        return None
    nombres = [(c.id, _palabras_de(c.nombre)) for c in arbol]
    exactas = [i for i, n in nombres if n == palabras]
    if len(exactas) == 1:
        return exactas[0]
    from taxonomia.palabras import categoria_por_palabras

    por_palabras = categoria_por_palabras(" ".join(palabras), arbol) or categoria_por_palabras(
        categoria.replace("_", " "), arbol
    )
    if por_palabras is not None:
        return por_palabras

    def contiene(nombre: tuple[str, ...]) -> bool:
        it = iter(nombre)
        return all(p in it for p in palabras)

    con_todas = [(i, n) for i, n in nombres if contiene(n)]
    # Primero las que terminan igual (el sustantivo, en inglés, va al final):
    # "usb_drives" es "USB Flash Drives" y no "USB Drive Duplicators".
    con_todas = [(i, n) for i, n in con_todas if n[-1] == palabras[-1]] or con_todas
    cerca = sorted((len(n) - len(palabras), i) for i, n in con_todas)
    if cerca and (len(cerca) == 1 or cerca[0][0] < cerca[1][0]):
        return cerca[0][1]
    return None


def _es_de_la_taxonomia(valor: Any, cats: Mapping[str, str]) -> bool:
    """¿El valor de la categoría es un ID del árbol (y no una del catálogo)?"""
    arbol = _arbol()
    return (
        isinstance(valor, str) and valor not in cats and valor.isdigit()
        and arbol is not None and valor in arbol
    )


def _taxonomia_de_valor(valor: Any, cats: Mapping[str, str]) -> str | None:
    if _es_de_la_taxonomia(valor, cats):
        return valor
    return taxonomia_de_categoria(valor) if valor in cats else None


def _general(cands: Candidatos, cats: Mapping[str, str]) -> tuple[Nombrada, list[str]] | None:
    """(la categoría general que nombró el usuario, las del catálogo en su rama).

    Solo si es más general que alguna del catálogo ("computadoras" frente a
    laptops, desktops y tablets) y ninguna del catálogo la contiene: si el
    catálogo tiene "computers", el modelo elige esa.
    """
    nombrada, arbol = cands.categoria, _arbol()
    if nombrada is None or not cats or arbol is None:
        return None
    en_rama: list[str] = []
    for c in cats:
        t = taxonomia_de_categoria(c)
        if t is None:
            continue
        if arbol.es_descendiente(nombrada.id, t):
            return None
        if arbol.es_descendiente(t, nombrada.id, incluir_igual=False):
            en_rama.append(c)
    return (nombrada, en_rama) if en_rama else None


def _achica(opcion: str, cands: Candidatos) -> bool:
    """¿Elegir esta categoría del catálogo achica lo que dijo el usuario?

    Sí si el usuario nombró una más general ("computadoras") y ninguna de sus
    palabras nombra esta ni una de su rama ("notebooks"): la elegiría el
    modelo, no el usuario.
    """
    nombrada, arbol, t = cands.categoria, _arbol(), taxonomia_de_categoria(opcion)
    if nombrada is None or arbol is None or t is None:
        return False
    if not arbol.es_descendiente(t, nombrada.id, incluir_igual=False):
        return False
    return not any(arbol.es_descendiente(m, t) for m in cands.mencionadas)


def _ajena_al_catalogo(id_categoria: str, cats: Mapping[str, str]) -> bool:
    """¿Ninguna categoría del catálogo está en su rama ni la contiene? Solo si
    se sabe la de todas: si no, no se dice que está fuera del catálogo."""
    arbol = _arbol()
    if not cats or arbol is None:
        return False
    ts = [taxonomia_de_categoria(c) for c in cats]
    if any(t is None for t in ts):
        return False
    return not any(
        arbol.es_descendiente(t, id_categoria) or arbol.es_descendiente(id_categoria, t)
        for t in ts
    )


# ---------------------------------------------------------------------------
# 2. Preguntas para el modelo (elige; no escribe)
# ---------------------------------------------------------------------------

_BASE = (
    "El estado trae el pedido de compra del usuario, la fecha de hoy y la "
    "conversación con nuestro agente (nuestras preguntas y las respuestas del "
    "usuario). Si el usuario corrigió un dato, vale lo último que dijo. "
)
_FIRMEZA = {
    "exige": "lo exige: lo necesita sí o sí (tiene que, sí o sí, must, only)",
    "prefiere": "lo prefiere: idealmente, si se puede, de ser posible, ideally, preferably",
    "sin_indicacion": "no dice si lo exige o lo prefiere",
}

_P_SLUG = re.compile(r"[\w\- ]{1,60}")


def normalizar_categorias(categorias: Sequence[str] | Mapping[str, str] | None) -> dict[str, str]:
    """Categorías del catálogo → opciones (valor → descripción).

    Vienen del catálogo público del comercio: solo nombres cortos, sin
    caracteres raros, y como mucho `MAX_CATEGORIAS`.
    """
    if not categorias:
        return {}
    pares = categorias.items() if isinstance(categorias, Mapping) else ((c, None) for c in categorias)
    salida: dict[str, str] = {}
    for valor, desc in pares:
        if not isinstance(valor, str) or not _P_SLUG.fullmatch(valor):
            continue
        if valor in ("fuera_de_catalogo", "no_dicha", "dicha", "general"):
            continue
        salida[valor] = desc if isinstance(desc, str) and desc else valor.replace("_", " ")
        if len(salida) >= MAX_CATEGORIAS:
            break
    return salida


def _desc(c: Candidato, texto_valor: str) -> str:
    return f'{texto_valor} (dice "{c.frase}")'


_LO_QUE_DIJO = {
    "categoria": "qué producto quiere",
    "cantidad": "cuántas unidades quiere",
    "precio": "el tope de precio",
    "moneda": "la moneda del tope de precio",
    "plazo": "para cuándo lo necesita (la fecha límite)",
}


def _valor_para_modelo(p: Parametro) -> str:
    """El valor de un parámetro en la pregunta de corrección (plantilla)."""
    if p.nombre == "plazo" and isinstance(p.valor, str):
        return f"{_DIAS_LARGOS[date.fromisoformat(p.valor).weekday()]} {p.valor}"
    return p.texto or _valor_texto(p.valor)


def preguntas(
    cands: Candidatos,
    categorias: Sequence[str] | Mapping[str, str] | None,
    anterior: ResultadoIntake | None = None,
) -> list[Pregunta]:
    """Las preguntas del intake. Cada elección trae los candidatos del texto y
    una opción "no dicho"; las menciones, si las pide y si las exige.

    Con `anterior` (el turno anterior), por cada parámetro necesario que el
    usuario dijo y quedó resuelto: ¿en su última respuesta lo corrige?
    (`corrige_<parámetro>`). Sin eso, `interpretar` no lo cambia.
    """
    cats = normalizar_categorias(categorias)
    qs: list[Pregunta] = []
    general = _general(cands, cats)
    if general is not None:
        nombrada, en_rama = general
        qs.append(Eleccion(
            "categoria",
            _BASE + "¿Qué tipo de producto quiere comprar el usuario? Elegí la categoría "
            "del catálogo. Si nombró un tipo general de producto que abarca varias de estas "
            f"categorías (por ejemplo, dijo «{nombrada.frase}» sin decir de qué tipo), elegí "
            "general. Si dijo un producto concreto que no es de ninguna de estas categorías, "
            "elegí fuera_de_catalogo. Si todavía no dijo qué producto quiere, elegí no_dicha.",
            {**cats,
             "general": f"{nombrada.frase} en general, de cualquier tipo: "
                        f"cualquiera de {', '.join(en_rama)}",
             "fuera_de_catalogo": "un producto concreto que no es de ninguna de las categorías anteriores",
             "no_dicha": "todavía no dijo qué producto quiere"},
        ))
    elif cats:
        qs.append(Eleccion(
            "categoria",
            _BASE + "¿Qué tipo de producto quiere comprar el usuario? Elegí la categoría "
            "del catálogo. Si dijo un producto concreto que no es de ninguna de estas "
            "categorías, elegí fuera_de_catalogo. Si todavía no dijo qué producto "
            "quiere, o lo que dijo es tan vago que podría ser de varias, elegí no_dicha.",
            {**cats,
             "fuera_de_catalogo": "un producto concreto que no es de ninguna de las categorías anteriores",
             "no_dicha": "todavía no dijo qué producto quiere, o es tan vago que no se puede saber"},
        ))
    else:
        qs.append(Eleccion(
            "categoria",
            _BASE + "¿El usuario ya dijo qué producto concreto quiere comprar?",
            {"dicha": "sí, dijo qué producto quiere",
             "no_dicha": "no, o es tan vago que no se sabe qué comprar"},
        ))
    qs.append(Eleccion(
        "cantidad",
        _BASE + "¿Cuántas unidades quiere comprar? Elegí el número que indica cuántas "
        "unidades (no el precio, el talle ni una fecha). Si habla de un solo producto "
        "(en singular, o un par de zapatillas), elegí una. Si no dijo cuántas y no se "
        "puede asumir una sola, elegí no_dicha.",
        {**{c.clave: _desc(c, str(c.valor)) for c in cands.cantidades},
         "una": "una sola unidad: el pedido está en singular o es un solo par "
                "(unas zapatillas, una notebook, a laptop)",
         "no_dicha": "no dijo cuántas y no se puede asumir una sola"},
    ))
    # Sin los números de cosas ("20 computadoras"): con "20" entre las
    # opciones, "hasta 20000 USD" quedaba en 0,59 con decisiones real (0,80 a
    # 0,95 sin ella). Un número de cosas nunca es el tope.
    montos = [c for c in cands.montos if not c.de_cosas]
    if montos:
        # Sin números en el texto no hay tope que elegir: no se pregunta (queda
        # faltante). Además, OpenAI Decisions rechaza una elección de una opción.
        qs.append(Eleccion(
            "precio",
            _BASE + "¿Cuál es el tope de precio que dio el usuario (lo máximo que quiere "
            "pagar)? Elegí el monto entre los números del texto; no elijas cantidades, "
            "talles ni fechas. Si no dio un tope de precio concreto, elegí no_dicho.",
            {**{c.clave: _desc(c, _numero_texto(c.valor)) for c in montos},
             "no_dicho": "no dio un tope de precio concreto"},
        ))
        qs.append(Eleccion(
            "precio_alcance",
            _BASE + "El tope de precio que dio el usuario, ¿es el total de la compra o "
            "es por cada unidad?",
            {"total": "el total de toda la compra (o compra una sola unidad)",
             "por_unidad": "es por unidad: cada uno, c/u, por unidad, each, apiece"},
        ))
    opciones_moneda: dict[str, str | None] = dict(DESCRIPCION_MONEDAS)
    if cands.simbolo_ambiguo:
        opciones_moneda["solo_simbolo"] = "usó solo el signo $ sin decir de qué país es la moneda"
    opciones_moneda["no_dicha"] = "no dijo la moneda (o no dio precio)"
    qs.append(Eleccion(
        "moneda", _BASE + "¿En qué moneda está el tope de precio del usuario?", opciones_moneda
    ))
    qs.append(Eleccion(
        "plazo",
        _BASE + "¿Para cuándo necesita el usuario que le llegue la compra (la fecha "
        "límite)? Elegí la fecha que dijo. Si dijo que no tiene apuro o que le sirve "
        "cualquier fecha, elegí sin_apuro. Si no dijo para cuándo, elegí no_dicho.",
        {**{c.clave: _desc(c, f"{_DIAS_LARGOS[date.fromisoformat(c.valor).weekday()]} {c.valor}")
            for c in cands.fechas},
         "sin_apuro": "dijo que no tiene apuro o que cualquier fecha le sirve",
         "no_dicho": "no dijo para cuándo lo necesita"},
    ))
    if cands.fechas:
        qs.append(Eleccion(
            "plazo_firmeza",
            _BASE + "Sobre la fecha límite: ¿el usuario la exige o la prefiere?", dict(_FIRMEZA)
        ))
    for i, m in enumerate(cands.menciones):
        etiqueta = m.etiqueta("es")
        qs.append(Predicado(
            f"pide_{i}",
            _BASE + f'¿El usuario pide que el producto tenga {etiqueta} (dice "{m.frase}")? '
            "Respondé que no si lo descartó, lo negó o lo corrigió después.",
        ))
        qs.append(Eleccion(
            f"firmeza_{i}", _BASE + f"Sobre {etiqueta}: ¿el usuario lo exige o lo prefiere?",
            dict(_FIRMEZA),
        ))
    for p in anterior.parametros if anterior is not None else ():
        # "Fuera del catálogo" no es algo que dijo: lo precisa cualquier
        # categoría concreta (ver `_refina_categoria`), sin corrección.
        if p.valor in ("fuera_de_catalogo", "dicha"):
            continue
        if p.necesario and p.estado == "resuelto" and p.origen == "dicho":
            qs.append(Predicado(
                f"corrige_{p.nombre}",
                _BASE + f"Antes el usuario había dicho {_LO_QUE_DIJO[p.nombre]}: "
                f"{_valor_para_modelo(p)}. ¿En su última respuesta lo corrige o lo cambia? "
                "Respondé que sí solo si da otro valor en lugar de ese o dice que ese está mal "
                "(por ejemplo: «mejor para el viernes», «no, son 25», «en realidad hasta 300»). "
                "Si su última respuesta habla de otra cosa, agrega información (para qué lo "
                "necesita, cuándo empieza cada uno) o repite lo mismo, respondé que no.",
            ))
    return [q for q in qs if not isinstance(q, Eleccion) or len(q.opciones) >= 2]


# ---------------------------------------------------------------------------
# 3. Interpretar con umbrales
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Parametro:
    nombre: str  # uno de NECESARIOS, o "mencion:<clave>"
    estado: EstadoParametro
    valor: Any = None
    origen: Literal["dicho", "inferido"] | None = None
    probabilidad: float = 0.0
    texto: str = ""  # el valor en palabras (plantilla)
    requisito: Requisito | None = None
    necesario: bool = True
    # Lo elegido antes de derivar el valor; la memoria entre turnos compara
    # esto: del precio, el monto y si es por unidad (no el total, que cambia
    # con la cantidad); del plazo, la fecha o "sin_apuro"; de la categoría, la
    # del catálogo o, si es un tipo general, el ID de la taxonomía ("278"; el
    # valor es la palabra, "computadoras").
    eleccion: Any = None

    def a_json(self) -> dict[str, Any]:
        return {
            "parametro": self.nombre,
            "estado": self.estado,
            "valor": self.valor,
            "origen": self.origen,
            "probabilidad": round(self.probabilidad, 4),
            "texto": self.texto,
            "necesario": self.necesario,
            "requisito": self.requisito.model_dump(mode="json") if self.requisito else None,
        }


@dataclass(frozen=True)
class ResultadoIntake:
    parametros: tuple[Parametro, ...]
    respondidos: int  # respuestas del usuario hasta acá
    idioma: Idioma
    fuera_de_catalogo: bool
    siguiente: Siguiente
    pregunta: str  # plantilla; "" si no hay más preguntas
    cortado: bool = False
    mensaje: str | None = None

    @property
    def faltan(self) -> tuple[str, ...]:
        return tuple(p.nombre for p in self.parametros if p.necesario and p.estado == "faltante")

    @property
    def a_confirmar(self) -> tuple[str, ...]:
        return tuple(p.nombre for p in self.parametros if p.estado == "a_confirmar")

    @property
    def completo(self) -> bool:
        return not self.faltan

    @property
    def requisitos(self) -> list[Requisito]:
        """La ficha parcial: lo resuelto y lo que está por confirmar."""
        return [p.requisito for p in self.parametros if p.requisito is not None]

    def requisitos_autorizables(self, confirmada: bool) -> list[Requisito]:
        """Lo que puede ir a los parámetros autorizados: lo resuelto que dijo el
        usuario y, si confirmó la ficha, también lo que estaba por confirmar."""
        return [
            p.requisito
            for p in self.parametros
            if p.requisito is not None
            and (confirmada or (p.estado == "resuelto" and p.requisito.origen == "dicho"))
        ]

    def a_json(self) -> dict[str, Any]:
        return {
            "respondidos": self.respondidos,
            "idioma": self.idioma,
            "siguiente": self.siguiente,
            "pregunta": self.pregunta,
            "faltan": list(self.faltan),
            "a_confirmar": list(self.a_confirmar),
            "completo": self.completo,
            "cortado": self.cortado,
            "mensaje": self.mensaje,
            "fuera_de_catalogo": self.fuera_de_catalogo,
            "ficha_parcial": [p.a_json() for p in self.parametros],
        }


def _elegida(resp: Any) -> tuple[str | None, float]:
    """(opción, probabilidad de esa opción); (None, 0) si no hay respuesta."""
    if isinstance(resp, RespuestaEleccion):
        return resp.eleccion, float(resp.probabilidades.get(resp.eleccion, resp.confianza))
    return None, 0.0


def _por_valor(resp: Any, valor_de: Mapping[str, Any]) -> tuple[Any, float]:
    """(valor, probabilidad) sumando las opciones que dan el mismo valor.

    `valor_de`: opción → valor (las opciones "no dicho" no están). Por
    ejemplo, "$" suelto: USD 0,55 + "solo el símbolo" (= USD) 0,26 → USD
    0,81. Gana el valor más probable; si gana "no dicho" (o nada), (None, p).
    """
    if not isinstance(resp, RespuestaEleccion):
        return None, 0.0
    probs = dict(resp.probabilidades) or {resp.eleccion: resp.confianza}
    masa: dict[Any, float] = {}
    nada = 0.0
    for opcion, p in probs.items():
        if opcion in valor_de:
            clave = valor_de[opcion]
            masa[clave] = masa.get(clave, 0.0) + float(p)
        else:
            nada += float(p)
    if not masa:
        return None, nada
    valor, p = max(masa.items(), key=lambda kv: kv[1])
    return (valor, p) if p > nada else (None, nada)


def _nivel(p: float, dicho: bool, u: Umbrales) -> EstadoParametro:
    if p < u.duda:
        return "faltante"
    if dicho and p >= u.resuelto:
        return "resuelto"
    return "a_confirmar"


def _peor(a: EstadoParametro, b: EstadoParametro) -> EstadoParametro:
    orden = {"resuelto": 0, "a_confirmar": 1, "faltante": 2}
    return a if orden[a] >= orden[b] else b


def _firmeza(resp: Any, u: Umbrales) -> tuple[bool, bool]:
    """(obligatorio, claro). Sin indicación clara: obligatorio, pero a confirmar."""
    opcion, p = _elegida(resp)
    if opcion == "prefiere" and p >= u.duda:
        return False, True
    if opcion == "exige" and p >= u.duda:
        return True, True
    return True, False


def _req(campo: str, operador: str, valor: Any, origen: str, obligatorio: bool = True,
         **extra: Any) -> Requisito:
    return Requisito(campo=campo, operador=operador, valor=valor, obligatorio=obligatorio,
                     origen=origen, **extra)


# -- memoria entre turnos ------------------------------------------------------------


@dataclass(frozen=True)
class _Lectura:
    """La elección de un parámetro en este turno, con la memoria aplicada."""

    eleccion: Any
    p: float
    conservado: Parametro | None = None  # el del turno anterior, tal cual
    igual: Parametro | None = None  # el del turno anterior, con la misma elección


Refina = Callable[[Any, Any, float], bool]


class _Memoria:
    """El turno anterior y lo que el usuario corrigió en su última respuesta.

    Corrigió = la pregunta `corrige_<parámetro>` (ver `preguntas`) con
    probabilidad ≥ `resuelto`. Sin respuesta (rechazo o una pregunta que no
    se hizo) no corrigió: lo dicho se mantiene.
    """

    def __init__(self, anterior: ResultadoIntake | None, respuestas: Mapping[str, Any],
                 u: Umbrales) -> None:
        self.u = u
        self.previos = {
            q.nombre: q
            for q in (anterior.parametros if anterior is not None else ())
            if q.necesario and q.estado != "faltante" and q.eleccion is not None
        }
        self.corregidos = {
            n for n in self.previos
            if isinstance(r := respuestas.get(f"corrige_{n}"), RespuestaPredicado)
            and r.probabilidad >= u.resuelto
        }
        # Los atributos que pidió (marca, talle…) y quedaron resueltos.
        self.menciones = {
            q.nombre: q
            for q in (anterior.parametros if anterior is not None else ())
            if not q.necesario and q.estado == "resuelto" and q.origen == "dicho"
        }

    def leer(self, nombre: str, eleccion: Any, p: float, refina: Refina | None = None) -> _Lectura:
        viejo = self.previos.get(nombre)
        if viejo is None:
            return _Lectura(eleccion, p)
        if eleccion is not None and eleccion == viejo.eleccion:
            return _Lectura(eleccion, max(p, viejo.probabilidad), igual=viejo)
        if nombre in self.corregidos:
            return _Lectura(eleccion, p)
        claro = eleccion is not None and p >= self.u.duda
        if viejo.estado == "resuelto" and viejo.origen == "dicho":
            if claro and refina is not None and refina(viejo.eleccion, eleccion, p):
                return _Lectura(eleccion, p)
            return _Lectura(viejo.eleccion, viejo.probabilidad, conservado=viejo)
        if claro:  # estaba a confirmar: un valor nuevo claro lo reemplaza
            return _Lectura(eleccion, p)
        return _Lectura(viejo.eleccion, viejo.probabilidad, conservado=viejo)


def _en_rama(categoria: str, id_rama: str) -> bool:
    """¿La categoría del catálogo cae en esa rama de la taxonomía (o es ella)?"""
    arbol, t = _arbol(), taxonomia_de_categoria(categoria)
    return arbol is not None and t is not None and arbol.es_descendiente(t, id_rama)


def _refina_categoria(
    cats: Mapping[str, str], u: Umbrales, mencionadas: frozenset[str] = frozenset()
) -> Refina:
    """¿La categoría nueva precisa la anterior (está dentro de lo que dijo)?

    - de "fuera del catálogo" (o "dicha", sin catálogo) a una concreta, si el
      modelo está seguro (≥ `resuelto`) y las palabras del usuario la nombran
      (`mencionadas`, o una de su rama): si no, sería el modelo cambiando lo
      que se autoriza;
    - de una rama de la taxonomía a una de adentro ("computadoras" →
      laptops), o a la categoría del catálogo que es esa misma ("calzado" →
      running_shoes).
    """

    def refina(antes: Any, ahora: Any, p: float) -> bool:
        arbol = _arbol()
        t_antes, t_ahora = _taxonomia_de_valor(antes, cats), _taxonomia_de_valor(ahora, cats)
        if antes in ("fuera_de_catalogo", "dicha"):
            return (
                ahora not in ("fuera_de_catalogo", "dicha") and p >= u.resuelto
                and arbol is not None and t_ahora is not None
                and any(arbol.es_descendiente(t_ahora, m) or arbol.es_descendiente(m, t_ahora)
                        for m in mencionadas)
            )
        if arbol is None or t_antes is None or t_ahora is None:
            return False
        if t_ahora == t_antes:
            return _es_de_la_taxonomia(antes, cats) and ahora in cats
        return arbol.es_descendiente(t_ahora, t_antes, incluir_igual=False)

    return refina


# -- cada parámetro, desde su elección ------------------------------------------------


def _parametro_categoria(
    eleccion: Any, p: float, cats: Mapping[str, str], nombrada: Nombrada | None,
    textos: Mapping[str, str], u: Umbrales,
) -> Parametro:
    if eleccion is None:
        return Parametro("categoria", "faltante", probabilidad=p)
    estado = _nivel(p, True, u)
    if eleccion in cats:
        requisito = _req("categoria", "igual", eleccion, "dicho") if estado != "faltante" else None
        return Parametro("categoria", estado, eleccion, "dicho", p, eleccion, requisito,
                         eleccion=eleccion)
    if eleccion in ("fuera_de_catalogo", "dicha"):
        fuera = eleccion == "fuera_de_catalogo" and estado != "faltante"
        return Parametro("categoria", estado, eleccion, "dicho", p,
                         textos["fuera" if fuera else "dicha"], eleccion=eleccion)
    # Una categoría de la taxonomía: sin requisito; la rama la verifica el
    # Guard con `Ficha.categoria_taxonomia`. El ID va en `eleccion`; el valor y
    # el texto son la palabra (de la tabla o del árbol), lo que se muestra.
    if nombrada is not None and nombrada.id == eleccion:
        texto = nombrada.frase
    else:
        arbol = _arbol()
        texto = arbol[eleccion].nombre if arbol is not None and eleccion in arbol else str(eleccion)
    return Parametro("categoria", estado, texto, "dicho", p, texto, eleccion=eleccion)


def _parametro_precio(
    eleccion: tuple[float, bool], p: float, estado: EstadoParametro, cantidad: int | None,
    textos: Mapping[str, str],
) -> Parametro:
    """El tope total: el monto o, si es por unidad, el monto × la cantidad de ahora."""
    monto, por_unidad = eleccion
    texto = _numero_texto(monto)
    total: float | None = monto
    if por_unidad:
        texto = textos["por_unidad"].format(monto=texto)
        total = monto * cantidad if cantidad else None
        if total is not None:
            texto += f" × {cantidad} = {_numero_texto(total)}"
    requisito = (
        _req("precio", "maximo", total, "dicho") if total is not None and estado != "faltante"
        else None
    )
    return Parametro("precio", estado, total, "dicho", p, texto, requisito, eleccion=eleccion)


def _parametro_plazo(
    eleccion: Any, p: float, estado: EstadoParametro, cantidad: int | None, obligatorio: bool,
    idioma: Idioma, textos: Mapping[str, str],
) -> Parametro:
    """La fecha límite y las entregas: la cantidad de ahora a más tardar esa fecha."""
    if eleccion is None:
        return Parametro("plazo", "faltante", probabilidad=p)
    if eleccion == SIN_APURO:
        dicho = estado != "faltante"
        return Parametro("plazo", estado, None, "dicho" if dicho else None, p,
                         textos["sin_apuro"] if dicho else "", eleccion=SIN_APURO)
    requisito = None
    if estado != "faltante" and cantidad:
        requisito = _req("entregas", "cubre", [{"cantidad": cantidad, "fecha": eleccion}],
                         "dicho", obligatorio)
    return Parametro("plazo", estado, eleccion, "dicho", p, fecha_corta(eleccion, idioma),
                     requisito, eleccion=eleccion)


def interpretar(
    respuestas: Mapping[str, Any],
    cands: Candidatos,
    categorias: Sequence[str] | Mapping[str, str] | None,
    respondidos: int,
    umbrales: Umbrales | None = None,
    anterior: ResultadoIntake | None = None,
) -> ResultadoIntake:
    """Las probabilidades → estado por parámetro, ficha parcial y siguiente pregunta.

    `anterior` (el resultado del turno anterior) es la memoria (`_Memoria`):

    - si el modelo sigue eligiendo el mismo valor, su probabilidad no baja de
      la de antes (visto con decisiones real: "$200" con 0,98 y, un turno
      después, 0,52);
    - lo que el usuario dijo y quedó resuelto no se pierde porque una
      respuesta no lo mencione (visto: el plazo, ante "Entran 10 trabajadores
      el lunes y otros 10 el jueves", pasó a faltante y la ficha se confirmó
      sin él). Cambia solo si el usuario lo corrige (`corrige_<parámetro>` ≥
      `resuelto`; sin corrección, ni "no dicho" ni otro valor lo pisan) o si
      lo precisa: una categoría dentro de la rama de la anterior;
    - lo que estaba a confirmar no lo borra un "no dicho": un valor nuevo
      claro (≥ `duda`) lo reemplaza.

    La categoría: la del catálogo que elige el modelo; si el usuario nombró
    un tipo general (`Candidatos.categoria`: "computadoras", "cámaras"), esa
    categoría de la taxonomía, resuelta y dicha, sin requisito (la rama la
    verifica el Guard con `Ficha.categoria_taxonomia`): cuando el modelo la
    elige (`general`), cuando elige una del catálogo que achica lo que dijo
    el usuario (laptops para "computadoras") o cuando no reconoce el
    producto. Que no esté en el catálogo (`fuera_de_catalogo`) es un juicio
    del modelo y no se pisa.
    """
    u = umbrales or UMBRALES
    idioma = cands.idioma
    textos = _TEXTOS[idioma]
    cats = normalizar_categorias(categorias)
    params: dict[str, Parametro] = {}
    memoria = _Memoria(anterior, respuestas, u)

    # Categoría
    nombrada = cands.categoria
    general = _general(cands, cats)
    opcion, p = _elegida(respuestas.get("categoria"))
    eleccion: Any = None
    if opcion in cats:
        eleccion = nombrada.id if nombrada is not None and _achica(opcion, cands) else opcion
    elif opcion == "general" and general is not None:
        eleccion = general[0].id
    elif opcion == "fuera_de_catalogo" and cats:
        eleccion = opcion
    elif opcion == "dicha" and not cats:
        eleccion = nombrada.id if nombrada is not None else opcion
    if nombrada is not None and eleccion == nombrada.id:
        p = 1.0  # lo dicen sus palabras (`taxonomia.palabras`), no el modelo
    refina = _refina_categoria(cats, u, cands.mencionadas)
    lectura = memoria.leer("categoria", eleccion, p, refina=refina)
    # El atajo: si el modelo no reconoce el producto (o duda), la que nombró el
    # usuario. Si la acaba de corregir, la que nombra su última respuesta (o
    # ninguna: se repregunta). Si el modelo apunta a otra cosa (fuera del
    # catálogo, otra rama), aunque dude, no se fuerza.
    corregida = "categoria" in memoria.corregidos
    atajo = cands.ultima if corregida else nombrada
    coherente = corregida or opcion in (None, "no_dicha", "dicha", "general") or (
        opcion in cats and atajo is not None and _en_rama(opcion, atajo.id)
    )
    if (atajo is not None and coherente and lectura.conservado is None
            and (lectura.eleccion is None or lectura.p < u.duda)):
        lectura = _Lectura(atajo.id, 1.0)
    elif (nombrada is not None and lectura.conservado is not None
          and _es_de_la_taxonomia(lectura.eleccion, cats)
          and refina(lectura.eleccion, nombrada.id, 1.0)):
        # Un tipo general de antes, precisado por sus palabras ("computadoras"
        # → "notebooks"). "Fuera del catálogo" no: es un juicio del modelo.
        lectura = _Lectura(nombrada.id, 1.0)
    else:
        atajo = nombrada
    params["categoria"] = lectura.conservado or _parametro_categoria(
        lectura.eleccion, lectura.p, cats, atajo, textos, u
    )
    elegida = params["categoria"].eleccion
    fuera = params["categoria"].estado != "faltante" and (
        elegida == "fuera_de_catalogo"
        or (_es_de_la_taxonomia(elegida, cats) and _ajena_al_catalogo(elegida, cats))
    )

    # Cantidad ("una" y un "1" escrito son el mismo valor: se suman)
    cantidad, p = _por_valor(
        respuestas.get("cantidad"), {**{c.clave: c.valor for c in cands.cantidades}, "una": 1}
    )
    lectura = memoria.leer("cantidad", cantidad, p)
    if lectura.conservado is not None:
        params["cantidad"] = lectura.conservado
    else:
        cantidad, p = lectura.eleccion, lectura.p
        estado, origen = "faltante", None
        if cantidad is not None:
            dicho = any(c.valor == cantidad and c.dicho for c in cands.cantidades)
            estado, origen = _nivel(p, dicho, u), "dicho" if dicho else "inferido"
            if estado == "faltante":
                cantidad = None
        params["cantidad"] = Parametro(
            "cantidad", estado, cantidad, origen if cantidad else None, p,
            str(cantidad) if cantidad else "",
            # Una sola unidad no es requisito: un cart es una unidad, y el Guard
            # escalaría toda oferta que no declare la cantidad.
            _req("cantidad", "igual", cantidad, origen) if cantidad and cantidad > 1 else None,
            eleccion=cantidad,
        )
    cantidad = params["cantidad"].valor if params["cantidad"].estado != "faltante" else None

    # Precio (tope total): se recuerda el monto y si es por unidad, no el total.
    por_clave = {c.clave: c for c in cands.montos}
    opcion, p = _elegida(respuestas.get("precio"))
    alcance, p_alcance = _elegida(respuestas.get("precio_alcance"))
    eleccion = None
    if opcion in por_clave:
        por_unidad = cantidad != 1 and alcance == "por_unidad" and p_alcance >= u.duda
        eleccion = (float(por_clave[opcion].valor), por_unidad)
    lectura = memoria.leer("precio", eleccion, p)
    if lectura.eleccion is None:
        params["precio"] = Parametro("precio", "faltante", probabilidad=lectura.p)
    else:
        if lectura.conservado is not None:
            estado = lectura.conservado.estado
        else:
            estado = _nivel(lectura.p, True, u)
            # Con una sola unidad, total y por unidad son lo mismo: no importa.
            # Si ya estaba resuelto con lo mismo, la duda sobre el alcance no lo baja.
            ya_resuelto = lectura.igual is not None and lectura.igual.estado == "resuelto"
            if (not ya_resuelto and cantidad != 1 and alcance in ("total", "por_unidad")
                    and p_alcance < u.resuelto):
                estado = _peor(estado, "a_confirmar")
        params["precio"] = _parametro_precio(lectura.eleccion, lectura.p, estado, cantidad, textos)

    # Moneda ("solo el símbolo" se propone como USD: se suma a USD)
    valor_de = {c: c for c in DESCRIPCION_MONEDAS}
    if cands.simbolo_ambiguo:
        valor_de["solo_simbolo"] = MONEDA_DEL_SIMBOLO
    codigo, p = _por_valor(respuestas.get("moneda"), valor_de)
    lectura = memoria.leer("moneda", codigo, p)
    if lectura.conservado is not None:
        params["moneda"] = lectura.conservado
    elif lectura.eleccion is not None:
        codigo, p = lectura.eleccion, lectura.p
        dicho = codigo in cands.monedas_dichas
        estado = _nivel(p, dicho, u)
        origen = "dicho" if dicho else "inferido"
        params["moneda"] = Parametro(
            "moneda", estado, codigo, origen, p, codigo,
            _req("moneda", "igual", codigo, origen) if estado != "faltante" else None,
            eleccion=codigo,
        )
    else:
        params["moneda"] = Parametro("moneda", "faltante", probabilidad=lectura.p)

    # Plazo (y las entregas: cantidad a más tardar en esa fecha)
    por_clave = {c.clave: c for c in cands.fechas}
    opcion, p = _elegida(respuestas.get("plazo"))
    if opcion in por_clave:
        eleccion = por_clave[opcion].valor
    else:
        eleccion = SIN_APURO if opcion == SIN_APURO else None
    lectura = memoria.leer("plazo", eleccion, p)
    obligatorio, claro = _firmeza(respuestas.get("plazo_firmeza"), u)
    previo = lectura.conservado or lectura.igual
    if previo is not None and previo.requisito is not None and "plazo" not in memoria.corregidos:
        # Lo que exigía no se afloja sin corrección: con una firmeza dudosa
        # queda como estaba, y una clara solo lo vuelve más estricto.
        obligatorio = previo.requisito.obligatorio or (claro and obligatorio)
    if lectura.conservado is not None:
        estado = lectura.conservado.estado
    elif lectura.eleccion is not None:
        estado = _nivel(lectura.p, True, u)
    else:
        estado = "faltante"
    params["plazo"] = _parametro_plazo(
        lectura.eleccion, lectura.p, estado, cantidad, obligatorio, idioma, textos
    )

    # Menciones (atributos): no son necesarias; si el usuario las pidió, van.
    # Una que ya estaba resuelta no se pierde porque el modelo dude después
    # (sin respuesta, o "pide" entre un "no" claro y `duda`) ni baja por una
    # firmeza dudosa: solo un "no" claro (≤ 1 - `resuelto`; la pregunta dice
    # "si lo descartó, lo negó o lo corrigió") la saca.
    menciones: list[tuple[Mencion, Parametro]] = []
    for i, m in enumerate(cands.menciones):
        viejo = memoria.menciones.get(f"mencion:{m.clave}")
        pide = respuestas.get(f"pide_{i}")
        p = pide.probabilidad if isinstance(pide, RespuestaPredicado) else None
        if viejo is not None and (p is None or 1 - u.resuelto < p < u.duda):
            menciones.append((m, viejo))
            continue
        if p is None or p < u.duda:
            continue
        obligatorio, claro = _firmeza(respuestas.get(f"firmeza_{i}"), u)
        if viejo is not None:
            p = max(p, viejo.probabilidad)
            if not claro and viejo.requisito is not None:
                obligatorio, claro = viejo.requisito.obligatorio, True
        estado = _nivel(p, True, u)
        if not claro:
            estado = _peor(estado, "a_confirmar")
        extra = {"atributo": m.atributo} if m.campo == "atributo" else {}
        if m.unidad:
            extra["unidad"] = m.unidad
        menciones.append((m, Parametro(
            f"mencion:{m.clave}", estado, m.valor, "dicho", p, m.etiqueta(idioma),
            _req(m.campo, m.operador, m.valor, "dicho", obligatorio, **extra), necesario=False,
        )))
    menciones = _resolver_menciones(menciones)

    params_lista = [params[n] for n in NECESARIOS] + [p for _, p in menciones]
    faltan = [p.nombre for p in params_lista if p.necesario and p.estado == "faltante"]
    dudosos = [p for p in params_lista if p.estado == "a_confirmar"]
    siguiente, pregunta, cortado, mensaje = _siguiente(faltan, dudosos, respondidos, idioma, fuera)
    return ResultadoIntake(
        parametros=tuple(params_lista),
        respondidos=respondidos,
        idioma=idioma,
        fuera_de_catalogo=fuera,
        siguiente=siguiente,
        pregunta=pregunta,
        cortado=cortado,
        mensaje=mensaje,
    )


def _resolver_menciones(
    menciones: list[tuple[Mencion, Parametro]],
) -> list[tuple[Mencion, Parametro]]:
    """Un valor por atributo (el más probable: una corrección pisa al anterior).
    Varias marcas ("Sony o Bose") quedan todas, como preferidas y a confirmar."""
    grupos: dict[tuple[str, str | None], list[tuple[Mencion, Parametro]]] = {}
    for m, p in menciones:
        grupos.setdefault((m.campo, m.atributo), []).append((m, p))
    salida: list[tuple[Mencion, Parametro]] = []
    for (campo, _), grupo in grupos.items():
        if len(grupo) == 1:
            salida.extend(grupo)
        elif campo == "marca":
            for m, p in grupo:
                req = p.requisito.model_copy(update={"obligatorio": False}) if p.requisito else None
                salida.append((m, Parametro(p.nombre, "a_confirmar", p.valor, p.origen,
                                            p.probabilidad, p.texto, req, necesario=False)))
        else:
            salida.append(max(grupo, key=lambda par: par[1].probabilidad))
    orden = {m.clave: i for i, (m, _) in enumerate(menciones)}
    return sorted(salida, key=lambda par: orden[par[0].clave])


def _nombres_faltan(faltan: Sequence[str]) -> list[str]:
    nombres = list(faltan)
    if "precio" in nombres and "moneda" in nombres:
        nombres[nombres.index("precio")] = "precio+moneda"
        nombres.remove("moneda")
    return nombres


def _lista_faltan(faltan: Sequence[str], idioma: Idioma) -> str:
    return _unir([_PIEZAS[idioma][n] for n in _nombres_faltan(faltan)], idioma)


def _ejemplos_faltan(faltan: Sequence[str], idioma: Idioma) -> str:
    return "; ".join(_EJEMPLOS[idioma][n] for n in _nombres_faltan(faltan))


def _item_a_confirmar(p: Parametro, idioma: Idioma) -> str:
    if not p.necesario:
        return p.texto
    return f"{_ETIQUETAS[idioma][p.nombre]} {p.texto}".strip()


def _siguiente(
    faltan: Sequence[str],
    dudosos: Sequence[Parametro],
    respondidos: int,
    idioma: Idioma,
    fuera: bool,
) -> tuple[Siguiente, str, bool, str | None]:
    """(qué sigue, pregunta, cortado, mensaje). Todo con plantillas."""
    lista = _lista_faltan(faltan, idioma)
    if respondidos >= MAX_TURNOS:
        mensaje = (
            _CORTADO[idioma].format(n=respondidos - 1, lista=lista) if faltan else None
        )
        return "fin", "", bool(faltan), mensaje
    if respondidos == 0:
        pregunta = PORQUE[idioma]
        if faltan:
            pregunta += " " + _FALTAN_INICIAL[idioma].format(lista=lista)
        return "porque", pregunta, False, None
    if faltan and respondidos - 1 < MAX_REPREGUNTAS:
        if respondidos == 1:
            return "repreguntar", _FALTAN[idioma].format(lista=lista), False, None
        pregunta = _FALTAN_OTRA_VEZ[idioma].format(
            lista=lista, ejemplos=_ejemplos_faltan(faltan, idioma)
        )
        return "repreguntar", pregunta, False, None
    partes = [CONFIRMAR[idioma]]
    if dudosos:
        partes.append(_A_CONFIRMAR[idioma].format(
            lista=_unir([_item_a_confirmar(p, idioma) for p in dudosos], idioma)
        ))
    if fuera:
        partes.append(_FUERA_DE_CATALOGO[idioma])
    mensaje = _CORTADO[idioma].format(n=respondidos - 1, lista=lista) if faltan else None
    if mensaje:
        partes.append(mensaje)
    return "confirmar", " ".join(partes), bool(faltan), mensaje


# ---------------------------------------------------------------------------
# 4. Una llamada por turno
# ---------------------------------------------------------------------------


def estado_para_modelo(pedido: str, turnos: Sequence[Turno], hoy: date) -> dict[str, Any]:
    """Lo que ve el modelo: el pedido, hoy y las respuestas del usuario."""
    return {
        "hoy": f"{_DIAS_LARGOS[hoy.weekday()]} {hoy.isoformat()}",
        "pedido": pedido[:LARGO_TEXTO],
        "conversacion": [
            {"pregunta": t.pregunta, "respuesta": (t.respuesta or "")[:LARGO_TEXTO]}
            for t in turnos
            if t.respuesta
        ],
    }


def respondidas(turnos: Sequence[Turno]) -> list[Turno]:
    return [t for t in turnos if t.respuesta and t.respuesta.strip()]


def candidatos_de(pedido: str, turnos: Sequence[Turno], hoy: date) -> Candidatos:
    return extraer([pedido, *[t.respuesta or "" for t in respondidas(turnos)]], hoy)


def evaluar(
    pedido: str,
    turnos: Sequence[Turno],
    hoy: date,
    cliente: ClienteDecisiones,
    categorias: Sequence[str] | Mapping[str, str] | None = None,
    umbrales: Umbrales | None = None,
    anterior: ResultadoIntake | None = None,
) -> ResultadoIntake:
    """Pedido + turnos → ResultadoIntake, con una sola llamada al cliente.

    `anterior`: el resultado del turno anterior (memoria, ver `interpretar`;
    con él se pregunta también si el usuario corrigió lo resuelto). Si el
    cliente falla, lanza `DecisionesError`: quien llama decide (el proxy
    sigue con lo que tiene y no repregunta).
    """
    if not pedido or not pedido.strip():
        raise ValueError("El pedido está vacío")
    hechas = respondidas(turnos)
    cands = candidatos_de(pedido, hechas, hoy)
    anterior = anterior if hechas else None
    qs = preguntas(cands, categorias, anterior)
    respuestas = cliente.decidir(estado_para_modelo(pedido, hechas, hoy), qs)
    return interpretar(
        respuestas, cands, categorias, respondidos=len(hechas), umbrales=umbrales, anterior=anterior
    )
