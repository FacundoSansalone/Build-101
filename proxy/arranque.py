"""Cargadores del arranque: lo que se arma desde el entorno antes de la app.

Los usan `python -m proxy` (el servidor local, que los re-exporta con los
mismos nombres) y la entrada serverless (Vercel). Este módulo no importa
uvicorn ni nada de la simulación: en Vercel uvicorn no está instalado
(`proxy/tests/test_arranque.py` lo prueba en un subproceso).

- Intención, filtro y fabricante: `cargar_agente`, `cargar_filtro` (y
  `modo_filtro`), `cargar_fabricante`; con `exigir_filtro` y
  `exigir_filtro_gateway`, el filtro obligatorio.
- Gateway: integradores y comercios desde un archivo
  (`GATEWAY_CLAVES_ARCHIVO`, `GATEWAY_COMERCIOS_ARCHIVO`) o desde una
  variable con el mismo JSON (`GATEWAY_CLAVES_JSON`,
  `GATEWAY_COMERCIOS_JSON`: sin disco, como en Vercel), y la validación
  cruzada (comercios que nombran las claves, webhooks).
- La clave del recibo: `cargar_clave_recibo` (del entorno o del archivo).

Fail-closed: lo que se pidió y no sirve no arranca. Los cargadores de bajo
nivel lanzan `ConfigInvalida` / `ClaveReciboInvalida` (las dos son
`ValueError`) y los de `python -m proxy` las pasan a `SystemExit`. Ningún
mensaje lleva el contenido de una variable: puede tener secretos.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
from collections.abc import Mapping
from typing import Any

from proxy.claves import RegistroIntegradores
from proxy.comercios import RegistroComercios, comercio_por_defecto
from proxy.config import Config, _bool
from proxy.estado import estado_desde_entorno
from proxy.evaluador import ClaveRecibo, kid_del_entorno
from proxy.tipos_gateway import ConfigInvalida
from proxy.webhooks import DestinoNoPermitido, validar_url_estatica

# El mismo logger que `python -m proxy`: el log de la demo queda igual.
logger = logging.getLogger("proxy")

VAR_CLAVES_JSON = "GATEWAY_CLAVES_JSON"
VAR_COMERCIOS_JSON = "GATEWAY_COMERCIOS_JSON"
_NO_ARRANCA = "El proxy del Guard no arranca"


def _entorno(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return dict(os.environ) if env is None else env


# ---------------------------------------------------------------------------
# Intención, filtro y fabricante
# ---------------------------------------------------------------------------


def cargar_agente(env: Mapping[str, str] | None = None) -> Any:
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
    env = _entorno(env)
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


def cargar_filtro(env: Mapping[str, str] | None = None) -> Any:
    """Filtro de seguridad de Dev 1 (`GUARD_FILTRO=paquete.modulo:funcion`).

    Sin la variable no hay filtro. Si se pidió y no se puede cargar, el proxy
    no arranca: un filtro pedido que falta no se saltea en silencio.
    """
    env = _entorno(env)
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


def modo_filtro(filtro: Any, env: Mapping[str, str] | None = None) -> str | None:
    """El modo del filtro cargado, para el log y `/guard/state`: lo que dice
    `describir()` del módulo de `GUARD_FILTRO` (o del de la función), si lo
    tiene. Solo informa: si no está o falla, None y el filtro sigue igual."""
    if filtro is None:
        return None
    env = _entorno(env)
    nombres = [
        (env.get("GUARD_FILTRO") or "").strip().partition(":")[0],
        getattr(filtro, "__module__", None),
    ]
    for nombre in nombres:
        describir = getattr(sys.modules.get(nombre or ""), "describir", None)
        if not callable(describir):
            continue
        try:
            modo = describir()
        except Exception as e:  # noqa: BLE001 - informativo: no corta el arranque
            logger.warning("describir() del filtro falló (%s): sin modo", type(e).__name__)
            return None
        if isinstance(modo, dict):
            modo = modo.get("modo")
        return str(modo)[:200] if modo is not None else None
    return None


def cargar_fabricante(env: Mapping[str, str] | None = None) -> Any:
    """Fuente del fabricante por GTIN (`fabricante.fuente_desde_entorno`).

    `FABRICANTE_FUENTE=fixture|icecat|ninguna`, por defecto `fixture` (datos
    de prueba rotulados como tales). Con `ninguna` no se consulta: lo
    obligatorio que solo declara el comercio escala. Una configuración
    inválida (otro valor, `icecat` sin `ICECAT_USUARIO`) no arranca: una
    fuente pedida que falta no se saltea en silencio.
    """
    from fabricante import fuente_desde_entorno

    env = _entorno(env)
    try:
        return fuente_desde_entorno(env)
    except ValueError as e:
        raise SystemExit(f"{_NO_ARRANCA}: {e}") from e


def nombre_fabricante(fuente: Any) -> str:
    """Cómo se muestra la fuente del fabricante en la línea de arranque."""
    if fuente is None:
        return "ninguna (lo obligatorio que solo declara el comercio escala)"
    nombre = getattr(fuente, "nombre", "?")
    return f"{nombre} (datos de prueba)" if nombre == "fixture" else str(nombre)


def exigir_filtro(agente: Any, filtro: Any, env: Mapping[str, str] | None = None) -> bool:
    """Con la capa de intención activa, el filtro de seguridad es obligatorio.

    Nuestro agente mira lo que ofrece el proveedor para buscar oportunidades:
    sin filtro, esas respuestas le llegan sin revisar. Si hay agente y no hay
    filtro (`GUARD_FILTRO`), el proxy no arranca, salvo
    `PROXY_PERMITIR_SIN_FILTRO=1` explícito (tests y demo), que se avisa en el
    log. Devuelve True si arranca sin filtro con ese permiso.
    """
    if agente is None or filtro is not None:
        return False
    env = _entorno(env)
    try:
        permitido = _bool(
            env.get("PROXY_PERMITIR_SIN_FILTRO", "0"), "PROXY_PERMITIR_SIN_FILTRO"
        )
    except ValueError as e:
        raise SystemExit(f"{_NO_ARRANCA}: {e}") from e
    if not permitido:
        raise SystemExit(
            f"{_NO_ARRANCA}: la capa de intención está activa y no hay "
            "filtro de seguridad (GUARD_FILTRO=paquete.modulo:funcion). Configurá el "
            "filtro, apagá la intención con INTENCION_ACTIVA=0 o, solo para tests o "
            "la demo, PROXY_PERMITIR_SIN_FILTRO=1."
        )
    logger.warning(
        "SIN FILTRO DE SEGURIDAD (PROXY_PERMITIR_SIN_FILTRO=1): las respuestas del "
        "proveedor le llegan sin revisar al agente de intención. Solo para tests o la demo."
    )
    return True


def exigir_filtro_gateway(filtro: Any, env: Mapping[str, str] | None = None) -> bool:
    """Con el gateway activo el filtro es obligatorio, haya o no agente de
    intención: el tráfico de los integradores es de comercios que el operador
    no controla y sus respuestas llegan al agente del integrador. Respeta
    `PROXY_PERMITIR_SIN_FILTRO=1` (tests y demo), como `exigir_filtro`.
    Devuelve True si arranca sin filtro con ese permiso."""
    if filtro is not None:
        return False
    env = _entorno(env)
    try:
        permitido = _bool(
            env.get("PROXY_PERMITIR_SIN_FILTRO", "0"), "PROXY_PERMITIR_SIN_FILTRO"
        )
    except ValueError as e:
        raise SystemExit(f"{_NO_ARRANCA}: {e}") from e
    if not permitido:
        raise SystemExit(
            f"{_NO_ARRANCA}: el gateway (claves de API activas en GATEWAY_CLAVES_ARCHIVO "
            f"o {VAR_CLAVES_JSON}) está activo y no hay filtro de seguridad "
            "(GUARD_FILTRO=paquete.modulo:funcion). Configurá el filtro o, solo para "
            "tests o la demo, PROXY_PERMITIR_SIN_FILTRO=1."
        )
    logger.warning(
        "GATEWAY SIN FILTRO DE SEGURIDAD (PROXY_PERMITIR_SIN_FILTRO=1): las respuestas de "
        "los comercios no se revisan. Solo para tests o la demo."
    )
    return True


# ---------------------------------------------------------------------------
# Gateway: integradores y comercios (archivo o variable) y validación cruzada
# ---------------------------------------------------------------------------


def json_de_variable(env: Mapping[str, str], nombre: str) -> Any | None:
    """El JSON de la variable `nombre`, o None si la variable no está.

    Definida pero vacía (o solo espacios), un JSON inválido o `null` son un
    error: quien la definió quiso configurar algo (y `null` no puede
    confundirse con "no está", que volvería al archivo o a la configuración
    de siempre). El mensaje dice solo la posición del error, nunca el
    contenido (puede tener secretos), y la excepción no se encadena
    (`JSONDecodeError.doc` lleva el texto entero).
    """
    if nombre not in env:
        return None
    texto = env[nombre]
    if not isinstance(texto, str) or not texto.strip():
        raise ConfigInvalida(f"{nombre} está definida pero vacía: poné el JSON o sacala")
    try:
        datos = json.loads(texto)
    except json.JSONDecodeError as e:
        raise ConfigInvalida(
            f"{nombre} no es JSON válido (línea {e.lineno}, columna {e.colno})"
        ) from None
    if datos is None:
        raise ConfigInvalida(f"{nombre} es null: tiene que ser un objeto JSON")
    return datos


def _de_variable_o_archivo(
    env: Mapping[str, str], variable: str, archivo: Any, nombre_archivo: str
) -> Any | None:
    """El JSON de `variable` si está. Con la variable y el archivo a la vez
    es un error: no se elige uno en silencio."""
    if variable in env and archivo is not None:
        raise ConfigInvalida(
            f"{variable} y {nombre_archivo} están los dos: definí uno solo"
        )
    return json_de_variable(env, variable)


def cargar_integradores(
    config: Config, env: Mapping[str, str] | None = None
) -> RegistroIntegradores:
    """Los integradores y sus claves (solo hashes sha256): de
    `GATEWAY_CLAVES_JSON` (el mismo JSON que el archivo), de
    `GATEWAY_CLAVES_ARCHIVO` (`config.claves_archivo`) o, sin ninguno, vacío
    (sin oyente público). `ConfigInvalida` si algo está mal."""
    env = _entorno(env)
    datos = _de_variable_o_archivo(
        env, VAR_CLAVES_JSON, config.claves_archivo, "GATEWAY_CLAVES_ARCHIVO"
    )
    if datos is not None:
        try:
            return RegistroIntegradores.desde_dict(datos, env)
        except ConfigInvalida as e:
            raise ConfigInvalida(f"{VAR_CLAVES_JSON}: {e}") from None
    if config.claves_archivo is not None:
        return RegistroIntegradores.desde_archivo(config.claves_archivo, env)
    return RegistroIntegradores.vacio()


def cargar_comercios(
    config: Config, env: Mapping[str, str] | None = None
) -> RegistroComercios:
    """El registro de comercios: de `GATEWAY_COMERCIOS_JSON` (el mismo JSON
    que el archivo), de `GATEWAY_COMERCIOS_ARCHIVO`
    (`config.comercios_archivo`) o, sin ninguno, el comercio de siempre
    (`MERCHANT_UPSTREAM_URL`, `CATALOGO_URL`). `ConfigInvalida` si algo está
    mal."""
    env = _entorno(env)
    datos = _de_variable_o_archivo(
        env, VAR_COMERCIOS_JSON, config.comercios_archivo, "GATEWAY_COMERCIOS_ARCHIVO"
    )
    if datos is not None:
        try:
            comercios = RegistroComercios.desde_dict(datos)
        except ConfigInvalida as e:
            raise ConfigInvalida(f"{VAR_COMERCIOS_JSON}: {e}") from None
        logger.warning(
            "Comercios desde %s: MERCHANT_UPSTREAM_URL y CATALOGO_URL se ignoran.",
            VAR_COMERCIOS_JSON,
        )
        return comercios
    if config.comercios_archivo is not None:
        comercios = RegistroComercios.desde_archivo(config.comercios_archivo)
        logger.warning(
            "Comercios desde %s: MERCHANT_UPSTREAM_URL y CATALOGO_URL se ignoran.",
            config.comercios_archivo,
        )
        return comercios
    return RegistroComercios.solo(
        comercio_por_defecto(
            id=config.comercio_por_defecto_id,
            a2a_url=config.merchant_upstream_url,
            catalogo_url=config.catalogo_url,
            respaldo_fixture=config.catalogo_respaldo_fixture,
        )
    )


def validar_registros(
    integradores: RegistroIntegradores,
    comercios: RegistroComercios,
    *,
    webhooks_inseguros: bool,
) -> None:
    """La validación cruzada del arranque (`ConfigInvalida` si falla):

    - todo comercio que nombra un integrador está registrado (si no, sus
      compras no tendrían a dónde ir ni con qué `merchant_ids` atar lo
      firmado: la `Politica` del Guard sale de ahí);
    - el webhook de cada integrador pasa la regla de SSRF estática
      (`GATEWAY_WEBHOOKS_INSEGUROS=1` solo en desarrollo).
    """
    faltan = comercios.ids_inexistentes(integradores.todos())
    if faltan:
        raise ConfigInvalida(
            "las claves de API nombran comercios que no están registrados: "
            + ", ".join(faltan)
        )
    for integrador in integradores.todos():
        if integrador.webhook is None:
            continue
        try:
            validar_url_estatica(integrador.webhook.url, permitir_inseguro=webhooks_inseguros)
        except DestinoNoPermitido as e:
            raise ConfigInvalida(
                f"el webhook del integrador {integrador.id} no se puede usar ({e})."
            ) from None


def cargar_gateway(
    config: Config, env: Mapping[str, str] | None = None
) -> tuple[RegistroIntegradores, RegistroComercios, Any]:
    """Integradores, comercios y almacén del gateway, validados al arrancar.

    Cualquier configuración inválida no arranca (`SystemExit`): una
    configuración a medias podría dejar pasar a quien no corresponde o
    mandar tráfico a un comercio que el operador no aprobó.
    """
    env = _entorno(env)
    try:
        integradores = cargar_integradores(config, env)
        comercios = cargar_comercios(config, env)
        validar_registros(
            integradores, comercios, webhooks_inseguros=config.webhooks_inseguros
        )
    except ConfigInvalida as e:
        raise SystemExit(f"{_NO_ARRANCA}: {e}") from None
    try:
        almacen = estado_desde_entorno(env)
    except ValueError as e:
        raise SystemExit(f"{_NO_ARRANCA}: {e}") from e
    return integradores, comercios, almacen


# ---------------------------------------------------------------------------
# Clave del recibo
# ---------------------------------------------------------------------------


def cargar_clave_recibo(
    config: Config,
    env: Mapping[str, str] | None = None,
    *,
    crear_si_falta: bool = True,
) -> ClaveRecibo:
    """La clave con que se firman los recibos.

    La del entorno (`GUARD_SIGNING_KEY_PEM_B64` o `GUARD_SIGNING_KEY_PEM`,
    con `GUARD_SIGNING_KID`) si está; si no, la del archivo
    `config.guard_key_path` (`GUARD_KEY_PATH`, por defecto
    `.temp-db/guard_signing_key.pem`, con el kid de `GUARD_SIGNING_KID` o
    `guard-key-1`), que se crea si falta salvo `crear_si_falta=False` (con
    varias instancias cada una crearía otra con el mismo kid).

    `ClaveReciboInvalida` (un `ValueError`, sin la clave en el mensaje) si
    lo configurado no sirve.
    """
    env = _entorno(env)
    clave = ClaveRecibo.desde_entorno(env)
    if clave is not None:
        return clave
    return ClaveRecibo.desde_archivo(
        config.guard_key_path, kid_del_entorno(env), crear_si_falta=crear_si_falta
    )
