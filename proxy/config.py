"""Configuración del proxy, leída del entorno."""

from __future__ import annotations

import hmac
import ipaddress
import logging
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger("proxy.config")

REPO_ROOT = Path(__file__).resolve().parent.parent

_VERDADEROS = {"1", "true", "t", "yes", "y", "si", "sí", "on"}
_FALSOS = {"0", "false", "f", "no", "n", "off"}

MAX_RESPUESTA_BYTES = 5 * 1024 * 1024  # tope por respuesta del comercio
# La zona del "hoy" del agente de intención (KAIAI_ZONA_HORARIA).
ZONA_HORARIA_POR_DEFECTO = "America/Argentina/Buenos_Aires"
MAX_SUSCRIPTORES_SSE = 20
# Gateway de producción (diseño del gateway, §1.9).
MAX_CUERPO_V1_BYTES = 1024 * 1024
MAX_CUERPO_A2A_BYTES = 5 * 1024 * 1024
COMPRA_TTL_S = 24 * 3600
# Varias instancias (lote C del plan de Vercel). Los defaults dejan la demo
# local igual que siempre: una instancia y los webhooks en segundo plano.
PRESUPUESTO_LLAMADA_S = 570.0  # el agente del sample espera hasta 600 s por llamada
SONDEO_S = 1.0
GRACIA_ESPERA_S = 30.0
BARRERA_S = 10.0
INSTANCIAS = ("una", "varias")
PERFILES_WEBHOOKS = ("fondo", "en_linea")
# Retención de las tablas `gw_*` (`proxy/retencion.py`): lo más viejo que
# esto se borra con la purga. Tiene que pasar la vida de una compra.
RETENCION_DIAS = 90
RETENCION_MINIMA_DIAS = 7
RETENCION_MAXIMA_DIAS = 3650
# La ruta de la purga (Vercel Cron) y su secreto (`GATEWAY_PURGA_SECRETO`, o
# `CRON_SECRET`, el que Vercel manda en el `Authorization` de cada cron).
RUTA_PURGA = "/interno/purgar"
LARGO_MINIMO_SECRETO_PURGA = 16  # lo que pide Vercel para CRON_SECRET
LARGO_MAXIMO_SECRETO_PURGA = 512


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
    # KAIAI_ZONA_HORARIA (IANA): la zona del "hoy" del agente de intención
    # sin DEMO_HOY. En Vercel el reloj está en UTC: con la fecha del sistema,
    # un usuario de Argentina entre las 21 y las 24 recibía el día siguiente.
    # Sin tzdata, ver `zona_horaria`.
    zona_horaria: str = ZONA_HORARIA_POR_DEFECTO
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
    # -- varias instancias sin memoria compartida (Vercel) --------------------
    # `una` (la demo local, `python -m proxy`): las esperas son Futures en
    # memoria y las escrituras al almacén van en segundo plano, como siempre.
    # `varias` (GATEWAY_INSTANCIAS=varias): las esperas de las compras de
    # integrador viven en el almacén (`proxy/esperas.py`), cada pedido lee la
    # compra de la base y, antes de responder, se espera lo que se escribió.
    instancias: Literal["una", "varias"] = "una"
    # Lo que puede durar una llamada A2A con sus esperas (el porqué antes de
    # reenviar, la decisión después) y la llamada al comercio.
    presupuesto_llamada_s: float = PRESUPUESTO_LLAMADA_S
    # Cada cuánto se mira la espera en el almacén (con retroceso hasta 2 s).
    sondeo_s: float = SONDEO_S
    # Una espera abierta que pasó su vencimiento más esto se da por huérfana
    # (la instancia que la retenía murió) y la cierra del lado seguro quien la lea.
    gracia_espera_s: float = GRACIA_ESPERA_S
    # Tope de la barrera antes de responder (escrituras y webhooks en vuelo).
    barrera_s: float = BARRERA_S
    # `fondo`: reintentos con backoff en segundo plano (hasta ~7 min). `en_linea`:
    # pocos intentos cortos que se esperan antes de responder (sin `waitUntil`).
    webhooks_perfil: Literal["fondo", "en_linea"] = "fondo"
    # Latido de un pedido A2A retenido (GATEWAY_LATIDO_S): si la respuesta no
    # está a los `latido_s` segundos, se empieza a responder (200) y se manda
    # un espacio cada `latido_s` (en un stream, un comentario SSE) hasta el
    # cuerpo final: un proxy en el medio (Vercel) corta una conexión sin
    # bytes a los 60 s. None (el default, y "0" u "off") = apagado: como
    # siempre, la respuesta sale entera y con su status.
    latido_s: float | None = None
    # -- historial (panel del cliente) -----------------------------------------
    # Días que se guardan las tablas `gw_*` (GATEWAY_RETENCION_DIAS) y el
    # secreto de la purga (sin él, la ruta de la purga no existe).
    retencion_dias: int = RETENCION_DIAS
    purga_secreto: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # Fail-closed también con `dataclasses.replace` (el arranque de Vercel
        # fuerza estos campos así): un valor fuera de rango no arma la config.
        _validar_varias(self)
        _validar_retencion(self)

    @property
    def varias_instancias(self) -> bool:
        return self.instancias == "varias"

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
            zona_horaria=(env.get("KAIAI_ZONA_HORARIA") or "").strip()
            or base.zona_horaria,
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
            instancias=_opcion(env.get("GATEWAY_INSTANCIAS"), INSTANCIAS, "GATEWAY_INSTANCIAS"),
            presupuesto_llamada_s=_segundos(
                env.get("PROXY_PRESUPUESTO_LLAMADA_S"), base.presupuesto_llamada_s,
                "PROXY_PRESUPUESTO_LLAMADA_S",
            ),
            sondeo_s=_en_rango(
                env.get("GATEWAY_SONDEO_S"), base.sondeo_s, "GATEWAY_SONDEO_S", *_RANGO_SONDEO
            ),
            gracia_espera_s=_en_rango(
                env.get("GATEWAY_GRACIA_ESPERA_S"), base.gracia_espera_s,
                "GATEWAY_GRACIA_ESPERA_S", *_RANGO_GRACIA,
            ),
            barrera_s=_en_rango(
                env.get("GATEWAY_BARRERA_S"), base.barrera_s, "GATEWAY_BARRERA_S", *_RANGO_BARRERA
            ),
            webhooks_perfil=_opcion(
                env.get("GATEWAY_WEBHOOKS_PERFIL"), PERFILES_WEBHOOKS, "GATEWAY_WEBHOOKS_PERFIL"
            ),
            latido_s=_latido(env.get("GATEWAY_LATIDO_S")),
            retencion_dias=_entero_positivo(
                env.get("GATEWAY_RETENCION_DIAS"), base.retencion_dias, "GATEWAY_RETENCION_DIAS"
            ),
            purga_secreto=_secreto_purga(env),
        )


def _segundos(valor: str | None, defecto: float, nombre: str) -> float:
    if valor is None or not valor.strip():
        return defecto
    segundos = float(valor)
    if not 0 < segundos < 600:
        raise ValueError(f"{nombre} tiene que estar entre 0 y 600 segundos, no {valor!r}")
    return segundos


# (mínimo, máximo, ¿vale el mínimo?) de los campos de varias instancias.
_RANGO_PRESUPUESTO = (0.0, 600.0, False)
_RANGO_SONDEO = (0.0, 10.0, False)
_RANGO_GRACIA = (0.0, 3600.0, True)
_RANGO_BARRERA = (0.0, 60.0, False)
_RANGO_LATIDO = (0.0, 60.0, False)
_LATIDO_APAGADO = frozenset({"0", "off", "no", "false", "apagado"})


def _latido(valor: str | None) -> float | None:
    """`GATEWAY_LATIDO_S`: vacío, "0" u "off" = apagado (None); si no, segundos
    (más de 0 y hasta 60: el corte de Vercel es a los 60 s sin bytes)."""
    if valor is None or not valor.strip():
        return None
    if valor.strip().lower() in _LATIDO_APAGADO:
        return None
    return _en_rango(valor, 0.0, "GATEWAY_LATIDO_S", *_RANGO_LATIDO)


def _en_rango(
    valor: str | None,
    defecto: float,
    nombre: str,
    minimo: float,
    maximo: float,
    vale_minimo: bool,
) -> float:
    if valor is None or not valor.strip():
        return defecto
    try:
        numero = float(valor.strip())
    except ValueError:
        raise ValueError(f"{nombre} tiene que ser un número, no {valor!r}") from None
    _chequear_rango(numero, nombre, minimo, maximo, vale_minimo)
    return numero


def _chequear_rango(
    numero: Any, nombre: str, minimo: float, maximo: float, vale_minimo: bool
) -> None:
    """Un número finito en el rango (NaN e infinito no: un NaN da falso en
    toda comparación y dejaría una espera o una barrera sin tope)."""
    valido = (
        isinstance(numero, (int, float))
        and not isinstance(numero, bool)
        and math.isfinite(numero)
        and (numero >= minimo if vale_minimo else numero > minimo)
        and numero <= maximo
    )
    if not valido:
        desde = "desde" if vale_minimo else "más de"
        raise ValueError(
            f"{nombre} tiene que ser {desde} {minimo:g} y hasta {maximo:g}, no {numero!r}"
        )


def _opcion(valor: str | None, opciones: tuple[str, ...], nombre: str) -> Any:
    """Una de `opciones` (sin valor, la primera: la de la demo local)."""
    normalizado = (valor or "").strip().lower() or opciones[0]
    if normalizado not in opciones:
        raise ValueError(f"{nombre} tiene que ser {' o '.join(opciones)}, no {valor!r}")
    return normalizado


def _validar_varias(config: Config) -> None:
    """Los campos de varias instancias, también cuando llegan por `replace`."""
    if config.instancias not in INSTANCIAS:
        raise ValueError(f"instancias tiene que ser una o varias, no {config.instancias!r}")
    if config.webhooks_perfil not in PERFILES_WEBHOOKS:
        raise ValueError(
            f"webhooks_perfil tiene que ser fondo o en_linea, no {config.webhooks_perfil!r}"
        )
    presupuesto = config.presupuesto_llamada_s
    _chequear_rango(presupuesto, "presupuesto_llamada_s", *_RANGO_PRESUPUESTO)
    if presupuesto >= _RANGO_PRESUPUESTO[1]:
        # Como `_segundos`: menos que el DEFAULT_TIMEOUT (600 s) del agente.
        raise ValueError(f"presupuesto_llamada_s tiene que ser menos de 600, no {presupuesto!r}")
    _chequear_rango(config.sondeo_s, "sondeo_s", *_RANGO_SONDEO)
    _chequear_rango(config.gracia_espera_s, "gracia_espera_s", *_RANGO_GRACIA)
    _chequear_rango(config.barrera_s, "barrera_s", *_RANGO_BARRERA)
    if config.latido_s is not None:
        _chequear_rango(config.latido_s, "latido_s", *_RANGO_LATIDO)


def _validar_retencion(config: Config) -> None:
    """La retención entre los topes y más larga que la vida de una compra
    (si no, la purga borraría compras vivas); el secreto, bien formado."""
    dias = config.retencion_dias
    if (
        not isinstance(dias, int) or isinstance(dias, bool)
        or not RETENCION_MINIMA_DIAS <= dias <= RETENCION_MAXIMA_DIAS
    ):
        raise ValueError(
            f"GATEWAY_RETENCION_DIAS tiene que estar entre {RETENCION_MINIMA_DIAS} y "
            f"{RETENCION_MAXIMA_DIAS} días, no {dias!r}"
        )
    if dias * 86400 <= config.compra_ttl_s:
        raise ValueError(
            "GATEWAY_RETENCION_DIAS tiene que ser más larga que la vida de una compra "
            "(GATEWAY_COMPRA_TTL_S)"
        )
    if config.purga_secreto is not None:
        _chequear_secreto_purga(config.purga_secreto, "el secreto de la purga")


def _chequear_secreto_purga(secreto: Any, nombre: str) -> str:
    """Largo y sin espacios ni caracteres de control (va en un header). El
    mensaje nombra la variable, nunca el valor."""
    if not isinstance(secreto, str) or not (
        LARGO_MINIMO_SECRETO_PURGA <= len(secreto) <= LARGO_MAXIMO_SECRETO_PURGA
    ):
        raise ValueError(
            f"{nombre} tiene que tener de {LARGO_MINIMO_SECRETO_PURGA} a "
            f"{LARGO_MAXIMO_SECRETO_PURGA} caracteres"
        )
    if not secreto.isascii() or not secreto.isprintable() or any(c.isspace() for c in secreto):
        raise ValueError(f"{nombre} tiene que ser ASCII imprimible y sin espacios")
    return secreto


def _secreto_purga(env: Mapping[str, str]) -> str | None:
    """`GATEWAY_PURGA_SECRETO` o, si no está, `CRON_SECRET` (el de Vercel
    Cron). Con las dos, tienen que ser iguales.

    Un secreto que no sirve (corto, con espacios, o las dos distintas) no
    tumba el motor: la purga queda apagada (None) y el motivo va al log, sin
    el secreto. La purga no decide nada de una compra."""
    propio = (env.get("GATEWAY_PURGA_SECRETO") or "").strip()
    cron = (env.get("CRON_SECRET") or "").strip()
    try:
        if propio and cron and not hmac.compare_digest(propio.encode(), cron.encode()):
            raise ValueError("GATEWAY_PURGA_SECRETO y CRON_SECRET tienen que ser iguales (o una sola)")
        if propio:
            return _chequear_secreto_purga(propio, "GATEWAY_PURGA_SECRETO")
        if cron:
            return _chequear_secreto_purga(cron, "CRON_SECRET")
    except ValueError as e:
        logger.warning("La purga de la retención queda apagada: %s.", e)
    return None


def _jsonl_privado(principal: Path, pedido: Path | None) -> Path:
    return pedido or principal.with_name("guard_events_privados.jsonl")


# Argentina no tiene horario de verano desde 2009: UTC-3 fijo sirve igual.
_UTC_MENOS_3 = timezone(timedelta(hours=-3), "UTC-03")


def zona_horaria(nombre: str) -> tzinfo:
    """La zona de `KAIAI_ZONA_HORARIA`. Si `zoneinfo` no la encuentra (sin
    tzdata, como puede pasar en Vercel, o un nombre que no existe): la de
    Argentina, UTC-3 fijo; otra, UTC con un aviso en el log."""
    try:
        return ZoneInfo(nombre)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        if nombre == ZONA_HORARIA_POR_DEFECTO:
            return _UTC_MENOS_3
        logger.warning("KAIAI_ZONA_HORARIA=%r no se encontró: el hoy va en UTC", nombre)
        return timezone.utc


def hoy_en(zona: tzinfo, ahora: datetime | None = None) -> date:
    """La fecha de hoy en `zona` (`ahora`, para los tests)."""
    return (ahora or datetime.now(timezone.utc)).astimezone(zona).date()


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
                "activas (GATEWAY_CLAVES_ARCHIVO o GATEWAY_CLAVES_JSON): no se abre el "
                "gateway público y se "
                "ignora GATEWAY_HOST.",
                config.gateway_host,
            )
        return
    if config.gateway_port == config.port:
        raise SystemExit(
            f"GATEWAY_PORT={config.gateway_port} es el mismo puerto que PROXY_PORT: el "
            "oyente público y el local tienen que usar puertos distintos."
        )
