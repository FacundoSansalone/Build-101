"""Mide el filtro sobre los escenarios de la demo y variantes (`filtro/casos.py`).

Como el benchmark: por caso, si se bloquea o pasa, y en total la tasa de
detección (maliciosos bloqueados) y la de falsos positivos (honestos
bloqueados), por capa:

- heurísticas (capa a, sin red);
- modelo solo (capa b, sin heurísticas): cuánto ve el modelo por su cuenta;
- filtro completo: lo que hace `FiltroSeguridad` (heurísticas y, si no
  marcan nada, el modelo).

Uso, desde la raíz del repo:

    PYTHONUTF8=1 world/ap2/.venv/Scripts/python -m filtro.evaluar

Con decisiones real (gasta llamadas: como mucho `--max-llamadas`, 20 por
defecto; los casos que no entran quedan "sin evaluar"):

    set -a; source .env; set +a
    RUN_DECISIONES_TESTS=1 PYTHONUTF8=1 world/ap2/.venv/Scripts/python -m filtro.evaluar --decisiones
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any

from decisiones import ClienteDecisiones
from filtro import heuristicas
from filtro.casos import Caso, casos
from filtro.modelo import MAX_CARACTERES_MODELO, PREGUNTAS, juzgar
from filtro.nucleo import FiltroSeguridad, desde_entorno
from filtro.textos import RespuestaExcesiva, extraer_textos

BLOQUEA, PASA, SIN_EVALUAR, ERROR = "bloquea", "pasa", "sin evaluar", "error"


class SinPresupuesto(RuntimeError):
    """Se llegó al máximo de llamadas."""


class ClienteMedido:
    """Envuelve al cliente real: cuenta llamadas, corta en el máximo y no repite
    un pedido idéntico (el filtro completo y el modelo solo comparten caché)."""

    def __init__(self, cliente: ClienteDecisiones, maximo: int) -> None:
        self.cliente = cliente
        self.nombre = getattr(cliente, "nombre", "decisiones")
        self.maximo = maximo
        self.llamadas = 0
        self.agotado = False
        self._cache: dict[str, Any] = {}

    def decidir(self, estado: Any, preguntas: list[Any]) -> Any:
        clave = json.dumps(estado, sort_keys=True, ensure_ascii=False)
        if clave in self._cache:
            return self._cache[clave]
        if self.llamadas >= self.maximo:
            self.agotado = True
            raise SinPresupuesto()
        self.llamadas += 1
        respuesta = self.cliente.decidir(estado, preguntas)
        self._cache[clave] = respuesta
        return respuesta


@dataclass
class Fila:
    caso: Caso
    heuristicas: str = PASA
    senales_heuristicas: list[str] = field(default_factory=list)
    modelo: str | None = None
    probabilidades: dict[str, float] = field(default_factory=dict)
    completo: str | None = None
    senales_completo: list[str] = field(default_factory=list)


def _evaluar_heuristicas(fila: Fila) -> None:
    try:
        textos = extraer_textos(fila.caso.respuesta)
    except RespuestaExcesiva:
        fila.heuristicas, fila.senales_heuristicas = BLOQUEA, ["filtro:respuesta_excesiva"]
        return
    fila.senales_heuristicas = heuristicas.revisar(t.texto for t in textos)
    fila.heuristicas = BLOQUEA if fila.senales_heuristicas else PASA


def _evaluar_completo(fila: Fila, medido: ClienteMedido) -> None:
    medido.agotado = False
    r = FiltroSeguridad(cliente=medido)(fila.caso.respuesta)
    if medido.agotado:
        fila.completo = SIN_EVALUAR
        return
    fila.completo = PASA if r.pasa else BLOQUEA
    fila.senales_completo = list(r.senales)


def _evaluar_modelo(fila: Fila, medido: ClienteMedido) -> None:
    libres = [t for t in extraer_textos(fila.caso.respuesta) if t.libre]
    if not libres:
        fila.modelo = PASA  # nada que juzgar
        return
    if sum(len(t.texto) for t in libres) > MAX_CARACTERES_MODELO:
        fila.modelo = BLOQUEA
        return
    medido.agotado = False
    try:
        juicio = juzgar(medido, libres)
    except SinPresupuesto:
        fila.modelo = SIN_EVALUAR
        return
    except Exception:  # noqa: BLE001 - se informa como error, no se oculta
        fila.modelo = ERROR
        return
    fila.probabilidades = dict(juicio.probabilidades)
    fila.modelo = BLOQUEA if (juicio.senales or juicio.sin_respuesta) else PASA


def evaluar(
    lista: list[Caso], cliente: ClienteDecisiones | None = None, max_llamadas: int = 20
) -> tuple[list[Fila], ClienteMedido | None]:
    filas = [Fila(c) for c in lista]
    for fila in filas:
        _evaluar_heuristicas(fila)
    if cliente is None:
        return filas, None
    medido = ClienteMedido(cliente, max_llamadas)
    # Primero el filtro completo (el modelo solo se llama si las heurísticas no
    # marcan nada): son los números que importan. Con lo que sobra, el modelo
    # solo sobre los casos que ya bloquearon las heurísticas.
    for fila in filas:
        _evaluar_completo(fila, medido)
    for fila in filas:
        _evaluar_modelo(fila, medido)
    return filas, medido


# ---------------------------------------------------------------------------
# Resumen y salida
# ---------------------------------------------------------------------------


def _tasa(filas: list[Fila], capa: str) -> dict[str, Any]:
    def valor(f: Fila) -> str | None:
        return getattr(f, capa)

    maliciosos = [f for f in filas if f.caso.malicioso and valor(f) in (BLOQUEA, PASA)]
    honestos = [f for f in filas if not f.caso.malicioso and valor(f) in (BLOQUEA, PASA)]
    return {
        "deteccion": [sum(valor(f) == BLOQUEA for f in maliciosos), len(maliciosos)],
        "falsos_positivos": [sum(valor(f) == BLOQUEA for f in honestos), len(honestos)],
        "sin_evaluar": sum(valor(f) not in (BLOQUEA, PASA) for f in filas),
    }


def _pct(par: list[int]) -> str:
    n, d = par
    return f"{n}/{d} ({100 * n / d:.0f}%)" if d else "0/0"


def _celda(valor: str | None, malicioso: bool) -> str:
    if valor is None:
        return "-"
    if valor in (BLOQUEA, PASA) and (valor == BLOQUEA) != malicioso:
        return f"{valor} (!)"  # se equivocó
    return valor


def _prob(fila: Fila) -> str:
    if not fila.probabilidades:
        return ""
    return " ".join(f"{fila.probabilidades.get(p.nombre, float('nan')):.2f}" for p in PREGUNTAS)


def imprimir(filas: list[Fila], medido: ClienteMedido | None) -> str:
    capas = ["heuristicas"] + (["modelo", "completo"] if medido else [])
    nombres = {"heuristicas": "heurísticas", "modelo": "modelo solo", "completo": "filtro completo"}
    modo = (
        f"heurísticas + decisiones ({medido.nombre}, {medido.llamadas} llamadas)"
        if medido
        else "solo heurísticas (sin red)"
    )
    lineas = [f"## Filtro de seguridad: {len(filas)} casos, modo {modo}", ""]
    cab = ["caso", "grupo", "esperado"] + [nombres[c] for c in capas] + ["señales"]
    if medido:
        cab.append("p(" + "/".join(p.nombre for p in PREGUNTAS) + ")")
    lineas += ["| " + " | ".join(cab) + " |", "|" + "---|" * len(cab)]
    for f in filas:
        senales = f.senales_completo if medido else f.senales_heuristicas
        fila = [f.caso.nombre, f.caso.grupo, "malicioso" if f.caso.malicioso else "honesto"]
        fila += [_celda(getattr(f, c), f.caso.malicioso) for c in capas]
        fila.append(", ".join(senales))
        if medido:
            fila.append(_prob(f))
        lineas.append("| " + " | ".join(fila) + " |")
    lineas += [
        "",
        "| capa | detección (maliciosos bloqueados) | falsos positivos (honestos bloqueados) "
        "| sin evaluar |",
        "|---|---|---|---|",
    ]
    for c in capas:
        t = _tasa(filas, c)
        lineas.append(
            f"| {nombres[c]} | {_pct(t['deteccion'])} | {_pct(t['falsos_positivos'])} "
            f"| {t['sin_evaluar']} |"
        )
    return "\n".join(lineas)


def _json(filas: list[Fila], medido: ClienteMedido | None) -> str:
    capas = ["heuristicas"] + (["modelo", "completo"] if medido else [])
    return json.dumps(
        {
            "modo": "decisiones" if medido else "solo_heuristicas",
            "llamadas": medido.llamadas if medido else 0,
            "casos": [
                {
                    "caso": f.caso.nombre,
                    "grupo": f.caso.grupo,
                    "malicioso": f.caso.malicioso,
                    "heuristicas": f.heuristicas,
                    "senales_heuristicas": f.senales_heuristicas,
                    "modelo": f.modelo,
                    "probabilidades": f.probabilidades,
                    "completo": f.completo,
                    "senales_completo": f.senales_completo,
                }
                for f in filas
            ],
            "resumen": {c: _tasa(filas, c) for c in capas},
        },
        ensure_ascii=False,
        indent=2,
    )


def main(argv: list[str] | None = None, *, cliente: ClienteDecisiones | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    modo = parser.add_mutually_exclusive_group()
    modo.add_argument("--solo-heuristicas", action="store_true", help="sin red (por defecto)")
    modo.add_argument(
        "--decisiones", action="store_true",
        help="también el modelo real (exige RUN_DECISIONES_TESTS=1)",
    )
    parser.add_argument("--max-llamadas", type=int, default=20)
    parser.add_argument("--json", action="store_true", help="salida en JSON")
    args = parser.parse_args(argv)

    if args.decisiones:
        if os.environ.get("RUN_DECISIONES_TESTS") != "1":
            print(
                "--decisiones llama a la API real: exportá RUN_DECISIONES_TESTS=1 para "
                "confirmarlo.",
                file=sys.stderr,
            )
            return 2
        if cliente is None:
            cliente = desde_entorno().cliente  # mismo timeout que en el proxy
        if cliente is None:
            print(
                "No hay cliente de decisiones: falta OPENAI_API_KEY o TYPESAFE_API_KEY.",
                file=sys.stderr,
            )
            return 2
    else:
        cliente = None

    filas, medido = evaluar(casos(), cliente, max(0, args.max_llamadas))
    print(_json(filas, medido) if args.json else imprimir(filas, medido))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
