"""Configuración del proxy, leída del entorno."""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent

_VERDADEROS = {"1", "true", "t", "yes", "y", "si", "sí", "on"}
_FALSOS = {"0", "false", "f", "no", "n", "off"}

MAX_RESPUESTA_BYTES = 5 * 1024 * 1024  # tope por respuesta del comercio
MAX_SUSCRIPTORES_SSE = 20


def _bool(valor: str, nombre: str) -> bool:
    normalizado = valor.strip().lower()
    if normalizado in _VERDADEROS:
        return True
    if normalizado in _FALSOS:
        return False
    raise ValueError(f"{nombre} tiene que ser true o false, no {valor!r}")


def _ruta(valor: str) -> Path:
    """Las rutas relativas se toman desde la raíz del repo."""
    path = Path(valor)
    return path if path.is_absolute() else REPO_ROOT / path


@dataclass(frozen=True)
class Config:
    # Servicios locales por IPv4: en Windows `localhost` prueba primero ::1
    # (donde no escuchan) y cada intento fallido suma ~2 s.
    merchant_upstream_url: str = "http://127.0.0.1:8001/a2a/merchant_agent"
    proxy_public_url: str = "http://localhost:8010/a2a/merchant_agent"
    catalogo_url: str = "http://127.0.0.1:8011"
    guard_activo: bool = True
    events_jsonl: Path | None = REPO_ROOT / ".logs" / "guard_events.jsonl"
    guard_key_path: Path = REPO_ROOT / ".temp-db" / "guard_signing_key.pem"
    # Token de la API de control (uno por arranque) para el servidor del
    # dashboard. None: solo en memoria (tests).
    api_token_path: Path | None = REPO_ROOT / ".temp-db" / "guard_api_token"
    port: int = 8010
    host: str = "127.0.0.1"
    dashboard_origins: tuple[str, ...] = field(
        default=("http://localhost:8020", "http://127.0.0.1:8020")
    )
    max_respuesta_bytes: int = MAX_RESPUESTA_BYTES
    max_suscriptores_sse: int = MAX_SUSCRIPTORES_SSE
    permitir_remoto: bool = False  # PROXY_ALLOW_REMOTE=1
    # PROXY_ALLOW_STUB=1: sin el guard.evaluar() real, usar el stub de prueba
    # (si no, el proxy no arranca). Los veredictos quedan marcados "stub".
    permitir_stub: bool = False

    @property
    def rpc_path(self) -> str:
        """Ruta en la que el proxy atiende el JSON-RPC (la de PROXY_PUBLIC_URL)."""
        return urlparse(self.proxy_public_url).path.rstrip("/") or "/"

    @property
    def upstream_card_url(self) -> str:
        return f"{self.merchant_upstream_url}/.well-known/agent-card.json"

    @property
    def hosts_permitidos(self) -> frozenset[str]:
        """`Host` aceptados en /guard/* (protege del DNS rebinding)."""
        return frozenset({f"localhost:{self.port}", f"127.0.0.1:{self.port}"})

    @classmethod
    def desde_entorno(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        base = cls()
        jsonl = env.get("GUARD_EVENTS_JSONL")
        return cls(
            merchant_upstream_url=env.get(
                "MERCHANT_UPSTREAM_URL", base.merchant_upstream_url
            ).rstrip("/"),
            proxy_public_url=env.get(
                "PROXY_PUBLIC_URL", base.proxy_public_url
            ).rstrip("/"),
            catalogo_url=env.get("CATALOGO_URL", base.catalogo_url).rstrip("/"),
            guard_activo=_bool(env.get("GUARD_ACTIVO", "true"), "GUARD_ACTIVO"),
            events_jsonl=_ruta(jsonl) if jsonl else base.events_jsonl,
            guard_key_path=_ruta(env["GUARD_KEY_PATH"])
            if env.get("GUARD_KEY_PATH")
            else base.guard_key_path,
            api_token_path=_ruta(env["GUARD_API_TOKEN_PATH"])
            if env.get("GUARD_API_TOKEN_PATH")
            else base.api_token_path,
            port=int(env.get("PROXY_PORT", base.port)),
            host=env.get("PROXY_HOST", base.host),
            max_respuesta_bytes=int(
                env.get("PROXY_MAX_RESPUESTA_BYTES", base.max_respuesta_bytes)
            ),
            permitir_remoto=_bool(
                env.get("PROXY_ALLOW_REMOTE", "0"), "PROXY_ALLOW_REMOTE"
            ),
            permitir_stub=_bool(env.get("PROXY_ALLOW_STUB", "0"), "PROXY_ALLOW_STUB"),
        )


def es_loopback(host: str) -> bool:
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip().strip("[]")).is_loopback
    except ValueError:
        return False


def verificar_host_local(config: Config) -> None:
    """El proxy expone eventos con mandatos y checkout_jwt completos: solo
    escucha en loopback salvo que se pida explícitamente lo contrario."""
    if es_loopback(config.host) or config.permitir_remoto:
        return
    raise SystemExit(
        f"PROXY_HOST={config.host!r} no es una interfaz local. El proxy expone "
        "eventos con mandatos y checkouts firmados: si de verdad hace falta "
        "escuchar en la red, definí PROXY_ALLOW_REMOTE=1."
    )
