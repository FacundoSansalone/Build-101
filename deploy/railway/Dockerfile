# syntax=docker/dockerfile:1
#
# La demo entera de KaiAI en un solo contenedor (Railway): los 5 procesos de
# world/, el proxy del Guard y el panel, como deploy/start_all.sh, y el
# enrutador de deploy/vercel/tunel_router.py como única puerta en $PORT
# (deploy/railway/arrancar.sh). Ver deploy/railway/README.md.
#
# El contexto es la raíz del repo (lo que entra lo fija .dockerignore):
#
#   docker build -f deploy/railway/Dockerfile -t kaiai-demo .
#
# Los secretos (GOOGLE_API_KEY, DASHBOARD_ACCESS_TOKEN, TUNEL_PREFIJO) van en
# las variables del servicio, nunca en la imagen.

FROM python:3.12-slim-trixie

# bash y curl: start_all.sh y run_world.sh. git: uv.lock trae x402-a2a de un
# repo git (uv usa el git del sistema). tini es el PID 1: le pasa las señales
# de Railway a arrancar.sh y junta los procesos huérfanos.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends bash curl ca-certificates git tini \
 && rm -rf /var/lib/apt/lists/*

# uv con versión exacta (la del equipo: uv.lock está escrito con ella).
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /uvx /usr/local/bin/

# El Python de la imagen (world/ap2 pide >= 3.11; el venv local y Vercel
# usan 3.12). UV_FROZEN: run_world.sh vuelve a correr `uv sync` en cada
# arranque, y así usa el lock tal cual, sin resolver ni bajar nada.
ENV PYTHONUTF8=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PYTHON_DOWNLOADS=never \
    UV_FROZEN=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

# Sin root: el código, el venv, .logs/ y .temp-db/ son del usuario app.
RUN useradd --create-home --uid 1000 --shell /bin/bash app \
 && mkdir /app \
 && chown app:app /app
USER app
WORKDIR /app

# Primero solo lo que define el venv, en una capa aparte: cambia poco y es
# lo que más tarda.
COPY --chown=app:app world/ap2/pyproject.toml world/ap2/uv.lock world/ap2/README.md world/ap2/
COPY --chown=app:app world/ap2/code/samples/python/pyproject.toml world/ap2/code/samples/python/
RUN cd world/ap2 && uv sync --package ap2-samples --no-install-workspace

# El código y los paquetes del sample (ap2, ap2-samples), como run_world.sh.
# Los .sh con LF aunque el contexto venga de un checkout de Windows con CRLF.
COPY --chown=app:app . .
RUN find . -name '*.sh' -exec sed -i 's/\r$//' {} + \
 && cd world/ap2 \
 && uv sync --package ap2-samples \
 && mkdir -p /app/.logs /app/.temp-db

EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash", "deploy/railway/arrancar.sh"]
