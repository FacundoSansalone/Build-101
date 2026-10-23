#!/bin/bash
# postCreateCommand del Codespace (.devcontainer/devcontainer.json): corre una
# sola vez, al crearlo. Instala uv (la misma versión que deploy/railway/Dockerfile:
# uv.lock está escrito con ella) y arma el venv de world/ap2 como run_world.sh
# (uv sync --package ap2-samples, con UV_FROZEN=1: el lock tal cual).
#
#   bash deploy/codespaces/instalar.sh
#
# Las variables de uv (UV_PYTHON, UV_FROZEN…) vienen de containerEnv. No usa
# ningún secreto. Ver deploy/codespaces/README.md.
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$RAIZ"

UV_VERSION="0.13.1"
export PATH="$HOME/.local/bin:$PATH"

if [ "$(uv --version 2>/dev/null | cut -d' ' -f2)" != "$UV_VERSION" ]; then
  echo "-> uv $UV_VERSION"
  python3 -m pip install --user --quiet --disable-pip-version-check "uv==$UV_VERSION"
fi

echo "-> El venv de world/ap2 (uv sync --package ap2-samples; la primera vez tarda unos minutos)"
(cd world/ap2 && uv sync --package ap2-samples)
mkdir -p .logs .temp-db

echo "Listo. Al prender, deploy/codespaces/prender.sh levanta la demo (log en .logs/codespaces.log)."
