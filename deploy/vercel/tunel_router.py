"""Un solo puerto para exponer la simulación con UN túnel.

El motor en Vercel le habla a la simulación (que sigue en tu máquina) por
dos caminos: el Merchant Agent (A2A, :8001) y el catálogo público (:8011).
Este enrutador los junta en :8090, así alcanza con un túnel:

    /a2a/*   → http://127.0.0.1:8001/a2a/*   (Merchant Agent)
    el resto → http://127.0.0.1:8011/…       (catálogo: /products, /products/<sku>)

Con `--prefijo <secreto>` (o `TUNEL_PREFIJO`) solo atiende rutas que empiezan
con `/<secreto>/` y se lo saca antes de reenviar: el dominio del túnel es
público y así no lo usa cualquiera que lo encuentre (el comercio llama a un
LLM en cada checkout). En el registro de comercios del motor va con el
prefijo: `https://<dominio>/<secreto>/a2a/merchant_agent` y
`https://<dominio>/<secreto>`.

Solo GET, HEAD, POST y OPTIONS; cuerpo de hasta 5 MB; las respuestas (SSE
también) pasan en streaming. No reescribe nada del contenido.

Desde la raíz del repo, con la simulación levantada (`bash world/run_world.sh general`):

    PYTHONUTF8=1 world/ap2/.venv/Scripts/python deploy/vercel/tunel_router.py
    ngrok http 8090 --url https://<tu-dominio>.ngrok-free.app

Ver deploy/vercel/README.md ("Túnel").
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable

import httpx

PUERTO = 8090
COMERCIO = "http://127.0.0.1:8001"
CATALOGO = "http://127.0.0.1:8011"
METODOS = frozenset({"GET", "HEAD", "POST", "OPTIONS"})
MAX_CUERPO = 5 * 1024 * 1024
# El create_checkout del comercio del sample usa su LLM: como el motor.
TIMEOUT = httpx.Timeout(600.0, connect=10.0)
_PREFIJO_RE = re.compile(r"[A-Za-z0-9_-]{8,128}")
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade",
})


def destino(path: str, *, comercio: str, catalogo: str, prefijo: str | None) -> tuple[str, str] | None:
    """(base, ruta) a la que va `path`, o None si no se atiende.

    Con prefijo, `path` tiene que ser `/<prefijo>` o empezar con
    `/<prefijo>/`; se lo saca. `/a2a` y lo de abajo van al comercio; el resto
    (también `/`), al catálogo."""
    if prefijo:
        base = f"/{prefijo}"
        if path == base:
            path = "/"
        elif path.startswith(base + "/"):
            path = path[len(base):]
        else:
            return None
    if path == "/a2a" or path.startswith("/a2a/"):
        return comercio, path
    return catalogo, path


class Enrutador:
    def __init__(
        self,
        *,
        comercio: str = COMERCIO,
        catalogo: str = CATALOGO,
        prefijo: str | None = None,
        transporte: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.comercio = comercio.rstrip("/")
        self.catalogo = catalogo.rstrip("/")
        self.prefijo = prefijo
        self._transporte = transporte
        self._client: httpx.AsyncClient | None = None

    def _cliente(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._transporte, timeout=TIMEOUT, follow_redirects=False,
                trust_env=False,
            )
        return self._client

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] == "lifespan":
            while True:
                mensaje = await receive()
                if mensaje["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif mensaje["type"] == "lifespan.shutdown":
                    if self._client is not None:
                        await self._client.aclose()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        if scope["method"] not in METODOS:
            await _error(send, 405, "metodo_no_permitido")
            return
        a_donde = destino(
            scope["path"], comercio=self.comercio, catalogo=self.catalogo, prefijo=self.prefijo
        )
        if a_donde is None:
            await _error(send, 404, "no_encontrado")
            return
        base, ruta = a_donde
        cuerpo = await _leer_cuerpo(receive)
        if cuerpo is None:
            await _error(send, 413, "cuerpo_demasiado_grande")
            return
        if scope.get("query_string"):
            ruta += "?" + scope["query_string"].decode("latin-1")

        headers = []
        for nombre, valor in scope["headers"]:
            bajo = nombre.decode("latin-1").lower()
            # El Host lo pone httpx (el del upstream); los X-Forwarded-*, nosotros.
            if bajo in _HOP_BY_HOP or bajo in ("host", "content-length") or bajo.startswith("x-forwarded-"):
                continue
            headers.append((nombre, valor))
        host = next((v for n, v in scope["headers"] if n.lower() == b"host"), b"")
        cliente_ip = (scope.get("client") or ("", 0))[0]
        headers += [(b"x-forwarded-proto", b"https"), (b"x-forwarded-for", cliente_ip.encode())]
        if host:
            headers.append((b"x-forwarded-host", host))

        cliente = self._cliente()
        pedido = cliente.build_request(scope["method"], base + ruta, headers=headers, content=cuerpo)
        try:
            respuesta = await cliente.send(pedido, stream=True)
        except httpx.HTTPError as e:
            print(f"[túnel] {scope['method']} {ruta} → {base}: {type(e).__name__}", flush=True)
            await _error(send, 502, "simulacion_no_disponible")
            return
        try:
            salida = [
                (n, v) for n, v in respuesta.headers.raw
                if n.decode("latin-1").lower() not in _HOP_BY_HOP
            ]
            await send({"type": "http.response.start", "status": respuesta.status_code,
                        "headers": salida})
            async for fragmento in respuesta.aiter_raw():
                await send({"type": "http.response.body", "body": fragmento, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except httpx.HTTPError as e:
            print(f"[túnel] {ruta}: se cortó la respuesta ({type(e).__name__})", flush=True)
        finally:
            await respuesta.aclose()


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


async def _error(send: Callable, status: int, codigo: str) -> None:
    cuerpo = json.dumps({"error": codigo}).encode()
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"application/json"), (b"content-length", str(len(cuerpo)).encode()),
    ]})
    await send({"type": "http.response.body", "body": cuerpo})


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Junta el Merchant Agent (/a2a/*) y el catálogo (el resto) en un solo "
        "puerto, para exponer la simulación con un túnel."
    )
    p.add_argument("--puerto", type=int, default=PUERTO, help=f"puerto ({PUERTO})")
    p.add_argument("--host", default="127.0.0.1",
                   help="interfaz (127.0.0.1: el túnel corre en esta máquina)")
    p.add_argument("--comercio", default=COMERCIO, help=f"Merchant Agent ({COMERCIO})")
    p.add_argument("--catalogo", default=CATALOGO, help=f"catálogo público ({CATALOGO})")
    p.add_argument("--prefijo", default=None,
                   help="secreto de ruta (8 a 128 de [A-Za-z0-9_-]); también TUNEL_PREFIJO")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    prefijo = args.prefijo or (os.environ.get("TUNEL_PREFIJO") or "").strip() or None
    if prefijo is not None and not _PREFIJO_RE.fullmatch(prefijo):
        print("el prefijo tiene que tener de 8 a 128 caracteres de [A-Za-z0-9_-]", file=sys.stderr)
        return 2
    import uvicorn

    enrutador = Enrutador(comercio=args.comercio, catalogo=args.catalogo, prefijo=prefijo)
    con_prefijo = "/<prefijo>" if prefijo else ""
    print(f"[túnel] http://{args.host}:{args.puerto}{con_prefijo}/a2a/* → {args.comercio}; "
          f"el resto → {args.catalogo}", flush=True)
    uvicorn.run(enrutador, host=args.host, port=args.puerto, log_level="warning",
                lifespan="on", server_header=False, date_header=False, timeout_keep_alive=75)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
