#!/bin/bash
# Deploy de la demo en un Tenki Sandbox: una VM Linux con los 7 procesos
# adentro (deploy/start_all.sh) y solo el dashboard (:8020) publicado.
#
#   tenki login                       # una vez (o TENKI_API_KEY=tk_...)
#   bash deploy/tenki.sh              # comercio general (preset `general`)
#   bash deploy/tenki.sh B            # otro escenario (después se cambia desde /demo)
#
# Volver a correrlo con TENKI_SESSION=<id> sube el código de nuevo y reinicia
# la demo en la misma VM (la clave del Guard en .temp-db/ se conserva).
#
# Variables:
#   TENKI_SESSION        reusar una sesión existente (si no, crea una)
#   TENKI_SANDBOX_NAME   nombre de la sesión nueva (guard-demo)
#   TENKI_CPU            vCPU (2)
#   TENKI_MEMORY_MB      memoria en MB (4096)
#   PREVIEW_URL          URL pública del :8020, si `expose` no la imprime
#   DASHBOARD_EXTRA_HOSTS  Host extra si el túnel de Tenki lo reescribe
#   EXPOSE_ADK=1         publicar también la dev-ui de ADK (:8000); no hace
#                        falta, /demo la reemplaza
#   DASHBOARD_ACCESS_TOKEN  contraseña del panel publicado (HTTP Basic, usuario
#                        cualquiera). Del entorno o del .env; si no hay, el
#                        script genera una en .temp-db/tenki_dashboard_token
#                        (ignorado por git) y la reusa en cada redeploy. Viaja
#                        a la VM en .temp-db/deploy.env, no por la línea de
#                        comandos.
#
# Gateway de producción (opcional). Se leen del entorno o, si no están, del
# .env de la raíz; las rutas son de esta máquina:
#   GATEWAY_CLAVES_ARCHIVO     si apunta a un archivo, se sube a la VM
#                              (.temp-db/gateway_claves.json, chmod 600) y se
#                              publica también el gateway (:8030) por HTTPS
#   GATEWAY_COMERCIOS_ARCHIVO  registro de comercios; se sube solo junto con
#                              las claves (.temp-db/gateway_comercios.json)
#   GATEWAY_PREVIEW_URL        URL pública del :8030, si `expose` no la imprime
# Las rutas en la VM y la URL publicada van en .temp-db/deploy.env, que
# start_all.sh carga después del .env. Sin claves, ese archivo queda con las
# variables del gateway vacías (no se publica nada más que el dashboard). Los secretos de webhook (los que nombra
# `secreto_env`) viajan en el .env.
#
# Sube el código (archivos de git, sin lo ignorado ni lo borrado) y el .env de
# la raíz: la VM necesita GOOGLE_API_KEY y las demás claves que use el proxy.
# Del .env no sube SUPABASE_ACCESS_TOKEN (token personal con poder sobre toda
# la cuenta de Supabase: solo lo usa deploy/vercel/aplicar_migraciones.py) ni
# las VERCEL_* y TENKI_*, que la VM no usa.

set -euo pipefail

ESCENARIO="${1:-general}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

NOMBRE="${TENKI_SANDBOX_NAME:-guard-demo}"
CPU="${TENKI_CPU:-2}"
MEM="${TENKI_MEMORY_MB:-4096}"

if ! command -v tenki >/dev/null; then
  echo "Falta la CLI de Tenki (https://tenki.cloud/docs/sandbox/quickstart). Después: tenki login" >&2
  exit 1
fi
if [ ! -f .env ] || ! grep -Eq '^(GOOGLE_API_KEY=.|GOOGLE_GENAI_USE_VERTEXAI=(true|TRUE|True))' .env; then
  echo "Falta .env en la raíz con GOOGLE_API_KEY (o Vertex AI)." >&2
  exit 1
fi
case "$ESCENARIO" in *[!A-Za-z0-9_-]*) echo "Escenario inválido: $ESCENARIO" >&2; exit 1 ;; esac

de_env() {  # de_env <VARIABLE>: del entorno o, si no, del .env (sin source: no ejecuta nada)
  local nombre="$1" valor
  valor="${!nombre:-}"
  if [ -z "$valor" ]; then
    valor="$(grep -E "^(export[[:space:]]+)?$nombre=" .env | tail -n1 | cut -d= -f2- || true)"
    valor="${valor%\"}"; valor="${valor#\"}"; valor="${valor%\'}"; valor="${valor#\'}"
  fi
  printf '%s' "$valor"
}

# El gateway solo se publica si hay archivo de claves: sin claves el proxy no
# abre el oyente público.
CLAVES="$(de_env GATEWAY_CLAVES_ARCHIVO)"
COMERCIOS=""
if [ -n "$CLAVES" ]; then
  if [ ! -f "$CLAVES" ]; then
    echo "GATEWAY_CLAVES_ARCHIVO=$CLAVES no existe." >&2
    exit 1
  fi
  COMERCIOS="$(de_env GATEWAY_COMERCIOS_ARCHIVO)"
  if [ -n "$COMERCIOS" ] && [ ! -f "$COMERCIOS" ]; then
    echo "GATEWAY_COMERCIOS_ARCHIVO=$COMERCIOS no existe." >&2
    exit 1
  fi
fi

# El panel publicado pide contraseña (HTTP Basic): sin ella no arranca. Se
# reusa la generada antes para no cambiarla en cada redeploy.
CLAVE_PANEL="$(de_env DASHBOARD_ACCESS_TOKEN)"
CLAVE_PANEL_ARCHIVO=".temp-db/tenki_dashboard_token"
CLAVE_PANEL_ORIGEN="DASHBOARD_ACCESS_TOKEN"
if [ -z "$CLAVE_PANEL" ]; then
  CLAVE_PANEL_ORIGEN="$CLAVE_PANEL_ARCHIVO"
  if [ -s "$CLAVE_PANEL_ARCHIVO" ]; then
    CLAVE_PANEL="$(tr -d '\r\n' < "$CLAVE_PANEL_ARCHIVO")"
  else
    # od y no tr sobre /dev/urandom: lee 24 bytes y termina (sin SIGPIPE).
    CLAVE_PANEL="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
    (umask 077; mkdir -p .temp-db; printf '%s\n' "$CLAVE_PANEL" > "$CLAVE_PANEL_ARCHIVO")
    echo "-> Generé la contraseña del panel en $CLAVE_PANEL_ARCHIVO."
  fi
fi
# Va a un archivo que start_all.sh carga con `source`: nada que el shell
# interprete (ni comillas, ni $, ni espacios, ni ~ al principio).
case "$CLAVE_PANEL" in
  *[!A-Za-z0-9._+/=-]*) echo "$CLAVE_PANEL_ORIGEN: solo letras, números y . _ + / = -" >&2; exit 1 ;;
esac
if [ "${#CLAVE_PANEL}" -lt 16 ]; then
  echo "$CLAVE_PANEL_ORIGEN: al menos 16 caracteres." >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- 1. Sesión ---------------------------------------------------------------
if [ -z "${TENKI_SESSION:-}" ]; then
  echo "-> Creando la VM ($NOMBRE, ${CPU} vCPU, ${MEM} MB, sticky)..."
  salida="$(tenki sandbox create --name "$NOMBRE" --cpu "$CPU" --memory-mb "$MEM" --sticky 2>&1)" || {
    echo "$salida" >&2; exit 1; }
  echo "$salida"
  # La doc no fija el formato: primero un UUID; si no, la última palabra.
  TENKI_SESSION="$(printf '%s\n' "$salida" \
    | grep -Eo '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}' | tail -n1 || true)"
  if [ -z "$TENKI_SESSION" ]; then
    TENKI_SESSION="$(printf '%s\n' "$salida" | awk 'NF {ultima=$NF} END {print ultima}')"
  fi
  if [ -z "$TENKI_SESSION" ]; then
    echo "No pude leer el id de la sesión. Corré: TENKI_SESSION=<id> bash deploy/tenki.sh $ESCENARIO" >&2
    exit 1
  fi
fi
echo "   sesión: $TENKI_SESSION"

remoto() {  # remoto <comando> [timeout]
  tenki sandbox exec --session "$TENKI_SESSION" --timeout "${2:-10m}" -c "$1"
}

# --- 2. Código y .env --------------------------------------------------------
echo "-> Subiendo el código..."
# Sin los versionados que se borraron del working tree (ls-files -c los
# lista igual y tar falla con "Cannot stat").
git ls-files -co --exclude-standard -z | while IFS= read -r -d '' f; do
  if [ -e "$f" ] || [ -L "$f" ]; then printf '%s\0' "$f"; fi
done > "$TMP/archivos"
# COPYFILE_DISABLE: sin esto, el tar de macOS agrega archivos ._* (metadatos de Apple).
COPYFILE_DISABLE=1 tar --null -T "$TMP/archivos" -czf "$TMP/repo.tgz"
# En base64: la doc de `write` no aclara si acepta binarios.
base64 < "$TMP/repo.tgz" | tr -d '\n' > "$TMP/repo.b64"
tenki sandbox write --session "$TENKI_SESSION" --path /home/tenki/repo.b64 --data-file "$TMP/repo.b64"
# grep -v sale con 1 si no quedó ninguna línea (no pasa: está GOOGLE_API_KEY);
# con 2 (error), corta.
grep -Ev '^[[:space:]]*(export[[:space:]]+)?(SUPABASE_ACCESS_TOKEN|VERCEL_[A-Za-z0-9_]*|TENKI_[A-Za-z0-9_]*)[[:space:]]*=' \
  .env > "$TMP/env" || [ $? -eq 1 ]
tenki sandbox write --session "$TENKI_SESSION" --path /home/tenki/guard-demo.env --data-file "$TMP/env"
remoto 'set -e; cd /home/tenki
  pkill -f "deploy/[s]tart_all.sh" || true
  mkdir -p app; base64 -d repo.b64 | tar -xzf - -C app; rm -f repo.b64
  mv guard-demo.env app/.env; chmod 600 app/.env'

# Después de extraer el código: .temp-db/ está ignorado por git, así que el tar
# no lo pisa, pero el orden deja claro que estos archivos ganan.
if [ -n "$CLAVES" ]; then
  echo "-> Subiendo la configuración del gateway..."
  tenki sandbox write --session "$TENKI_SESSION" --path /home/tenki/gateway_claves.json --data-file "$CLAVES"
  if [ -n "$COMERCIOS" ]; then
    tenki sandbox write --session "$TENKI_SESSION" --path /home/tenki/gateway_comercios.json --data-file "$COMERCIOS"
  fi
  remoto 'set -e; cd /home/tenki; umask 077
    mkdir -p app/.temp-db
    mv gateway_claves.json app/.temp-db/gateway_claves.json
    chmod 600 app/.temp-db/gateway_claves.json
    if [ -f gateway_comercios.json ]; then
      mv gateway_comercios.json app/.temp-db/gateway_comercios.json
      chmod 600 app/.temp-db/gateway_comercios.json
    fi'
fi

# --- 3. Dependencias (idempotente) ------------------------------------------
echo "-> Instalando dependencias (la primera vez tarda unos minutos)..."
remoto 'set -e
  SUDO=""; [ "$(id -u)" = 0 ] || SUDO=sudo
  if ! command -v git >/dev/null || ! command -v curl >/dev/null; then
    $SUDO apt-get update -qq
    $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git curl ca-certificates
  fi
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
  # Python 3.13: con 3.14 se rompe el rechazo de JSON profundo del proxy.
  cd /home/tenki/app/world/ap2
  uv python install 3.13
  UV_PYTHON=3.13 uv sync --package ap2-samples' 30m

# --- 4. URL pública ----------------------------------------------------------
url_de() {  # url_de <puerto>: la preview URL que imprime expose (o ports)
  local puerto="$1" salida url
  salida="$(tenki sandbox expose --session "$TENKI_SESSION" --port "$puerto" 2>&1 || true)"
  url="$(printf '%s\n' "$salida" | grep -Eo 'https://[^[:space:]"'"'"',]+' | head -n1 || true)"
  if [ -z "$url" ]; then
    url="$(tenki sandbox preview-url list --session "$TENKI_SESSION" 2>&1 | grep -E "(^|[^0-9])$puerto([^0-9]|$)" \
      | grep -Eo 'https://[^[:space:]"'"'"',]+' | head -n1 || true)"
  fi
  [ -n "$url" ] || printf '%s\n' "$salida" >&2
  printf '%s' "$url"
}

echo "-> Publicando el dashboard (:8020)..."
URL="${PREVIEW_URL:-$(url_de 8020)}"
if [ -z "$URL" ]; then
  echo "No encontré la URL pública. Mirala con: tenki sandbox preview-url list --session $TENKI_SESSION" >&2
  echo "y volvé a correr: PREVIEW_URL=https://... TENKI_SESSION=$TENKI_SESSION bash deploy/tenki.sh $ESCENARIO" >&2
  exit 1
fi
# El origen es esquema + host (sin ruta): es lo que manda el navegador en Origin.
ORIGEN="$(printf '%s' "$URL" | sed -E 's#^(https?://[^/]+).*#\1#')"
case "$ORIGEN" in *[!A-Za-z0-9.:/_-]*) echo "URL pública rara: $ORIGEN" >&2; exit 1 ;; esac
EXTRA="${DASHBOARD_EXTRA_HOSTS:-}"
case "$EXTRA" in *[!A-Za-z0-9.:,_-]*) echo "DASHBOARD_EXTRA_HOSTS inválido" >&2; exit 1 ;; esac

# El gateway va por HTTPS (lo termina el túnel de Tenki): las claves de los
# integradores no viajan en claro.
GW_ORIGEN=""
if [ -n "$CLAVES" ]; then
  echo "-> Publicando el gateway (:8030)..."
  GW_URL="${GATEWAY_PREVIEW_URL:-$(url_de 8030)}"
  if [ -z "$GW_URL" ]; then
    echo "No encontré la URL pública del gateway. Mirala con: tenki sandbox preview-url list --session $TENKI_SESSION" >&2
    echo "y volvé a correr: GATEWAY_PREVIEW_URL=https://... TENKI_SESSION=$TENKI_SESSION bash deploy/tenki.sh $ESCENARIO" >&2
    exit 1
  fi
  GW_ORIGEN="$(printf '%s' "$GW_URL" | sed -E 's#^(https?://[^/]+).*#\1#')"
  case "$GW_ORIGEN" in *[!A-Za-z0-9.:/_-]*) echo "URL pública rara: $GW_ORIGEN" >&2; exit 1 ;; esac
  case "$GW_ORIGEN" in https://?*) ;; *) echo "El gateway tiene que publicarse por https: $GW_ORIGEN" >&2; exit 1 ;; esac
fi

# Lo que lee start_all.sh después del .env (rutas en la VM, no las locales).
# Sin gateway, las variables vacías: pisan las rutas de la máquina local que
# pueda traer el .env subido (el proxy no podría abrirlas y no arrancaría) y
# un deploy anterior no deja el gateway prendido. Se arma acá y se sube como
# archivo: la contraseña del panel no va en la línea de comandos.
{
  if [ -n "$CLAVES" ]; then
    printf '%s\n' 'GATEWAY_CLAVES_ARCHIVO=.temp-db/gateway_claves.json' 'GATEWAY_PORT=8030' \
      "GATEWAY_PUBLIC_URL=$GW_ORIGEN"
    if [ -n "$COMERCIOS" ]; then
      printf '%s\n' 'GATEWAY_COMERCIOS_ARCHIVO=.temp-db/gateway_comercios.json'
    else
      printf '%s\n' 'GATEWAY_COMERCIOS_ARCHIVO='
    fi
  else
    printf '%s\n' 'GATEWAY_CLAVES_ARCHIVO=' 'GATEWAY_COMERCIOS_ARCHIVO='
  fi
  printf '%s\n' "DASHBOARD_ACCESS_TOKEN=$CLAVE_PANEL"
} > "$TMP/deploy.env"
tenki sandbox write --session "$TENKI_SESSION" --path /home/tenki/deploy.env --data-file "$TMP/deploy.env"

URL_ADK=""
if [ "${EXPOSE_ADK:-0}" = "1" ]; then
  URL_ADK="$(url_de 8000)"
fi

# --- 5. Arranque -------------------------------------------------------------
echo "-> Arrancando la demo (escenario $ESCENARIO)..."
remoto "set -e; cd /home/tenki/app
  export PATH=\"\$HOME/.local/bin:\$PATH\" UV_PYTHON=3.13
  export DASHBOARD_PUBLIC_ORIGINS='$ORIGEN' DASHBOARD_EXTRA_HOSTS='$EXTRA'
  mkdir -p .logs .temp-db
  (umask 077; mv /home/tenki/deploy.env .temp-db/deploy.env; chmod 600 .temp-db/deploy.env)
  # El start_all anterior tarda unos segundos en apagar todo: esperar a que
  # termine y a que se liberen los puertos (si no, el nuevo los ve ocupados).
  for i in \$(seq 1 60); do
    pgrep -f '^bash deploy/[s]tart_all.sh' >/dev/null || ss -tln | grep -qE ':(8000|8001|8002|8003|8010|8011|8020|8030) ' || break
    sleep 1
  done
  nohup setsid bash deploy/start_all.sh '$ESCENARIO' > .logs/start_all.log 2>&1 < /dev/null &
  echo lanzado"

echo "-> Esperando a que respondan el panel y el Shopping Agent..."
NL=$'\n'
CR=$'\r'
fallas=0
for i in $(seq 1 90); do
  # Sin -f: el panel publicado contesta 401 sin la contraseña, y eso alcanza
  # para saber que está levantado.
  if remoto 'curl -s -o /dev/null --max-time 2 http://127.0.0.1:8020/ && curl -s -o /dev/null --max-time 2 http://127.0.0.1:8000/list-apps' 1m >/dev/null 2>&1; then
    echo ""
    echo "Demo en la nube (escenario $ESCENARIO):"
    echo "  Comprar:        $ORIGEN/demo"
    echo "  Panel:          $ORIGEN/"
    echo "  Contraseña:     la de $CLAVE_PANEL_ORIGEN (el navegador la pide; usuario: cualquiera, p. ej. kaiai)"
    [ -n "$URL_ADK" ] && echo "  dev-ui de ADK:  ${URL_ADK%/}/dev-ui"
    [ -n "$GW_ORIGEN" ] && echo "  Gateway:        $GW_ORIGEN/v1/docs"
    echo "  Sesión Tenki:   $TENKI_SESSION"
    echo "  Logs:           tenki sandbox exec --session $TENKI_SESSION -c 'tail -n 50 ~/app/.logs/*.log'"
    echo "  Pausar:         tenki sandbox pause --session $TENKI_SESSION   (al reanudar, volvé a correr este script)"
    exit 0
  fi
  # ¿Sigue vivo start_all.sh? El comando remoto contesta siempre con una
  # marca: si la CLI falla (red, rate-limit), no hay marca y se reintenta en
  # vez de tomarlo como "start_all.sh terminó".
  estado=""
  if salida="$(remoto 'if pgrep -f "deploy/[s]tart_all.sh" >/dev/null; then echo START_ALL_VIVO; else echo START_ALL_TERMINADO; fi' 1m 2>"$TMP/tenki_error")"; then
    salida="${salida//$CR/}"
    case "$NL$salida$NL" in
      *"${NL}START_ALL_TERMINADO${NL}"*) estado=terminado ;;
      *"${NL}START_ALL_VIVO${NL}"*) estado=vivo ;;
    esac
  fi
  case "$estado" in
    terminado)
      echo "start_all.sh terminó con error. Log:" >&2
      remoto 'tail -n 60 /home/tenki/app/.logs/start_all.log' 1m >&2 || true
      exit 1
      ;;
    vivo) fallas=0 ;;
    *)
      fallas=$((fallas + 1))
      if [ "$fallas" -ge 6 ]; then
        echo "La CLI de Tenki falló $fallas veces seguidas al consultar la VM. Último error:" >&2
        cat "$TMP/tenki_error" >&2 || true
        exit 1
      fi
      echo "   (la CLI de Tenki no contestó; reintento)" >&2
      ;;
  esac
  sleep 10
done
echo "La demo no respondió en 15 minutos. Log:" >&2
remoto 'tail -n 60 /home/tenki/app/.logs/start_all.log' 1m >&2 || true
exit 1
