"""Protección de la API de control (/guard/*) del proxy.

- `Host` tiene que ser `localhost:<puerto>` o `127.0.0.1:<puerto>` en todas
  las rutas /guard/* (si no, 403): frena el DNS rebinding, en el que una
  página ajena resuelve su dominio a 127.0.0.1 y lee la API como "mismo
  origen".
- Los POST que cambian estado (/guard/state, /guard/aprobaciones) exigen
  `Content-Type: application/json` (415): un formulario o un `fetch` simple
  desde otra página no pueden mandarlo sin preflight de CORS.
- Si el pedido trae `Origin`, tiene que ser uno de los orígenes del
  dashboard (403). Sin `Origin` (el reenvío del servidor del panel) se
  acepta.
- Esos POST exigen además `X-Guard-Token` (403): un token aleatorio que el
  proxy genera en cada arranque y escribe en `.temp-db/guard_api_token`
  (modo 0600 donde aplica). Así un proceso local, o el agente con una
  herramienta HTTP, no puede apagar el Guard ni aprobar un checkout. El
  servidor del dashboard lo lee y lo agrega en su reenvío; el navegador nunca
  lo ve. Los GET de solo lectura no lo piden.

Middleware ASGI puro (no BaseHTTPMiddleware) para no interferir con el SSE.
"""

from __future__ import annotations

import hmac
import os
import secrets
from collections.abc import Iterable
from pathlib import Path

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

PREFIJO_CONTROL = "/guard/"
HEADER_TOKEN = "X-Guard-Token"


def generar_token() -> str:
    """Token aleatorio de la API de control (uno por arranque)."""
    return secrets.token_urlsafe(32)


def escribir_token(path: Path, token: str) -> None:
    """Escribe el token para el servidor del dashboard, solo legible por el
    usuario (0600 en POSIX; en Windows el archivo hereda los permisos del
    perfil). Borra el anterior y crea uno nuevo sin seguir symlinks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")


class ControlLocal:
    def __init__(
        self,
        app: ASGIApp,
        *,
        hosts: Iterable[str],
        origenes: Iterable[str],
        rutas_post_json: Iterable[str],
        token: str,
    ) -> None:
        if not token:
            raise ValueError("La API de control necesita un token")
        self.app = app
        self.hosts = frozenset(h.lower() for h in hosts)
        self.origenes = frozenset(origenes)
        self.rutas_post_json = frozenset(rutas_post_json)
        self._token = token.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(PREFIJO_CONTROL):
            await self.app(scope, receive, send)
            return
        rechazo = self._rechazo(scope)
        if rechazo is not None:
            await rechazo(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _rechazo(self, scope: Scope) -> JSONResponse | None:
        headers = Headers(scope=scope)
        host = headers.get("host", "").strip().lower()
        if host not in self.hosts:
            return JSONResponse(
                {"error": "host_no_permitido",
                 "detalle": "La API de control solo atiende localhost."},
                status_code=403,
            )
        if scope["method"] != "POST" or scope["path"] not in self.rutas_post_json:
            return None
        origen = headers.get("origin")
        if origen is not None and origen not in self.origenes:
            return JSONResponse({"error": "origen_no_permitido"}, status_code=403)
        tipo = headers.get("content-type", "").split(";")[0].strip().lower()
        if tipo != "application/json":
            return JSONResponse(
                {"error": "se_espera_json", "detalle": "Content-Type: application/json"},
                status_code=415,
            )
        recibido = headers.get(HEADER_TOKEN, "").encode("utf-8", "replace")
        if not hmac.compare_digest(recibido, self._token):
            return JSONResponse(
                {"error": "token_invalido",
                 "detalle": f"Falta {HEADER_TOKEN} (lo agrega el servidor del dashboard)."},
                status_code=403,
            )
        return None
