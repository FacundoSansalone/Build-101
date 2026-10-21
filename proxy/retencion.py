"""Retención de las tablas del gateway (`gw_*`): la purga de lo viejo.

Lo de cada compra de integrador (la compra, sus contextos, ofertas, últimos
veredictos, aprobaciones, esperas, eventos del historial y entregas de
webhooks) se guarda `GATEWAY_RETENCION_DIAS` días (90 por defecto; de 7 a
3650, y en Vercel hasta 365; ver `proxy.config`) y después se borra con
`AlmacenEstado.purgar`.

La purga la dispara afuera un pedido a `RUTA_PURGA` (`GET` o `POST`: Vercel
Cron llama con `GET`), solo en el oyente público y solo con
`Authorization: Bearer <secreto>`. El secreto es `GATEWAY_PURGA_SECRETO` o,
si no está, `CRON_SECRET` (el que Vercel manda solo en cada cron); de 32
caracteres o más. Sin secreto la ruta no existe (404, como cualquier otra).
La ruta no está bajo `/v1`: ninguna clave de API (completa ni de solo
lectura) la puede usar, y no aparece en la OpenAPI de los integradores.
"""

from __future__ import annotations

import hmac
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from proxy.config import RUTA_PURGA
from proxy.tipos_gateway import AlmacenEstado, ErrorAlmacen

logger = logging.getLogger("proxy.retencion")

_SIN_CACHE = {"Cache-Control": "no-store"}


def _error(status: int, codigo: str, mensaje: str, **headers: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"codigo": codigo, "mensaje": mensaje}}, status_code=status,
        headers={**_SIN_CACHE, **headers},
    )


def antes_de(ahora: datetime, dias: int) -> datetime:
    """El corte de la purga: se borra lo creado antes de esto."""
    return ahora - timedelta(days=dias)


def _secreto_recibido(request: Request) -> str | None:
    valores = request.headers.getlist("authorization")
    if len(valores) != 1:
        return None
    esquema, _, valor = valores[0].partition(" ")
    return valor if esquema.lower() == "bearer" and valor else None


def ruta_purga(almacen: AlmacenEstado, *, secreto: str, retencion_dias: int) -> Route:
    """`GET|POST RUTA_PURGA`: borra del almacén lo más viejo que la retención
    y devuelve cuántas filas de cada tabla (`{"antes_de", "retencion_dias",
    "borradas"}`). 404 fuera del oyente público, 401 sin el secreto (en
    tiempo constante) y 503 si el almacén falla (la próxima sigue)."""
    esperado = secreto.encode("utf-8")

    async def purgar(request: Request) -> Response:
        if getattr(request.state, "oyente", None) != "publico":
            return _error(404, "no_encontrado", "No existe.")
        recibido = (_secreto_recibido(request) or "").encode("utf-8", "replace")
        if not hmac.compare_digest(recibido, esperado):
            return _error(401, "no_autorizado", "Falta el secreto de la purga.",
                          **{"WWW-Authenticate": "Bearer"})
        corte = antes_de(datetime.now(timezone.utc), retencion_dias)
        try:
            borradas = await almacen.purgar(corte)
        except ErrorAlmacen as e:
            logger.error("La purga no terminó: %s", e)
            return _error(503, "estado_no_disponible", "El almacén no respondió; reintentá.")
        logger.warning(
            "Purga de lo creado antes de %s (%d días): %s", corte.isoformat(), retencion_dias,
            ", ".join(f"{tabla} {n}" for tabla, n in borradas.items()),
        )
        cuerpo: dict[str, Any] = {
            "antes_de": corte.isoformat(), "retencion_dias": retencion_dias, "borradas": borradas,
        }
        return JSONResponse(cuerpo, headers=_SIN_CACHE)

    return Route(RUTA_PURGA, purgar, methods=["GET", "POST"])
