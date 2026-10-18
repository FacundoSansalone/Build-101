"""Carga de `guard.evaluar()` y firma del recibo con `guard.recibo`.

El proxy no decide nada: transporta, registra y aplica lo que devuelve
`evaluar()`. Falla cerrado: si no puede importar el `guard.evaluar()` real o
este no acepta `checkout=` (no ataría el checkout firmado al cart evaluado),
el proxy **no arranca** (`EvaluadorNoDisponible`). Solo con
`PROXY_ALLOW_STUB=1` explícito usa `proxy/guard_stub.py`, y lo marca en el
log, en `GET /guard/state` y en cada evento `veredicto`
(`"evaluador": "stub"`).
"""

from __future__ import annotations

import importlib
import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

from contracts import Recibo, Veredicto
from proxy import guard_stub

logger = logging.getLogger("proxy.evaluador")

KID_POR_DEFECTO = "guard-key-1"  # CLAUDE.md → Decisiones, punto 6


def acepta_parametro(funcion: Callable[..., Any], nombre: str) -> bool:
    """¿`funcion` acepta `nombre=` como keyword? (o tiene **kwargs)."""
    try:
        parametros = inspect.signature(funcion).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        p.kind is inspect.Parameter.VAR_KEYWORD
        or (
            p.name == nombre
            and p.kind
            in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        )
        for p in parametros
    )


@dataclass(frozen=True)
class EvaluadorCargado:
    evaluar: Callable[..., Veredicto]
    nombre: Literal["guard", "stub"]
    # Se detecta en tiempo de ejecución: un guard/ viejo (o un stub) puede no
    # aceptar todavía `checkout=` (CLAUDE.md → Ronda de revisión 2026-10-17).
    acepta_checkout: bool = field(init=False)
    # `ahora=` es del contrato, pero un evaluador de prueba puede no tenerlo:
    # sin él, el Guard toma su propio reloj.
    acepta_ahora: bool = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "acepta_checkout", acepta_parametro(self.evaluar, "checkout")
        )
        object.__setattr__(self, "acepta_ahora", acepta_parametro(self.evaluar, "ahora"))


class EvaluadorNoDisponible(RuntimeError):
    """No hay un `guard.evaluar()` real que ate el checkout firmado: el proxy
    no arranca (salvo `PROXY_ALLOW_STUB=1`)."""


_PISTA_STUB = (
    "Para probar sin el Guard real, definí PROXY_ALLOW_STUB=1 (los veredictos "
    "serán del stub, no del Guard)."
)


def _stub(motivo: str) -> EvaluadorCargado:
    logger.warning(
        "PROXY_ALLOW_STUB=1: %s. Usando el STUB (proxy/guard_stub.py): los "
        "veredictos NO son del Guard real.",
        motivo,
    )
    return EvaluadorCargado(evaluar=guard_stub.evaluar, nombre="stub")


def validar_evaluador(evaluador: EvaluadorCargado, permitir_stub: bool) -> None:
    """Lanza `EvaluadorNoDisponible` si el evaluador no sirve para decidir.

    Un `evaluar()` que no acepta `checkout=` nunca se usa (ni con permiso):
    evaluaría sin atar lo firmado. El stub solo con `PROXY_ALLOW_STUB=1`.
    """
    if not evaluador.acepta_checkout:
        raise EvaluadorNoDisponible(
            f"evaluar() ({evaluador.nombre}) no acepta checkout=: no ataría el "
            "checkout firmado al cart evaluado. Actualizá guard/."
        )
    if evaluador.nombre == "stub" and not permitir_stub:
        raise EvaluadorNoDisponible(
            "el evaluador es el stub de prueba y no se pidió explícitamente. "
            + _PISTA_STUB
        )


def cargar_evaluador(permitir_stub: bool = False) -> EvaluadorCargado:
    """El `guard.evaluar()` real, o `EvaluadorNoDisponible`.

    Con `permitir_stub=True` (`PROXY_ALLOW_STUB=1`), si el real no está o no
    acepta `checkout=`, devuelve el stub marcado como tal.
    """
    try:
        from guard import evaluar  # type: ignore[attr-defined]

        if isinstance(evaluar, ModuleType):  # guard/evaluar.py sin re-export
            evaluar = evaluar.evaluar
        if not callable(evaluar):
            raise TypeError("guard.evaluar no es una función")
    except Exception as e:  # noqa: BLE001 - cualquier falla: no hay Guard real
        motivo = f"guard.evaluar() no está disponible ({type(e).__name__}: {e})"
        if permitir_stub:
            return _stub(motivo)
        raise EvaluadorNoDisponible(
            f"{motivo}. El proxy no arranca sin el Guard real. {_PISTA_STUB}"
        ) from e
    cargado = EvaluadorCargado(evaluar=evaluar, nombre="guard")
    if not cargado.acepta_checkout:
        motivo = (
            "guard.evaluar() no acepta checkout=: no ataría el checkout firmado "
            "al cart evaluado"
        )
        if permitir_stub:
            return _stub(motivo)
        raise EvaluadorNoDisponible(f"{motivo}. Actualizá guard/. {_PISTA_STUB}")
    logger.info("Usando guard.evaluar() de guard/")
    return cargado


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


def preparar_clave(key_path: Path) -> bool:
    """Crea (o carga) la clave del recibo al arrancar el proxy, no en la
    primera decisión. False si no hay `guard.recibo`."""
    modulo = _modulo_recibo()
    if modulo is None:
        return False
    _cargar_clave(modulo, key_path)
    return True


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
