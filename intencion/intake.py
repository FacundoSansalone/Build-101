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
salvo `evaluar()`, que hace una llamada al cliente de decisiones.
"""

from __future__ import annotations

import calendar
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

from contracts import Requisito, Turno
from decisiones import (
    ClienteDecisiones,
    Eleccion,
    Predicado,
    Pregunta,
    RespuestaEleccion,
    RespuestaPredicado,
)

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
_FUERA_DE_CATALOGO = {
    "es": "Lo que pediste no está entre las categorías del catálogo del proveedor.",
    "en": "What you asked for is not among the provider's catalog categories.",
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
_ETIQUETAS = {
    "es": {"categoria": "categoría", "cantidad": "cantidad", "precio": "tope",
           "moneda": "moneda", "plazo": "plazo"},
    "en": {"categoria": "category", "cantidad": "quantity", "precio": "budget",
           "moneda": "currency", "plazo": "deadline"},
}
_TEXTOS = {
    "es": {"sin_apuro": "sin apuro", "fuera": "fuera del catálogo", "dicha": "dicha",
           "por_unidad": "{monto} por unidad"},
    "en": {"sin_apuro": "no rush", "fuera": "outside the catalog", "dicha": "given",
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
class Candidatos:
    montos: list[Candidato] = field(default_factory=list)
    cantidades: list[Candidato] = field(default_factory=list)
    fechas: list[Candidato] = field(default_factory=list)
    monedas_dichas: dict[str, str] = field(default_factory=dict)  # ISO → frase
    simbolo_ambiguo: bool = False  # "$" sin decir de qué país
    menciones: list[Mencion] = field(default_factory=list)
    idioma: Idioma = "es"


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
_P_SEMANA_PROX = re.compile(
    r"\b(?:la\s+semana\s+que\s+viene|la\s+proxima\s+semana|la\s+semana\s+proxima|next\s+week)\b"
)
_P_FIN_MES = re.compile(r"\b(?:fin(?:es|ales)?\s+de(?:l)?\s+mes|end\s+of\s+(?:the\s+)?month)\b")
_P_PASADO = re.compile(r"\b(?:pasado\s+manana|day\s+after\s+tomorrow)\b")
_P_MANANA = re.compile(r"\b(?:manana|tomorrow)\b")
_P_HOY = re.compile(r"\b(?:hoy|today)\b")
_P_DIA = re.compile(rf"\b({_DIAS_RE})\b")


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
    return hoy + timedelta(days=((dia_semana - hoy.weekday() - 1) % 7) + 1)


def _fechas(texto: str, hoy: date, idioma: Idioma) -> list[tuple[tuple[int, int], date | None]]:
    """(span, fecha) en `texto` (ya en minúsculas y sin tildes). Fecha None si
    no es válida o ya pasó: el span igual se marca (sus números no son
    cantidades)."""
    hallazgos: list[tuple[tuple[int, int], date | None]] = []
    ocupado: list[tuple[int, int]] = []

    def libre(span: tuple[int, int]) -> bool:
        return all(span[1] <= a or span[0] >= b for a, b in ocupado)

    def agregar(m: re.Match, f: date | None) -> None:
        if libre(m.span()):
            ocupado.append(m.span())
            hallazgos.append((m.span(), f))

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
    for m in _P_RELATIVO.finditer(texto):
        n = _a_entero(m.group(1))
        if n is not None and n > 0:
            dias = n * (7 if m.group(2).startswith(("semana", "week")) else 1)
            agregar(m, hoy + timedelta(days=dias))
    for m in _P_SEMANA_PROX.finditer(texto):
        agregar(m, _proximo_dia(hoy, 0) + timedelta(days=4))
    for m in _P_FIN_MES.finditer(texto):
        agregar(m, date(hoy.year, hoy.month, calendar.monthrange(hoy.year, hoy.month)[1]))
    for m in _P_PASADO.finditer(texto):
        agregar(m, hoy + timedelta(days=2))
    for m in _P_MANANA.finditer(texto):
        agregar(m, hoy + timedelta(days=1))
    for m in _P_HOY.finditer(texto):
        agregar(m, hoy)
    for m in _P_DIA.finditer(texto):
        agregar(m, _proximo_dia(hoy, _DIAS[m.group(1)]))
    return [(s, f if f is not None and f >= hoy else None) for s, f in hallazgos]


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
    cantidades ni montos.
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
            # La frase incluye el signo de moneda de adelante ("$150", "US$ 150").
            frase = original[m.start() - (len(pre.group(0)) if pre else 0):fin]
            if n > 0 and n not in vistos_monto and len(montos) < MAX_CANDIDATOS:
                vistos_monto.add(n)
                montos.append(Candidato(f"m{len(montos)}", n, frase.strip()))
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

    return Candidatos(
        montos=montos,
        cantidades=cantidades,
        fechas=fechas[:MAX_CANDIDATOS],
        monedas_dichas=monedas,
        simbolo_ambiguo=simbolo,
        menciones=_dedup_menciones(menciones),
        idioma=idioma,
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
        if valor in ("fuera_de_catalogo", "no_dicha", "dicha"):
            continue
        salida[valor] = desc if isinstance(desc, str) and desc else valor.replace("_", " ")
        if len(salida) >= MAX_CATEGORIAS:
            break
    return salida


def _desc(c: Candidato, texto_valor: str) -> str:
    return f'{texto_valor} (dice "{c.frase}")'


def preguntas(
    cands: Candidatos, categorias: Sequence[str] | Mapping[str, str] | None
) -> list[Pregunta]:
    """Las preguntas del intake. Cada elección trae los candidatos del texto y
    una opción "no dicho"; las menciones, si las pide y si las exige."""
    cats = normalizar_categorias(categorias)
    qs: list[Pregunta] = []
    if cats:
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
    if cands.montos:
        # Sin números en el texto no hay tope que elegir: no se pregunta (queda
        # faltante). Además, OpenAI Decisions rechaza una elección de una opción.
        qs.append(Eleccion(
            "precio",
            _BASE + "¿Cuál es el tope de precio que dio el usuario (lo máximo que quiere "
            "pagar)? Elegí el monto entre los números del texto; no elijas cantidades, "
            "talles ni fechas. Si no dio un tope de precio concreto, elegí no_dicho.",
            {**{c.clave: _desc(c, _numero_texto(c.valor)) for c in cands.montos},
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


def interpretar(
    respuestas: Mapping[str, Any],
    cands: Candidatos,
    categorias: Sequence[str] | Mapping[str, str] | None,
    respondidos: int,
    umbrales: Umbrales | None = None,
) -> ResultadoIntake:
    """Las probabilidades → estado por parámetro, ficha parcial y siguiente pregunta."""
    u = umbrales or UMBRALES
    idioma = cands.idioma
    textos = _TEXTOS[idioma]
    cats = normalizar_categorias(categorias)
    params: dict[str, Parametro] = {}

    # Categoría
    opcion, p = _elegida(respuestas.get("categoria"))
    fuera = False
    if opcion in cats:
        estado = _nivel(p, True, u)
        params["categoria"] = Parametro(
            "categoria", estado, opcion, "dicho", p, opcion,
            _req("categoria", "igual", opcion, "dicho") if estado != "faltante" else None,
        )
    elif (opcion == "fuera_de_catalogo" and cats) or (opcion == "dicha" and not cats):
        estado = _nivel(p, True, u)
        fuera = opcion == "fuera_de_catalogo" and estado != "faltante"
        params["categoria"] = Parametro(
            "categoria", estado, None, "dicho", p, textos["fuera" if fuera else "dicha"]
        )
    else:
        params["categoria"] = Parametro("categoria", "faltante", probabilidad=p)

    # Cantidad ("una" y un "1" escrito son el mismo valor: se suman)
    cantidad, p = _por_valor(
        respuestas.get("cantidad"), {**{c.clave: c.valor for c in cands.cantidades}, "una": 1}
    )
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
    )

    # Precio (tope total)
    por_clave = {c.clave: c for c in cands.montos}
    opcion, p = _elegida(respuestas.get("precio"))
    if opcion in por_clave:
        monto = float(por_clave[opcion].valor)
        estado = _nivel(p, True, u)
        alcance, p_alcance = _elegida(respuestas.get("precio_alcance"))
        texto = _numero_texto(monto)
        total: float | None = monto
        # Con una sola unidad, total y por unidad son lo mismo: no importa.
        if cantidad != 1:
            if alcance == "por_unidad" and p_alcance >= u.duda:
                texto = textos["por_unidad"].format(monto=texto)
                total = monto * cantidad if cantidad else None
                if total is not None:
                    texto += f" × {cantidad} = {_numero_texto(total)}"
            if alcance in ("total", "por_unidad") and p_alcance < u.resuelto:
                estado = _peor(estado, "a_confirmar")
        params["precio"] = Parametro(
            "precio", estado, total, "dicho", p, texto,
            _req("precio", "maximo", total, "dicho")
            if total is not None and estado != "faltante"
            else None,
        )
    else:
        params["precio"] = Parametro("precio", "faltante", probabilidad=p)

    # Moneda ("solo el símbolo" se propone como USD: se suma a USD)
    valor_de = {c: c for c in DESCRIPCION_MONEDAS}
    if cands.simbolo_ambiguo:
        valor_de["solo_simbolo"] = MONEDA_DEL_SIMBOLO
    codigo, p = _por_valor(respuestas.get("moneda"), valor_de)
    if codigo is not None:
        dicho = codigo in cands.monedas_dichas
        estado = _nivel(p, dicho, u)
        origen = "dicho" if dicho else "inferido"
        params["moneda"] = Parametro(
            "moneda", estado, codigo, origen, p, codigo,
            _req("moneda", "igual", codigo, origen) if estado != "faltante" else None,
        )
    else:
        params["moneda"] = Parametro("moneda", "faltante", probabilidad=p)

    # Plazo (y las entregas: cantidad a más tardar en esa fecha)
    por_clave = {c.clave: c for c in cands.fechas}
    opcion, p = _elegida(respuestas.get("plazo"))
    if opcion in por_clave:
        fecha = por_clave[opcion].valor
        estado = _nivel(p, True, u)
        obligatorio, _ = _firmeza(respuestas.get("plazo_firmeza"), u)
        requisito = None
        if estado != "faltante" and cantidad:
            requisito = _req("entregas", "cubre", [{"cantidad": cantidad, "fecha": fecha}],
                             "dicho", obligatorio)
        params["plazo"] = Parametro("plazo", estado, fecha, "dicho", p,
                                    fecha_corta(fecha, idioma), requisito)
    elif opcion == "sin_apuro":
        estado = _nivel(p, True, u)
        params["plazo"] = Parametro("plazo", estado, None, "dicho" if estado != "faltante" else None,
                                    p, textos["sin_apuro"] if estado != "faltante" else "")
    else:
        params["plazo"] = Parametro("plazo", "faltante", probabilidad=p)

    # Menciones (atributos): no son necesarias; si el usuario las pidió, van.
    menciones: list[tuple[Mencion, Parametro]] = []
    for i, m in enumerate(cands.menciones):
        pide = respuestas.get(f"pide_{i}")
        p = pide.probabilidad if isinstance(pide, RespuestaPredicado) else 0.0
        if p < u.duda:
            continue
        obligatorio, claro = _firmeza(respuestas.get(f"firmeza_{i}"), u)
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


def _lista_faltan(faltan: Sequence[str], idioma: Idioma) -> str:
    piezas = _PIEZAS[idioma]
    nombres = list(faltan)
    if "precio" in nombres and "moneda" in nombres:
        nombres[nombres.index("precio")] = "precio+moneda"
        nombres.remove("moneda")
    return _unir([piezas[n] for n in nombres], idioma)


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
        return "repreguntar", _FALTAN[idioma].format(lista=lista), False, None
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
) -> ResultadoIntake:
    """Pedido + turnos → ResultadoIntake, con una sola llamada al cliente.

    Si el cliente falla, lanza `DecisionesError`: quien llama decide (el
    proxy sigue con lo que tiene y no repregunta).
    """
    if not pedido or not pedido.strip():
        raise ValueError("El pedido está vacío")
    hechas = respondidas(turnos)
    cands = candidatos_de(pedido, hechas, hoy)
    respuestas = cliente.decidir(estado_para_modelo(pedido, hechas, hoy), preguntas(cands, categorias))
    return interpretar(respuestas, cands, categorias, respondidos=len(hechas), umbrales=umbrales)
