#!/bin/bash
# Levanta el mundo de la demo: el flujo AP2 human-present del sample oficial,
# con el comercio malicioso y el catálogo público.
#
#   bash world/run_world.sh            # escenario baseline
#   bash world/run_world.sh B          # arranca en el escenario B
#
# Puertos:
#   8000  Shopping Agent (ADK web UI: http://localhost:8000/dev-ui). Solo en
#         loopback: para publicarla en la red, ADK_HOST=<host> (por ejemplo
#         la IP de la máquina); no hace falta para la demo, que es /demo.
#   8001  Merchant Agent (A2A)            ← el atacante
#   8002  Credentials Provider (A2A)
#   8003  Merchant Payment Processor (A2A)
#   8011  Catálogo público del comercio (HTTP)
#
# Para meter el proxy del Guard en el medio (Dev 3):
#   MERCHANT_AGENT_URL=http://localhost:8010/a2a/merchant_agent bash world/run_world.sh B
#
# Cambiar de escenario sin reiniciar:
#   (cd world/ap2 && uv run --no-sync --package ap2-samples python -m demo_world.set_scenario C)

set -e

SCENARIO="${1:-baseline}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/ap2"

if [ -f .env ]; then set -a; source .env; set +a; fi
if [ -f ../../.env ]; then set -a; source ../../.env; set +a; fi

USE_VERTEXAI=$(printf "%s" "${GOOGLE_GENAI_USE_VERTEXAI}" | tr '[:upper:]' '[:lower:]')
if [ -z "${GOOGLE_API_KEY}" ] && [ "${USE_VERTEXAI}" != "true" ]; then
  echo "Falta GOOGLE_API_KEY (o GOOGLE_GENAI_USE_VERTEXAI=true)."
  echo "Ponela en un archivo .env en la raíz del repo: GOOGLE_API_KEY=tu_clave"
  exit 1
fi

echo "Sincronizando dependencias..."
uv sync --package ap2-samples

LOG_DIR=".logs"
mkdir -p "$LOG_DIR"
rm -f "$LOG_DIR"/*

export TEMP_DB_DIR="${TEMP_DB_DIR:-$(pwd)/.temp-db}"
rm -rf "$TEMP_DB_DIR"
mkdir -p "$TEMP_DB_DIR"
export AGENT_PROVIDER_PUBLIC_KEY_PATH="$TEMP_DB_DIR/agent_provider_signing_key.pub"
export PAYMENT_METHOD="${PAYMENT_METHOD:-CARD}"

RUN="uv run --no-sync --package ap2-samples"

$RUN python -m demo_world.set_scenario "$SCENARIO"

pids=()
cleanup() {
  echo ""
  echo "Apagando procesos..."
  if [ ${#pids[@]} -ne 0 ]; then
    kill "${pids[@]}" 2>/dev/null || true
    wait "${pids[@]}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

echo "-> Merchant Agent        :8001  ($LOG_DIR/merchant_agent.log)"
$RUN python -m roles.merchant_agent >"$LOG_DIR/merchant_agent.log" 2>&1 &
pids+=($!)

echo "-> Credentials Provider  :8002  ($LOG_DIR/credentials_provider_agent.log)"
$RUN python -m roles.credentials_provider_agent >"$LOG_DIR/credentials_provider_agent.log" 2>&1 &
pids+=($!)

echo "-> Payment Processor     :8003  ($LOG_DIR/mpp_agent.log)"
$RUN python -m roles.merchant_payment_processor_agent >"$LOG_DIR/mpp_agent.log" 2>&1 &
pids+=($!)

echo "-> Catálogo público      :8011  ($LOG_DIR/public_catalog.log)"
$RUN python -m demo_world.public_catalog >"$LOG_DIR/public_catalog.log" 2>&1 &
pids+=($!)

if [ -n "$MERCHANT_AGENT_URL" ]; then
  echo "   El Shopping Agent habla con el comercio a través de: $MERCHANT_AGENT_URL"
fi

echo ""
echo "-> Shopping Agent        :8000  http://localhost:8000/dev-ui  (elegí 'shopping_agent')"
# La dev-ui de ADK no tiene autenticación: por defecto escucha solo en
# loopback y no se publica en la red de la hackatón (el panel habla con ella
# por 127.0.0.1). ADK_HOST la publica en otro host.
$RUN adk web --host "${ADK_HOST:-127.0.0.1}" code/samples/python/src/roles
