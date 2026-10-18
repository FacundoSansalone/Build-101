"""Corre demo_world.benchmark respetando la cuota del plan gratuito de Gemini.

El plan gratuito permite 15 pedidos por minuto por modelo y el benchmark no
espacia las llamadas. Este lanzador no modifica world/ap2/: reemplaza
`make_llm_decider` por uno que espera entre llamadas y reintenta las fallas
transitorias de Gemini (429, 500, 503, desconexiones), y después llama a
`benchmark.main()` con los mismos argumentos.

  cd world/ap2 && PYTHONUTF8=1 PYTHONPATH=../.. uv run --no-sync --package ap2-samples \
    python ../bench_con_pausa.py --trials 10 --guard guard.benchmark_hook:evaluate \
    --out ../../results

Variables: BENCH_INTERVALO_S (default 4.5 s entre llamadas),
BENCH_REINTENTOS (default 5).
"""

from __future__ import annotations

import os
import threading
import time

from demo_world import benchmark

INTERVALO_S = float(os.environ.get("BENCH_INTERVALO_S", "4.5"))
REINTENTOS = int(os.environ.get("BENCH_REINTENTOS", "5"))

_original = benchmark.make_llm_decider


def _es_transitorio(error: Exception) -> bool:
    """Fallas de Gemini que se reintentan: cuota (429), saturación (500/503)
    y desconexiones. Solo envuelve la llamada al LLM: un error del lado A2A
    (proxy o comercio) no pasa por acá y se sigue reportando."""
    if getattr(error, "code", None) in (429, 500, 503):
        return True
    texto = f"{type(error).__name__}: {error}"
    return any(
        marca in texto
        for marca in ("RESOURCE_EXHAUSTED", "UNAVAILABLE", "RemoteProtocolError",
                      "Server disconnected", "ReadTimeout", "ConnectError")
    )


def make_llm_decider_con_pausa(model: str):
    decide = _original(model)
    candado = threading.Lock()
    ultima = [0.0]

    def decide_con_pausa(carts):
        for intento in range(1, REINTENTOS + 1):
            with candado:
                espera = ultima[0] + INTERVALO_S - time.monotonic()
                if espera > 0:
                    time.sleep(espera)
                ultima[0] = time.monotonic()
            try:
                return decide(carts)
            except Exception as e:  # el benchmark registra el error si se agota
                if not _es_transitorio(e) or intento == REINTENTOS:
                    raise
                time.sleep(30)
        raise RuntimeError("inalcanzable")

    return decide_con_pausa


benchmark.make_llm_decider = make_llm_decider_con_pausa

if __name__ == "__main__":
    benchmark.main()
