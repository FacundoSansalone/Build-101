"""Carga de `guard.evaluar()` y firma del recibo con `guard.recibo`.

El proxy no decide nada: transporta, registra y aplica lo que devuelve
`evaluar()`. Falla cerrado: si no puede importar el `guard.evaluar()` real o
este no acepta `checkout=` (no ataría el checkout firmado al cart evaluado),
el proxy **no arranca** (`EvaluadorNoDisponible`). Solo con
`PROXY_ALLOW_STUB=1` explícito usa `proxy/guard_stub.py`, y lo marca en el
log, en `GET /guard/state` y en cada evento `veredicto`
(`"evaluador": "stub"`).

La clave del recibo es una `ClaveRecibo`: del entorno
(`GUARD_SIGNING_KEY_PEM_B64` o `GUARD_SIGNING_KEY_PEM`, con
`GUARD_SIGNING_KID`), que es lo que sirve con varias instancias sin disco
compartido, o del archivo de la demo local (`.temp-db/guard_signing_key.pem`).
"""

from __future__ import annotations

import base64
import binascii
import importlib
import inspect
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from os import PathLike
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_pem_private_key

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


def clave_publica(key_path: Path) -> dict[str, Any] | None:
    """`{"kid", "jwk"}` de la clave del Guard, o None si no hay guard.recibo
    (o no expone `clave_publica_jwk`: sin ella no se publica nada)."""
    modulo = _modulo_recibo()
    if modulo is None:
        return None
    funcion = getattr(modulo, "clave_publica_jwk", None)
    if not callable(funcion):
        return None
    jwk = funcion(_cargar_clave(modulo, key_path))
    if not isinstance(jwk, dict):
        return None
    kid = jwk.get("kid") or _kid(modulo)
    return {"kid": kid, "jwk": {**jwk, "kid": kid, "alg": jwk.get("alg", "ES256"), "use": "sig"}}


# ---------------------------------------------------------------------------
# ClaveRecibo: la clave del recibo, del entorno o de un archivo
# ---------------------------------------------------------------------------

VAR_CLAVE_PEM_B64 = "GUARD_SIGNING_KEY_PEM_B64"
VAR_CLAVE_PEM = "GUARD_SIGNING_KEY_PEM"
VAR_KID = "GUARD_SIGNING_KID"
# El kid va en el encabezado del JWS y en la JWK publicada: corto y sin
# espacios ni comillas.
_KID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


class ClaveReciboInvalida(ValueError):
    """La clave del recibo configurada no sirve: no es PEM, no es EC P-256,
    se pidió por dos variables a la vez o falta el archivo y no se puede
    crear. El mensaje nunca incluye la clave ni el valor de la variable."""


def _validar_kid(kid: Any, origen: str) -> str:
    if not isinstance(kid, str) or _KID_RE.fullmatch(kid) is None:
        # Sin el valor: si alguien pegó la clave en la variable del kid, no sale.
        raise ClaveReciboInvalida(
            f"{origen}: el kid tiene que tener de 1 a 64 caracteres (letras, dígitos, "
            "'.', '_' o '-')"
        )
    return kid


def kid_del_entorno(env: Mapping[str, str]) -> str:
    """`GUARD_SIGNING_KID`, o `guard-key-1` si no está (o está vacía)."""
    kid = (env.get(VAR_KID) or "").strip()
    return _validar_kid(kid, VAR_KID) if kid else KID_POR_DEFECTO


def _es_p256(clave: Any) -> bool:
    return isinstance(clave, ec.EllipticCurvePrivateKey) and isinstance(
        clave.curve, ec.SECP256R1
    )


@dataclass(frozen=True, eq=False, repr=False)
class ClaveRecibo:
    """La clave privada EC P-256 con que se firman los recibos y su `kid`.

    `origen` dice de dónde salió (la variable o la ruta), para el log; nunca
    la clave. `repr()` tampoco la muestra.

    Con varias instancias sin disco compartido (Vercel) la clave sale del
    entorno (`desde_entorno`): todas firman con la misma. Del archivo
    (`desde_archivo`) es la de la demo local; con `crear_si_falta=False` no se
    crea si falta, porque cada instancia crearía otra con el mismo kid y los
    recibos de unas no se podrían verificar con la clave que publican otras.
    """

    clave: ec.EllipticCurvePrivateKey
    kid: str = KID_POR_DEFECTO
    origen: str = "entorno"

    def __post_init__(self) -> None:
        if not _es_p256(self.clave):
            raise ClaveReciboInvalida(
                f"{self.origen}: la clave del recibo tiene que ser una clave privada EC P-256"
            )
        _validar_kid(self.kid, self.origen)

    def __repr__(self) -> str:
        return f"ClaveRecibo(kid={self.kid!r}, origen={self.origen!r})"

    # -- uso -------------------------------------------------------------------

    def firmar(self, recibo: Recibo) -> Recibo:
        """El recibo firmado (JWS compacto ES256, `guard.recibo.firmar_recibo`)
        con esta clave y este `kid`."""
        from guard.recibo import firmar_recibo as firmar_con_guard

        return firmar_con_guard(recibo, self.clave, self.kid)

    def firmar_veredicto(self, veredicto: Veredicto) -> Veredicto:
        """El veredicto con el recibo firmado. Sin recibo, o si ya viene
        firmado, lo devuelve igual (como `firmar_recibo(veredicto, path)`)."""
        if veredicto.recibo is None or veredicto.recibo.firma:
            return veredicto
        return veredicto.model_copy(update={"recibo": self.firmar(veredicto.recibo)})

    def jwk_publica(self) -> dict[str, Any]:
        """La JWK pública (EC P-256, `alg` ES256, `use` sig, este `kid`), sin
        `d` (`guard.recibo.clave_publica_jwk`)."""
        from guard.recibo import clave_publica_jwk

        return clave_publica_jwk(self.clave, self.kid)

    def clave_publica(self) -> dict[str, Any]:
        """`{"kid", "jwk"}`: la misma forma que `clave_publica(key_path)`."""
        return {"kid": self.kid, "jwk": self.jwk_publica()}

    # -- constructores ---------------------------------------------------------

    @classmethod
    def desde_pem(
        cls, pem: bytes | str, *, kid: str = KID_POR_DEFECTO, origen: str
    ) -> ClaveRecibo:
        """Desde un PEM (PKCS8 o SEC1, sin contraseña). `ClaveReciboInvalida`
        si no se puede leer o no es EC P-256."""
        datos = pem.encode("utf-8") if isinstance(pem, str) else bytes(pem)
        try:
            clave = load_pem_private_key(datos, password=None)
        except (ValueError, TypeError, UnsupportedAlgorithm):
            # Sin encadenar la excepción: el detalle de cryptography no suma y
            # el mensaje de acá no lleva nada de la clave.
            raise ClaveReciboInvalida(
                f"{origen}: no es una clave privada PEM sin contraseña"
            ) from None
        if not _es_p256(clave):
            raise ClaveReciboInvalida(f"{origen}: la clave no es EC P-256 (ES256)")
        return cls(clave=clave, kid=kid, origen=origen)

    @classmethod
    def desde_entorno(cls, env: Mapping[str, str]) -> ClaveRecibo | None:
        """La clave de `GUARD_SIGNING_KEY_PEM_B64` (el PEM en base64) o de
        `GUARD_SIGNING_KEY_PEM` (el PEM directo, con los saltos de línea
        reales o escritos `\\n`), con el kid de `GUARD_SIGNING_KID` (default
        `guard-key-1`). None si no está ninguna de las dos (vacías cuentan
        como que no están).

        `ClaveReciboInvalida` (sin la clave en el mensaje) si están las dos,
        si el base64 o el PEM no se pueden leer, si no es EC P-256 o si el kid
        no es válido."""
        b64 = (env.get(VAR_CLAVE_PEM_B64) or "").strip()
        directo = (env.get(VAR_CLAVE_PEM) or "").strip()
        if b64 and directo:
            raise ClaveReciboInvalida(
                f"{VAR_CLAVE_PEM_B64} y {VAR_CLAVE_PEM} están las dos: definí una sola"
            )
        if not b64 and not directo:
            return None
        kid = kid_del_entorno(env)
        if b64:
            try:
                pem = base64.b64decode("".join(b64.split()), validate=True)
            except (binascii.Error, ValueError):
                raise ClaveReciboInvalida(
                    f"{VAR_CLAVE_PEM_B64} no es base64 válido (para el PEM tal cual, "
                    f"usá {VAR_CLAVE_PEM})"
                ) from None
            return cls.desde_pem(pem, kid=kid, origen=VAR_CLAVE_PEM_B64)
        pem_texto = directo.replace("\\r\\n", "\n").replace("\\n", "\n")
        return cls.desde_pem(pem_texto, kid=kid, origen=VAR_CLAVE_PEM)

    @classmethod
    def desde_archivo(
        cls,
        path: str | PathLike[str],
        kid: str = KID_POR_DEFECTO,
        *,
        crear_si_falta: bool = True,
    ) -> ClaveRecibo:
        """La clave del archivo PEM (la de la demo local). Si no existe, con
        `crear_si_falta=True` (lo de siempre) la crea `guard.recibo.
        cargar_o_crear_clave` (P-256, modo 600); con `False` es
        `ClaveReciboInvalida` y no escribe nada."""
        ruta = Path(path)
        origen = str(ruta)
        if crear_si_falta and not ruta.exists():
            from guard.recibo import cargar_o_crear_clave

            try:
                clave = cargar_o_crear_clave(ruta)
            except (OSError, ValueError, TypeError) as e:
                raise ClaveReciboInvalida(
                    f"{origen}: no se pudo crear la clave del recibo ({type(e).__name__})"
                ) from None
            return cls(clave=clave, kid=kid, origen=origen)
        try:
            datos = ruta.read_bytes()
        except FileNotFoundError:
            raise ClaveReciboInvalida(
                f"{origen}: no está la clave del recibo y no se crea (con varias instancias "
                f"cada una tendría otra): configurá {VAR_CLAVE_PEM_B64}"
            ) from None
        except OSError as e:
            raise ClaveReciboInvalida(
                f"{origen}: no se pudo leer la clave del recibo ({type(e).__name__})"
            ) from None
        return cls.desde_pem(datos, kid=kid, origen=origen)
