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

Con `--resto <url>` (solo junto con el prefijo; lo usa el contenedor de
Railway, `deploy/railway/`) lo que **no** lleva el prefijo va a ese upstream
tal cual, en vez del 404: el panel (:8020), que pide su contraseña. A ese le
llega el `Host` público (el panel lo compara con `DASHBOARD_PUBLIC_ORIGINS`);
al comercio y al catálogo, el suyo, como siempre. Con `--salud`, `GET /salud`
responde 200 si el comercio, el catálogo y el resto contestan (cualquier
status por debajo de 500: el 401 del panel cuenta) y 503 si no, sin decir
qué falta (va al log). Es el healthcheck de Railway.

Solo GET, HEAD, POST y OPTIONS; cuerpo de hasta 5 MB; las respuestas (SSE
también) pasan en streaming y, si el cliente corta, se suelta el upstream.
No reescribe nada del contenido. El puerto: `--puerto`, si no `PORT` (el
que da Railway), si no 8090.

Desde la raíz del repo, con la simulación levantada (`bash world/run_world.sh general`):

    PYTHONUTF8=1 world/ap2/.venv/Scripts/python deploy/vercel/tunel_router.py
    ngrok http 8090 --url https://<tu-dominio>.ngrok-free.app

Ver deploy/vercel/README.md ("Túnel") y deploy/railway/README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

PUERTO = 8090
COMERCIO = "http://127.0.0.1:8001"
CATALOGO = "http://127.0.0.1:8011"
RUTA_SALUD = "/salud"
METODOS = frozenset({"GET", "HEAD", "POST", "OPTIONS"})
MAX_CUERPO = 5 * 1024 * 1024
# El create_checkout del comercio del sample usa su LLM: como el motor.
TIMEOUT = httpx.Timeout(600.0, connect=10.0)
TIMEOUT_SALUD = httpx.Timeout(3.0)
_PREFIJO_RE = re.compile(r"[A-Za-z0-9_-]{8,128}")
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade",
})


def destino(
    path: str, *, comercio: str, catalogo: str, prefijo: str | None, resto: str | None = None
) -> tuple[str, str] | None:
    """(base, ruta) a la que va `path`, o None si no se atiende.

    Con prefijo, `path` tiene que ser `/<prefijo>` o empezar con
    `/<prefijo>/`; se lo saca. `/a2a` y lo de abajo van al comercio; el resto
    (también `/`), al catálogo. Lo que no lleva el prefijo va a `resto` tal
    cual, o a ningún lado si no hay."""
    if prefijo:
        base = f"/{prefijo}"
        if path == base:
            path = "/"
        elif path.startswith(base + "/"):
            path = path[len(base):]
        else:
            return (resto, path) if resto else None
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
        resto: str | None = None,
        salud: bool = False,
        transporte: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if resto and not prefijo:
            # Sin prefijo todo es del comercio y del catálogo.
            raise ValueError("el resto necesita un prefijo")
        self.comercio = comercio.rstrip("/")
        self.catalogo = catalogo.rstrip("/")
        self.prefijo = prefijo
        self.resto = resto.rstrip("/") if resto else None
        self.salud = salud
        self._transporte = transporte
        self._client: httpx.AsyncClient | None = None
        self._ultima_salud: tuple[str, ...] | None = None

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
        if self.salud and scope["path"] == RUTA_SALUD:
            await self._responder_salud(scope, send)
            return
        if scope["method"] not in METODOS:
            await _error(send, 405, "metodo_no_permitido")
            return
        a_donde = destino(
            scope["path"], comercio=self.comercio, catalogo=self.catalogo, prefijo=self.prefijo,
            resto=self.resto,
        )
        if a_donde is None:
            await _error(send, 404, "no_encontrado")
            return
        base, ruta = a_donde
        al_resto = self.resto is not None and base == self.resto
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
            if al_resto:
                # El panel compara el Host con su dominio público (DNS rebinding):
                # le llega el que pidió el navegador, no 127.0.0.1.
                headers.append((b"host", host))

        cliente = self._cliente()
        pedido = cliente.build_request(scope["method"], base + ruta, headers=headers, content=cuerpo)
        try:
            respuesta = await cliente.send(pedido, stream=True)
        except httpx.HTTPError as e:
            print(f"[túnel] {scope['method']} {_para_log(ruta, al_resto)} → {base}: "
                  f"{type(e).__name__}", flush=True)
            await _error(send, 502, "simulacion_no_disponible")
            return
        try:
            salida = [
                (n, v) for n, v in respuesta.headers.raw
                if n.decode("latin-1").lower() not in _HOP_BY_HOP
            ]
            await send({"type": "http.response.start", "status": respuesta.status_code,
                        "headers": salida})
            await _copiar_hasta_el_corte(respuesta, receive, send)
        except httpx.HTTPError as e:
            print(f"[túnel] {_para_log(ruta, al_resto)}: se cortó la respuesta "
                  f"({type(e).__name__})", flush=True)
        finally:
            await respuesta.aclose()

    async def _responder_salud(self, scope: dict, send: Callable) -> None:
        if scope["method"] not in ("GET", "HEAD"):
            await _error(send, 405, "metodo_no_permitido")
            return
        chequeos = {"comercio": self.comercio + "/", "catálogo": self.catalogo + "/health"}
        if self.resto:
            chequeos["resto"] = self.resto + "/"
        resultados = await asyncio.gather(*(self._contesta(url) for url in chequeos.values()))
        caidos = tuple(nombre for nombre, ok in zip(chequeos, resultados) if not ok)
        if caidos != self._ultima_salud:
            # Solo los cambios: el healthcheck pregunta seguido.
            print("[túnel] salud: ok" if not caidos else f"[túnel] salud: no contesta {', '.join(caidos)}",
                  flush=True)
            self._ultima_salud = caidos
        cuerpo = json.dumps({"estado": "no_disponible" if caidos else "ok"}).encode()
        await send({"type": "http.response.start", "status": 503 if caidos else 200, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(cuerpo)).encode()),
            (b"cache-control", b"no-store"),
        ]})
        await send({"type": "http.response.body", "body": b"" if scope["method"] == "HEAD" else cuerpo})

    async def _contesta(self, url: str) -> bool:
        """¿Contesta HTTP (cualquier status por debajo de 500)? Sin nada del
        pedido del cliente."""
        try:
            respuesta = await self._cliente().get(url, timeout=TIMEOUT_SALUD)
        except httpx.HTTPError:
            return False
        return respuesta.status_code < 500


def _para_log(ruta: str, al_resto: bool) -> str:
    # Del panel, sin la query: no tiene nada que hacer en un log compartido.
    return ruta.split("?", 1)[0] if al_resto else ruta


async def _copiar_hasta_el_corte(respuesta: httpx.Response, receive: Callable, send: Callable) -> None:
    """La respuesta al cliente a medida que llega, hasta que termina o el
    cliente corta (un SSE del panel no termina nunca: sin esto, cada pestaña
    cerrada dejaba abierta su suscripción al proxy)."""

    async def copiar() -> None:
        async for fragmento in respuesta.aiter_raw():
            await send({"type": "http.response.body", "body": fragmento, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def esperar_corte() -> None:
        while (await receive())["type"] != "http.disconnect":
            pass

    tareas = [asyncio.ensure_future(copiar()), asyncio.ensure_future(esperar_corte())]
    try:
        hechas, _ = await asyncio.wait(tareas, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for tarea in tareas:
            tarea.cancel()
        await asyncio.gather(*tareas, return_exceptions=True)
    if tareas[0] in hechas:
        tareas[0].result()  # un httpx.HTTPError a mitad de camino sube a quien llama


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


class OpcionInvalida(ValueError):
    """Una opción que no sirve (el mensaje no trae el prefijo)."""


@dataclass(frozen=True)
class Opciones:
    host: str
    puerto: int
    comercio: str
    catalogo: str
    prefijo: str | None
    resto: str | None
    salud: bool


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Junta el Merchant Agent (/a2a/*) y el catálogo (el resto) en un solo "
        "puerto, para exponer la simulación con un túnel."
    )
    p.add_argument("--puerto", type=int, default=None,
                   help=f"puerto (si no, PORT; si no, {PUERTO})")
    p.add_argument("--host", default="127.0.0.1",
                   help="interfaz (127.0.0.1: el túnel corre en esta máquina)")
    p.add_argument("--comercio", default=COMERCIO, help=f"Merchant Agent ({COMERCIO})")
    p.add_argument("--catalogo", default=CATALOGO, help=f"catálogo público ({CATALOGO})")
    p.add_argument("--prefijo", default=None,
                   help="secreto de ruta (8 a 128 de [A-Za-z0-9_-]); también TUNEL_PREFIJO")
    p.add_argument("--resto", default=None, metavar="URL",
                   help="upstream para lo que no lleva el prefijo (p. ej. el panel, "
                   "http://127.0.0.1:8020); solo con prefijo y solo por opción, nunca del entorno")
    p.add_argument("--salud", action="store_true",
                   help=f"GET {RUTA_SALUD}: 200 si el comercio, el catálogo y el resto contestan")
    return p


def opciones(argv: list[str] | None, entorno: Mapping[str, str]) -> Opciones:
    """Las opciones de la línea de comandos con lo que se toma del entorno
    (`TUNEL_PREFIJO`, `PORT`). `OpcionInvalida` si algo no sirve."""
    args = parser().parse_args(argv)
    prefijo = args.prefijo or (entorno.get("TUNEL_PREFIJO") or "").strip() or None
    if prefijo is not None and not _PREFIJO_RE.fullmatch(prefijo):
        raise OpcionInvalida("el prefijo tiene que tener de 8 a 128 caracteres de [A-Za-z0-9_-]")
    puerto = args.puerto
    if puerto is None:
        crudo = (entorno.get("PORT") or "").strip()
        if not crudo:
            puerto = PUERTO
        elif crudo.isdigit():
            puerto = int(crudo)
        else:
            raise OpcionInvalida("PORT tiene que ser un número de puerto")
    if not 0 < puerto < 65536:
        raise OpcionInvalida("el puerto tiene que estar entre 1 y 65535")
    resto = args.resto.rstrip("/") if args.resto else None
    if resto is not None:
        partes = urlsplit(resto)
        if partes.scheme not in ("http", "https") or not partes.hostname or partes.path:
            raise OpcionInvalida("--resto tiene que ser http(s)://host[:puerto], sin ruta")
        if prefijo is None:
            raise OpcionInvalida("--resto necesita el prefijo (--prefijo o TUNEL_PREFIJO)")
    return Opciones(host=args.host, puerto=puerto, comercio=args.comercio, catalogo=args.catalogo,
                    prefijo=prefijo, resto=resto, salud=args.salud)


def main(argv: list[str] | None = None) -> int:
    try:
        op = opciones(argv, os.environ)
    except OpcionInvalida as e:
        print(str(e), file=sys.stderr)
        return 2
    import uvicorn

    enrutador = Enrutador(comercio=op.comercio, catalogo=op.catalogo, prefijo=op.prefijo,
                          resto=op.resto, salud=op.salud)
    con_prefijo = "/<prefijo>" if op.prefijo else ""
    if op.resto:
        print(f"[túnel] http://{op.host}:{op.puerto}{con_prefijo}/a2a/* → {op.comercio}; "
              f"{con_prefijo}/… → {op.catalogo}; el resto → {op.resto}", flush=True)
    else:
        print(f"[túnel] http://{op.host}:{op.puerto}{con_prefijo}/a2a/* → {op.comercio}; "
              f"el resto → {op.catalogo}", flush=True)
    if op.salud:
        print(f"[túnel] salud en {RUTA_SALUD}", flush=True)
    uvicorn.run(enrutador, host=op.host, port=op.puerto, log_level="warning",
                lifespan="on", server_header=False, date_header=False, timeout_keep_alive=75)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
