"""`python -m proxy`: levanta el proxy del Guard (default en :8010)."""

from __future__ import annotations

import logging

import uvicorn

from proxy.app import create_app
from proxy.config import Config, verificar_host_local
from proxy.evaluador import EvaluadorNoDisponible
from proxy.persistencia import Persistencia


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    config = Config.desde_entorno()
    verificar_host_local(config)  # loopback salvo PROXY_ALLOW_REMOTE=1
    # Supabase es opcional: sin SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY, nada.
    persistencia = Persistencia.desde_entorno()
    try:
        app = create_app(config, persistencia=persistencia)
    except EvaluadorNoDisponible as e:  # fail-closed: sin Guard real no arranca
        raise SystemExit(f"El proxy del Guard no arranca: {e}") from e
    proxy = app.state.proxy
    logging.getLogger("proxy").info(
        "Proxy del Guard en http://%s:%d%s → comercio %s | catálogo %s | "
        "Guard %s | evaluador: %s | eventos: %s | Supabase: %s",
        config.host,
        config.port,
        config.rpc_path,
        config.merchant_upstream_url,
        config.catalogo_url,
        "sí" if config.guard_activo else "no",
        proxy.evaluador.nombre,
        config.events_jsonl,
        "sí" if persistencia else "no",
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
