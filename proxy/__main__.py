"""`python -m proxy`: levanta el proxy del Guard (default en :8010).

Con claves de API activas (`GATEWAY_CLAVES_ARCHIVO` o `GATEWAY_CLAVES_JSON`),
el mismo proceso abre además el oyente público del gateway (default
`127.0.0.1:8030`): una sola app y un solo servidor sobre varios sockets, así
comparten el estado de las compras, las ofertas y las aprobaciones (diseño
del gateway, §1.1). Con `PROXY_HOST=127.0.0.1` (el default) el oyente local
escucha además en `[::1]`: el Shopping Agent habla con `localhost`, que en
Windows prueba primero IPv6.

Con `GATEWAY_CONSOLA=1` sirve además la consola web (`/consola`, ver
`proxy/consola/`) en los dos oyentes; sin la variable, la demo queda igual.

Los cargadores que arman cosas desde el entorno (intención, filtro,
fabricante, integradores, comercios, clave del recibo) viven en
`proxy.arranque`, que no importa uvicorn (los usa también la entrada
serverless); acá se re-exportan con los mismos nombres. Este módulo es el
único que importa uvicorn y la demo local (`proxy.demo_local`, dentro de
`main()`).
"""

from __future__ import annotations

import errno
import logging
import os
import socket
import sys
from typing import Any

import uvicorn

from proxy.app import create_app
from proxy.arranque import (
    VAR_CLAVES_JSON,
    VAR_COMERCIOS_JSON,
    cargar_agente,
    cargar_clave_recibo,
    cargar_comercios,
    cargar_fabricante,
    cargar_filtro,
    cargar_gateway,
    cargar_integradores,
    exigir_filtro,
    exigir_filtro_gateway,
    json_de_variable,
    modo_filtro,
    nombre_fabricante,
    validar_registros,
)
from proxy.config import Config, _bool, verificar_gateway, verificar_host_local
from proxy.evaluador import (
    VAR_CLAVE_PEM,
    VAR_CLAVE_PEM_B64,
    ClaveRecibo,
    ClaveReciboInvalida,
    EvaluadorNoDisponible,
    cargar_evaluador,
    validar_evaluador,
)
from proxy.persistencia import Persistencia

# Lo que se importaba de acá antes de `proxy.arranque` (tests, scripts y
# deploy/) sigue con el mismo nombre.
__all__ = [
    "VAR_CLAVES_JSON",
    "VAR_COMERCIOS_JSON",
    "abrir_socket",
    "cargar_agente",
    "cargar_clave_recibo",
    "cargar_comercios",
    "cargar_fabricante",
    "cargar_filtro",
    "cargar_gateway",
    "cargar_integradores",
    "consola_del_entorno",
    "exigir_filtro",
    "exigir_filtro_gateway",
    "json_de_variable",
    "main",
    "modo_filtro",
    "servir",
    "sockets_de",
    "validar_registros",
]

logger = logging.getLogger("proxy")


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


def clave_recibo_del_entorno(
    config: Config, env: dict[str, str] | None = None
) -> ClaveRecibo | None:
    """La clave del recibo de `GUARD_SIGNING_KEY_PEM_B64` / `_PEM` (con
    `GUARD_SIGNING_KID`), para inyectarla en `create_app`; None si no está
    ninguna de las dos variables, y entonces todo queda como siempre (la del
    archivo `GUARD_KEY_PATH`, que `create_app` crea si falta).

    Si está y no sirve, no arranca (fail-closed), sin la clave en el mensaje.
    """
    env = dict(os.environ) if env is None else env
    if not any((env.get(nombre) or "").strip() for nombre in (VAR_CLAVE_PEM_B64, VAR_CLAVE_PEM)):
        return None
    try:
        return cargar_clave_recibo(config, env)  # con las variables: la del entorno
    except ClaveReciboInvalida as e:
        raise SystemExit(f"El proxy del Guard no arranca: {e}") from None


def consola_del_entorno(env: dict[str, str] | None = None) -> bool:
    """`GATEWAY_CONSOLA`: la consola web (`/consola`). Sin la variable o
    vacía, apagada; un valor que no es true/false no arranca (ValueError)."""
    env = dict(os.environ) if env is None else env
    valor = (env.get("GATEWAY_CONSOLA") or "").strip()
    return _bool(valor, "GATEWAY_CONSOLA") if valor else False


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        config = Config.desde_entorno()
        consola = consola_del_entorno()
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
    clave_recibo = clave_recibo_del_entorno(config)
    # Supabase es opcional: sin SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY, nada.
    persistencia = Persistencia.desde_entorno()
    agente = cargar_agente()
    filtro = cargar_filtro()
    filtro_modo = modo_filtro(filtro)
    fabricante = cargar_fabricante()
    # La demo local: fichas de los escenarios fijos y chequeo de AP2 desde
    # demo_world (fuera del motor; se importa acá, no al importar el módulo).
    from proxy.demo_local import FuentesDemo

    try:
        app = create_app(
            config, evaluador=evaluador, persistencia=persistencia, agente=agente, filtro=filtro,
            filtro_modo=filtro_modo, fabricante=fabricante, integradores=integradores,
            comercios=comercios, almacen=almacen, demo=FuentesDemo(),
            clave_recibo=clave_recibo, consola=consola,
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
        (f"sí ({filtro_modo})" if filtro_modo else "sí")
        if filtro
        else ("NO (PROXY_PERMITIR_SIN_FILTRO=1)" if sin_filtro else "no"),
        nombre_fabricante(fabricante),
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
    if consola:
        logger.info(
            "Consola web: http://%s:%d/consola%s",
            config.host,
            config.port,
            f" y {config.gateway_public_url}/consola" if gateway
            else " (sin claves de API activas: /v1 da 401)",
        )
    # Un solo servidor sobre los sockets: el local (127.0.0.1 y [::1]) y, con
    # gateway, el público. Todos comparten el estado.
    servir(app, sockets_de(app, config, gateway))


if __name__ == "__main__":
    main()
