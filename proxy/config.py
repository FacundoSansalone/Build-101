"""Configuración del proxy, leída del entorno."""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

logger = logging.getLogger("proxy.config")

REPO_ROOT = Path(__file__).resolve().parent.parent

_VERDADEROS = {"1", "true", "t", "yes", "y", "si", "sí", "on"}
_FALSOS = {"0", "false", "f", "no", "n", "off"}

MAX_RESPUESTA_BYTES = 5 * 1024 * 1024  # tope por respuesta del comercio
MAX_SUSCRIPTORES_SSE = 20
# Gateway de producción (diseño del gateway, §1.9).
MAX_CUERPO_V1_BYTES = 1024 * 1024
MAX_CUERPO_A2A_BYTES = 5 * 1024 * 1024
COMPRA_TTL_S = 24 * 3600


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
    # Catálogo público del comercio: no siempre existe. None (CATALOGO_URL
    # vacío) = este comercio no publica catálogo; no se consulta nada.
    catalogo_url: str | None = "http://127.0.0.1:8011"
    # Si el catálogo no responde, ¿usar fixtures/public_catalog.json? Solo para
    # la demo offline (CATALOGO_RESPALDO_FIXTURE=1): fuera de la demo sería
    # verificar contra una copia vieja que no es de este comercio.
    catalogo_respaldo_fixture: bool = False
    guard_activo: bool = True
    events_jsonl: Path | None = REPO_ROOT / ".logs" / "guard_events.jsonl"
    # Los eventos de compras de integrador (llevan checkout_jwt y mandatos)
    # van a otro archivo: el principal lo sirve el dashboard para el replay.
    # None = junto al principal (`guard_events_privados.jsonl`); ver
    # `jsonl_privado`.
    events_jsonl_privado: Path | None = None
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
    # Etapa 2: cuánto se retiene un find_products esperando al usuario. Las dos
    # esperas caen en la misma llamada del agente del cliente, que espera
    # hasta 600 s (DEFAULT_TIMEOUT del sample): la suma tiene que quedar abajo.
    espera_contexto_s: float = 120.0  # el porqué (si vence, sigue el pedido literal)
    espera_decision_s: float = 420.0  # la consulta (si vence, no se compra nada)
    # Manda la ficha que confirmó el usuario (etapa 3): por defecto no se usan
    # las fichas de la demo (scenarios.json o fixtures/), como con un comercio
    # real. Mandan los parámetros que armó y confirmó el intake; sin compra no
    # hay parámetros autorizados (escala). PROXY_FICHA_DEMO=1 vuelve a usar la
    # del escenario fijo (grabaciones, benchmark por el proxy, smoke_checkout).
    ficha_demo: bool = False
    # DEMO_HOY=YYYY-MM-DD: el "hoy" que ve el agente de intención. Sin fijar,
    # el del reloj (el comercio general arma las fechas desde hoy). Solo para
    # reproducir una grabación de los escenarios fijos, que tienen fechas
    # fijas ("el lunes" es el 2026-10-19). No llega al Guard (los vencimientos
    # del checkout firmado se miran con el reloj de verdad).
    demo_hoy: date | None = None
    # -- gateway de producción (diseño del gateway, §1.1 y §1.9) -------------
    # Sin `GATEWAY_CLAVES_ARCHIVO` (o sin claves activas) no hay oyente
    # público y todo sigue como antes: estos campos no cambian nada.
    gateway_host: str = "127.0.0.1"
    gateway_port: int = 8030
    # La base pública (https en el deploy): Agent Card del oyente público,
    # `a2a_url` de las compras y `servers` del OpenAPI.
    gateway_public_url: str = "http://localhost:8030"
    claves_archivo: Path | None = None
    comercios_archivo: Path | None = None
    comercio_por_defecto_id: str = "merchant_1"
    # `supabase` solo explícito: las credenciales SUPABASE_* solas no lo
    # activan (la demo las tiene y las tablas 003 pueden no existir).
    estado_almacen: Literal["memoria", "supabase"] = "memoria"
    webhooks_inseguros: bool = False  # solo desarrollo: http e IPs privadas
    max_cuerpo_v1: int = MAX_CUERPO_V1_BYTES
    max_cuerpo_a2a: int = MAX_CUERPO_A2A_BYTES
    compra_ttl_s: int = COMPRA_TTL_S

    @property
    def jsonl_privado(self) -> Path | None:
        """El JSONL de los eventos de compras de integrador: el pedido o,
        junto al principal, `guard_events_privados.jsonl`. Sin JSONL
        principal (None), tampoco privado."""
        if self.events_jsonl is None:
            return None
        return _jsonl_privado(self.events_jsonl, self.events_jsonl_privado)

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

    @property
    def base_publica_local(self) -> str:
        """`scheme://host[:port]` de `PROXY_PUBLIC_URL`: la base de las URL
        `/a2a/m/{id}` que ve un cliente del oyente local."""
        from proxy.comercios import base_url  # comercios importa este módulo

        return base_url(self.proxy_public_url)

    @classmethod
    def desde_entorno(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        base = cls()
        jsonl = env.get("GUARD_EVENTS_JSONL")
        events_jsonl = _ruta(jsonl) if jsonl else base.events_jsonl
        privado = (env.get("GUARD_EVENTS_JSONL_PRIVADO") or "").strip()
        events_jsonl_privado = _ruta(privado) if privado else None
        mismo = events_jsonl is not None and (
            _jsonl_privado(events_jsonl, events_jsonl_privado) == events_jsonl
        )
        if mismo:
            raise ValueError(
                "GUARD_EVENTS_JSONL_PRIVADO tiene que ser otro archivo que GUARD_EVENTS_JSONL "
                "(el principal lo sirve el dashboard)"
            )
        gateway_port = _entero_positivo(env.get("GATEWAY_PORT"), base.gateway_port, "GATEWAY_PORT")
        return cls(
            merchant_upstream_url=env.get(
                "MERCHANT_UPSTREAM_URL", base.merchant_upstream_url
            ).rstrip("/"),
            proxy_public_url=env.get(
                "PROXY_PUBLIC_URL", base.proxy_public_url
            ).rstrip("/"),
            catalogo_url=env.get("CATALOGO_URL", base.catalogo_url or "").strip().rstrip("/")
            or None,
            catalogo_respaldo_fixture=_bool(
                env.get("CATALOGO_RESPALDO_FIXTURE", "0"), "CATALOGO_RESPALDO_FIXTURE"
            ),
            guard_activo=_bool(env.get("GUARD_ACTIVO", "true"), "GUARD_ACTIVO"),
            events_jsonl=events_jsonl,
            events_jsonl_privado=events_jsonl_privado,
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
            max_suscriptores_sse=_entero_positivo(
                env.get("PROXY_MAX_SUSCRIPTORES_SSE"), base.max_suscriptores_sse,
                "PROXY_MAX_SUSCRIPTORES_SSE",
            ),
            permitir_remoto=_bool(
                env.get("PROXY_ALLOW_REMOTE", "0"), "PROXY_ALLOW_REMOTE"
            ),
            permitir_stub=_bool(env.get("PROXY_ALLOW_STUB", "0"), "PROXY_ALLOW_STUB"),
            espera_contexto_s=_segundos(
                env.get("PROXY_ESPERA_CONTEXTO_S"), base.espera_contexto_s,
                "PROXY_ESPERA_CONTEXTO_S",
            ),
            espera_decision_s=_segundos(
                env.get("PROXY_ESPERA_DECISION_S"), base.espera_decision_s,
                "PROXY_ESPERA_DECISION_S",
            ),
            ficha_demo=_bool(env.get("PROXY_FICHA_DEMO", "0"), "PROXY_FICHA_DEMO"),
            demo_hoy=_fecha(env.get("DEMO_HOY"), "DEMO_HOY"),
            gateway_host=(env.get("GATEWAY_HOST") or base.gateway_host).strip(),
            gateway_port=gateway_port,
            # Sin URL pública, la local del puerto que se usa: con otro
            # GATEWAY_PORT, la Agent Card y la `a2a_url` no apuntan a :8030.
            gateway_public_url=(
                env.get("GATEWAY_PUBLIC_URL") or f"http://localhost:{gateway_port}"
            ).strip().rstrip("/"),
            claves_archivo=_ruta(env["GATEWAY_CLAVES_ARCHIVO"].strip())
            if (env.get("GATEWAY_CLAVES_ARCHIVO") or "").strip()
            else None,
            comercios_archivo=_ruta(env["GATEWAY_COMERCIOS_ARCHIVO"].strip())
            if (env.get("GATEWAY_COMERCIOS_ARCHIVO") or "").strip()
            else None,
            comercio_por_defecto_id=(
                env.get("GATEWAY_COMERCIO_POR_DEFECTO") or base.comercio_por_defecto_id
            ).strip(),
            estado_almacen=_almacen(env.get("GATEWAY_ESTADO")),
            webhooks_inseguros=_bool(
                env.get("GATEWAY_WEBHOOKS_INSEGUROS", "0"), "GATEWAY_WEBHOOKS_INSEGUROS"
            ),
            max_cuerpo_v1=_entero_positivo(
                env.get("GATEWAY_MAX_CUERPO_BYTES"), base.max_cuerpo_v1, "GATEWAY_MAX_CUERPO_BYTES"
            ),
            max_cuerpo_a2a=_entero_positivo(
                env.get("GATEWAY_MAX_CUERPO_A2A_BYTES"), base.max_cuerpo_a2a,
                "GATEWAY_MAX_CUERPO_A2A_BYTES",
            ),
            compra_ttl_s=_entero_positivo(
                env.get("GATEWAY_COMPRA_TTL_S"), base.compra_ttl_s, "GATEWAY_COMPRA_TTL_S"
            ),
        )


def _segundos(valor: str | None, defecto: float, nombre: str) -> float:
    if valor is None or not valor.strip():
        return defecto
    segundos = float(valor)
    if not 0 < segundos < 600:
        raise ValueError(f"{nombre} tiene que estar entre 0 y 600 segundos, no {valor!r}")
    return segundos


def _jsonl_privado(principal: Path, pedido: Path | None) -> Path:
    return pedido or principal.with_name("guard_events_privados.jsonl")


_FECHA_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _fecha(valor: str | None, nombre: str) -> date | None:
    """Una fecha `YYYY-MM-DD` (vacía = None). Solo ese formato: `fromisoformat`
    acepta además `20261018` o `2026-W42-7`, que no se escriben a propósito."""
    if valor is None or not valor.strip():
        return None
    texto = valor.strip()
    try:
        if not _FECHA_RE.fullmatch(texto):
            raise ValueError
        return date.fromisoformat(texto)
    except ValueError:
        raise ValueError(f"{nombre} tiene que ser una fecha YYYY-MM-DD, no {valor!r}") from None


def _entero_positivo(valor: str | None, defecto: int, nombre: str) -> int:
    if valor is None or not valor.strip():
        return defecto
    try:
        numero = int(valor.strip())
    except ValueError:
        raise ValueError(f"{nombre} tiene que ser un entero, no {valor!r}") from None
    if numero <= 0:
        raise ValueError(f"{nombre} tiene que ser mayor que 0, no {valor!r}")
    return numero


def _almacen(valor: str | None) -> Literal["memoria", "supabase"]:
    """`GATEWAY_ESTADO`: el almacén lo arma `estado.estado_desde_entorno`; acá
    solo se valida, para que un valor mal escrito no arranque."""
    normalizado = (valor or "memoria").strip().lower()
    if normalizado not in ("memoria", "supabase"):
        raise ValueError(f"GATEWAY_ESTADO tiene que ser memoria o supabase, no {valor!r}")
    return normalizado  # type: ignore[return-value]


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


def verificar_gateway(config: Config, hay_claves: bool) -> None:
    """El oyente público solo existe con claves (§1.1). Con claves, los dos
    oyentes no pueden compartir puerto (el middleware los distingue por el
    puerto local de la conexión).

    Sin claves activas el proxy no abre ese puerto (ni en loopback ni
    afuera), así que un `GATEWAY_HOST` fuera de loopback no expone nada: se
    avisa y se sigue con la demo. Antes cortaba el arranque, y revocar la
    última clave tiraba también el proxy local y el dashboard."""
    if not hay_claves:
        if not es_loopback(config.gateway_host):
            logger.warning(
                "GATEWAY_HOST=%r no es una interfaz local, pero no hay claves de API "
                "activas (GATEWAY_CLAVES_ARCHIVO): no se abre el gateway público y se "
                "ignora GATEWAY_HOST.",
                config.gateway_host,
            )
        return
    if config.gateway_port == config.port:
        raise SystemExit(
            f"GATEWAY_PORT={config.gateway_port} es el mismo puerto que PROXY_PORT: el "
            "oyente público y el local tienen que usar puertos distintos."
        )
