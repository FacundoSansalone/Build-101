#!/bin/bash
# Entrypoint del contenedor de Railway (deploy/railway/Dockerfile): la demo
# entera con deploy/start_all.sh y, como única puerta pública en $PORT, el
# enrutador de deploy/vercel/tunel_router.py:
#
#   /<TUNEL_PREFIJO>/a2a/*  → Merchant Agent :8001  (lo usa el motor de Vercel)
#   /<TUNEL_PREFIJO>/…      → catálogo público :8011
#   /salud                  → 200 si el comercio, el catálogo y el panel
#                             contestan (el healthcheck de Railway)
#   el resto                → el panel :8020 (/demo), con contraseña (HTTP Basic)
#
# La dev-ui de ADK (:8000), el proxy (:8010), los otros agentes y el panel
# escuchan solo en 127.0.0.1: nada de eso se publica. Si start_all.sh o el
# enrutador terminan, el contenedor termina con error y Railway lo reinicia
# (restartPolicyType ON_FAILURE en railway.json).
#
#   bash deploy/railway/arrancar.sh                 # lo corre el contenedor
#   bash deploy/railway/arrancar.sh --solo-validar  # solo revisa las variables
#
# Obligatorias (variables del servicio; nunca se imprimen):
#   GOOGLE_API_KEY            Gemini: los agentes del sample y la intención
#   DASHBOARD_ACCESS_TOKEN    contraseña del panel (16 caracteres o más)
#   TUNEL_PREFIJO             prefijo secreto de ruta: el de
#                             .temp-db/vercel/TUNEL_PREFIJO.txt, así el motor
#                             de Vercel usa el mismo
#   DASHBOARD_PUBLIC_ORIGINS  https://<dominio de Railway> (sin ruta)
# Opcionales:
#   PORT                      el que da Railway (8080 si no hay); no puede ser
#                             uno de los de adentro
#   ESCENARIO                 comportamiento del comercio al arrancar (general)
#   DASHBOARD_EXTRA_HOSTS     Host extra, si el borde de Railway lo cambiara
#   ENRUTADOR_HOST            interfaz del enrutador (0.0.0.0); solo para
#                             probar en tu máquina
#   y las de deploy/README.md ("Variables"): DEMO_PAUSA_AUTO_S,
#   FILTRO_SOLO_HEURISTICAS, OPENAI_API_KEY o TYPESAFE_API_KEY, SUPABASE_*…
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$RAIZ"

SOLO_VALIDAR=0
case "${1:-}" in
  "") ;;
  --solo-validar) SOLO_VALIDAR=1 ;;
  *) echo "Uso: bash deploy/railway/arrancar.sh [--solo-validar]" >&2; exit 2 ;;
esac

# Los puertos de adentro (start_all.sh): el enrutador no puede usar ninguno.
PUERTOS_INTERNOS="8000 8001 8002 8003 8010 8011 8020 8030"

recortar() {  # recortar <texto>: sin espacios ni saltos al principio y al final
  local t="$1"
  t="${t#"${t%%[![:space:]]*}"}"
  t="${t%"${t##*[![:space:]]}"}"
  printf '%s' "$t"
}

# --- 1. Variables: todos los problemas juntos, sin mostrar ningún valor ------
errores=()
if [ -z "$(recortar "${GOOGLE_API_KEY:-}")" ]; then
  errores+=("GOOGLE_API_KEY: falta (la clave de Gemini de los agentes).")
fi

CLAVE_PANEL="$(recortar "${DASHBOARD_ACCESS_TOKEN:-}")"
if [ -z "$CLAVE_PANEL" ]; then
  errores+=("DASHBOARD_ACCESS_TOKEN: falta (la contraseña del panel, 16 caracteres o más).")
elif [ "${#CLAVE_PANEL}" -lt 16 ]; then
  errores+=("DASHBOARD_ACCESS_TOKEN: es demasiado corta (al menos 16 caracteres).")
fi

PREFIJO="$(recortar "${TUNEL_PREFIJO:-}")"
if [ -z "$PREFIJO" ]; then
  errores+=("TUNEL_PREFIJO: falta (el de .temp-db/vercel/TUNEL_PREFIJO.txt, el mismo que usa el motor de Vercel).")
elif ! [[ "$PREFIJO" =~ ^[A-Za-z0-9_-]{8,128}$ ]]; then
  errores+=("TUNEL_PREFIJO: tiene que tener de 8 a 128 caracteres de [A-Za-z0-9_-].")
fi

ORIGEN=""
ORIGENES="$(recortar "${DASHBOARD_PUBLIC_ORIGINS:-}")"
origen_malo=0
IFS=',' read -r -a lista <<< "$ORIGENES"
for o in ${lista[@]+"${lista[@]}"}; do
  o="$(recortar "$o")"
  o="${o%/}"
  [ -n "$o" ] || continue
  # Solo https: la contraseña del panel viaja en cada pedido.
  if [[ "$o" =~ ^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?$ ]]; then
    [ -n "$ORIGEN" ] || ORIGEN="$o"
  else
    origen_malo=1
  fi
done
if [ "$origen_malo" = 1 ]; then
  errores+=("DASHBOARD_PUBLIC_ORIGINS: cada origen tiene que ser https://dominio[:puerto], sin ruta.")
elif [ -z "$ORIGEN" ]; then
  errores+=("DASHBOARD_PUBLIC_ORIGINS: falta (https://<el dominio de Railway>, sin ruta).")
fi

PUERTO="$(recortar "${PORT:-8080}")"
if ! [[ "$PUERTO" =~ ^[0-9]{1,5}$ ]] || [ "$((10#$PUERTO))" -lt 1 ] || [ "$((10#$PUERTO))" -gt 65535 ]; then
  errores+=("PORT: tiene que ser un número de puerto (Railway lo da; si no, 8080).")
else
  PUERTO="$((10#$PUERTO))"
  for p in $PUERTOS_INTERNOS; do
    if [ "$PUERTO" = "$p" ]; then
      errores+=("PORT: el $PUERTO lo usa un proceso de adentro ($PUERTOS_INTERNOS); usá otro, p. ej. 8080.")
    fi
  done
fi

if ! [[ "${DASHBOARD_EXTRA_HOSTS:-}" =~ ^[A-Za-z0-9.:,_-]*$ ]]; then
  errores+=("DASHBOARD_EXTRA_HOSTS: solo hosts separados por coma (letras, números y . : _ -).")
fi
ESCENARIO="$(recortar "${ESCENARIO:-general}")"
if ! [[ "$ESCENARIO" =~ ^[A-Za-z0-9_-]{1,64}$ ]]; then
  errores+=("ESCENARIO: solo letras, números, _ y - (por ejemplo general).")
fi

if [ "${#errores[@]}" -gt 0 ]; then
  echo "KaiAI no arranca: revisá estas variables del servicio (Railway → Variables)." >&2
  for e in "${errores[@]}"; do echo "  - $e" >&2; done
  exit 2
fi
if [ "$SOLO_VALIDAR" = 1 ]; then
  echo "Variables en orden (enrutador en :$PUERTO, panel en $ORIGEN/demo)."
  exit 0
fi

PY="world/ap2/.venv/bin/python"
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) PY="world/ap2/.venv/Scripts/python.exe" ;; esac
if [ ! -x "$PY" ]; then
  echo "Falta $PY: el venv de world/ap2 lo arma el Dockerfile (uv sync --package ap2-samples)." >&2
  exit 1
fi

export DASHBOARD_ACCESS_TOKEN="$CLAVE_PANEL" TUNEL_PREFIJO="$PREFIJO" PORT="$PUERTO"
# El panel solo detrás del enrutador (start_all.sh lo dejaría en 0.0.0.0).
export DASHBOARD_SOLO_LOOPBACK=1
# El gateway (:8030) no se abre acá: el motor está en Vercel.
unset GATEWAY_CLAVES_ARCHIVO GATEWAY_COMERCIOS_ARCHIVO

# --- 2. Procesos: si uno termina, termina el contenedor ----------------------
pids=()
nombres=()
terminar() {
  trap - EXIT INT TERM
  for pid in ${pids[@]+"${pids[@]}"}; do kill -TERM "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap terminar EXIT
trap 'terminar; exit 143' TERM
trap 'terminar; exit 130' INT

esperar() {  # esperar <nombre> <url> <segundos>
  local nombre="$1" url="$2" limite="$3" i=0
  until curl -s -o /dev/null --max-time 2 "$url"; do
    for j in "${!pids[@]}"; do
      if ! kill -0 "${pids[$j]}" 2>/dev/null; then
        echo "${nombres[$j]} terminó antes de que respondiera $nombre." >&2
        exit 1
      fi
    done
    i=$((i + 2))
    if [ "$i" -ge "$limite" ]; then
      echo "$nombre no respondió en ${limite}s." >&2
      exit 1
    fi
    sleep 2
  done
}

echo "-> La demo: deploy/start_all.sh $ESCENARIO"
bash deploy/start_all.sh "$ESCENARIO" &
pids+=($!)
nombres+=("start_all.sh")
# start_all.sh espera al resto; acá, lo que tiene que estar para abrir la
# puerta: el panel y el Shopping Agent (ADK, el que más tarda).
esperar "el panel :8020" "http://127.0.0.1:8020/" 900
esperar "el Shopping Agent :8000" "http://127.0.0.1:8000/list-apps" 900

# Publicado, el panel pide la contraseña: si no, no se abre la puerta.
codigo="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8020/ || true)"
if [ "$codigo" != 401 ]; then
  echo "El panel contestó $codigo sin contraseña (se esperaba 401): no lo publico." >&2
  exit 1
fi

echo "-> Enrutador en :$PUERTO"
"$PY" deploy/vercel/tunel_router.py --host "${ENRUTADOR_HOST:-0.0.0.0}" --puerto "$PUERTO" \
  --resto http://127.0.0.1:8020 --salud &
pids+=($!)
nombres+=("el enrutador")
esperar "el enrutador :$PUERTO" "http://127.0.0.1:$PUERTO/salud" 60

echo ""
echo "KaiAI en Railway (comercio: $ESCENARIO)."
echo "  Comprar:  $ORIGEN/demo   (usuario cualquiera; la contraseña es DASHBOARD_ACCESS_TOKEN)"
echo "  Panel:    $ORIGEN/"
echo "  Motor:    el comercio está en $ORIGEN/<TUNEL_PREFIJO>/a2a/merchant_agent"
echo "            (bash deploy/railway/apuntar_vercel.sh, desde tu máquina)"
echo "  Salud:    $ORIGEN/salud"

# Si un proceso termina, se apaga todo (el trap de EXIT) y el contenedor sale
# con error: Railway lo reinicia.
codigo=0
wait -n "${pids[@]}" || codigo=$?
for j in "${!pids[@]}"; do
  if ! kill -0 "${pids[$j]}" 2>/dev/null; then
    echo "${nombres[$j]} terminó (código $codigo): apago todo para que Railway reinicie el contenedor." >&2
  fi
done
exit 1
