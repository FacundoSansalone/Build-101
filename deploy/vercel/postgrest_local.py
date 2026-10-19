"""Un PostgREST falso por HTTP, para la prueba local con varias instancias sin Supabase.

Sirve en http://127.0.0.1:54321 las tablas del gateway (`gw_*` de
`proxy/sql/003_gateway.sql` y `004_serverless.sql`) con el PostgREST en
memoria de los tests (`proxy/tests/postgrest_falso.py`, `BaseFalsa`): los
mismos filtros, `Prefer`, índices únicos (también parciales), claves
foráneas, `check` y el trigger `gw_compras_proteger`. Así las instancias de
`local_multi.py` comparten la base sin tocar el Supabase real:

    SUPABASE_URL=http://127.0.0.1:54321
    SUPABASE_SERVICE_ROLE_KEY=local-sin-supabase     (cualquier texto: no se valida)

Rutas: `/rest/v1/<tabla>` (GET, POST, PATCH, como PostgREST) y
`GET /__tablas` (cuántas filas tiene cada tabla, para mirar). Cada pedido se
procesa entero sin ceder el loop, como una sentencia en la base. Todo vive
en memoria: al cortarlo se pierde. Solo escucha en loopback.

No es para producción ni para Vercel (`deploy/` no se sube). Desde la raíz
del repo:

    PYTHONUTF8=1 world/ap2/.venv/Scripts/python deploy/vercel/postgrest_local.py

Ver deploy/vercel/README.md ("Prueba local sin Supabase").
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

RAIZ = Path(__file__).resolve().parents[2]
PUERTO = 54321  # el de `supabase start`, así la URL se reconoce
MAX_CUERPO = 5 * 1024 * 1024
PREFIJO_REST = "/rest/v1/"


def _base_falsa() -> Any:
    if str(RAIZ) not in sys.path:
        sys.path.insert(0, str(RAIZ))
    from proxy.tests.postgrest_falso import BaseFalsa

    return BaseFalsa()


def _es_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class PostgrestLocal:
    """ASGI: cada pedido a `/rest/v1/<tabla>` pasa a `BaseFalsa` como un
    `httpx.Request` y vuelve su respuesta tal cual."""

    def __init__(self, base: Any | None = None) -> None:
        self.base = base if base is not None else _base_falsa()

    async def __call__(self, scope: dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope["type"] == "lifespan":
            while True:
                mensaje = await receive()
                if mensaje["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif mensaje["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        path: str = scope["path"]
        if path == "/__tablas" and scope["method"] == "GET":
            cuentas = {nombre: len(t.filas) for nombre, t in self.base.tablas.items()}
            await _responder(send, httpx.Response(200, json=cuentas))
            return
        if not path.startswith(PREFIJO_REST):
            await _responder(send, httpx.Response(404, json={"code": "PGRST000"}))
            return
        cuerpo = await _leer_cuerpo(receive)
        if cuerpo is None:
            await _responder(send, httpx.Response(413, json={"code": "PGRST000"}))
            return
        consulta = scope.get("query_string", b"").decode("latin-1")
        pedido = httpx.Request(
            scope["method"],
            f"http://127.0.0.1{path}" + (f"?{consulta}" if consulta else ""),
            headers=[(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope["headers"]],
            content=cuerpo,
        )
        # Sin await en el medio: una sentencia entera, como en la base.
        respuesta = self.base(pedido)
        await _responder(send, respuesta)


async def _leer_cuerpo(receive: Callable) -> bytes | None:
    partes: list[bytes] = []
    total = 0
    while True:
        mensaje = await receive()
        if mensaje["type"] == "http.disconnect":
            break
        trozo = mensaje.get("body", b"")
        total += len(trozo)
        if total > MAX_CUERPO:
            return None
        partes.append(trozo)
        if not mensaje.get("more_body", False):
            break
    return b"".join(partes)


async def _responder(send: Callable, respuesta: httpx.Response) -> None:
    cuerpo = respuesta.content
    headers = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in respuesta.headers.items()
        if k.lower() not in ("content-length", "transfer-encoding")
    ]
    if cuerpo and not any(k == b"content-type" for k, _ in headers):
        headers.append((b"content-type", b"application/json"))
    headers.append((b"content-length", str(len(cuerpo)).encode()))
    await send({"type": "http.response.start", "status": respuesta.status_code, "headers": headers})
    await send({"type": "http.response.body", "body": cuerpo})


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="PostgREST falso (las tablas gw_* en memoria) por HTTP, para que las "
        "instancias de local_multi.py compartan la base sin Supabase."
    )
    p.add_argument("--puerto", type=int, default=PUERTO, help=f"puerto ({PUERTO})")
    p.add_argument("--host", default="127.0.0.1", help="interfaz (solo loopback)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not _es_loopback(args.host):
        print("solo escucha en loopback (no tiene autenticación)", file=sys.stderr)
        return 2
    import uvicorn

    app = PostgrestLocal()
    print(
        f"[postgrest] http://{args.host}:{args.puerto}{PREFIJO_REST}<tabla> "
        f"({', '.join(sorted(app.base.tablas))}); en memoria",
        flush=True,
    )
    print(json.dumps({"SUPABASE_URL": f"http://{args.host}:{args.puerto}"}), flush=True)
    uvicorn.run(app, host=args.host, port=args.puerto, log_level="warning", lifespan="on",
                server_header=False, date_header=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
