"""Mide el agente de intención contra Gemini real (los tests usan uno falso).

Corre los tres pasos (pregunta, contexto, detección) sobre la oferta de
`fixtures/etapa2/computadoras.json`, en tres casos, varias veces cada uno:

- `miercoles`: hay una oferta con 10 el lunes y 10 el miércoles → tiene que
  proponer esa (cart_2).
- `viernes`: solo hay 10 el lunes y 10 el viernes → no tiene que proponer
  (el segundo grupo empieza el jueves).
- `martes`: solo hay 20 el martes → no tiene que proponer (faltan 10 el lunes).

Uso, desde la raíz del repo (con GOOGLE_API_KEY en el entorno):

    set -a; source .env; set +a
    PYTHONPATH=. world/ap2/.venv/bin/python -m intencion.evaluar_casos --veces 3

Con el plan gratuito de Gemini (15 pedidos por minuto) usá `--pausa 4`.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from contracts import OfertasVistas
from guard.extractor import extraer_ofertas
from intencion.agente import MODELO_POR_DEFECTO, AgenteGemini

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "etapa2" / "computadoras.json"
HOY = date(2026, 10, 18)  # las fechas del fixture son relativas a este domingo


def _casos(datos: dict[str, Any]) -> dict[str, tuple[dict[str, Any], str | None]]:
    """Nombre → (datos con las ofertas del caso, cart esperado o None)."""
    miercoles = copy.deepcopy(datos)

    viernes = copy.deepcopy(datos)
    viernes["carts"] = [c for c in viernes["carts"] if c["cart_id"] == "cart_1"]
    viernes["offer_details"] = [d for d in viernes["offer_details"] if d["cart_id"] == "cart_1"]

    martes = copy.deepcopy(viernes)
    martes["carts"][0]["item_label"] = "Lumen Notebook 14 x20. All 20 units on Tuesday 2026-10-20."
    martes["offer_details"][0]["deliveries"] = [{"quantity": 20, "date": "2026-10-20"}]

    return {"miercoles": (miercoles, "cart_2"), "viernes": (viernes, None), "martes": (martes, None)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--veces", type=int, default=3)
    parser.add_argument("--modelo", default=MODELO_POR_DEFECTO)
    parser.add_argument("--pausa", type=float, default=0.0, help="segundos entre llamadas")
    args = parser.parse_args(argv)

    datos = json.loads(FIXTURE.read_text("utf-8"))
    agente = AgenteGemini(modelo=args.modelo)
    pedido = datos["intent"]["catalog_search"]
    respuesta = datos["contexto_demo"]["respuesta_usuario"]

    def pausa() -> None:
        if args.pausa:
            time.sleep(args.pausa)

    aciertos: dict[str, int] = {}
    errores = 0
    for nombre, (caso, esperado) in _casos(datos).items():
        ofertas = extraer_ofertas(
            OfertasVistas(carts=caso["carts"], offer_details=caso["offer_details"])
        )
        aciertos[nombre] = 0
        for intento in range(1, args.veces + 1):
            try:
                pregunta = agente.preguntar(pedido, HOY)
                pausa()
                contexto = agente.armar_contexto(pedido, pregunta.pregunta, respuesta, HOY)
                pausa()
                oportunidad = agente.detectar_oportunidad(
                    contexto, pregunta.ficha(pedido), ofertas
                )
                pausa()
            except Exception as error:  # noqa: BLE001 - se cuenta como error y sigue
                errores += 1
                print(f"[{nombre} {intento}/{args.veces}] ERROR {type(error).__name__}: "
                      f"{str(error)[:120]}")
                pausa()
                continue
            obtenido = oportunidad.cart_id if oportunidad else None
            acierto = obtenido == esperado
            aciertos[nombre] += acierto
            print(f"[{nombre} {intento}/{args.veces}] {'OK ' if acierto else 'MAL'} "
                  f"esperado={esperado} obtenido={obtenido}")
            print(f"    pregunta: {pregunta.pregunta}")
            print(f"    parámetros: {[(r.campo, r.valor) for r in pregunta.requisitos]}")
            print(f"    necesidad: {[(e.cantidad, e.fecha.isoformat()) for e in contexto.necesidad_entregas]}")
            if oportunidad:
                print(f"    por qué: {oportunidad.por_que}")

    print()
    for nombre, cantidad in aciertos.items():
        print(f"{nombre:10s} {cantidad}/{args.veces}")
    total = sum(aciertos.values())
    print(f"{'total':10s} {total}/{args.veces * len(aciertos)}  (modelo {args.modelo})")
    if errores:
        print(f"errores del modelo (503, cuota…): {errores}: no cuentan como acierto")
    return 0 if total == args.veces * len(aciertos) else 1


if __name__ == "__main__":
    sys.exit(main())
