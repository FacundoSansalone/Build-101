"""Línea de comandos de la taxonomía.

    python -m taxonomia descargar [--locale en-US]
        Baja el archivo oficial a la caché local (.temp-db/taxonomia/).

    python -m taxonomia subconjunto --ids 187,278,223 [--locale en-US] [--salida RUTA]
        Arma el subconjunto (ramas pedidas + raíces y hermanos en el camino)
        desde el archivo completo de la caché. Así se generó el del repo.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from taxonomia.cargar import RUTA_SUBCONJUNTO, cargar_completa, descargar

ENCABEZADO_SUBCONJUNTO = (
    "Subconjunto de la taxonomía de productos de Google para la demo de Pre-Sign Guard.",
    "Fuente: https://www.google.com/basepages/producttype/taxonomy-with-ids.en-US.txt",
    "Regenerar: python -m taxonomia subconjunto --ids {ids}",
)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m taxonomia")
    sub = parser.add_subparsers(dest="comando", required=True)
    p_desc = sub.add_parser("descargar")
    p_desc.add_argument("--locale", default="en-US")
    p_sub = sub.add_parser("subconjunto")
    p_sub.add_argument("--ids", required=True, help="IDs de las ramas, separados por comas")
    p_sub.add_argument("--locale", default="en-US")
    p_sub.add_argument("--salida", default=str(RUTA_SUBCONJUNTO))
    args = parser.parse_args(argv)

    if args.comando == "descargar":
        print(descargar(args.locale))
        return
    ids = [i.strip() for i in args.ids.split(",") if i.strip()]
    completa = cargar_completa(args.locale)
    encabezado = [linea.format(ids=",".join(ids)) for linea in ENCABEZADO_SUBCONJUNTO]
    texto = completa.subconjunto(ids).a_texto(encabezado)
    Path(args.salida).write_text(texto, "utf-8")
    print(f"{args.salida}: {texto.count(chr(10)) - len(encabezado)} líneas")


if __name__ == "__main__":
    main()
