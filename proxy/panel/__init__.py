"""Panel del cliente: `/panel`, el historial de compras de una empresa integradora.

La empresa entra con su clave de solo lectura (`psg_…`, permiso `lectura`; ver
`proxy/claves.py`) y ve sus compras, la explicación de cada una en lenguaje
claro y si la intentaron estafar. No tiene permisos propios ni estado: la
página es pública y los datos los pide el navegador a `/v1` con la clave que
pega el usuario (`GET /v1/resumen`, `GET /v1/compras`, la explicación y
`GET /v1/public-key` para verificar el recibo), y la API decide igual que con
cualquier integrador.

Lo que sirve este módulo son archivos estáticos, sin estado y sin clave (como
la consola):

- `GET /panel` (la página), `GET /panel/app.js` (la vista),
  `GET /panel/logica.mjs` (lógica pura), `GET /panel/vista.mjs` (arma los
  nodos con `textContent`), `GET /panel/estilo.css` y `GET /panel/recibo.mjs`,
  que es **el mismo archivo** que `/consola/recibo.mjs` (una sola
  verificación del recibo para las dos páginas);
- `GET /panel/` → 307 a `/panel`.

Todo con la CSP estricta de la consola (solo recursos propios, sin scripts ni
estilos en línea, `connect-src 'self'`: la clave solo puede viajar a este
mismo origen), `nosniff`, `no-referrer` y `no-store`. Sin recursos externos.

Lo activa `create_app(..., panel=True)` (lo prende `proxy/vercel.py`; en
`python -m proxy`, `GATEWAY_PANEL=1`). `RUTAS_ABIERTAS` son las rutas que
`AutenticacionGateway` deja pasar sin clave también en el oyente público.

Los archivos se leen una vez, al armar las rutas. Si falta alguno, el panel
responde 503 `panel_no_disponible` y el resto del motor sigue igual.

Este módulo es del motor: no importa la simulación ni nada de afuera de
starlette y la biblioteca estándar.
"""

from __future__ import annotations

import logging
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

logger = logging.getLogger("proxy.panel")

CARPETA = Path(__file__).resolve().parent
RUTA = "/panel"
_JS = "text/javascript; charset=utf-8"

# ruta → (archivo, relativo a esta carpeta; Content-Type)
ARCHIVOS: dict[str, tuple[str, str]] = {
    RUTA: ("index.html", "text/html; charset=utf-8"),
    f"{RUTA}/app.js": ("app.js", _JS),
    f"{RUTA}/logica.mjs": ("logica.mjs", _JS),
    f"{RUTA}/vista.mjs": ("vista.mjs", _JS),
    f"{RUTA}/estilo.css": ("estilo.css", "text/css; charset=utf-8"),
    # La verificación del recibo es la de la consola: el mismo archivo.
    f"{RUTA}/recibo.mjs": ("../consola/recibo.mjs", _JS),
}
REDIRECCIONES = (f"{RUTA}/",)

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
                "El panel del cliente no está disponible: no se pudo leer %s (responde 503).",
                archivo,
            )
            return None
    return contenidos


def rutas_panel(carpeta: Path = CARPETA) -> list[Route]:
    """Las rutas del panel. `carpeta` es para los tests."""
    contenidos = _leer(carpeta)

    def servir(ruta: str, tipo: str):
        async def handler(request: Request) -> Response:
            if contenidos is None:
                return JSONResponse(
                    {"error": "panel_no_disponible"}, status_code=503, headers=CABECERAS
                )
            return Response(contenidos[ruta], media_type=tipo, headers=CABECERAS)

        handler.__name__ = f"panel_{ruta.rsplit('/', 1)[-1].replace('.', '_')}"
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
