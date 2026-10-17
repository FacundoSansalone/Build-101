"""`python -m dashboard` → http://localhost:8020 (desde la raíz del repo).

Variables de entorno:
  DASHBOARD_PORT  puerto (8020)
  DASHBOARD_HOST  interfaz (127.0.0.1; WebCrypto necesita localhost o HTTPS)
  PROXY_URL       proxy del Guard (http://localhost:8010)
"""

from __future__ import annotations

import os

import uvicorn

from dashboard.server import DEFAULT_PORT, config_desde_entorno, create_app


def main() -> None:
    cfg = config_desde_entorno()
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", DEFAULT_PORT))
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
