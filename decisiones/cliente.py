"""Juicios tipados con probabilidad: OpenAI Decisions o Jev (TypeSafe).

Una sola interfaz para las dos APIs de "System One": el código arma las
preguntas (sí/no, elegir una opción, puntaje en una escala) y el modelo
devuelve probabilidades. Nunca genera texto. Los umbrales los pone quien
llama, en código.

- OpenAI Decisions: `POST https://api.openai.com/v1/decisions`, modelo
  `gpt-6-luna` (beta pública desde 2026-10-06).
- Jev: `POST https://api.typesafe.ai/v1/systemone`, modelo `jev-latest`.

Va por HTTP con httpx (sin SDK) para no sumar dependencias a world/ap2.
Cualquier falla (red, HTTP, formato) es `DecisionesError`: quien llama
decide fallar cerrado (el filtro bloquea, el intake repregunta).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

# ---------------------------------------------------------------------------
# Preguntas
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Predicado:
    """¿Se cumple esto? Probabilidad de sí."""

    nombre: str
    instrucciones: str | dict | list


@dataclass(frozen=True)
class Eleccion:
    """Una opción de un conjunto cerrado (valor -> descripción o None)."""

    nombre: str
    instrucciones: str | dict | list
    opciones: Mapping[str, str | None]


@dataclass(frozen=True)
class Puntaje:
    """Posición en una escala ordenada de menor a mayor (2 a 10 niveles)."""

    nombre: str
    instrucciones: str | dict | list
    niveles: tuple[str, ...]


Pregunta = Predicado | Eleccion | Puntaje

# ---------------------------------------------------------------------------
# Respuestas
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RespuestaPredicado:
    nombre: str
    probabilidad: float


@dataclass(frozen=True)
class RespuestaEleccion:
    nombre: str
    eleccion: str
    probabilidades: dict[str, float]
    confianza: float


@dataclass(frozen=True)
class RespuestaPuntaje:
    nombre: str
    puntaje: float                  # promedio ponderado de los índices de nivel
    probabilidades: dict[int, float]
    confianza: float


@dataclass(frozen=True)
class Rechazo:
    """El modelo no contestó esa pregunta: tratarlo como "no sé"."""

    nombre: str


Respuesta = RespuestaPredicado | RespuestaEleccion | RespuestaPuntaje | Rechazo


class DecisionesError(RuntimeError):
    """La API no respondió algo usable. Quien llama falla cerrado."""


class ClienteDecisiones(Protocol):
    nombre: str

    def decidir(
        self, estado: str | dict | list, preguntas: list[Pregunta]
    ) -> dict[str, Respuesta]:
        """Una llamada con todas las preguntas; respuestas por nombre."""
        ...


# ---------------------------------------------------------------------------
# Utilidades comunes
# ---------------------------------------------------------------------------

_REINTENTABLES = {429, 500, 502, 503, 529}


def _probabilidad(valor: Any) -> float:
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        raise DecisionesError(f"probabilidad no numérica: {valor!r}")
    if not 0.0 <= float(valor) <= 1.0:
        raise DecisionesError(f"probabilidad fuera de [0, 1]: {valor!r}")
    return float(valor)


def _validar_nombres(preguntas: list[Pregunta]) -> None:
    nombres = [p.nombre for p in preguntas]
    if not nombres:
        raise ValueError("hace falta al menos una pregunta")
    if len(set(nombres)) != len(nombres):
        raise ValueError("los nombres de las preguntas tienen que ser únicos")


@dataclass
class _Http:
    url: str
    clave: str
    timeout: float = 20.0
    reintentos: int = 2
    transport: httpx.BaseTransport | None = field(default=None, repr=False)

    def post(self, cuerpo: dict[str, Any]) -> dict[str, Any]:
        cabeceras = {"Authorization": f"Bearer {self.clave}", "Content-Type": "application/json"}
        ultimo: Exception | None = None
        for intento in range(self.reintentos + 1):
            try:
                with httpx.Client(transport=self.transport, timeout=self.timeout) as c:
                    resp = c.post(self.url, headers=cabeceras, json=cuerpo)
            except httpx.HTTPError as e:
                ultimo = e
            else:
                if resp.status_code == 200:
                    try:
                        datos = resp.json()
                    except ValueError as e:
                        raise DecisionesError("respuesta que no es JSON") from e
                    if not isinstance(datos, dict):
                        raise DecisionesError("respuesta que no es un objeto JSON")
                    return datos
                if resp.status_code not in _REINTENTABLES:
                    raise DecisionesError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                ultimo = DecisionesError(f"HTTP {resp.status_code}")
            if intento < self.reintentos:
                time.sleep(0.5 * 2**intento)
        raise DecisionesError(f"sin respuesta usable: {ultimo}")


# ---------------------------------------------------------------------------
# OpenAI Decisions
# ---------------------------------------------------------------------------


@dataclass
class OpenAIDecisiones:
    clave: str
    modelo: str = "gpt-6-luna"
    url: str = "https://api.openai.com/v1/decisions"
    timeout: float = 20.0
    transport: httpx.BaseTransport | None = field(default=None, repr=False)
    nombre: str = "openai-decisions"

    def decidir(
        self, estado: str | dict | list, preguntas: list[Pregunta]
    ) -> dict[str, Respuesta]:
        _validar_nombres(preguntas)
        entrada = estado if isinstance(estado, str) else json.dumps(estado, ensure_ascii=False)
        cuerpo = {
            "model": self.modelo,
            "input": entrada,
            "questions": [self._pregunta(p) for p in preguntas],
        }
        datos = _Http(self.url, self.clave, self.timeout, transport=self.transport).post(cuerpo)
        return self._respuestas(datos, preguntas)

    @staticmethod
    def _pregunta(p: Pregunta) -> dict[str, Any]:
        instrucciones = (
            p.instrucciones
            if isinstance(p.instrucciones, str)
            else json.dumps(p.instrucciones, ensure_ascii=False)
        )
        if isinstance(p, Predicado):
            return {"type": "predicate", "name": p.nombre, "instructions": instrucciones}
        if isinstance(p, Eleccion):
            return {
                "type": "choice",
                "name": p.nombre,
                "instructions": instrucciones,
                "choices": [
                    {"value": v, "description": d or v} for v, d in p.opciones.items()
                ],
            }
        return {
            "type": "score",
            "name": p.nombre,
            "instructions": instrucciones,
            "levels": [{"label": f"nivel_{i}", "description": d} for i, d in enumerate(p.niveles)],
        }

    @staticmethod
    def _respuestas(datos: dict[str, Any], preguntas: list[Pregunta]) -> dict[str, Respuesta]:
        por_nombre = {p.nombre: p for p in preguntas}
        salida: dict[str, Respuesta] = {}
        for a in datos.get("answers") or []:
            if not isinstance(a, dict) or a.get("name") not in por_nombre:
                continue
            nombre, tipo = a["name"], a.get("type")
            try:
                if tipo == "predicate":
                    salida[nombre] = RespuestaPredicado(nombre, _probabilidad(a["probability"]))
                elif tipo == "choice":
                    probs = {
                        str(x["value"]): _probabilidad(x["probability"]) for x in a["probabilities"]
                    }
                    salida[nombre] = RespuestaEleccion(
                        nombre, str(a["choice"]), probs, _probabilidad(a["confidence"])
                    )
                elif tipo == "score":
                    probs = {
                        int(x["value"]): _probabilidad(x["probability"]) for x in a["probabilities"]
                    }
                    salida[nombre] = RespuestaPuntaje(
                        nombre, float(a["score"]), probs, _probabilidad(a["confidence"])
                    )
                else:  # "refusal" u otro tipo desconocido: no sé
                    salida[nombre] = Rechazo(nombre)
            except (KeyError, TypeError, ValueError) as e:
                raise DecisionesError(f"respuesta mal formada para {nombre!r}: {e}") from e
        for nombre in por_nombre:
            salida.setdefault(nombre, Rechazo(nombre))  # sin respuesta = no sé
        return salida


# ---------------------------------------------------------------------------
# Jev (TypeSafe)
# ---------------------------------------------------------------------------


@dataclass
class JevDecisiones:
    clave: str
    modelo: str = "jev-latest"
    url: str = "https://api.typesafe.ai/v1/systemone"
    timeout: float = 20.0
    transport: httpx.BaseTransport | None = field(default=None, repr=False)
    nombre: str = "jev"

    def decidir(
        self, estado: str | dict | list, preguntas: list[Pregunta]
    ) -> dict[str, Respuesta]:
        _validar_nombres(preguntas)
        cuerpo = {
            "model": self.modelo,
            "state": estado,
            "questions": {p.nombre: self._pregunta(p) for p in preguntas},
        }
        datos = _Http(self.url, self.clave, self.timeout, transport=self.transport).post(cuerpo)
        return self._respuestas(datos, preguntas)

    @staticmethod
    def _pregunta(p: Pregunta) -> dict[str, Any]:
        if isinstance(p, Predicado):
            return {"type": "noul", "instructions": p.instrucciones}
        if isinstance(p, Eleccion):
            return {"type": "choice", "instructions": p.instrucciones, "criteria": dict(p.opciones)}
        return {"type": "score", "instructions": p.instrucciones, "criteria": list(p.niveles)}

    @staticmethod
    def _respuestas(datos: dict[str, Any], preguntas: list[Pregunta]) -> dict[str, Respuesta]:
        respuestas = datos.get("answers") or {}
        salida: dict[str, Respuesta] = {}
        for p in preguntas:
            a = respuestas.get(p.nombre) if isinstance(respuestas, dict) else None
            if not isinstance(a, dict):
                salida[p.nombre] = Rechazo(p.nombre)
                continue
            try:
                if a.get("type") == "noul":
                    salida[p.nombre] = RespuestaPredicado(p.nombre, _probabilidad(a["noul"]))
                elif a.get("type") == "choice":
                    probs = {str(k): _probabilidad(v) for k, v in a["probabilities"].items()}
                    salida[p.nombre] = RespuestaEleccion(
                        p.nombre, str(a["choice"]), probs, _probabilidad(a["confidence"])
                    )
                elif a.get("type") == "score":
                    probs = {int(k): _probabilidad(v) for k, v in a["probabilities"].items()}
                    salida[p.nombre] = RespuestaPuntaje(
                        p.nombre, float(a["score"]), probs, _probabilidad(a["confidence"])
                    )
                else:
                    salida[p.nombre] = Rechazo(p.nombre)
            except (KeyError, TypeError, ValueError, AttributeError) as e:
                raise DecisionesError(f"respuesta mal formada para {p.nombre!r}: {e}") from e
        return salida


# ---------------------------------------------------------------------------
# Selección por entorno
# ---------------------------------------------------------------------------


def cliente_desde_entorno(env: Mapping[str, str] | None = None) -> ClienteDecisiones | None:
    """`DECISIONES_PROVEEDOR=openai|jev`; sin la variable, el que tenga clave
    (primero OpenAI). None si no hay ninguno: quien llama decide qué hacer."""
    env = os.environ if env is None else env
    proveedor = (env.get("DECISIONES_PROVEEDOR") or "").strip().lower()
    openai_clave = (env.get("OPENAI_API_KEY") or "").strip()
    jev_clave = (env.get("TYPESAFE_API_KEY") or "").strip()
    if proveedor not in ("", "openai", "jev"):
        raise ValueError(f"DECISIONES_PROVEEDOR desconocido: {proveedor!r}")
    if proveedor in ("", "openai") and openai_clave:
        modelo = env.get("DECISIONES_MODELO_OPENAI") or "gpt-6-luna"
        return OpenAIDecisiones(openai_clave, modelo=modelo)
    if proveedor in ("", "jev") and jev_clave:
        modelo = env.get("DECISIONES_MODELO_JEV") or "jev-latest"
        return JevDecisiones(jev_clave, modelo=modelo)
    return None
