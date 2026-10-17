"""Configuración del proxy, leída del entorno."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent

_VERDADEROS = {"1", "true", "t", "yes", "y", "si", "sí", "on"}
_FALSOS = {"0", "false", "f", "no", "n", "off"}


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
    merchant_upstream_url: str = "http://localhost:8001/a2a/merchant_agent"
    proxy_public_url: str = "http://localhost:8010/a2a/merchant_agent"
    catalogo_url: str = "http://localhost:8011"
    guard_activo: bool = True
    events_jsonl: Path | None = REPO_ROOT / ".logs" / "guard_events.jsonl"
    guard_key_path: Path = REPO_ROOT / ".temp-db" / "guard_signing_key.pem"
    port: int = 8010
    host: str = "127.0.0.1"
    dashboard_origins: tuple[str, ...] = field(
        default=("http://localhost:8020", "http://127.0.0.1:8020")
    )

    @property
    def rpc_path(self) -> str:
        """Ruta en la que el proxy atiende el JSON-RPC (la de PROXY_PUBLIC_URL)."""
        return urlparse(self.proxy_public_url).path.rstrip("/") or "/"

    @property
    def upstream_card_url(self) -> str:
        return f"{self.merchant_upstream_url}/.well-known/agent-card.json"

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
            port=int(env.get("PROXY_PORT", base.port)),
            host=env.get("PROXY_HOST", base.host),
        )
