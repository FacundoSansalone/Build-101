"""Mide el intake contra el set de pedidos (`intencion/tests/pedidos_intake.json`).

Para cada pedido simula la conversación: nuestro agente pregunta, el usuario
simulado contesta con las respuestas del caso (en orden) y confirma la ficha
cuando se la piden. Mide:

- **turno 0**: con el pedido solo, ¿el estado de cada parámetro necesario
  (resuelto, a confirmar, faltante) es el esperado? Es la decisión de
  "preguntar o no".
- **valores finales** después de confirmar, y los **resueltos con valor
  equivocado** (el error caro: algo que no se le pregunta al usuario y está mal).
- **repreguntas** contra las esperadas, menciones y cronograma.
- **latencia** por llamada.

Sin red por defecto: mide la cobertura de candidatos (que el valor esperado
esté entre las opciones: el modelo no puede elegir uno que no está) y, si hay
respuestas grabadas, las repite. Contra decisiones real solo con
`RUN_DECISIONES_TESTS=1` (como mucho `--max-llamadas`, 40 por defecto); las
respuestas se graban para repetir y calibrar sin volver a pagar:

    PYTHONUTF8=1 PYTHONPATH=. world/ap2/.venv/Scripts/python -m intencion.evaluar_intake
    RUN_DECISIONES_TESTS=1 ... -m intencion.evaluar_intake --env-file .env
    ... -m intencion.evaluar_intake --calibrar      # barre umbrales con lo grabado

`--env-file` toma solo las claves de decisiones del archivo y nunca las
imprime.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from contracts import Turno
from decisiones import (
    DecisionesError,
    Rechazo,
    Respuesta,
    RespuestaEleccion,
    RespuestaPredicado,
    RespuestaPuntaje,
    cliente_desde_entorno,
)
from intencion import intake
from intencion.agente_decisiones import AgenteDecisiones, cronograma_candidato
from intencion.intake import NECESARIOS, Umbrales

AQUI = Path(__file__).resolve().parent
SET_PATH = AQUI / "tests" / "pedidos_intake.json"
CACHE_PATH = AQUI / "tests" / "respuestas_decisiones.json"
CLAVES_ENV = (
    "OPENAI_API_KEY",
    "TYPESAFE_API_KEY",
    "DECISIONES_PROVEEDOR",
    "DECISIONES_MODELO_OPENAI",
    "DECISIONES_MODELO_JEV",
)
GRILLA = [
    Umbrales(resuelto, duda)
    for resuelto in (0.6, 0.7, 0.8, 0.85, 0.9, 0.95)
    for duda in (0.3, 0.4, 0.5, 0.6)
    if duda < resuelto
]


def cargar_set(path: Path = SET_PATH) -> dict[str, Any]:
    return json.loads(path.read_text("utf-8"))


def leer_env(path: Path) -> dict[str, str]:
    """Solo las claves de decisiones de un .env (nunca se imprimen)."""
    salida: dict[str, str] = {}
    for linea in Path(path).read_text("utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, _, valor = linea.partition("=")
        clave = clave.strip().removeprefix("export ").strip()
        if clave in CLAVES_ENV:
            salida[clave] = valor.strip().strip('"').strip("'")
    return salida


# ---------------------------------------------------------------------------
# Respuestas grabadas
# ---------------------------------------------------------------------------


def serializar(respuestas: Mapping[str, Respuesta]) -> dict[str, Any]:
    salida: dict[str, Any] = {}
    for nombre, r in respuestas.items():
        if isinstance(r, RespuestaEleccion):
            salida[nombre] = {"tipo": "eleccion", "eleccion": r.eleccion,
                              "probabilidades": r.probabilidades, "confianza": r.confianza}
        elif isinstance(r, RespuestaPredicado):
            salida[nombre] = {"tipo": "predicado", "probabilidad": r.probabilidad}
        elif isinstance(r, RespuestaPuntaje):
            salida[nombre] = {"tipo": "puntaje", "puntaje": r.puntaje,
                              "probabilidades": {str(k): v for k, v in r.probabilidades.items()},
                              "confianza": r.confianza}
        else:
            salida[nombre] = {"tipo": "rechazo"}
    return salida


def deserializar(datos: Mapping[str, Any]) -> dict[str, Respuesta]:
    salida: dict[str, Respuesta] = {}
    for nombre, d in datos.items():
        tipo = d.get("tipo")
        if tipo == "eleccion":
            salida[nombre] = RespuestaEleccion(nombre, d["eleccion"], dict(d["probabilidades"]),
                                               d["confianza"])
        elif tipo == "predicado":
            salida[nombre] = RespuestaPredicado(nombre, d["probabilidad"])
        elif tipo == "puntaje":
            salida[nombre] = RespuestaPuntaje(nombre, d["puntaje"],
                                              {int(k): v for k, v in d["probabilidades"].items()},
                                              d["confianza"])
        else:
            salida[nombre] = Rechazo(nombre)
    return salida


class SinGrabacion(LookupError):
    """No hay respuesta grabada para ese turno y no se puede llamar."""


class ClienteGrabado:
    """Envuelve al cliente real: graba por (caso, turno) y repite lo grabado.

    La clave es `<caso>#<respuestas del usuario hasta ahí>`: con otros
    umbrales cambia el texto de nuestras preguntas, pero no lo que contestó
    el usuario, así la calibración repite sin volver a llamar.
    """

    def __init__(self, real: Any, cache: dict[str, Any], max_llamadas: int = 40) -> None:
        self.real = real
        self.cache = cache
        self.max_llamadas = max_llamadas
        self.llamadas = 0  # llamadas reales
        self.latencias: list[float] = []
        self.clave = ""
        self.nombre = getattr(real, "nombre", "grabado")
        self.modelo = getattr(real, "modelo", "grabado")

    def decidir(self, estado: Any, preguntas: list) -> dict[str, Respuesta]:
        entrada = self.cache.get(self.clave)
        if entrada is None:
            if self.real is None or self.llamadas >= self.max_llamadas:
                raise SinGrabacion(self.clave)
            self.llamadas += 1
            inicio = time.monotonic()
            respuestas = self.real.decidir(estado, preguntas)
            entrada = {
                "latencia_s": round(time.monotonic() - inicio, 3),
                "estado": estado,
                "respuestas": serializar(respuestas),
            }
            self.cache[self.clave] = entrada
        self.latencias.append(float(entrada.get("latencia_s") or 0.0))
        respuestas = deserializar(entrada["respuestas"])
        return {p.nombre: respuestas.get(p.nombre, Rechazo(p.nombre)) for p in preguntas}


# ---------------------------------------------------------------------------
# Cobertura de candidatos (sin modelo)
# ---------------------------------------------------------------------------


def _esperados(valor: Any) -> list[Any]:
    return valor if isinstance(valor, list) else [valor]


def del_caso(caso: Mapping[str, Any], hoy: date, categorias: Sequence[str]) -> tuple[date, list[str]]:
    """El día y el catálogo del caso: los del set, salvo que el caso traiga los suyos
    (el pedido del 2026-10-19, un lunes, con el catálogo del comercio general)."""
    if caso.get("hoy"):
        hoy = date.fromisoformat(caso["hoy"])
    return hoy, list(caso.get("categorias") or categorias)


def cobertura(caso: Mapping[str, Any], hoy: date, categorias: Sequence[str]) -> list[str]:
    """Lo esperado que el modelo no podría elegir (lista vacía = todo está)."""
    hoy, categorias = del_caso(caso, hoy, categorias)
    cands = intake.extraer([caso["pedido"], *caso["respuestas"]], hoy)
    problemas: list[str] = []
    final = caso["final"]
    # Una categoría de la taxonomía (un ID) la pone el código si el usuario la nombró.
    nombrada = cands.categoria.id if cands.categoria is not None else None
    for cat in _esperados(final["categoria"]):
        if cat not in (*categorias, "fuera_de_catalogo", "faltante", nombrada):
            problemas.append(f"categoría {cat} no es opción")
    cantidad = final["cantidad"]
    # Los que se ofrecen como tope: sin los números de cosas (ver `intake.preguntas`).
    montos = {c.valor for c in cands.montos if not c.de_cosas}
    cantidades = {c.valor for c in cands.cantidades} | {1}
    if cantidad != "faltante" and cantidad not in cantidades:
        problemas.append(f"cantidad {cantidad} no está entre {sorted(cantidades)}")
    precio = final["precio"]
    if precio != "faltante":
        unidad = precio / cantidad if isinstance(cantidad, int) and cantidad else precio
        if precio not in montos and unidad not in montos:
            problemas.append(f"precio {precio} no está entre {sorted(montos)}")
    for moneda in _esperados(final["moneda"]):
        if moneda not in intake.DESCRIPCION_MONEDAS:
            problemas.append(f"moneda {moneda} no es opción")
    plazo = final["plazo"]
    if plazo not in (None, "faltante") and plazo not in {c.valor for c in cands.fechas}:
        problemas.append(f"plazo {plazo} no está entre {[c.valor for c in cands.fechas]}")
    menciones = {_clave_mencion(m.campo, m.atributo): m.valor for m in cands.menciones}
    for clave, esperado in (caso.get("menciones") or {}).items():
        valor = esperado["valor"] if isinstance(esperado, dict) else esperado
        if menciones.get(clave) != valor:
            problemas.append(f"mención {clave}={valor} no está entre {menciones}")
    if caso.get("cronograma"):
        pares = [[c, f] for c, f, _ in cronograma_candidato(caso["respuestas"], hoy)]
        for par in caso["cronograma"]:
            if par not in pares:
                problemas.append(f"cronograma {par} no está entre {pares}")
    return problemas


def _clave_mencion(campo: str, atributo: str | None) -> str:
    return f"atributo:{atributo}" if campo == "atributo" else campo


# ---------------------------------------------------------------------------
# Simulación
# ---------------------------------------------------------------------------


@dataclass
class ResultadoCaso:
    id: str
    turno0: dict[str, str] = field(default_factory=dict)  # parámetro → estado
    final: dict[str, Any] = field(default_factory=dict)  # parámetro → valor o "faltante"
    estados_finales: dict[str, str] = field(default_factory=dict)
    repreguntas: int = 0
    preguntas: list[str] = field(default_factory=list)
    menciones: dict[str, dict[str, Any]] = field(default_factory=dict)
    cronograma: list[list[Any]] | None = None
    confirmada: bool = False
    sin_cache: bool = False
    error: str | None = None


def _valor_final(p: intake.Parametro, fuera: bool, confirmada: bool) -> Any:
    if p.estado == "faltante" or (p.estado == "a_confirmar" and not confirmada):
        return "faltante"
    if p.nombre == "categoria":  # la del catálogo o el ID de la taxonomía ("278")
        return "fuera_de_catalogo" if fuera else (p.eleccion if p.eleccion is not None else p.valor)
    return p.valor


def correr_caso(
    caso: Mapping[str, Any],
    cliente: ClienteGrabado,
    hoy: date,
    categorias: Sequence[str],
    umbrales: Umbrales | None = None,
) -> ResultadoCaso:
    """La conversación simulada. El usuario confirma cuando se lo piden."""
    hoy, categorias = del_caso(caso, hoy, categorias)
    agente = AgenteDecisiones(cliente, umbrales=umbrales)
    pedido = caso["pedido"]
    res = ResultadoCaso(id=caso["id"])
    pendientes = list(caso["respuestas"])
    turnos: list[Turno] = []
    contexto = None
    r = None
    try:
        cliente.clave = f"{caso['id']}#0"
        r = agente.preguntar(pedido, hoy, list(categorias)).intake
        res.turno0 = {p.nombre: p.estado for p in r.parametros if p.necesario}
        res.preguntas.append(r.pregunta)
        while r.siguiente in ("porque", "repreguntar") and pendientes:
            turnos.append(Turno(pregunta=r.pregunta, respuesta=pendientes.pop(0)))
            cliente.clave = f"{caso['id']}#{len(turnos)}"
            r = agente.evaluar_intake(pedido, turnos, hoy, list(categorias))
            contexto = agente.armar_contexto(
                pedido, turnos[0].pregunta, turnos[0].respuesta or "", hoy, historial=turnos[1:]
            )
            if r.siguiente in ("repreguntar", "confirmar"):
                res.preguntas.append(r.pregunta)
            if r.siguiente == "repreguntar":
                res.repreguntas += 1
    except SinGrabacion:
        res.sin_cache = True
    except DecisionesError as e:
        res.error = str(e)[:200]
    if r is None:
        return res
    res.confirmada = r.siguiente == "confirmar"
    for p in r.parametros:
        if p.necesario:
            res.final[p.nombre] = _valor_final(p, r.fuera_de_catalogo, res.confirmada)
            res.estados_finales[p.nombre] = p.estado
    for q in r.requisitos_autorizables(res.confirmada):
        if q.campo in ("marca", "talle", "atributo"):
            res.menciones[_clave_mencion(q.campo, q.atributo)] = {
                "valor": q.valor, "obligatorio": q.obligatorio,
            }
    if contexto is not None:
        res.cronograma = [[e.cantidad, e.fecha.isoformat()] for e in contexto.necesidad_entregas]
    return res


# ---------------------------------------------------------------------------
# Puntaje
# ---------------------------------------------------------------------------


def _igual(esperado: Any, real: Any) -> bool:
    if isinstance(esperado, (int, float)) and isinstance(real, (int, float)):
        return abs(float(esperado) - float(real)) < 0.01
    if isinstance(esperado, str) and isinstance(real, str):
        return esperado.strip().lower() == real.strip().lower()
    return esperado == real


def puntuar(caso: Mapping[str, Any], res: ResultadoCaso) -> dict[str, Any]:
    t0 = sum(res.turno0.get(n) in _esperados(caso["turno0"][n]) for n in NECESARIOS)
    de_mas = sum(
        res.turno0.get(n) == "faltante" and "faltante" not in _esperados(caso["turno0"][n])
        for n in NECESARIOS
    )
    de_menos = sum(
        res.turno0.get(n) not in (None, "faltante") and _esperados(caso["turno0"][n]) == ["faltante"]
        for n in NECESARIOS
    )
    aciertos_final = {
        n: any(_igual(e, res.final.get(n)) for e in _esperados(caso["final"][n])) for n in NECESARIOS
    }
    resuelto_mal = sum(
        not aciertos_final[n] and res.estados_finales.get(n) == "resuelto" for n in NECESARIOS
    )
    esperadas = caso.get("menciones") or {}
    menciones_ok = 0
    for clave, esperado in esperadas.items():
        real = res.menciones.get(clave)
        if real is None:
            continue
        valor = esperado["valor"] if isinstance(esperado, dict) else esperado
        obligatorio_ok = (
            not isinstance(esperado, dict)
            or "obligatorio" not in esperado
            or real["obligatorio"] == esperado["obligatorio"]
        )
        menciones_ok += _igual(valor, real["valor"]) and obligatorio_ok
    extras = len(set(res.menciones) - set(esperadas))
    cronograma_ok = None
    if caso.get("cronograma"):
        cronograma_ok = sorted(res.cronograma or []) == sorted(caso["cronograma"])
    repreguntas_ok = res.repreguntas == caso["repreguntas"]
    perfecto = (
        all(aciertos_final.values())
        and repreguntas_ok
        and menciones_ok == len(esperadas)
        and extras == 0
        and cronograma_ok is not False
        and res.error is None
        and not res.sin_cache
    )
    return {
        "turno0": (t0, len(NECESARIOS)),
        "final": (sum(aciertos_final.values()), len(NECESARIOS)),
        "fallas_final": [n for n, ok in aciertos_final.items() if not ok],
        "resuelto_mal": resuelto_mal,
        "de_mas": de_mas,
        "de_menos": de_menos,
        "repreguntas": (res.repreguntas, caso["repreguntas"]),
        "repreguntas_ok": repreguntas_ok,
        "menciones": (menciones_ok, len(esperadas)),
        "menciones_extra": extras,
        "cronograma_ok": cronograma_ok,
        "perfecto": perfecto,
    }


def resumen(puntajes: Sequence[dict[str, Any]], latencias: Sequence[float] = ()) -> dict[str, Any]:
    def suma(clave: str, i: int) -> int:
        return sum(p[clave][i] for p in puntajes)

    def pct(a: int, b: int) -> float:
        return round(100.0 * a / b, 1) if b else 0.0

    cron = [p["cronograma_ok"] for p in puntajes if p["cronograma_ok"] is not None]
    n = len(puntajes)
    return {
        "casos": n,
        "turno0": (suma("turno0", 0), suma("turno0", 1)),
        "turno0_pct": pct(suma("turno0", 0), suma("turno0", 1)),
        "final": (suma("final", 0), suma("final", 1)),
        "final_pct": pct(suma("final", 0), suma("final", 1)),
        "resuelto_mal": sum(p["resuelto_mal"] for p in puntajes),
        "de_mas": sum(p["de_mas"] for p in puntajes),
        "de_menos": sum(p["de_menos"] for p in puntajes),
        "repreguntas_exactas": sum(p["repreguntas_ok"] for p in puntajes),
        "repreguntas_real": suma("repreguntas", 0),
        "repreguntas_esperadas": suma("repreguntas", 1),
        "menciones": (suma("menciones", 0), suma("menciones", 1)),
        "menciones_extra": sum(p["menciones_extra"] for p in puntajes),
        "cronograma": (sum(cron), len(cron)),
        "perfectos": sum(p["perfecto"] for p in puntajes),
        "llamadas": len(latencias),
        "latencia_media_s": round(statistics.fmean(latencias), 2) if latencias else None,
        "latencia_p50_s": round(statistics.median(latencias), 2) if latencias else None,
        "latencia_max_s": round(max(latencias), 2) if latencias else None,
    }


def calibrar(
    casos: Sequence[Mapping[str, Any]],
    cache: dict[str, Any],
    hoy: date,
    categorias: Sequence[str],
    grilla: Sequence[Umbrales] = GRILLA,
) -> list[dict[str, Any]]:
    """Repite lo grabado con cada par de umbrales (sin llamar al modelo)."""
    filas = []
    for u in grilla:
        puntajes, sin_cache = [], 0
        for caso in casos:
            res = correr_caso(caso, ClienteGrabado(None, cache), hoy, categorias, u)
            sin_cache += res.sin_cache
            puntajes.append(puntuar(caso, res))
        fila = resumen(puntajes)
        fila["umbrales"] = (u.resuelto, u.duda)
        fila["sin_grabacion"] = sin_cache
        filas.append(fila)
    return filas


def _orden_calibracion(f: dict[str, Any]) -> tuple:
    """Primero el error caro (resuelto con valor equivocado), después no
    preguntar lo que faltaba, después aciertos y repreguntas de más."""
    return (
        f["resuelto_mal"],
        f["de_menos"],
        -f["final_pct"],
        -f["turno0_pct"],
        abs(f["repreguntas_real"] - f["repreguntas_esperadas"]),
        f["sin_grabacion"],
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _linea_caso(caso: Mapping[str, Any], res: ResultadoCaso, p: dict[str, Any]) -> str:
    estado = "OK " if p["perfecto"] else "   "
    extra = []
    if p["fallas_final"]:
        extra.append("falla: " + ", ".join(
            f"{n}={res.final.get(n)!r} (esperado {caso['final'][n]!r})" for n in p["fallas_final"]
        ))
    if p["menciones"][1] or p["menciones_extra"]:
        extra.append(f"menciones {p['menciones'][0]}/{p['menciones'][1]} +{p['menciones_extra']}")
    if p["cronograma_ok"] is not None:
        extra.append(f"cronograma {'ok' if p['cronograma_ok'] else res.cronograma}")
    if res.sin_cache:
        extra.append("sin grabación")
    if res.error:
        extra.append(f"error: {res.error}")
    return (
        f"{estado}{res.id:32} turno0 {p['turno0'][0]}/{p['turno0'][1]}  "
        f"final {p['final'][0]}/{p['final'][1]}  "
        f"repreguntas {p['repreguntas'][0]}/{p['repreguntas'][1]}  " + "  ".join(extra)
    )


def _imprimir_resumen(r: dict[str, Any]) -> None:
    print(f"\nResumen ({r['casos']} casos):")
    print(f"  turno 0, ¿pregunta lo que falta?: {r['turno0'][0]}/{r['turno0'][1]} ({r['turno0_pct']}%)"
          f"  · de más {r['de_mas']} · de menos {r['de_menos']}")
    print(f"  valores finales: {r['final'][0]}/{r['final'][1]} ({r['final_pct']}%)"
          f"  · resueltos con valor equivocado: {r['resuelto_mal']}")
    print(f"  repreguntas exactas: {r['repreguntas_exactas']}/{r['casos']}"
          f"  · total {r['repreguntas_real']} (esperadas {r['repreguntas_esperadas']})")
    print(f"  menciones: {r['menciones'][0]}/{r['menciones'][1]} (extra {r['menciones_extra']})"
          f"  · cronograma: {r['cronograma'][0]}/{r['cronograma'][1]}")
    print(f"  casos perfectos: {r['perfectos']}/{r['casos']}")
    if r["llamadas"]:
        print(f"  latencia por llamada ({r['llamadas']}): media {r['latencia_media_s']} s, "
              f"p50 {r['latencia_p50_s']} s, máx {r['latencia_max_s']} s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--casos", default="", help="ids separados por coma (default: todos)")
    parser.add_argument("--cache", default=str(CACHE_PATH), help="respuestas grabadas (JSON)")
    parser.add_argument("--env-file", default=None, help=".env con OPENAI_API_KEY o TYPESAFE_API_KEY")
    parser.add_argument("--max-llamadas", type=int, default=40)
    parser.add_argument("--regrabar", action="store_true", help="ignora lo grabado (con red)")
    parser.add_argument("--calibrar", action="store_true", help="barre umbrales con lo grabado")
    parser.add_argument("--resuelto", type=float, default=intake.UMBRALES.resuelto)
    parser.add_argument("--duda", type=float, default=intake.UMBRALES.duda)
    args = parser.parse_args(argv)

    datos = cargar_set()
    hoy = date.fromisoformat(datos["hoy"])
    categorias = datos["categorias"]
    ids = {i.strip() for i in args.casos.split(",") if i.strip()}
    casos = [c for c in datos["casos"] if not ids or c["id"] in ids]

    print("Cobertura de candidatos (¿el valor esperado está entre las opciones?):")
    sin_cobertura = 0
    for caso in casos:
        problemas = cobertura(caso, hoy, categorias)
        sin_cobertura += bool(problemas)
        print(f"  {'ok ' if not problemas else 'NO '}{caso['id']}" + (f": {problemas}" if problemas else ""))
    print(f"  {len(casos) - sin_cobertura}/{len(casos)} con todo cubierto")

    cache_path = Path(args.cache)
    cache: dict[str, Any] = {}
    if cache_path.is_file() and not args.regrabar:
        cache = json.loads(cache_path.read_text("utf-8"))
    real = None
    if os.environ.get("RUN_DECISIONES_TESTS") == "1":
        env = dict(os.environ)
        if args.env_file:
            env.update(leer_env(Path(args.env_file)))
        real = cliente_desde_entorno(env)
        if real is None:
            print("\nRUN_DECISIONES_TESTS=1 pero no hay OPENAI_API_KEY ni TYPESAFE_API_KEY.")
            return 2
        print(f"\nContra {real.nombre} ({getattr(real, 'modelo', '?')}), "
              f"hasta {args.max_llamadas} llamadas nuevas.")
    elif not cache:
        print("\nSin respuestas grabadas: para medir con decisiones real, corré con "
              "RUN_DECISIONES_TESTS=1 (y --env-file .env).")
        return 0
    else:
        print(f"\nRepitiendo respuestas grabadas de {cache_path} (sin red).")

    umbrales = Umbrales(args.resuelto, args.duda)
    cliente = ClienteGrabado(real, cache, args.max_llamadas)
    puntajes = []
    print(f"Umbrales: resuelto {umbrales.resuelto}, duda {umbrales.duda}\n")
    for caso in casos:
        res = correr_caso(caso, cliente, hoy, categorias, umbrales)
        p = puntuar(caso, res)
        puntajes.append(p)
        print(_linea_caso(caso, res, p))
    _imprimir_resumen(resumen(puntajes, cliente.latencias))
    if real is not None:
        print(f"  llamadas nuevas a {real.nombre}: {cliente.llamadas}")
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), "utf-8")
        print(f"  respuestas grabadas en {cache_path}")

    if args.calibrar:
        filas = sorted(calibrar(casos, cache, hoy, categorias), key=_orden_calibracion)
        print("\nCalibración (con lo grabado; ordenado por resueltos mal, de menos, aciertos):")
        print("  resuelto duda | turno0 | final | mal | de más | de menos | repreg. | sin grab.")
        for f in filas[:12]:
            marca = " ←" if f["umbrales"] == (umbrales.resuelto, umbrales.duda) else ""
            print(f"  {f['umbrales'][0]:>8} {f['umbrales'][1]:>4} | {f['turno0_pct']:>5}% | "
                  f"{f['final_pct']:>4}% | {f['resuelto_mal']:>3} | {f['de_mas']:>6} | "
                  f"{f['de_menos']:>8} | {f['repreguntas_real']:>3}/{f['repreguntas_esperadas']:<3} | "
                  f"{f['sin_grabacion']:>3}{marca}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
