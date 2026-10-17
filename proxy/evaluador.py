"""Carga de `guard.evaluar()` (o el stub) y firma del recibo con `guard.recibo`.

El proxy no decide nada: transporta, registra y aplica lo que devuelve
`evaluar()`. Mientras Dev 2 no entregue `guard/`, usa `proxy/guard_stub.py`
y lo avisa fuerte en el log y en `GET /guard/state` (`"evaluador": "stub"`).
"""

from __future__ import annotations

import importlib
import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

from contracts import Recibo, Veredicto
from proxy import guard_stub

logger = logging.getLogger("proxy.evaluador")

KID_POR_DEFECTO = "guard-key-1"  # CLAUDE.md → Decisiones, punto 6


@dataclass(frozen=True)
class EvaluadorCargado:
    evaluar: Callable[..., Veredicto]
    nombre: Literal["guard", "stub"]


def cargar_evaluador() -> EvaluadorCargado:
    try:
        from guard import evaluar  # type: ignore[attr-defined]

        if isinstance(evaluar, ModuleType):  # guard/evaluar.py sin re-export
            evaluar = evaluar.evaluar
        if not callable(evaluar):
            raise TypeError("guard.evaluar no es una función")
    except Exception as e:  # noqa: BLE001 - cualquier falla cae al stub
        logger.warning(
            "guard.evaluar() no está disponible (%s: %s): usando stub "
            "(proxy/guard_stub.py). Los veredictos NO son del Guard real.",
            type(e).__name__,
            e,
        )
        return EvaluadorCargado(evaluar=guard_stub.evaluar, nombre="stub")
    logger.info("Usando guard.evaluar() de guard/")
    return EvaluadorCargado(evaluar=evaluar, nombre="guard")


# ---------------------------------------------------------------------------
# Recibo firmado (guard.recibo.firmar_recibo + cargar_o_crear_clave)
# ---------------------------------------------------------------------------


def _modulo_recibo() -> ModuleType | None:
    try:
        modulo = importlib.import_module("guard.recibo")
    except Exception:  # noqa: BLE001 - sin guard.recibo no se firma
        return None
    if callable(getattr(modulo, "firmar_recibo", None)) and callable(
        getattr(modulo, "cargar_o_crear_clave", None)
    ):
        return modulo
    return None


def _kid(modulo: ModuleType) -> str:
    for nombre in ("KID", "GUARD_KID", "KID_GUARD"):
        valor = getattr(modulo, nombre, None)
        if isinstance(valor, str) and valor:
            return valor
    return KID_POR_DEFECTO


def _cargar_clave(modulo: ModuleType, key_path: Path) -> Any:
    cargar = modulo.cargar_o_crear_clave
    try:
        acepta_ruta = bool(inspect.signature(cargar).parameters)
    except (TypeError, ValueError):
        acepta_ruta = True
    return cargar(key_path) if acepta_ruta else cargar()


def firmar_recibo(veredicto: Veredicto, key_path: Path) -> Veredicto:
    """Firma el recibo si `guard.recibo` lo permite; si no, lo deja igual."""
    if veredicto.recibo is None or veredicto.recibo.firma:
        return veredicto
    modulo = _modulo_recibo()
    if modulo is None:
        return veredicto
    clave = _cargar_clave(modulo, key_path)
    firmado = modulo.firmar_recibo(veredicto.recibo, clave)
    if isinstance(firmado, Recibo):
        recibo = firmado
    elif isinstance(firmado, str):
        recibo = veredicto.recibo.model_copy(
            update={"firma": firmado, "kid": veredicto.recibo.kid or _kid(modulo)}
        )
    elif isinstance(firmado, dict):
        recibo = Recibo.model_validate(firmado)
    else:
        raise TypeError(f"firmar_recibo devolvió {type(firmado).__name__}")
    return veredicto.model_copy(update={"recibo": recibo})


def _jwk_publica(clave: Any) -> dict[str, Any] | None:
    from jwcrypto.jwk import JWK

    if isinstance(clave, JWK):
        return clave.export_public(as_dict=True)
    if hasattr(clave, "public_key"):
        return JWK.from_pyca(clave.public_key()).export_public(as_dict=True)
    if isinstance(clave, dict):
        return {k: v for k, v in clave.items() if k not in {"d", "p", "q", "dp", "dq", "qi"}}
    return None


def clave_publica(key_path: Path) -> dict[str, Any] | None:
    """`{"kid", "jwk"}` de la clave del Guard, o None si no hay guard.recibo."""
    modulo = _modulo_recibo()
    if modulo is None:
        return None
    clave = _cargar_clave(modulo, key_path)
    jwk = None
    for nombre in ("jwk_publica", "clave_publica_jwk", "public_jwk"):
        funcion = getattr(modulo, nombre, None)
        if callable(funcion):
            jwk = funcion(clave)
            break
    if jwk is None:
        jwk = _jwk_publica(clave)
    if not isinstance(jwk, dict):
        return None
    kid = jwk.get("kid") or _kid(modulo)
    return {"kid": kid, "jwk": {**jwk, "kid": kid, "alg": jwk.get("alg", "ES256"), "use": "sig"}}
