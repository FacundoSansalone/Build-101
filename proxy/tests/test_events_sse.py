"""GET /guard/events por SSE, contra un servidor uvicorn de verdad."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn

from contracts import Evento


def _puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def servidor(armar_proxy, config) -> Iterator[str]:
    from dataclasses import replace

    puerto = _puerto_libre()
    # La API de control solo atiende Host localhost/127.0.0.1 con su puerto.
    app = armar_proxy(config=replace(config, port=puerto), ping_segundos=0.2)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=puerto, log_level="warning")
    )
    hilo = threading.Thread(target=server.run, daemon=True)
    hilo.start()
    limite = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > limite:
            raise RuntimeError("uvicorn no arrancó")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{puerto}"
    server.should_exit = True
    hilo.join(timeout=10)


def test_sse_reenvia_historial_y_eventos_nuevos(servidor: str):
    with httpx.Client(base_url=servidor, timeout=10) as c:
        c.post("/guard/state", json={"guard_activo": False})
        c.post("/guard/state", json={"guard_activo": True})

        with c.stream("GET", "/guard/events") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            lineas = resp.iter_lines()
            viejos = []
            nombre = None
            for linea in lineas:
                if linea.startswith("event:"):
                    nombre = linea[6:].strip()
                elif linea.startswith("data:"):
                    assert nombre == "evento"
                    viejos.append(Evento.model_validate_json(linea[5:].strip()))
                    if len(viejos) == 2:
                        break
            assert [e.datos["guard_activo"] for e in viejos] == [False, True]

            # Un evento nuevo llega por la misma conexión.
            with httpx.Client(base_url=servidor, timeout=10) as otro:
                otro.post("/guard/state", json={"guard_activo": False})
            for linea in lineas:
                if linea.startswith("data:"):
                    nuevo = Evento.model_validate_json(linea[5:].strip())
                    break
            assert nuevo.tipo == "guard_cambiado"
            assert nuevo.datos == {"guard_activo": False}
