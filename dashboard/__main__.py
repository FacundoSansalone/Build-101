"""`python -m dashboard` → http://localhost:8020 (desde la raíz del repo).

Variables de entorno:
  DASHBOARD_PORT          puerto (8020)
  DASHBOARD_HOST          interfaz (127.0.0.1; WebCrypto necesita localhost o HTTPS)
  DASHBOARD_ALLOW_REMOTE  1 para escuchar fuera de loopback (no recomendado)
  PROXY_URL               proxy del Guard (http://127.0.0.1:8010)
  GUARD_API_TOKEN_PATH    token de la API del proxy (.temp-db/guard_api_token)
"""

from __future__ import annotations

import os

import uvicorn

from dashboard.server import config_desde_entorno, create_app, verificar_host_local


def main() -> None:
    cfg = config_desde_entorno()
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    permitir = os.environ.get("DASHBOARD_ALLOW_REMOTE", "0").strip().lower() in {"1", "true", "si", "sí"}
    verificar_host_local(host, permitir_remoto=permitir)
    port = cfg.port
    print(f"Dashboard en http://localhost:{port}  (proxy: {cfg.proxy_url})")
    uvicorn.run(
        create_app(cfg),
        host=host,
        port=port,
        log_level="info",
        # El SSE reenviado queda abierto: no esperar para siempre al cortar.
        timeout_graceful_shutdown=2,
    )


if __name__ == "__main__":
    main()
