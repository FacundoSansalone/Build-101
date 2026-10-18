"""`python -m proxy`: levanta el proxy del Guard (default en :8010)."""

from __future__ import annotations

import importlib
import logging
import os
from typing import Any

import uvicorn

from proxy.app import create_app
from proxy.config import Config, _bool, verificar_host_local
from proxy.evaluador import EvaluadorNoDisponible
from proxy.persistencia import Persistencia

logger = logging.getLogger("proxy")


def cargar_agente(env: dict[str, str] | None = None) -> Any:
    """Agente de intención (etapa 2), o None.

    `INTENCION_PROVEEDOR=decisiones|gemini`. Sin la variable: `decisiones` si
    hay cliente de decisiones (OPENAI_API_KEY o TYPESAFE_API_KEY, ver
    `decisiones.cliente_desde_entorno`), si no `gemini`.

    - decisiones: intake con juicios tipados (repregunta hasta tener todo).
      Las oportunidades las detecta Gemini si hay GOOGLE_API_KEY o
      GEMINI_API_KEY; si no, no hay oportunidades.
    - gemini: el agente de siempre (una sola pregunta). INTENCION_MODELO
      cambia el modelo.

    Sin la clave del proveedor, o con INTENCION_ACTIVA=0, no hay pregunta ni
    oportunidades: el proxy sigue como en la etapa 1. Un proveedor
    desconocido no arranca.
    """
    env = dict(os.environ) if env is None else env
    if not _bool(env.get("INTENCION_ACTIVA", "1"), "INTENCION_ACTIVA"):
        return None
    proveedor = (env.get("INTENCION_PROVEEDOR") or "").strip().lower()
    if proveedor not in ("", "decisiones", "gemini"):
        raise SystemExit(
            f"INTENCION_PROVEEDOR={proveedor!r} no existe: usá decisiones o gemini."
        )
    from decisiones import cliente_desde_entorno

    try:
        cliente = cliente_desde_entorno(env)
    except ValueError as e:  # DECISIONES_PROVEEDOR desconocido
        raise SystemExit(str(e)) from e
    clave_gemini = env.get("GOOGLE_API_KEY") or env.get("GEMINI_API_KEY")

    def gemini() -> Any:
        from google import genai

        from intencion import MODELO_POR_DEFECTO, AgenteGemini

        return AgenteGemini(
            cliente=genai.Client(api_key=clave_gemini),
            modelo=env.get("INTENCION_MODELO") or MODELO_POR_DEFECTO,
        )

    if proveedor == "decisiones" or (not proveedor and cliente is not None):
        if cliente is None:
            logger.warning(
                "INTENCION_PROVEEDOR=decisiones sin OPENAI_API_KEY ni TYPESAFE_API_KEY: "
                "la capa de intención (pregunta y oportunidades) queda apagada."
            )
            return None
        from intencion.agente_decisiones import AgenteDecisiones

        if not clave_gemini:
            logger.warning(
                "Intención con decisiones sin GOOGLE_API_KEY: hay intake (pregunta y "
                "repreguntas) pero no detección de oportunidades."
            )
        return AgenteDecisiones(cliente, detector=gemini() if clave_gemini else None)
    if not clave_gemini:
        logger.warning(
            "Sin GOOGLE_API_KEY ni GEMINI_API_KEY (ni cliente de decisiones): la capa "
            "de intención (pregunta y oportunidades) queda apagada."
        )
        return None
    return gemini()


def cargar_filtro(env: dict[str, str] | None = None) -> Any:
    """Filtro de seguridad de Dev 1 (`GUARD_FILTRO=paquete.modulo:funcion`).

    Sin la variable no hay filtro. Si se pidió y no se puede cargar, el proxy
    no arranca: un filtro pedido que falta no se saltea en silencio.
    """
    env = dict(os.environ) if env is None else env
    ruta = (env.get("GUARD_FILTRO") or "").strip()
    if not ruta:
        return None
    modulo, _, nombre = ruta.partition(":")
    try:
        filtro = getattr(importlib.import_module(modulo), nombre or "filtrar")
    except (ImportError, AttributeError) as e:
        raise SystemExit(f"GUARD_FILTRO={ruta!r} no se pudo cargar: {e}") from e
    if not callable(filtro):
        raise SystemExit(f"GUARD_FILTRO={ruta!r} no es una función")
    return filtro


def exigir_filtro(agente: Any, filtro: Any, env: dict[str, str] | None = None) -> bool:
    """Con la capa de intención activa, el filtro de seguridad es obligatorio.

    Nuestro agente mira lo que ofrece el proveedor para buscar oportunidades:
    sin filtro, esas respuestas le llegan sin revisar. Si hay agente y no hay
    filtro (`GUARD_FILTRO`), el proxy no arranca, salvo
    `PROXY_PERMITIR_SIN_FILTRO=1` explícito (tests y demo), que se avisa en el
    log. Devuelve True si arranca sin filtro con ese permiso.
    """
    if agente is None or filtro is not None:
        return False
    env = dict(os.environ) if env is None else env
    try:
        permitido = _bool(
            env.get("PROXY_PERMITIR_SIN_FILTRO", "0"), "PROXY_PERMITIR_SIN_FILTRO"
        )
    except ValueError as e:
        raise SystemExit(f"El proxy del Guard no arranca: {e}") from e
    if not permitido:
        raise SystemExit(
            "El proxy del Guard no arranca: la capa de intención está activa y no hay "
            "filtro de seguridad (GUARD_FILTRO=paquete.modulo:funcion). Configurá el "
            "filtro, apagá la intención con INTENCION_ACTIVA=0 o, solo para tests o "
            "la demo, PROXY_PERMITIR_SIN_FILTRO=1."
        )
    logger.warning(
        "SIN FILTRO DE SEGURIDAD (PROXY_PERMITIR_SIN_FILTRO=1): las respuestas del "
        "proveedor le llegan sin revisar al agente de intención. Solo para tests o la demo."
    )
    return True


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    config = Config.desde_entorno()
    verificar_host_local(config)  # loopback salvo PROXY_ALLOW_REMOTE=1
    # Supabase es opcional: sin SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY, nada.
    persistencia = Persistencia.desde_entorno()
    agente = cargar_agente()
    filtro = cargar_filtro()
    try:
        app = create_app(config, persistencia=persistencia, agente=agente, filtro=filtro)
    except EvaluadorNoDisponible as e:  # fail-closed: sin Guard real no arranca
        raise SystemExit(f"El proxy del Guard no arranca: {e}") from e
    # Con la intención activa, sin filtro no arranca (después del evaluador:
    # si falta el Guard real, ese es el primer error que se ve).
    sin_filtro = exigir_filtro(agente, filtro)
    proxy = app.state.proxy
    logger.info(
        "Proxy del Guard en http://%s:%d%s → comercio %s | catálogo %s | "
        "Guard %s | evaluador: %s | eventos: %s | Supabase: %s | intención: %s | "
        "filtro: %s",
        config.host,
        config.port,
        config.rpc_path,
        config.merchant_upstream_url,
        config.catalogo_url,
        "sí" if config.guard_activo else "no",
        proxy.evaluador.nombre,
        config.events_jsonl,
        "sí" if persistencia else "no",
        getattr(agente, "modelo", None) or "apagada",
        "sí" if filtro else ("NO (PROXY_PERMITIR_SIN_FILTRO=1)" if sin_filtro else "no"),
    )
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_level="info",
        timeout_keep_alive=120,
        timeout_graceful_shutdown=3,
    )


if __name__ == "__main__":
    main()
