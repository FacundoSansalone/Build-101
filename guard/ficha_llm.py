"""Ficha del pedido con un LLM: el único uso de LLM del Guard.

Convierte el pedido en lenguaje natural en requisitos estructurados (mismo
formato que `scenarios.json` → `intent`) con salida estructurada de Gemini,
y devuelve `Ficha.desde_intent(..., confirmada=False)`: la ficha **no** está
confirmada hasta que el usuario la acepta. Las decisiones del Guard nunca
pasan por acá; `evaluar()` sigue siendo determinístico.

Para la demo y el benchmark alcanza con `Ficha.desde_intent()`.

La salida del LLM se valida antes de armar la ficha (si no, ValueError):
`max_price` finito y > 0, `currency` ISO 4217 de 3 letras, textos de hasta
64 caracteres, y `hard` tiene que traer `max_price` y `currency` (sin tope
ni moneda no hay mandato).
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

from contracts import Ficha

MODELO_POR_DEFECTO = "gemini-3.1-flash-lite-preview"

# Claves de intent.hard / intent.preferred que entiende Ficha.desde_intent().
_CLAVES = ("max_price", "currency", "category", "use", "size", "brand", "merchant")

_TIPOS = {
    "max_price": "NUMBER",
    "currency": "STRING",
    "category": "STRING",
    "use": "STRING",
    "size": "INTEGER",
    "brand": "STRING",
    "merchant": "STRING",
}

_GRUPO = {
    "type": "OBJECT",
    "properties": {clave: {"type": tipo, "nullable": True} for clave, tipo in _TIPOS.items()},
}

# Esquema de salida (formato Schema de google-genai). Equivale a intent.
ESQUEMA_SALIDA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {"hard": _GRUPO, "preferred": _GRUPO},
    "required": ["hard", "preferred"],
}

_INSTRUCCION = """\
Convertí el pedido de compra del usuario en requisitos estructurados.
- "hard": lo que el usuario exige. "preferred": lo que prefiere pero no exige.
- max_price: tope de precio (número). currency: código ISO 4217 (por ejemplo USD).
- category: tipo de producto. use: uso del producto. size: talle (entero).
- brand: marca. merchant: comercio, solo si el usuario lo menciona.
- La marca es una preferencia: va en "preferred" y en "hard" queda null.
  Solo va en "hard" si el usuario dice que no acepta otra marca ("solo Nike",
  "only Nike", "tiene que ser Nike"). Ejemplo: "I want Nike running shoes"
  → preferred.brand = "Nike", hard.brand = null.
- Un presupuesto ("hasta 150 USD", "up to 150 USD") es un tope obligatorio
  (hard.max_price) con su moneda (hard.currency).
- Tipo de producto, uso y talle pedidos son obligatorios (hard).
- Dejá en null todo lo que el pedido no menciona. No inventes requisitos.
"""


LARGO_MAXIMO_TEXTO = 64
_ISO_4217 = re.compile(r"[A-Z]{3}")

_Texto = Annotated[str, StringConstraints(max_length=LARGO_MAXIMO_TEXTO)]


class _Requisitos(BaseModel):
    """Lo que devuelve el LLM, validado: nada que no sea una ficha sana pasa."""

    model_config = ConfigDict(extra="ignore")

    max_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    currency: str | None = None
    category: _Texto | None = None
    use: _Texto | None = None
    size: int | None = Field(default=None, gt=0)
    brand: _Texto | None = None
    merchant: _Texto | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _vacios_a_none(cls, valor: Any) -> Any:
        """"" o espacios cuentan como "no lo dijo"; los textos se recortan."""
        if isinstance(valor, str):
            valor = valor.strip()
            return valor or None
        return valor

    @field_validator("max_price", "size", mode="before")
    @classmethod
    def _sin_booleanos(cls, valor: Any) -> Any:
        if isinstance(valor, bool):
            raise ValueError("no puede ser un booleano")
        return valor

    @field_validator("currency", mode="before")
    @classmethod
    def _moneda_iso(cls, valor: Any) -> Any:
        # Los validadores "before" corren del último al primero: se recorta acá.
        if isinstance(valor, str):
            valor = valor.strip().upper() or None
        if valor is None:
            return None
        if not isinstance(valor, str) or not _ISO_4217.fullmatch(valor):
            raise ValueError("currency tiene que ser un código ISO 4217 de 3 letras")
        return valor


class _Salida(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hard: _Requisitos
    preferred: _Requisitos = _Requisitos()


def vocabulario_desde_catalogo(catalogo: dict[str, dict]) -> dict[str, list[str]]:
    """Valores que usa el catálogo (sku -> producto), para orientar al LLM."""
    claves = {"category": "category", "use": "use", "brand": "brand", "currency": "currency"}
    vocabulario: dict[str, list[str]] = {}
    for clave, clave_catalogo in claves.items():
        valores = {
            p[clave_catalogo]
            for p in catalogo.values()
            if isinstance(p.get(clave_catalogo), str) and p[clave_catalogo]
        }
        if valores:
            vocabulario[clave] = sorted(valores)
    return vocabulario


def ficha_desde_pedido(
    pedido: str,
    cliente: Any = None,
    modelo: str = MODELO_POR_DEFECTO,
    vocabulario: dict[str, list[str]] | None = None,
) -> Ficha:
    """Pedido en lenguaje natural → `Ficha` sin confirmar.

    `cliente` es un `google.genai.Client` (o algo con la misma interfaz);
    si es None se crea uno, que toma la clave de GOOGLE_API_KEY.
    `vocabulario` (opcional, p. ej. `vocabulario_desde_catalogo()`) le dice
    al modelo qué valores usar para que la ficha se pueda chequear contra el
    catálogo. Lanza ValueError si la respuesta no es una ficha válida.
    """
    if not pedido or not pedido.strip():
        raise ValueError("El pedido está vacío")
    if cliente is None:
        from google import genai

        cliente = genai.Client()

    respuesta = cliente.models.generate_content(
        model=modelo,
        contents=pedido,
        config={
            "system_instruction": _instruccion(vocabulario),
            "response_mime_type": "application/json",
            "response_schema": ESQUEMA_SALIDA,
            "temperature": 0,
        },
    )
    salida = _parsear(respuesta)
    if salida.hard.max_price is None or salida.hard.currency is None:
        raise ValueError(
            "La respuesta del modelo no trae max_price y currency obligatorios "
            "(hard): sin tope ni moneda no hay mandato contra el cual chequear."
        )
    hard = _limpiar(salida.hard)
    preferred = {k: v for k, v in _limpiar(salida.preferred).items() if k not in hard}
    intent = {
        "user_request": pedido,
        "hard": hard,
        "preferred": preferred,
        "criterion": "lowest_price_among_compliant",
    }
    return Ficha.desde_intent(intent, confirmada=False)


def _instruccion(vocabulario: dict[str, list[str]] | None) -> str:
    if not vocabulario:
        return _INSTRUCCION
    lineas = [f"- {clave}: {', '.join(valores)}" for clave, valores in vocabulario.items()]
    return (
        _INSTRUCCION
        + "Cuando corresponda, usá exactamente uno de estos valores del catálogo:\n"
        + "\n".join(lineas)
        + "\n"
    )


def _parsear(respuesta: Any) -> _Salida:
    """Valida la salida del modelo. Nada que no sea una ficha válida pasa."""
    try:
        parseado = getattr(respuesta, "parsed", None)
        datos = parseado if isinstance(parseado, dict) else json.loads(respuesta.text)
        return _Salida.model_validate(datos)
    except (ValidationError, ValueError, TypeError) as error:
        raise ValueError(f"La respuesta del modelo no es una ficha válida: {error}") from error


def _limpiar(requisitos: _Requisitos) -> dict[str, Any]:
    """Saca nulos y vacíos y normaliza moneda y textos, en orden fijo."""
    limpio: dict[str, Any] = {}
    for clave in _CLAVES:
        valor = getattr(requisitos, clave)
        if isinstance(valor, str):
            valor = valor.strip().upper() if clave == "currency" else valor.strip()
        if valor in (None, ""):
            continue
        limpio[clave] = valor
    return limpio
