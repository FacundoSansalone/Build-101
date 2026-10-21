"""Consola web del motor: `/consola`, para probar el gateway desde el navegador.

Hace en el navegador lo mismo que el agente de un integrador (crear la
compra, buscar, contestar el porqué y la oportunidad, pedir el checkout,
aprobar un `escalar`, verificar el recibo y probar `/v1/filtrar`), contra el
mismo motor y con la clave `psg_…` que pega el usuario. No tiene permisos
propios: cada paso es una llamada a `/v1` o a `/a2a/m/{comercio}` con esa
clave, y la API decide igual que con cualquier integrador.

Lo que sirve este módulo son archivos estáticos de esta carpeta, sin estado y
sin clave (como `/v1/docs`):

- `GET /consola` (la página), `GET /consola/app.js`, `GET /consola/logica.mjs`,
  `GET /consola/recibo.mjs` (la verificación del recibo, que comparte con el
  panel del cliente) y `GET /consola/estilo.css`;
- `GET /` y `GET /consola/` → 307 a `/consola`.

Todo con una CSP estricta (solo recursos propios, sin scripts ni estilos en
línea, `connect-src 'self'`: la clave solo puede viajar a este mismo origen),
`nosniff`, `no-referrer` y `no-store`. Sin CDNs ni recursos externos.

Lo activa `create_app(..., consola=True)` (lo prende `proxy/vercel.py`; en
`python -m proxy`, `GATEWAY_CONSOLA=1`). `RUTAS_ABIERTAS` son las rutas que
`AutenticacionGateway` deja pasar sin clave también en el oyente público.

Los archivos se leen una vez, al armar las rutas. Si falta alguno, la
consola responde 503 `consola_no_disponible` y el resto del motor sigue igual:
la consola no es parte de la verificación.

Este módulo es del motor: no importa la simulación ni nada de afuera de
starlette y la biblioteca estándar.
"""

from __future__ import annotations

import logging
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

logger = logging.getLogger("proxy.consola")

CARPETA = Path(__file__).resolve().parent
RUTA = "/consola"

# ruta → (archivo en esta carpeta, Content-Type)
ARCHIVOS: dict[str, tuple[str, str]] = {
    RUTA: ("index.html", "text/html; charset=utf-8"),
    f"{RUTA}/app.js": ("app.js", "text/javascript; charset=utf-8"),
    f"{RUTA}/logica.mjs": ("logica.mjs", "text/javascript; charset=utf-8"),
    # La verificación del recibo (la importa logica.mjs; el panel del cliente
    # sirve este mismo archivo, ver `proxy.panel`).
    f"{RUTA}/recibo.mjs": ("recibo.mjs", "text/javascript; charset=utf-8"),
    f"{RUTA}/estilo.css": ("estilo.css", "text/css; charset=utf-8"),
}
REDIRECCIONES = ("/", f"{RUTA}/")

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
CABECERAS: dict[str, str] = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

# Las que pasan sin clave por `AutenticacionGateway` (rutas exactas).
RUTAS_ABIERTAS: frozenset[str] = frozenset({*ARCHIVOS, *REDIRECCIONES})


def _leer(carpeta: Path) -> dict[str, bytes] | None:
    """El contenido de cada archivo por ruta, o None si falta alguno."""
    contenidos: dict[str, bytes] = {}
    for ruta, (archivo, _tipo) in ARCHIVOS.items():
        try:
            contenidos[ruta] = (carpeta / archivo).read_bytes()
        except OSError:
            logger.error(
                "La consola web no está disponible: no se pudo leer %s (responde 503).", archivo
            )
            return None
    return contenidos


def rutas_consola(carpeta: Path = CARPETA) -> list[Route]:
    """Las rutas de la consola. `carpeta` es para los tests."""
    contenidos = _leer(carpeta)

    def servir(ruta: str, tipo: str):
        async def handler(request: Request) -> Response:
            if contenidos is None:
                return JSONResponse(
                    {"error": "consola_no_disponible"}, status_code=503, headers=CABECERAS
                )
            return Response(contenidos[ruta], media_type=tipo, headers=CABECERAS)

        handler.__name__ = f"consola_{ruta.rsplit('/', 1)[-1].replace('.', '_')}"
        return handler

    async def redirigir(request: Request) -> Response:
        return RedirectResponse(RUTA, status_code=307, headers=CABECERAS)

    rutas = [
        Route(ruta, servir(ruta, tipo), methods=["GET"], include_in_schema=False)
        for ruta, (_archivo, tipo) in ARCHIVOS.items()
    ]
    rutas += [
        Route(ruta, redirigir, methods=["GET"], include_in_schema=False)
        for ruta in REDIRECCIONES
    ]
    return rutas
