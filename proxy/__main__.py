"""`python -m proxy`: levanta el proxy del Guard (default en :8010).

Con claves de API activas (`GATEWAY_CLAVES_ARCHIVO`), el mismo proceso abre
además el oyente público del gateway (default `127.0.0.1:8030`): una sola
app y un solo servidor sobre varios sockets, así comparten el estado de las
compras, las ofertas y las aprobaciones (diseño del gateway, §1.1). Con
`PROXY_HOST=127.0.0.1` (el default) el oyente local escucha además en
`[::1]`: el Shopping Agent habla con `localhost`, que en Windows prueba
primero IPv6.
"""

from __future__ import annotations

import errno
import importlib
import logging
import os
import socket
import sys
from typing import Any

import uvicorn

from proxy.app import create_app
from proxy.claves import RegistroIntegradores
from proxy.comercios import RegistroComercios, comercio_por_defecto
from proxy.config import Config, _bool, verificar_gateway, verificar_host_local
from proxy.estado import estado_desde_entorno
from proxy.evaluador import EvaluadorNoDisponible, cargar_evaluador, validar_evaluador
from proxy.persistencia import Persistencia
from proxy.tipos_gateway import ConfigInvalida
from proxy.webhooks import DestinoNoPermitido, validar_url_estatica

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


def cargar_fabricante(env: dict[str, str] | None = None) -> Any:
    """Fuente del fabricante por GTIN (`fabricante.fuente_desde_entorno`).

    `FABRICANTE_FUENTE=fixture|icecat|ninguna`, por defecto `fixture` (datos
    de prueba rotulados como tales). Con `ninguna` no se consulta: lo
    obligatorio que solo declara el comercio escala. Una configuración
    inválida (otro valor, `icecat` sin `ICECAT_USUARIO`) no arranca: una
    fuente pedida que falta no se saltea en silencio.
    """
    from fabricante import fuente_desde_entorno

    env = dict(os.environ) if env is None else env
    try:
        return fuente_desde_entorno(env)
    except ValueError as e:
        raise SystemExit(f"El proxy del Guard no arranca: {e}") from e


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


def exigir_filtro_gateway(filtro: Any, env: dict[str, str] | None = None) -> bool:
    """Con el gateway activo el filtro es obligatorio, haya o no agente de
    intención: el tráfico de los integradores es de comercios que el operador
    no controla y sus respuestas llegan al agente del integrador. Respeta
    `PROXY_PERMITIR_SIN_FILTRO=1` (tests y demo), como `exigir_filtro`.
    Devuelve True si arranca sin filtro con ese permiso."""
    if filtro is not None:
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
            "El proxy del Guard no arranca: el gateway (GATEWAY_CLAVES_ARCHIVO) está activo "
            "y no hay filtro de seguridad (GUARD_FILTRO=paquete.modulo:funcion). Configurá "
            "el filtro o, solo para tests o la demo, PROXY_PERMITIR_SIN_FILTRO=1."
        )
    logger.warning(
        "GATEWAY SIN FILTRO DE SEGURIDAD (PROXY_PERMITIR_SIN_FILTRO=1): las respuestas de "
        "los comercios no se revisan. Solo para tests o la demo."
    )
    return True


def cargar_gateway(
    config: Config, env: dict[str, str] | None = None
) -> tuple[RegistroIntegradores, RegistroComercios, Any]:
    """Integradores, comercios y almacén del gateway, validados al arrancar.

    Cualquier configuración inválida no arranca (`SystemExit`): una
    configuración a medias podría dejar pasar a quien no corresponde o
    mandar tráfico a un comercio que el operador no aprobó.
    """
    env = dict(os.environ) if env is None else env
    try:
        integradores = (
            RegistroIntegradores.desde_archivo(config.claves_archivo, env)
            if config.claves_archivo is not None
            else RegistroIntegradores.vacio()
        )
        if config.comercios_archivo is not None:
            comercios = RegistroComercios.desde_archivo(config.comercios_archivo)
            logger.warning(
                "Comercios desde %s: MERCHANT_UPSTREAM_URL y CATALOGO_URL se ignoran.",
                config.comercios_archivo,
            )
        else:
            comercios = RegistroComercios.solo(
                comercio_por_defecto(
                    id=config.comercio_por_defecto_id,
                    a2a_url=config.merchant_upstream_url,
                    catalogo_url=config.catalogo_url,
                    respaldo_fixture=config.catalogo_respaldo_fixture,
                )
            )
    except ConfigInvalida as e:
        raise SystemExit(f"El proxy del Guard no arranca: {e}") from e
    faltan = comercios.ids_inexistentes(integradores.todos())
    if faltan:
        raise SystemExit(
            "El proxy del Guard no arranca: el archivo de claves nombra comercios que no "
            f"están registrados: {', '.join(faltan)}"
        )
    for integrador in integradores.todos():
        if integrador.webhook is None:
            continue
        try:
            validar_url_estatica(
                integrador.webhook.url, permitir_inseguro=config.webhooks_inseguros
            )
        except DestinoNoPermitido as e:
            raise SystemExit(
                f"El proxy del Guard no arranca: el webhook del integrador {integrador.id} "
                f"no se puede usar ({e})."
            ) from e
    try:
        almacen = estado_desde_entorno(env)
    except ValueError as e:
        raise SystemExit(f"El proxy del Guard no arranca: {e}") from e
    return integradores, comercios, almacen


# IPv6 que no está (sin pila o sin `::1`): el proxy sigue solo con IPv4.
_SIN_IPV6 = {errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT}


def abrir_socket(host: str, puerto: int) -> socket.socket:
    """Un socket TCP atado a `host:puerto` (uvicorn lo pone a escuchar).

    En Windows con `SO_EXCLUSIVEADDRUSE`: con `SO_REUSEADDR` (lo que usa
    uvicorn) otro proceso se puede atar al mismo puerto y quedarse con
    conexiones que eran del proxy. En el resto, `SO_REUSEADDR` (rearrancar sin
    esperar TIME_WAIT; ahí no deja compartir un puerto que escucha).
    """
    familia = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(familia, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if familia == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind((host.strip("[]"), puerto))
    except BaseException:
        sock.close()
        raise
    sock.set_inheritable(True)
    return sock


def _abrir_o_salir(host: str, puerto: int) -> socket.socket:
    try:
        return abrir_socket(host, puerto)
    except OSError as e:
        raise SystemExit(
            f"El proxy del Guard no arranca: no se pudo escuchar en {host}:{puerto} ({e})."
        ) from e


def _ipv6_local(puerto: int) -> socket.socket | None:
    """`[::1]` al mismo puerto que `127.0.0.1`, o None (con un aviso) si no hay
    IPv6. Si el puerto lo tiene otro proceso, no arranca: un cliente de
    `localhost` (que prueba primero `::1`) le hablaría a ese y no al proxy."""
    try:
        return abrir_socket("::1", puerto)
    except OSError as e:
        if e.errno not in _SIN_IPV6:
            raise SystemExit(
                f"El proxy del Guard no arranca: no se pudo escuchar en [::1]:{puerto} ({e}). "
                "Si otro proceso tiene ese puerto, los clientes de localhost le hablarían a "
                "ese y no al proxy."
            ) from e
        logger.warning(
            "Sin IPv6 en [::1] (%s): el proxy escucha solo en 127.0.0.1. En Windows, cada "
            "conexión nueva a localhost tarda ~2 s más (prueba primero ::1).",
            e,
        )
        return None


def sockets_de(app: Any, config: Config, gateway: bool) -> list[socket.socket]:
    """Los sockets del servidor: el local siempre (y `[::1]` al mismo puerto
    si el host es `127.0.0.1`) y, con el gateway activo, el público. Se abren
    acá (no en uvicorn) para que un solo servidor atienda todos; el
    middleware los distingue por el puerto local. Si uno no se puede abrir,
    no arranca (y cierra los que ya abrió)."""
    sockets = [_abrir_o_salir(config.host, config.port)]
    try:
        if config.host == "127.0.0.1":
            # El puerto real del primero (con PROXY_PORT=0 lo elige el sistema).
            extra = _ipv6_local(sockets[0].getsockname()[1])
            if extra is not None:
                sockets.append(extra)
        if gateway:
            sockets.append(_abrir_o_salir(config.gateway_host, config.gateway_port))
    except BaseException:
        for sock in sockets:
            sock.close()
        raise
    for sock in sockets:
        host, puerto = sock.getsockname()[:2]
        logger.info("Escuchando en http://%s:%d", f"[{host}]" if ":" in host else host, puerto)
    return sockets


def servir(app: Any, sockets: list[socket.socket]) -> None:
    """Un solo servidor uvicorn sobre todos los sockets (comparten el estado)."""
    servidor = uvicorn.Server(
        uvicorn.Config(app, log_level="info", timeout_keep_alive=120, timeout_graceful_shutdown=3)
    )
    try:
        servidor.run(sockets=sockets)
    except KeyboardInterrupt:  # como uvicorn.run: Ctrl+C cierra limpio
        pass


def _nombre_fabricante(fuente: Any) -> str:
    if fuente is None:
        return "ninguna (lo obligatorio que solo declara el comercio escala)"
    nombre = getattr(fuente, "nombre", "?")
    return f"{nombre} (datos de prueba)" if nombre == "fixture" else str(nombre)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        config = Config.desde_entorno()
    except ValueError as e:
        raise SystemExit(f"El proxy del Guard no arranca: {e}") from e
    integradores, comercios, almacen = cargar_gateway(config)
    gateway = integradores.hay_claves()
    verificar_host_local(config)  # loopback salvo PROXY_ALLOW_REMOTE=1
    verificar_gateway(config, gateway)  # el público, solo con claves
    if gateway and config.permitir_stub:
        raise SystemExit(
            "El proxy del Guard no arranca: con el gateway activo (claves de API) no se "
            "permite el evaluador stub (PROXY_ALLOW_STUB=1)."
        )
    # Fail-closed primero: sin Guard real no arranca, y el error que se ve es
    # ese (el fabricante, el filtro y la intención también importan guard/).
    try:
        evaluador = cargar_evaluador(config.permitir_stub)
        validar_evaluador(evaluador, config.permitir_stub)
    except EvaluadorNoDisponible as e:
        raise SystemExit(f"El proxy del Guard no arranca: {e}") from e
    # Supabase es opcional: sin SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY, nada.
    persistencia = Persistencia.desde_entorno()
    agente = cargar_agente()
    filtro = cargar_filtro()
    fabricante = cargar_fabricante()
    try:
        app = create_app(
            config, evaluador=evaluador, persistencia=persistencia, agente=agente, filtro=filtro,
            fabricante=fabricante, integradores=integradores, comercios=comercios, almacen=almacen,
        )
    except EvaluadorNoDisponible as e:  # fail-closed: sin Guard real no arranca
        raise SystemExit(f"El proxy del Guard no arranca: {e}") from e
    # Con la intención activa, sin filtro no arranca (después del evaluador:
    # si falta el Guard real, ese es el primer error que se ve). Con el
    # gateway activo, tampoco aunque no haya agente.
    sin_filtro = exigir_filtro(agente, filtro)
    if gateway:
        sin_filtro = exigir_filtro_gateway(filtro) or sin_filtro
    proxy = app.state.proxy
    logger.info(
        "Proxy del Guard en http://%s:%d%s → comercio %s | catálogo %s | "
        "Guard %s | evaluador: %s | eventos: %s | Supabase: %s | intención: %s | "
        "filtro: %s | fabricante: %s",
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
        _nombre_fabricante(fabricante),
    )
    logger.info(
        "Gateway: %s | integradores: %d | comercios: %s | almacén: %s",
        f"sí, en http://{config.gateway_host}:{config.gateway_port} (pública: "
        f"{config.gateway_public_url})"
        if gateway
        else "no (sin claves de API activas)",
        len(integradores.todos()),
        ", ".join(c.id for c in comercios.todos()),
        almacen.nombre,
    )
    # Un solo servidor sobre los sockets: el local (127.0.0.1 y [::1]) y, con
    # gateway, el público. Todos comparten el estado.
    servir(app, sockets_de(app, config, gateway))


if __name__ == "__main__":
    main()
