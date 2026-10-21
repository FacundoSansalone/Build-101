"""Entrada serverless del motor (Vercel): `index.py` hace `from proxy.vercel import app`.

`crear_app_serverless(env)` arma el gateway para varias instancias sin
memoria ni disco compartidos (diseño 2.2 del plan de Vercel):

- solo `/a2a/m/{comercio}` (POST y Agent Card) y `/v1/*`: sin la API de
  control (`/guard/*`), sin la ruta vieja del comercio por defecto, sin
  `ControlLocal` ni CORS, y todo pedido es del oyente público
  (`create_app(..., rutas_control=False, rutas_heredadas=False)`);
- además, la consola web (`/consola`, y `/` → 307 a `/consola`;
  `create_app(..., consola=True)`, ver `proxy/consola/`): estáticos sin
  clave que llaman a la API con la clave que pega el usuario;
- integradores y comercios de `GATEWAY_CLAVES_JSON` y
  `GATEWAY_COMERCIOS_JSON` (los archivos no se leen), con la validación
  cruzada de siempre y sin webhooks inseguros;
- la clave del recibo SOLO del entorno (`ClaveRecibo.desde_entorno`): nunca
  de un archivo ni creándola (cada instancia crearía otra con el mismo kid);
- el estado en Supabase (`GATEWAY_ESTADO=supabase`), las esperas
  compartidas y los webhooks en línea (`instancias="varias"`,
  `webhooks_perfil="en_linea"`); nada se escribe en disco (sin JSONL ni
  archivo de token);
- el filtro de seguridad obligatorio y del motor; el Guard real con
  `checkout=`; la intención si hay credenciales del LLM (sin ellas queda
  apagada y las compras siguen el pedido literal); el fabricante de
  `FABRICANTE_FUENTE`;
- los tiempos de Hobby (300 s por pedido): presupuesto por llamada 270 s,
  espera del porqué 60 s y de la decisión 150 s, y presupuesto + barrera +
  15 ≤ 300. Se pueden bajar por entorno; subirlos por encima del tope no
  arranca. Los cuerpos, hasta 4 MB (Vercel corta en 4,5 MB);
- el latido de un pedido A2A retenido cada 15 s (`GATEWAY_LATIDO_S`, hasta
  30 s; apagado no arranca): Vercel corta una conexión sin bytes a los 60 s;
- la retención de las tablas `gw_*` (`GATEWAY_RETENCION_DIAS`, 90 días, hasta
  365) y la purga por Vercel Cron (`/interno/purgar`, con `CRON_SECRET` o
  `GATEWAY_PURGA_SECRETO`; sin secreto la ruta no existe).

Fail-closed: si falta algo o algo no sirve, la app es una mínima que
responde 503 `{"error": "motor_no_configurado"}` a todo (también a
`/v1/openapi.json`), y el motivo va al log sin secretos. Armar la app no
hace I/O de red (los clientes HTTP y el de Supabase se conectan recién en
el primer pedido) ni escribe archivos.

Fuera del motor desplegado: el registro de auditoría de la demo
(`Persistencia`, tablas de `001_persistencia.sql`): el estado del gateway
va a las tablas `gw_*`.

Este módulo no importa `proxy.__main__`, uvicorn ni `proxy.demo_local`.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from proxy.app import create_app
from proxy.arranque import (
    VAR_CLAVES_JSON,
    VAR_COMERCIOS_JSON,
    cargar_agente,
    cargar_comercios,
    cargar_fabricante,
    cargar_filtro,
    cargar_integradores,
    modo_filtro,
    nombre_fabricante,
    validar_registros,
)
from proxy.config import Config, _bool, es_loopback
from proxy.estado import estado_desde_entorno
from proxy.evaluador import (
    VAR_CLAVE_PEM,
    VAR_CLAVE_PEM_B64,
    ClaveRecibo,
    EvaluadorNoDisponible,
    cargar_evaluador,
    validar_evaluador,
)
from proxy.persistencia import credenciales

logger = logging.getLogger("proxy.vercel")

# -- topes de Vercel Hobby -------------------------------------------------------
LIMITE_FUNCION_S = 300.0  # maxDuration de vercel.json (el máximo de Hobby)
MARGEN_S = 15.0  # para responder después de la barrera
PRESUPUESTO_LLAMADA_S = 270.0
ESPERA_CONTEXTO_S = 60.0
ESPERA_DECISION_S = 150.0
MAX_CUERPO_BYTES = 4_000_000  # Vercel corta pedidos y respuestas en 4,5 MB
# Entre el cliente y la función, una conexión sin bytes se corta a los 60 s:
# un pedido A2A retenido (el porqué, una decisión) late cada tanto. Se puede
# bajar por entorno (GATEWAY_LATIDO_S), no apagar ni subir de esto.
LATIDO_S = 15.0
MAX_LATIDO_S = 30.0
# La retención de las tablas `gw_*` (`proxy/retencion.py`): en la base del
# plan gratuito de Supabase, como mucho un año. La purga la dispara el cron
# de `vercel.json` con `CRON_SECRET` (o `GATEWAY_PURGA_SECRETO`).
MAX_RETENCION_DIAS = 365

# Variable → (campo de Config, tope en Vercel). Sin la variable, el tope; con
# ella, su valor si no lo supera (si lo supera, no arranca).
TOPES: dict[str, tuple[str, float]] = {
    "PROXY_PRESUPUESTO_LLAMADA_S": ("presupuesto_llamada_s", PRESUPUESTO_LLAMADA_S),
    "PROXY_ESPERA_CONTEXTO_S": ("espera_contexto_s", ESPERA_CONTEXTO_S),
    "PROXY_ESPERA_DECISION_S": ("espera_decision_s", ESPERA_DECISION_S),
    "GATEWAY_MAX_CUERPO_BYTES": ("max_cuerpo_v1", MAX_CUERPO_BYTES),
    "GATEWAY_MAX_CUERPO_A2A_BYTES": ("max_cuerpo_a2a", MAX_CUERPO_BYTES),
    "PROXY_MAX_RESPUESTA_BYTES": ("max_respuesta_bytes", MAX_CUERPO_BYTES),
    "GATEWAY_RETENCION_DIAS": ("retencion_dias", MAX_RETENCION_DIAS),
}

# Prendidas, no arranca: son para tests o la demo local.
PROHIBIDAS = (
    "PROXY_ALLOW_STUB",
    "PROXY_PERMITIR_SIN_FILTRO",
    "PROXY_FICHA_DEMO",
    "CATALOGO_RESPALDO_FIXTURE",
    "GATEWAY_WEBHOOKS_INSEGUROS",
)

# La frontera del motor, escrita acá (no se lee `pyproject.toml` en runtime):
# `proxy/tests/test_vercel_entrada.py` verifica que coincide con
# `[tool.kaiai.motor]`. `GUARD_FILTRO` solo puede apuntar a un módulo de
# estos paquetes que no esté excluido (en Vercel lo excluido no está).
PAQUETES_MOTOR = (
    "contracts",
    "guard",
    "filtro",
    "intencion",
    "decisiones",
    "taxonomia",
    "fabricante",
    "proxy",
)
MODULOS_EXCLUIDOS = (
    "*.tests",
    "*.tests.*",
    "proxy.scripts",
    "proxy.scripts.*",
    "proxy.subir_benchmark",
    "guard.benchmark_hook",
    "filtro.casos",
    "filtro.evaluar",
    "intencion.evaluar_*",
    "proxy.demo_local",
    # Entradas, no filtros.
    "proxy.__main__",
    "proxy.vercel",
)
_MODULO_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")
_NOMBRE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

RESPUESTA_NO_CONFIGURADO = {"error": "motor_no_configurado"}


class MotorNoConfigurado(ValueError):
    """Falta algo o algo no sirve: el motor responde 503 a todo. El mensaje
    va al log y nunca lleva el valor de una variable secreta."""


# ---------------------------------------------------------------------------
# La app mínima (fail-closed)
# ---------------------------------------------------------------------------


class AppNoConfigurada:
    """ASGI: 503 `{"error": "motor_no_configurado"}` a todo pedido HTTP (el
    motivo queda en el log, no en la respuesta) y el lifespan sin errores."""

    def __init__(self, motivo: str) -> None:
        self.motivo = motivo

    def __repr__(self) -> str:
        return "AppNoConfigurada()"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            while True:
                mensaje = await receive()
                if mensaje["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif mensaje["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] == "http":
            respuesta = JSONResponse(
                RESPUESTA_NO_CONFIGURADO, status_code=503, headers={"Cache-Control": "no-store"}
            )
            await respuesta(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1013})


# ---------------------------------------------------------------------------
# Chequeos
# ---------------------------------------------------------------------------


def _prendida(env: Mapping[str, str], nombre: str) -> bool:
    valor = (env.get(nombre) or "").strip()
    if not valor:
        return False
    try:
        return _bool(valor, nombre)
    except ValueError:
        return True  # un valor que no se entiende cuenta como prendida


def _chequear_prohibidas(env: Mapping[str, str]) -> None:
    prendidas = [nombre for nombre in PROHIBIDAS if _prendida(env, nombre)]
    if prendidas:
        raise MotorNoConfigurado(
            f"variables prohibidas en el despliegue: {', '.join(prendidas)} (son para tests "
            "o la demo local; sacalas)"
        )


def _config(env: Mapping[str, str]) -> Config:
    """La configuración del entorno con lo que el despliegue fuerza."""
    base = Config.desde_entorno(env)
    topes: dict[str, Any] = {}
    for variable, (campo, tope) in TOPES.items():
        valor = getattr(base, campo)
        if not (env.get(variable) or "").strip():
            # Sin la variable: el default de siempre si entra; si no, el tope.
            topes[campo] = valor if valor <= tope else type(valor)(tope)
        elif valor > tope:
            raise MotorNoConfigurado(
                f"{variable}={valor:g} supera el tope del despliegue ({tope:g})"
            )
    config = replace(
        base,
        **topes,
        # Nada en disco ni de la demo.
        events_jsonl=None,
        events_jsonl_privado=None,
        api_token_path=None,
        claves_archivo=None,
        comercios_archivo=None,
        ficha_demo=False,
        demo_hoy=None,
        catalogo_respaldo_fixture=False,
        permitir_stub=False,
        webhooks_inseguros=False,
        guard_activo=True,
        # Varias instancias sin memoria compartida.
        instancias="varias",
        webhooks_perfil="en_linea",
        latido_s=_latido(env, base),
    )
    total = config.presupuesto_llamada_s + config.barrera_s + MARGEN_S
    if total > LIMITE_FUNCION_S:
        raise MotorNoConfigurado(
            f"presupuesto por llamada ({config.presupuesto_llamada_s:g} s) + barrera "
            f"({config.barrera_s:g} s) + {MARGEN_S:g} s = {total:g} s supera los "
            f"{LIMITE_FUNCION_S:g} s de la función"
        )
    return config


def _latido(env: Mapping[str, str], base: Config) -> float:
    """El latido de un pedido retenido: `LATIDO_S` sin la variable; con ella,
    su valor si no pasa de `MAX_LATIDO_S`. Apagado no arranca: sin latido,
    un porqué o una decisión retenidos se cortan a los 60 s."""
    if not (env.get("GATEWAY_LATIDO_S") or "").strip():
        return LATIDO_S
    if base.latido_s is None:
        raise MotorNoConfigurado(
            "GATEWAY_LATIDO_S no se puede apagar en el despliegue (sin latido, un pedido "
            "retenido se corta a los 60 s sin bytes)"
        )
    if base.latido_s > MAX_LATIDO_S:
        raise MotorNoConfigurado(
            f"GATEWAY_LATIDO_S={base.latido_s:g} supera el tope del despliegue ({MAX_LATIDO_S:g})"
        )
    return base.latido_s


def _url_segura(nombre: str, url: str) -> None:
    """https, o http contra loopback (la prueba local); sin usuario, query ni
    fragment. El mensaje nombra la variable, no la URL."""
    try:
        partes = urlsplit(url)
        host = partes.hostname
        partes.port  # noqa: B018 - valida el puerto
    except ValueError:
        raise MotorNoConfigurado(f"{nombre} no es una URL válida") from None
    if not host or partes.username is not None or partes.password is not None:
        raise MotorNoConfigurado(f"{nombre} tiene que ser una URL con host y sin usuario")
    if partes.query or partes.fragment:
        raise MotorNoConfigurado(f"{nombre} no puede llevar query ni fragment")
    if partes.scheme == "https":
        return
    if partes.scheme == "http" and es_loopback(host):
        return
    raise MotorNoConfigurado(f"{nombre} tiene que ser https (http solo contra loopback)")


def _chequear_url_publica(env: Mapping[str, str], config: Config) -> None:
    if not (env.get("GATEWAY_PUBLIC_URL") or "").strip():
        raise MotorNoConfigurado("falta GATEWAY_PUBLIC_URL (la URL https del deploy)")
    _url_segura("GATEWAY_PUBLIC_URL", config.gateway_public_url)


def _chequear_supabase(env: Mapping[str, str], config: Config) -> None:
    if config.estado_almacen != "supabase":
        raise MotorNoConfigurado(
            "GATEWAY_ESTADO tiene que ser supabase: con varias instancias el estado no "
            "puede vivir en memoria"
        )
    datos = credenciales(env)
    if datos is None:
        raise MotorNoConfigurado(
            "faltan SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY (o SUPABASE_SECRET_KEY)"
        )
    _url_segura("SUPABASE_URL", datos[0])


def modulo_del_motor(modulo: str) -> bool:
    """¿`modulo` es del motor desplegado (un paquete del motor y no excluido)?"""
    if not _MODULO_RE.fullmatch(modulo):
        return False
    if modulo.split(".")[0] not in PAQUETES_MOTOR:
        return False
    return not any(fnmatch.fnmatchcase(modulo, patron) for patron in MODULOS_EXCLUIDOS)


def _cargar_filtro(env: Mapping[str, str]) -> tuple[Any, str | None]:
    """El filtro de `GUARD_FILTRO`, obligatorio y del motor, y su modo.

    Si es la función por defecto de un módulo que se arma desde el entorno
    (`filtro:filtrar`, que la primera vez lee `os.environ`), se arma acá con
    `desde_entorno(env)`: el mismo entorno que el resto de la app (en Vercel
    es el mismo; en los tests, el que se pasa)."""
    ruta = (env.get("GUARD_FILTRO") or "").strip()
    if not ruta:
        raise MotorNoConfigurado(
            "falta GUARD_FILTRO (el filtro de seguridad es obligatorio con el gateway)"
        )
    modulo, _, funcion = ruta.partition(":")
    if not modulo_del_motor(modulo) or (funcion and not _NOMBRE_RE.fullmatch(funcion)):
        raise MotorNoConfigurado(f"GUARD_FILTRO={ruta!r} no es un filtro del motor")
    filtro = cargar_filtro(env)
    armar = getattr(sys.modules.get(modulo), "desde_entorno", None)
    if (funcion or "filtrar") == "filtrar" and callable(armar):
        filtro = armar(env)
        describir = getattr(filtro, "describir", None)
        modo = describir() if callable(describir) else None
        return filtro, (str(modo)[:200] if modo is not None else None)
    return filtro, modo_filtro(filtro, env)


def _cargar_evaluador() -> Any:
    evaluador = cargar_evaluador(False)
    validar_evaluador(evaluador, False)
    if evaluador.nombre != "guard" or not evaluador.acepta_checkout:
        raise MotorNoConfigurado("el evaluador no es el guard.evaluar() real con checkout=")
    return evaluador


def _motivo(error: BaseException) -> str:
    if isinstance(error, SystemExit):
        return str(error.code) if isinstance(error.code, str) else "un cargador no arrancó"
    if isinstance(error, (ValueError, EvaluadorNoDisponible)):
        return str(error)
    # Inesperado: solo el tipo (el texto podría citar algo del entorno).
    return f"error inesperado al armar la app ({type(error).__name__})"


# ---------------------------------------------------------------------------
# La app
# ---------------------------------------------------------------------------


def _armar(
    env: Mapping[str, str],
    *,
    almacen: Any,
    transport: Any,
) -> ASGIApp:
    _chequear_prohibidas(env)
    config = _config(env)
    _chequear_url_publica(env, config)
    _chequear_supabase(env, config)

    clave = ClaveRecibo.desde_entorno(env)
    if clave is None:
        raise MotorNoConfigurado(
            f"falta la clave del recibo ({VAR_CLAVE_PEM_B64} o {VAR_CLAVE_PEM}): en el "
            "despliegue nunca se crea una"
        )

    if VAR_CLAVES_JSON not in env:
        raise MotorNoConfigurado(f"falta {VAR_CLAVES_JSON} (los integradores y sus claves)")
    if VAR_COMERCIOS_JSON not in env:
        raise MotorNoConfigurado(f"falta {VAR_COMERCIOS_JSON} (el registro de comercios)")
    integradores = cargar_integradores(config, env)
    if not integradores.hay_claves():
        raise MotorNoConfigurado(f"{VAR_CLAVES_JSON} no tiene ninguna clave activa")
    comercios = cargar_comercios(config, env)
    validar_registros(integradores, comercios, webhooks_inseguros=False)

    filtro, filtro_modo = _cargar_filtro(env)
    evaluador = _cargar_evaluador()
    agente = cargar_agente(env)
    fabricante = cargar_fabricante(env)
    if almacen is None:
        almacen = estado_desde_entorno(env)
    if not getattr(almacen, "durable", False):
        raise MotorNoConfigurado("el almacén no es durable: con varias instancias no sirve")

    app = create_app(
        config,
        transport=transport,
        evaluador=evaluador,
        persistencia=None,
        agente=agente,
        filtro=filtro,
        filtro_modo=filtro_modo,
        fabricante=fabricante,
        integradores=integradores,
        comercios=comercios,
        almacen=almacen,
        demo=None,
        clave_recibo=clave,
        rutas_control=False,
        rutas_heredadas=False,
        consola=True,
    )
    logger.info(
        "Motor KaiAI (serverless) | integradores: %d | comercios: %s | filtro: %s | "
        "intención: %s | fabricante: %s | almacén: %s | recibo: kid %s | presupuesto %g s, "
        "porqué %g s, decisión %g s, barrera %g s, latido %g s | retención %d días (purga: %s)",
        len(integradores.todos()),
        ", ".join(c.id for c in comercios.todos()),
        filtro_modo or "sí",
        getattr(agente, "modelo", None) or "apagada (sin credenciales del LLM: pedido literal)",
        nombre_fabricante(fabricante),
        getattr(almacen, "nombre", "?"),
        clave.kid,
        config.presupuesto_llamada_s,
        config.espera_contexto_s,
        config.espera_decision_s,
        config.barrera_s,
        config.latido_s,
        config.retencion_dias,
        "sí" if config.purga_secreto else "sin secreto",
    )
    return app


def crear_app_serverless(
    env: Mapping[str, str],
    *,
    almacen: Any = None,
    transport: Any = None,
) -> ASGIApp:
    """La app del motor para Vercel, o `AppNoConfigurada` (503 a todo) si falta
    algo o algo no sirve. Nunca lanza.

    `almacen` (un `EstadoSupabase` u otro durable) y `transport` (el del
    comercio) son para los tests; sin ellos, Supabase según el entorno y la
    red de verdad."""
    try:
        return _armar(env, almacen=almacen, transport=transport)
    except (Exception, SystemExit) as e:  # noqa: BLE001 - fail-closed: 503 a todo
        motivo = _motivo(e)
    logger.error("El motor no arranca: %s. Responde 503 motor_no_configurado a todo.", motivo)
    return AppNoConfigurada(motivo)


def _configurar_log(env: Mapping[str, str]) -> None:
    """En Vercel (`VERCEL=1`, lo pone la plataforma) nadie configura el
    logging: sin esto solo saldrían los WARNING, por el handler de último
    recurso. Fuera de Vercel (tests, `local_multi.py`) o si ya hay handlers,
    no toca nada."""
    if (env.get("VERCEL") or "").strip() and not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s"
        )


_configurar_log(os.environ)
app = crear_app_serverless(os.environ)
