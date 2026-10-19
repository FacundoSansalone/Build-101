"""Aplica las migraciones del gateway en Supabase por la API de administración.

    $PY deploy/vercel/aplicar_migraciones.py            # 003 y después 004
    $PY deploy/vercel/aplicar_migraciones.py --solo 004_serverless.sql

Usa `POST https://api.supabase.com/v1/projects/<ref>/database/query` con un
token de acceso personal (`SUPABASE_ACCESS_TOKEN=sbp_…`, de
supabase.com/dashboard/account/tokens). La clave secreta del proyecto no
sirve: solo lee y escribe filas por PostgREST, no crea tablas.

Lee `SUPABASE_URL` y `SUPABASE_ACCESS_TOKEN` del entorno o, si no están, del
`.env` de la raíz. Nunca imprime el token. Las migraciones son idempotentes
(`create … if not exists`, `create or replace`), así que correrlas otra vez no
rompe nada. Después, `verificar_supabase.py` confirma tablas y permisos.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import httpx

RAIZ = Path(__file__).resolve().parents[2]
SQL = RAIZ / "proxy" / "sql"
MIGRACIONES = ("003_gateway.sql", "004_serverless.sql")
API = "https://api.supabase.com/v1/projects/{ref}/database/query"


def _del_env(nombre: str) -> str:
    """El valor del entorno o, si no está, del `.env` de la raíz ("" si no hay)."""
    valor = os.environ.get(nombre, "").strip()
    if valor:
        return valor
    env = RAIZ / ".env"
    if not env.exists():
        return ""
    for linea in env.read_text(encoding="utf-8").splitlines():
        clave, _, resto = linea.partition("=")
        if clave.strip() == nombre:
            return resto.strip().strip('"').strip("'")
    return ""


def ref_del_proyecto(url: str) -> str:
    """`https://<ref>.supabase.co` → `<ref>`."""
    m = re.fullmatch(r"https://([a-z0-9]{20})\.supabase\.co/?", url.strip())
    if not m:
        raise ValueError("SUPABASE_URL no tiene la forma https://<ref>.supabase.co")
    return m.group(1)


def aplicar(ref: str, token: str, archivos: list[Path], *, cliente: httpx.Client) -> int:
    for archivo in archivos:
        r = cliente.post(
            API.format(ref=ref),
            headers={"Authorization": f"Bearer {token}"},
            json={"query": archivo.read_text(encoding="utf-8")},
        )
        if r.status_code >= 300:
            # El cuerpo trae el error de Postgres (no tiene secretos).
            print(f"FALLÓ {archivo.name}: HTTP {r.status_code}: {r.text[:500]}")
            return 1
        print(f"ok {archivo.name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--solo", action="append", choices=MIGRACIONES,
                   help="aplicar solo esta migración (se repite)")
    args = p.parse_args(argv)
    token = _del_env("SUPABASE_ACCESS_TOKEN")
    if not token.startswith("sbp_"):
        print("Falta SUPABASE_ACCESS_TOKEN=sbp_… (supabase.com/dashboard/account/tokens).")
        return 2
    try:
        ref = ref_del_proyecto(_del_env("SUPABASE_URL"))
    except ValueError as e:
        print(e)
        return 2
    archivos = [SQL / nombre for nombre in (args.solo or MIGRACIONES)]
    print(f"Supabase {ref}: {', '.join(a.name for a in archivos)}")
    with httpx.Client(timeout=120) as cliente:
        return aplicar(ref, token, archivos, cliente=cliente)


if __name__ == "__main__":
    sys.exit(main())
