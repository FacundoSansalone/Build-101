"""Parser determinístico de `catalog_search` (lo que manda el Shopping Agent).

Sin LLM: el comercio general (`demo_world.general`) busca en su dataset con lo
que sale de acá. Entiende castellano e inglés:

- categoría: la taxonomía de Google con `taxonomia.palabras` (la misma tabla
  que usa el proxy cuando no hay clasificador). "computadoras" da la rama
  general (278); el comercio decide qué ofrece para eso;
- cantidad (1 si no se dice): "20 computadoras", "dos monitores", "x 15",
  "15 units";
- fecha límite relativa a `hoy`: ISO, dd/mm, "30 de octubre", "October 30",
  "en N días", "in N days", "hoy", "mañana", "pasado mañana", "esta semana",
  "la semana que viene", "fin de mes" y días de la semana (el próximo: si hoy
  es lunes, "el lunes" es el de la semana que viene). Con un número sin mes
  ("el lunes 26"), el primer día desde hoy con ese número en ese día de la
  semana (si no hay en 12 meses, el próximo). Gana la explícita; una fecha
  pasada no cuenta;
- tope y moneda: "hasta 400 USD", "up to 1,500 USD", "USD 1.200", "$300",
  "500 mil pesos", "presupuesto de 150 euros", "under 250";
- atributos con unidad (pulgadas, GB/TB, kg/g, horas, litros, W, MP, cm) y
  su operador ("al menos", "o más", "hasta", "or larger"...), booleanos
  (inalámbrico, cancelación de ruido, impresión a color), color, resolución,
  Wi-Fi, talle, uso ("para diseño", "gaming", "for travel") y marcas (las que
  pasa quien llama).

Los nombres de atributo son los canónicos del Guard (`guard.atributos`):
`tamano_pantalla`, `memoria_ram`, `almacenamiento`, `weight`... Los números
de una fecha, un atributo o un monto no cuentan como cantidad.
"""

from __future__ import annotations

import calendar
import importlib.util
import json
import logging
import re
import sys
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[7]
LARGO_MAXIMO = 1000  # lo que se lee del pedido
CANTIDAD_MAXIMA = 9999


@dataclass(frozen=True)
class AtributoPedido:
  """Un atributo que nombra el pedido, con el nombre canónico del Guard."""

  atributo: str
  operador: str  # "igual" | "minimo" | "maximo" | "incluye"
  valor: Any
  unidad: str | None = None


@dataclass(frozen=True)
class Consulta:
  texto: str
  palabras: tuple[str, ...]          # sin tildes, sin números ni palabras vacías
  categoria_taxonomia: str | None    # ID de la taxonomía de Google
  cantidad: int
  fecha_limite: date | None
  tope: float | None
  moneda: str | None
  atributos: tuple[AtributoPedido, ...]
  talle: float | None
  uso: str | None
  marcas: tuple[str, ...]


# ---------------------------------------------------------------------------
# Taxonomía (paquete de la raíz del repo)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _taxonomia() -> Any | None:
  """El paquete `taxonomia` de la raíz del repo, o None.

  El comercio corre desde world/ap2 sin la raíz en el path: si no se puede
  importar, se carga por su ruta (solo ese paquete; no se toca sys.path).
  """
  try:
    import taxonomia  # noqa: PLC0415

    return taxonomia
  except ImportError:
    pass
  inicio = REPO_ROOT / "taxonomia" / "__init__.py"
  if not inicio.exists():
    logger.warning("demo_world: no está taxonomia/; la categoría sale de las palabras del título")
    return None
  try:
    spec = importlib.util.spec_from_file_location(
        "taxonomia", inicio, submodule_search_locations=[str(inicio.parent)]
    )
    modulo = importlib.util.module_from_spec(spec)
    sys.modules["taxonomia"] = modulo
    spec.loader.exec_module(modulo)
    return modulo
  except Exception:  # noqa: BLE001 - sin taxonomía se sigue buscando por palabras
    sys.modules.pop("taxonomia", None)
    logger.exception("demo_world: no se pudo cargar taxonomia/")
    return None


def categoria_de(texto: str) -> str | None:
  """ID de la categoría que nombra el texto (`taxonomia.palabras`), o None."""
  modulo = _taxonomia()
  if modulo is None:
    return None
  try:
    # `nombradas` saltea complementos ("mochila para la notebook").
    candidatas = modulo.palabras.nombradas(texto, modulo.cargar_subconjunto())
    return candidatas[0][0] if candidatas else None
  except Exception:  # noqa: BLE001
    logger.exception("demo_world: falló la categoría por palabras")
    return None


# ---------------------------------------------------------------------------
# Texto
# ---------------------------------------------------------------------------


def normalizar(texto: str) -> str:
  """Minúsculas y sin tildes; conserva la puntuación (números, fechas, $)."""
  sin_tildes = "".join(
      c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c)
  )
  sin_control = "".join(c if c.isprintable() else " " for c in sin_tildes)
  return re.sub(r"\s+", " ", sin_control.lower()).strip()


def _como_texto(crudo: Any) -> str:
  if crudo is None:
    return ""
  if isinstance(crudo, str):
    texto = crudo
  else:
    try:
      texto = json.dumps(crudo, ensure_ascii=False)
    except (TypeError, ValueError):
      texto = str(crudo)
  return texto[:LARGO_MAXIMO]


_NUM = r"\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d+)?"


def _numero(crudo: str) -> float | None:
  """"20.000" y "20,000" → 20000; "1.200,50" → 1200.5; "1,5" y "1.5" → 1.5."""
  s = crudo.strip()
  if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?", s):
    entero, _, dec = s.partition(",")
    return float(entero.replace(".", "") + ("." + dec if dec else ""))
  if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?", s):
    return float(s.replace(",", ""))
  if re.fullmatch(r"\d+[.,]\d+", s):
    return float(s.replace(",", "."))
  if re.fullmatch(r"\d+", s):
    return float(s)
  return None


def _entero_si_se_puede(valor: float) -> int | float:
  return int(valor) if float(valor).is_integer() else valor


class _Ocupados:
  """Spans ya interpretados: sus números no cuentan para otra cosa."""

  def __init__(self) -> None:
    self.spans: list[tuple[int, int]] = []

  def libre(self, span: tuple[int, int]) -> bool:
    return all(span[1] <= a or span[0] >= b for a, b in self.spans)

  def marcar(self, span: tuple[int, int]) -> None:
    self.spans.append(span)


# ---------------------------------------------------------------------------
# Fechas
# ---------------------------------------------------------------------------

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
_PALABRAS_NUMERO = {
    "un": 1, "una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6,
    "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12, "quince": 15,
    "veinte": 20, "treinta": 30, "cuarenta": 40, "cincuenta": 50, "cien": 100,
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "hundred": 100,
    "docena": 12, "dozen": 12,
}


def _alternativas(palabras: Iterable[str]) -> str:
  return "|".join(sorted((re.escape(p) for p in palabras), key=len, reverse=True))


_MESES_RE = _alternativas(_MESES)
_DIAS_RE = _alternativas(_DIAS)
_CANT_RE = r"\d{1,3}|" + _alternativas(_PALABRAS_NUMERO)

_P_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_P_BARRA = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?![\d/])")
_P_DE_MES = re.compile(rf"\b(\d{{1,2}})\s+de\s+({_MESES_RE})\b(?:\s+(?:de|del)\s+(\d{{4}}))?")
_P_MES_DIA = re.compile(rf"\b({_MESES_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?")
_P_DIA_MES = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MESES_RE})\b")
_P_RELATIVO = re.compile(
    rf"\b(?:en|dentro\s+de|in|within)\s+(?:(?:menos\s+de|less\s+than)\s+)?({_CANT_RE})\s+"
    r"(dias?|days?|semanas?|weeks?|horas?|hours?|hs)\b"
)
_P_PLAZO_SUELTO = re.compile(r"\b(\d{1,3})\s+(?:dias?|days?|semanas?|weeks?)\b")
_P_ESTA_SEMANA = re.compile(r"\b(?:esta\s+(?:misma\s+)?semana|this\s+week)\b")
_P_SEMANA_PROX = re.compile(
    r"\b(?:la\s+semana\s+que\s+viene|la\s+proxima\s+semana|la\s+semana\s+proxima|next\s+week)\b"
)
_P_FIN_MES = re.compile(r"\b(?:fin(?:es|ales)?\s+de(?:l)?\s+mes|end\s+of\s+(?:the\s+)?month)\b")
_P_PASADO = re.compile(r"\b(?:pasado\s+manana|day\s+after\s+tomorrow)\b")
_P_MANANA = re.compile(r"\b(?:manana|tomorrow)\b")
_P_MANANA_HORA = re.compile(r"\b(?:a|por|de|en)\s+la\s+manana\b|\bin\s+the\s+morning\b")  # no es fecha
_P_HOY = re.compile(r"\b(?:hoy|today)\b")
_P_DIA = re.compile(rf"\b({_DIAS_RE})\b")
# "el lunes 26": el día de la semana y un número sin mes. La palabra que sigue
# (grupo 3, sin consumir) dice si el número es el día o una cantidad ("el
# lunes 20 computadoras").
_P_DIA_NUMERO = re.compile(
    rf"\b({_DIAS_RE})\s+(?:the\s+)?(\d{{1,2}})(?:st|nd|rd|th)?\b(?![.,/:]\d)(?=\s*([a-z]*))"
)


def proximo_dia(hoy: date, dia_semana: int) -> date:
  """El próximo `dia_semana` después de hoy (si hoy es ese día, el de la semana que viene)."""
  return hoy + timedelta(days=((dia_semana - hoy.weekday() - 1) % 7) + 1)


def dia_con_numero(hoy: date, dia_semana: int, numero: int) -> date | None:
  """El primer día desde hoy que es `numero` del mes y cae en `dia_semana`
  ("el lunes 26"), en los próximos 12 meses; None si no hay."""
  for meses in range(13):
    anio, mes = divmod(hoy.month - 1 + meses, 12)
    try:
      fecha = date(hoy.year + anio, mes + 1, numero)
    except ValueError:
      continue
    if fecha >= hoy and fecha.weekday() == dia_semana:
      return fecha
  return None


def _fecha(anio: int | None, mes: int, dia: int, hoy: date) -> date | None:
  """Sin año: este año, o el que viene si ya pasó (dentro de 6 meses)."""
  try:
    if anio is not None:
      return date(anio + 2000 if anio < 100 else anio, mes, dia)
    fecha = date(hoy.year, mes, dia)
    if fecha < hoy:
      siguiente = date(hoy.year + 1, mes, dia)
      return siguiente if (siguiente - hoy).days <= 183 else None
    return fecha
  except ValueError:
    return None


def _cantidad_palabra(texto: str) -> int | None:
  return int(texto) if texto.isdigit() else _PALABRAS_NUMERO.get(texto)


def _fechas(texto: str, hoy: date, ocupados: _Ocupados) -> date | None:
  """La fecha límite: la explícita, si no la relativa y si no el día de la semana.
  Entre las del mismo tipo, la más próxima. Las pasadas no cuentan."""
  explicitas: list[date | None] = []
  relativas: list[date | None] = []
  semana: list[date | None] = []

  def tomar(patron: re.Pattern, destino: list, calcular) -> None:
    for m in patron.finditer(texto):
      if ocupados.libre(m.span()):
        ocupados.marcar(m.span())
        destino.append(calcular(m))

  def anio(grupo: str | None) -> int | None:
    return int(grupo) if grupo else None

  tomar(_P_ISO, explicitas, lambda m: _fecha(int(m[1]), int(m[2]), int(m[3]), hoy))

  def barra(m: re.Match) -> date | None:
    a, b = int(m[1]), int(m[2])
    dia, mes = (b, a) if b > 12 else (a, b)  # dd/mm salvo que no pueda serlo
    return _fecha(anio(m[3]), mes, dia, hoy) if 1 <= mes <= 12 else None

  tomar(_P_BARRA, explicitas, barra)
  tomar(_P_DE_MES, explicitas, lambda m: _fecha(anio(m[3]), _MESES[m[2]], int(m[1]), hoy))
  tomar(_P_MES_DIA, explicitas, lambda m: _fecha(anio(m[3]), _MESES[m[1]], int(m[2]), hoy))
  tomar(_P_DIA_MES, explicitas, lambda m: _fecha(None, _MESES[m[2]], int(m[1]), hoy))

  def relativo(m: re.Match) -> date | None:
    n = _cantidad_palabra(m[1])
    if not n:
      return None
    unidad = m[2]
    if unidad.startswith(("semana", "week")):
      return hoy + timedelta(days=7 * n)
    if unidad.startswith(("hora", "hour", "hs")):
      return hoy + timedelta(days=-(-n // 24))
    return hoy + timedelta(days=n)

  tomar(_P_RELATIVO, relativas, relativo)
  for m in _P_PLAZO_SUELTO.finditer(texto):  # "3 días" sin "en": no es cantidad ni monto
    if ocupados.libre(m.span()):
      ocupados.marcar(m.span())

  def esta_semana(_: re.Match) -> date:
    viernes = hoy + timedelta(days=4 - hoy.weekday())
    return viernes if viernes >= hoy else proximo_dia(hoy, 4)

  tomar(_P_ESTA_SEMANA, relativas, esta_semana)
  tomar(_P_SEMANA_PROX, relativas, lambda _: proximo_dia(hoy, 0) + timedelta(days=4))
  tomar(_P_FIN_MES, relativas, lambda _: date(hoy.year, hoy.month, calendar.monthrange(hoy.year, hoy.month)[1]))
  tomar(_P_PASADO, relativas, lambda _: hoy + timedelta(days=2))
  for m in _P_MANANA_HORA.finditer(texto):
    if ocupados.libre(m.span()):
      ocupados.marcar(m.span())
  tomar(_P_MANANA, relativas, lambda _: hoy + timedelta(days=1))
  tomar(_P_HOY, relativas, lambda _: hoy)
  for m in _P_DIA_NUMERO.finditer(texto):
    numero = int(m[2])
    if not 1 <= numero <= 31 or (m[3] and m[3] not in _VACIAS) or not ocupados.libre(m.span()):
      continue  # "el lunes 20 computadoras": el 20 es la cantidad
    ocupados.marcar(m.span())
    fecha = dia_con_numero(hoy, _DIAS[m[1]], numero)
    if fecha is not None:
      explicitas.append(fecha)
    else:  # no hay un viernes 31 en un año: el próximo viernes
      semana.append(proximo_dia(hoy, _DIAS[m[1]]))
  tomar(_P_DIA, semana, lambda m: proximo_dia(hoy, _DIAS[m[1]]))

  for grupo in (explicitas, relativas, semana):
    validas = [f for f in grupo if f is not None and f >= hoy]
    if validas:
      return min(validas)
  return None


# ---------------------------------------------------------------------------
# Atributos
# ---------------------------------------------------------------------------

_ANTES_MINIMO = re.compile(
    r"\b(?:al\s+menos|como\s+minimo|minimo(?:\s+de)?|min\.?|at\s+least|minimum(?:\s+of)?|desde"
    r"|from|mas\s+de|more\s+than|over|no\s+menos\s+de)\s*$|\+\s*$"
)
_ANTES_MAXIMO = re.compile(
    r"\b(?:hasta|como\s+maximo|maximo(?:\s+de)?|max\.?|up\s+to|at\s+most|maximum(?:\s+of)?"
    r"|menos\s+de|less\s+than|under|below|no\s+mas\s+de|no\s+more\s+than)\s*$"
)
_DESPUES_MINIMO = re.compile(
    r"^\s*(?:o\s+mas|o\s+superior|or\s+more|or\s+larger|or\s+bigger|or\s+higher|or\s+above"
    r"|como\s+minimo|at\s+least|minimo|minimum|\+)"
)
_DESPUES_MAXIMO = re.compile(
    r"^\s*(?:o\s+menos|or\s+less|or\s+smaller|or\s+lighter|or\s+lower|or\s+below|como\s+maximo"
    r"|at\s+most|maximo|maximum)"
)
_RAM_ANTES = re.compile(r"(?:\bram|\bmemoria(?:\s+ram)?|\bmemory)\s*(?:de\s+)?$")
_RAM_DESPUES = re.compile(
    r"^\s*(?:de\s+)?(?:memoria\b(?!\s+(?:interna|de\s+almacenamiento|storage))|ram\b|memory\b)"
)
_DISCO_DESPUES = re.compile(r"^\s*(?:de\s+)?(?:ssd|hdd|nvme|disco|almacenamiento|storage)\b")
_DATO_ANTES = re.compile(rf"(?:{_NUM})\s*(?:gb|tb|mb|gigas?|teras?)\s*(?:de\s+)?$")


def _es_memoria(texto: str, inicio: int, fin: int, unidad: str) -> bool:
  """¿"16 GB" es memoria RAM? Lo dice la palabra pegada al número ("16 GB de
  RAM", "RAM de 16 GB"); "32 GB RAM 2 TB": el "ram" ya es del 32. TB, nunca."""
  if unidad == "TB":
    return False
  despues = texto[fin:]
  if _RAM_DESPUES.match(despues):
    return True
  if _DISCO_DESPUES.match(despues):
    return False
  antes = _RAM_ANTES.search(texto[:inicio])
  return bool(antes) and not _DATO_ANTES.search(texto[:antes.start()])

# (atributo, patrón con el número en el grupo 1 y la unidad en el 2, unidad canónica, operador por defecto)
_UNIDADES: list[tuple[str, re.Pattern, dict[str, str], str]] = [
    ("tamano_pantalla",
     re.compile(rf"({_NUM})\s*(\"|”|''|pulgadas?\b|pulg\b\.?|-?inch(?:es)?\b|-in\b)"),
     {}, "igual"),
    ("datos",
     re.compile(rf"({_NUM})\s*(gb|tb|mb|gigas?|teras?)\b"),
     {"gb": "GB", "giga": "GB", "gigas": "GB", "tb": "TB", "tera": "TB", "teras": "TB", "mb": "MB"},
     "minimo"),
    ("weight",
     re.compile(rf"({_NUM})\s*(kg|kgs|kilos?|kilogramos?|gramos?|gr|lbs?)\b"),
     {"kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg", "kilogramo": "kg", "kilogramos": "kg",
      "gramo": "g", "gramos": "g", "gr": "g", "lb": "lb", "lbs": "lb"},
     "maximo"),
    ("autonomia_bateria",
     re.compile(rf"({_NUM})\s*(horas?|hours?|hrs?|hs|h)\b"),
     {}, "minimo"),
    ("capacity",
     re.compile(rf"({_NUM})\s*(litros?|liters?|litres?|lts?|l)\b"),
     {}, "minimo"),
    ("power",
     re.compile(rf"({_NUM})\s*(watts?|vatios?|w)\b"),
     {}, "minimo"),
    ("megapixel",
     re.compile(rf"({_NUM})\s*(megapixe?les|megapixels?|mpx?)\b"),
     {}, "minimo"),
    ("width",
     re.compile(rf"({_NUM})\s*(cm|centimetros?)\b"),
     {}, "minimo"),
]
_UNIDAD_FIJA = {
    "tamano_pantalla": "in", "autonomia_bateria": "h", "capacity": "l", "power": "W",
    "megapixel": "MP", "width": "cm",
}

_BOOLEANOS: list[tuple[str, re.Pattern, bool]] = [
    ("inalambrico", re.compile(r"\b(?:con\s+cables?|cableado|alambrico|wired)\b"), False),
    ("inalambrico", re.compile(r"\binalambric\w*|\bwireless\b|\bsin\s+cables?\b|\bbluetooth\b"), True),
    ("cancelacion_ruido",
     re.compile(r"\bcancelacion\s+(?:activa\s+)?de(?:l)?\s+ruido\b|\bnoise[\s-]+cancel\w*|\banc\b"), True),
    ("colour_printing",
     re.compile(r"\bimpres\w*\s+(?:a\s+|en\s+)?color\b|\b(?:a|en)\s+color\b|\bcolou?r\s+print\w*"), True),
]
_COLORES = {
    "negro": "negro", "negra": "negro", "negros": "negro", "negras": "negro", "black": "negro",
    "blanco": "blanco", "blanca": "blanco", "blancos": "blanco", "blancas": "blanco", "white": "blanco",
    "gris": "gris", "grises": "gris", "gray": "gris", "grey": "gris",
    "azul": "azul", "azules": "azul", "blue": "azul",
    "rojo": "rojo", "roja": "rojo", "rojos": "rojo", "rojas": "rojo", "red": "rojo",
    "verde": "verde", "verdes": "verde", "green": "verde",
    "rosa": "rosa", "pink": "rosa", "amarillo": "amarillo", "amarilla": "amarillo", "yellow": "amarillo",
    "plateado": "plateado", "plateada": "plateado", "silver": "plateado",
}
_P_COLOR = re.compile(rf"\b({_alternativas(_COLORES)})\b")
_RESOLUCIONES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(?:4k|uhd|2160p|3840\s*x\s*2160)\b"), "3840x2160"),
    (re.compile(r"\b(?:qhd|2k|1440p|2560\s*x\s*1440)\b"), "2560x1440"),
    (re.compile(r"\b(?:full\s+hd|fhd|1080p|1920\s*x\s*1080)\b"), "1920x1080"),
]
_P_RESOLUCION_SUELTA = re.compile(r"\b\d{3,4}\s*x\s*\d{3,4}\b")
_P_WIFI = re.compile(r"\bwi[\s-]?fi\s*([5-7])\b")
_P_TALLE = re.compile(r"\b(?:talle|talla|size|numero|nro\.?|n°)\s*:?\s*(\d{1,2}(?:[.,]5)?)\b")
_USOS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(?:disen\w*|design\w*|creativ\w*|edicion|editing)\b"), "design"),
    (re.compile(r"\b(?:gaming|gamer|juegos?|videojuegos?|games?)\b"), "gaming"),
    (re.compile(r"\b(?:programar|programacion|desarroll\w*|developers?|programming|coding"
                r"|trabajo|trabajar|work|business)\b"), "work"),
    (re.compile(r"\b(?:oficina|office)\b"), "office"),
    (re.compile(r"\b(?:viaj\w*|travel\w*)\b"), "travel"),
    (re.compile(r"\b(?:basquet|basket|basketball|baloncesto)\b"), "basketball"),
    (re.compile(r"\b(?:running|correr|runner)\b"), "running"),
    (re.compile(r"\b(?:exterior|outdoor|aire\s+libre|trekking|trail|hiking)\b"), "outdoor"),
    (re.compile(r"\b(?:urban\w*|calle|street|ciudad)\b"), "urban"),
    (re.compile(r"\b(?:hogar|casa|home)\b"), "home"),
]


def _operador(texto: str, inicio: int, fin: int, desde: int, por_defecto: str) -> str:
  despues = texto[fin:fin + 24]
  if _DESPUES_MINIMO.match(despues):
    return "minimo"
  if _DESPUES_MAXIMO.match(despues):
    return "maximo"
  antes = texto[max(desde, inicio - 28):inicio]
  if _ANTES_MINIMO.search(antes):
    return "minimo"
  if _ANTES_MAXIMO.search(antes):
    return "maximo"
  return por_defecto


def _atributos(texto: str, ocupados: _Ocupados) -> list[AtributoPedido]:
  hallados: list[tuple[int, AtributoPedido]] = []
  for nombre, patron, unidades, por_defecto in _UNIDADES:
    fin_anterior = 0
    for m in patron.finditer(texto):
      if not ocupados.libre(m.span()):
        continue
      valor = _numero(m[1])
      if valor is None or valor <= 0:
        continue
      ocupados.marcar(m.span())
      atributo = nombre
      if nombre == "datos":
        unidad = unidades.get(m[2], "GB")
        atributo = "memoria_ram" if _es_memoria(texto, m.start(), m.end(), unidad) else "almacenamiento"
      else:
        unidad = unidades.get(m[2], _UNIDAD_FIJA.get(nombre))
      operador = _operador(texto, m.start(), m.end(), fin_anterior, por_defecto)
      if atributo == "memoria_ram" and operador == "igual":
        operador = "minimo"
      hallados.append((m.start(), AtributoPedido(atributo, operador, _entero_si_se_puede(valor), unidad)))
      fin_anterior = m.end()
  for nombre, patron, valor in _BOOLEANOS:
    m = patron.search(texto)
    if m and not any(a.atributo == nombre for _, a in hallados):
      hallados.append((m.start(), AtributoPedido(nombre, "igual", valor)))
  m = _P_COLOR.search(texto)
  if m:
    hallados.append((m.start(), AtributoPedido("color", "igual", _COLORES[m[1]])))
  resolucion: tuple[int, str] | None = None
  for patron, valor in _RESOLUCIONES:
    for m in patron.finditer(texto):  # todas ocupan su span ("4K 3840 x 2160")
      ocupados.marcar(m.span())
      if resolucion is None or m.start() < resolucion[0]:
        resolucion = (m.start(), valor)
  for m in _P_RESOLUCION_SUELTA.finditer(texto):
    ocupados.marcar(m.span())
  if resolucion is not None:
    for nombre in ("display_resolution", "maximum_video_resolution"):
      hallados.append((resolucion[0], AtributoPedido(nombre, "igual", resolucion[1])))
  m = _P_WIFI.search(texto)
  if m:
    hallados.append((m.start(), AtributoPedido("wifi_standard", "igual", f"wifi {m[1]}")))
  return [a for _, a in sorted(hallados, key=lambda x: x[0])]


# ---------------------------------------------------------------------------
# Montos
# ---------------------------------------------------------------------------

_MONEDA = {
    "us$": "USD", "u$s": "USD", "u$d": "USD", "usd": "USD", "$": "USD", "dolar": "USD",
    "dolares": "USD", "dollar": "USD", "dollars": "USD", "bucks": "USD",
    "ars": "ARS", "peso": "ARS", "pesos": "ARS",
    "eur": "EUR", "€": "EUR", "euro": "EUR", "euros": "EUR",
    "brl": "BRL", "r$": "BRL", "reales": "BRL",
    "gbp": "GBP", "£": "GBP", "libras": "GBP", "pounds": "GBP",
}
_P_MONTO = re.compile(
    r"(?P<pre>\b(?:hasta|up\s+to|max(?:imo)?|maximum|menos\s+de|less\s+than|under|below|budget"
    r"(?:\s+of)?|presupuesto(?:\s+de)?|no\s+mas\s+de|no\s+more\s+than|tope(?:\s+de)?|por\s+menos\s+de)"
    r"\s*(?:de\s+)?)?"
    r"(?P<cur1>us\$|u\$s|u\$d|r\$|\busd\b|\bars\b|\beur\b|\bbrl\b|\bgbp\b|€|£|\$)?\s*"
    rf"(?P<num>{_NUM})(?!\d)"
    r"(?:\s*(?P<mult>k\b|mil\b|thousand\b))?"
    r"(?:\s*(?:de\s+)?(?P<cur2>usd|us\$|dolares|dolar|dollars|dollar|bucks|pesos|peso|ars|euros|euro"
    r"|eur|brl|reales|gbp|libras|pounds|€|£)\b)?"
    r"(?!\s*(?:unidades|unidad|units?|u\b|pcs|piezas|x\b))"
)
_P_MONEDA_SUELTA = re.compile(
    r"us\$|u\$s|u\$d|r\$|\busd\b|\bars\b|\beur\b|\bbrl\b|\bgbp\b|€|£|\bdolar(?:es)?\b"
    r"|\bdollars?\b|\bpesos?\b|\beuros?\b|\breales\b"
)


def _montos(texto: str, ocupados: _Ocupados) -> tuple[float | None, str | None]:
  candidatos: list[tuple[int, float, str | None]] = []  # (prioridad, monto, moneda)
  for m in _P_MONTO.finditer(texto):
    if not (m["pre"] or m["cur1"] or m["cur2"]):
      continue
    span = (m.start("num"), m.end())
    if not ocupados.libre(span):
      continue
    valor = _numero(m["num"])
    if valor is None or valor <= 0:
      continue
    if m["mult"]:
      valor *= 1000
    ocupados.marcar(span)
    moneda = _MONEDA.get((m["cur1"] or m["cur2"] or "").strip())
    candidatos.append((0 if m["pre"] else 1, valor, moneda))
  if not candidatos:
    return None, None
  _, tope, moneda = min(candidatos, key=lambda c: c[0])
  if moneda is None:
    suelta = _P_MONEDA_SUELTA.search(texto)
    moneda = _MONEDA.get(suelta.group(0)) if suelta else None
  return tope, moneda


# ---------------------------------------------------------------------------
# Cantidad, palabras y el resto
# ---------------------------------------------------------------------------

_P_UNIDADES = re.compile(
    r"\b(\d{1,4})\s*(?:unidades|unidad|units?|u\b|pcs|piezas|items?)\b|\bx\s*(\d{1,4})\b|\b(\d{1,4})\s*x\b"
)
_P_NUMERO_SUELTO = re.compile(r"(?<![\w.,/$-])(\d{1,4})(?=\s+[a-z])")
_P_NUMERO_PALABRA = re.compile(rf"\b({_alternativas(_PALABRAS_NUMERO)})\s+(?=[a-z])")
_VACIAS = frozenset(
    "a al an and any de del el en for i in la las lo los me mi my of on or para por que quiero "
    "necesito busco comprame compra compro buy need want looking the to un una unos unas with y "
    "con sin hasta up by delivered entregar entrega entregado antes before get some please "
    "menos mas al least more less than o or at from desde under over max maximo minimo "
    "es is it this that these esto este esta".split()
)


def _cantidad(texto: str, ocupados: _Ocupados) -> int:
  for m in _P_UNIDADES.finditer(texto):
    if ocupados.libre(m.span()):
      n = int(next(g for g in m.groups() if g))
      if 1 <= n <= CANTIDAD_MAXIMA:
        ocupados.marcar(m.span())
        return n
  for m in _P_NUMERO_SUELTO.finditer(texto):
    if ocupados.libre(m.span()):
      n = int(m[1])
      if 1 <= n <= CANTIDAD_MAXIMA and not 1900 <= n <= 2100:  # un año suelto no es cantidad
        ocupados.marcar(m.span())
        return n
  for m in _P_NUMERO_PALABRA.finditer(texto):  # "un"/"a" ya es el 1 por defecto
    if ocupados.libre(m.span(1)) and _PALABRAS_NUMERO[m[1]] > 1:
      return _PALABRAS_NUMERO[m[1]]
  return 1


def _palabras(texto: str, ocupados: _Ocupados) -> tuple[str, ...]:
  """Las palabras que quedan sin interpretar (sin fechas, montos ni atributos)."""
  libre = list(texto)
  for a, b in ocupados.spans:
    libre[a:b] = " " * (b - a)
  vistas: dict[str, None] = {}
  for palabra in re.findall(r"[a-z][a-z0-9]+", "".join(libre)):
    if palabra not in _VACIAS:
      vistas.setdefault(palabra)
  return tuple(vistas)


def _marcas(texto: str, marcas: Iterable[str]) -> tuple[str, ...]:
  halladas = []
  for marca in marcas:
    if re.search(rf"\b{re.escape(normalizar(marca))}\b", texto) and marca not in halladas:
      halladas.append(marca)
  return tuple(halladas)


def parsear(crudo: Any, hoy: date, *, marcas: Iterable[str] = ()) -> Consulta:
  """`catalog_search` → `Consulta`. Nunca lanza: lo que no entiende no lo usa."""
  original = _como_texto(crudo)
  texto = normalizar(original)
  ocupados = _Ocupados()
  fecha = _fechas(texto, hoy, ocupados)
  atributos = _atributos(texto, ocupados)
  talle_m = _P_TALLE.search(texto)
  talle = None
  if talle_m and ocupados.libre(talle_m.span(1)):
    ocupados.marcar(talle_m.span())
    talle = _entero_si_se_puede(float(talle_m[1].replace(",", ".")))
  tope, moneda = _montos(texto, ocupados)
  cantidad = _cantidad(texto, ocupados)
  uso = next((valor for patron, valor in _USOS if patron.search(texto)), None)
  return Consulta(
      texto=original,
      palabras=_palabras(texto, ocupados),
      categoria_taxonomia=categoria_de(texto) if texto else None,
      cantidad=cantidad,
      fecha_limite=fecha,
      tope=tope,
      moneda=moneda,
      atributos=tuple(atributos),
      talle=talle,
      uso=uso,
      marcas=_marcas(texto, marcas),
  )
