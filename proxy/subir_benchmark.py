"""`python -m proxy.subir_benchmark results/*.json`: sube corridas a Supabase.

Lee los JSON que escribe `demo_world.benchmark` (`--out ../../results`) y
hace upsert en `benchmark_corridas` y `benchmark_escenarios`. La corrida se
identifica por el nombre del archivo: subirla dos veces no la duplica.

Usa las mismas variables que el proxy (`SUPABASE_URL`,
`SUPABASE_SERVICE_ROLE_KEY`). No toca `world/`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx

from proxy.persistencia import cabeceras, credenciales

PREFER = "resolution=merge-duplicates,return=minimal"


def filas(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(fila de la corrida, filas de sus escenarios) para un archivo de results/."""
    datos = json.loads(path.read_text("utf-8"))
    meta = datos.get("meta") or {}
    escenarios = datos.get("scenarios") or {}
    if not isinstance(meta, dict) or not isinstance(escenarios, dict) or not escenarios:
        raise ValueError(f"{path.name} no tiene el formato del benchmark (meta + scenarios)")
    corrida_id = path.stem
    corrida = {
        "id": corrida_id,
        "started_at": meta.get("started_at"),
        "finished_at": meta.get("finished_at"),
        "modelo": meta.get("model"),
        "decider": meta.get("decider"),
        "trials": meta.get("trials"),
        "guard": meta.get("guard"),
        "meta": meta,
    }
    por_escenario = [
        {
            "corrida_id": corrida_id,
            "escenario": nombre,
            "titulo": escenario.get("title"),
            "ataque": escenario.get("attack"),
            "resumen": escenario.get("summary") or {},
            "intentos": escenario.get("trials") or [],
        }
        for nombre, escenario in escenarios.items()
    ]
    return corrida, por_escenario


def subir(path: Path, client: httpx.Client, url: str, clave: str) -> int:
    corrida, por_escenario = filas(path)
    rest = f"{url}/rest/v1"
    headers = cabeceras(clave, PREFER)
    resp = client.post(
        f"{rest}/benchmark_corridas", params={"on_conflict": "id"}, json=corrida, headers=headers
    )
    resp.raise_for_status()
    resp = client.post(
        f"{rest}/benchmark_escenarios",
        params={"on_conflict": "corrida_id,escenario"},
        json=por_escenario,
        headers=headers,
    )
    resp.raise_for_status()
    return len(por_escenario)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("archivos", nargs="+", type=Path, help="JSON de results/")
    args = parser.parse_args(argv)

    datos = credenciales()
    if datos is None:
        print("Faltan SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY en el entorno.", file=sys.stderr)
        return 2
    url, clave = datos
    fallidos = 0
    with httpx.Client(timeout=30, trust_env=False) as client:
        for path in args.archivos:
            try:
                n = subir(path, client, url, clave)
            except (OSError, ValueError, httpx.HTTPError) as e:
                fallidos += 1
                detalle = e.response.text[:300] if isinstance(e, httpx.HTTPStatusError) else e
                print(f"ERROR {path.name}: {detalle}", file=sys.stderr)
                continue
            print(f"OK {path.name}: {n} escenarios")
    return 1 if fallidos else 0


if __name__ == "__main__":
    raise SystemExit(main())
