#!/bin/bash
# Levanta la demo entera en una sola máquina (local o la VM de Tenki):
#
#   world/ (5 procesos)  Shopping Agent :8000 · Merchant :8001 · Credentials :8002
#                        · Payment :8003 · catálogo :8011
#   proxy del Guard      :8010  (el Shopping Agent habla con el comercio por acá)
#   dashboard            :8020  (/demo: el input de la compra; lo único que se
#                                publica en un deploy)
#
#   bash deploy/start_all.sh                 # escenario notebook_honesto (guion de DEMO.md)
#   bash deploy/start_all.sh B               # otro escenario (después se cambia desde /demo)
#
# Todo habla por 127.0.0.1 como en local: no cambia nada de world/ ni del proxy.
# Variables (además de las del .env de la raíz):
#   DASHBOARD_PUBLIC_ORIGINS  deploy: https://<dominio público> del panel. Si
#                             está, el panel escucha en 0.0.0.0.
#   DASHBOARD_EXTRA_HOSTS     deploy: Host extra si el túnel lo reescribe.
#   GUARD_FILTRO              filtro del proxy (filtro:filtrar)
#   FILTRO_SOLO_HEURISTICAS   1 por defecto: con el modelo, el filtro corta el
#                             desafío OTP del comercio honesto (DEMO.md)
#   DEMO_PAUSA_AUTO_S         8 por defecto: plan gratuito de Gemini (0 si es pago)
#
# Gateway de producción (opcional; sin estas variables, todo igual que antes):
#   GATEWAY_CLAVES_ARCHIVO    JSON de integradores (proxy/claves.py). El
#                             oyente público se espera solo si el proxy lo
#                             abrió (/guard/state → gateway.publico: hace
#                             falta al menos una clave activa). Si el archivo
#                             no existe, se sigue sin gateway.
#   GATEWAY_COMERCIOS_ARCHIVO JSON con el registro de comercios (opcional).
#   GATEWAY_PORT              puerto del oyente público (8030).
#   GATEWAY_PUBLIC_URL        URL https pública del gateway. Si no es
#                             http://localhost… ni http://127.0.0.1…, el
#                             oyente público escucha en 0.0.0.0.
#   .temp-db/deploy.env       si existe, se carga después del .env y le gana:
#                             lo escribe deploy/tenki.sh en la VM con las rutas
#                             de los archivos subidos y la URL publicada.
#
# Logs en .logs/ (proxy, dashboard, world). Ctrl+C (o si un proceso muere)
# apaga todo.

set -euo pipefail

ESCENARIO="${1:-notebook_honesto}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ -f .env ]; then set -a; source .env; set +a; fi
# Lo que escribe tenki.sh en la VM va después: le gana al .env subido, que
# puede traer rutas de la máquina local.
if [ -f .temp-db/deploy.env ]; then set -a; source .temp-db/deploy.env; set +a; fi
export GUARD_FILTRO="${GUARD_FILTRO:-filtro:filtrar}"
export FILTRO_SOLO_HEURISTICAS="${FILTRO_SOLO_HEURISTICAS:-1}"
export DEMO_PAUSA_AUTO_S="${DEMO_PAUSA_AUTO_S:-8}"

PY="world/ap2/.venv/bin/python"
# Los paquetes editables (ap2, demo_world) también por PYTHONPATH: en macOS,
# con el repo en una carpeta sincronizada, los .pth del venv quedan ocultos y
# Python 3.13 los ignora.
export PYTHONPATH="$ROOT/world/ap2/code/sdk/python:$ROOT/world/ap2/code/samples/python/src${PYTHONPATH:+:$PYTHONPATH}"
LOG_DIR="$ROOT/.logs"
mkdir -p "$LOG_DIR"

# El gateway solo se espera si hay archivo de claves: sin claves el proxy no
# abre el oyente público y no cambia nada respecto de la demo.
GATEWAY=0
puertos=(8000 8001 8002 8003 8010 8011 8020)
if [ -n "${GATEWAY_CLAVES_ARCHIVO:-}" ]; then
  if [ -f "$GATEWAY_CLAVES_ARCHIVO" ]; then
    GATEWAY=1
    GATEWAY_PORT="${GATEWAY_PORT:-8030}"
    GATEWAY_PUBLIC_URL="${GATEWAY_PUBLIC_URL:-http://localhost:$GATEWAY_PORT}"
    # Exportadas: el proxy tiene que usar el mismo puerto y la misma URL que
    # se muestran acá (Agent Card, `a2a_url` de las compras, OpenAPI).
    export GATEWAY_PORT GATEWAY_PUBLIC_URL
    puertos+=("$GATEWAY_PORT")
    # Una URL pública que no es local implica un túnel o proxy reverso que
    # llega por la red: el oyente tiene que escuchar fuera de loopback (el
    # proxy solo lo acepta porque hay claves).
    case "$GATEWAY_PUBLIC_URL" in
      http://localhost*|http://127.0.0.1*) ;;
      *) export GATEWAY_HOST=0.0.0.0 ;;
    esac
  else
    echo "Aviso: GATEWAY_CLAVES_ARCHIVO=$GATEWAY_CLAVES_ARCHIVO no existe; sigo sin gateway." >&2
    # Sin el unset, el proxy la lee igual, no puede abrir el archivo y no
    # arranca: se caería la demo entera en vez de seguir sin gateway.
    unset GATEWAY_CLAVES_ARCHIVO
  fi
fi
if [ -n "${GATEWAY_COMERCIOS_ARCHIVO:-}" ] && [ ! -f "$GATEWAY_COMERCIOS_ARCHIVO" ]; then
  echo "Aviso: GATEWAY_COMERCIOS_ARCHIVO=$GATEWAY_COMERCIOS_ARCHIVO no existe; sigo con el comercio de siempre." >&2
  unset GATEWAY_COMERCIOS_ARCHIVO
fi

for puerto in "${puertos[@]}"; do
  if curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$puerto/"; then
    echo "El puerto $puerto ya está en uso: ¿quedó otra demo levantada?" >&2
    exit 1
  fi
done

# Cada proceso en su propio grupo, para apagar también a sus hijos.
set -m
pids=()
nombres=()
apagar() {
  trap - EXIT INT TERM
  echo ""
  echo "Apagando..."
  # ${pids[@]+...}: bash 3.2 (macOS) trata un array vacío como no definido.
  for pid in ${pids[@]+"${pids[@]}"}; do kill -TERM -- "-$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap apagar EXIT INT TERM

lanzar() {  # lanzar <nombre> <log> <comando...>
  local nombre="$1" log="$2"
  shift 2
  "$@" >"$log" 2>&1 &
  pids+=($!)
  nombres+=("$nombre")
}

esperar() {  # esperar <nombre> <url> <segundos>
  local nombre="$1" url="$2" limite="$3" i=0
  until curl -s -o /dev/null --max-time 2 "$url"; do
    i=$((i + 1))
    if [ "$i" -ge "$limite" ]; then
      echo "$nombre no respondió en ${limite}s ($url). Últimas líneas del log:" >&2
      tail -n 30 "$LOG_DIR"/*.log >&2 || true
      exit 1
    fi
    for j in "${!pids[@]}"; do
      if ! kill -0 "${pids[$j]}" 2>/dev/null; then
        echo "${nombres[$j]} terminó antes de tiempo. Revisá $LOG_DIR." >&2
        exit 1
      fi
    done
    sleep 1
  done
  echo "   listo: $nombre"
}

echo "-> world/ (escenario $ESCENARIO), con el comercio detrás del proxy"
# run_world.sh sincroniza el venv (la primera vez tarda) y vacía .logs/ de world/ap2.
lanzar "world" "$LOG_DIR/world.log" \
  env MERCHANT_AGENT_URL="http://localhost:8010/a2a/merchant_agent" bash world/run_world.sh "$ESCENARIO"
esperar "catálogo :8011" "http://127.0.0.1:8011/health" 600
esperar "Merchant :8001" "http://127.0.0.1:8001/" 120

echo "-> proxy del Guard :8010"
lanzar "proxy" "$LOG_DIR/proxy.log" \
  env PYTHONUTF8=1 PYTHONPATH=".:$PYTHONPATH" "$PY" -m proxy
esperar "proxy :8010" "http://127.0.0.1:8010/guard/state" 120
if [ "$GATEWAY" = 1 ]; then
  # El proxy abre el oyente público solo con al menos una clave activa: un
  # archivo con todas revocadas no es un error, es "sin gateway".
  if curl -s --max-time 2 "http://127.0.0.1:8010/guard/state" | grep -Eq '"publico": ?true'; then
    # /v1/openapi.json no pide clave: sirve para saber que el oyente público está.
    esperar "gateway :$GATEWAY_PORT" "http://127.0.0.1:$GATEWAY_PORT/v1/openapi.json" 30
  else
    echo "Aviso: el archivo de claves no tiene claves activas; sigo sin gateway." >&2
    GATEWAY=0
  fi
fi

echo "-> dashboard :8020"
if [ -n "${DASHBOARD_PUBLIC_ORIGINS:-}" ]; then
  export DASHBOARD_HOST=0.0.0.0 DASHBOARD_ALLOW_REMOTE=1
fi
lanzar "dashboard" "$LOG_DIR/dashboard.log" \
  env PYTHONUTF8=1 PYTHONPATH=".:$PYTHONPATH" "$PY" -m dashboard
esperar "dashboard :8020" "http://127.0.0.1:8020/" 60

esperar "Shopping Agent :8000" "http://127.0.0.1:8000/list-apps" 180

echo ""
echo "Demo levantada (escenario $ESCENARIO)."
PANEL="${DASHBOARD_PUBLIC_ORIGINS:-http://localhost:8020}"
PANEL="${PANEL%%,*}"
echo "  Comprar:        $PANEL/demo"
echo "  Panel:          $PANEL/"
echo "  Shopping Agent: http://localhost:8000/dev-ui (solo dentro de la máquina)"
if [ "$GATEWAY" = 1 ]; then
  echo "  Gateway:        $GATEWAY_PUBLIC_URL (docs: /v1/docs)"
fi
echo "  Logs:           $LOG_DIR"

# Si un proceso muere, se apaga todo (el trap de EXIT).
while true; do
  for j in "${!pids[@]}"; do
    if ! kill -0 "${pids[$j]}" 2>/dev/null; then
      echo "${nombres[$j]} terminó. Revisá $LOG_DIR." >&2
      exit 1
    fi
  done
  sleep 5
done
