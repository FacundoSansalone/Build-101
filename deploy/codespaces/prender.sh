#!/bin/bash
# postStartCommand del Codespace (.devcontainer/devcontainer.json): corre cada
# vez que el Codespace prende. Levanta la demo entera con
# deploy/railway/arrancar.sh (el mismo arranque del contenedor de Railway:
# start_all.sh y el enrutador como única puerta) en segundo plano, y deja
# público el puerto 8080:
#
#   https://<codespace>-8080.app.github.dev/demo               → /demo (con contraseña)
#   https://<codespace>-8080.app.github.dev/<TUNEL_PREFIJO>/…   → el comercio y el
#                                                                 catálogo (el motor de Vercel)
#
# La salida va a .logs/codespaces.log y no a la terminal: lo que se escribe en
# una terminal cuenta como actividad, y el Codespace nunca se apagaría solo.
#
#   bash deploy/codespaces/prender.sh                 # lo corre el Codespace al prender; a
#                                                     # mano, si ya anda, muestra las URLs
#   bash deploy/codespaces/prender.sh --solo-validar  # solo revisa los secretos
#
# Los secretos (GOOGLE_API_KEY, DASHBOARD_ACCESS_TOKEN, TUNEL_PREFIJO) son los
# de Codespaces del usuario (deploy/codespaces/cargar_secretos.sh); nunca se
# imprimen. El origen público sale de CODESPACE_NAME y
# GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN, que pone GitHub.
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$RAIZ"
export PATH="$HOME/.local/bin:$PATH"

MODO=prender
case "${1:-}" in
  "") ;;
  --solo-validar) MODO=validar ;;
  --publicar) MODO=publicar ;;  # interno: lo lanza el modo prender en segundo plano
  *) echo "Uso: bash deploy/codespaces/prender.sh [--solo-validar]" >&2; exit 2 ;;
esac

PUERTO=8080
NOMBRE="${CODESPACE_NAME:-}"
DOMINIO_GH="${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
if ! [[ "$NOMBRE" =~ ^[A-Za-z0-9-]{1,100}$ ]]; then
  echo "Esto es para un Codespace de GitHub (falta CODESPACE_NAME). En tu máquina: bash deploy/start_all.sh" >&2
  exit 2
fi
if ! [[ "$DOMINIO_GH" =~ ^[A-Za-z0-9.-]{1,200}$ ]]; then
  echo "GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN no es un dominio." >&2
  exit 2
fi
ORIGEN="https://$NOMBRE-$PUERTO.$DOMINIO_GH"

# Lo que arrancar.sh necesita y en un Codespace no es un secreto. El
# enrutador escucha solo en 127.0.0.1: el reenvío de puertos de GitHub llega
# por ahí.
export KAIAI_PLATAFORMA=Codespace PORT="$PUERTO" ENRUTADOR_HOST=127.0.0.1 \
  DASHBOARD_PUBLIC_ORIGINS="$ORIGEN"

LOG=.logs/codespaces.log
PID=.temp-db/codespaces.pid

urls() {
  echo "  Comprar: $ORIGEN/demo   (usuario cualquiera; la contraseña es tu secreto DASHBOARD_ACCESS_TOKEN)"
  echo "  Panel:   $ORIGEN/"
  echo "  Motor:   bash deploy/codespaces/apuntar_vercel.sh $NOMBRE   (desde tu máquina, una vez por Codespace)"
}

andando() {  # ¿sigue vivo el arrancar.sh de la última vez?
  local pid
  pid="$(cat "$PID" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] && [ -r "/proc/$pid/cmdline" ] || return 1
  tr '\0' ' ' 2>/dev/null < "/proc/$pid/cmdline" | grep -q "deploy/railway/arrancar.sh"
}

if [ "$MODO" = publicar ]; then
  # Espera a que la demo conteste y hace público el 8080 (GitHub lo deja
  # privado: solo para vos, con tu sesión). Corre en segundo plano.
  i=0
  until [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$PUERTO/salud" || true)" = 200 ]; do
    if ! andando; then
      echo "La demo no arrancó: mirá los mensajes de arriba en este log." >&2
      exit 1
    fi
    i=$((i + 3))
    if [ "$i" -ge 900 ]; then
      echo "La demo no contestó en 900 s: mirá los mensajes de arriba en este log." >&2
      exit 1
    fi
    sleep 3
  done
  publico=0
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if gh codespace ports visibility "$PUERTO:public" -c "$NOMBRE" >/dev/null 2>&1; then
      publico=1
      break
    fi
    sleep 6
  done
  echo ""
  if [ "$publico" = 1 ]; then
    echo "Lista: el puerto $PUERTO es público."
  else
    echo "La demo está andando, pero no pude hacer público el puerto $PUERTO. Hacelo vos:" >&2
    echo "  pestaña Puertos → clic derecho en el $PUERTO → Port Visibility → Public, o desde tu máquina" >&2
    echo "  bash deploy/codespaces/apuntar_vercel.sh --solo-publicar $NOMBRE" >&2
  fi
  urls
  exit 0
fi

# Los secretos antes de levantar nada: si falta uno, el aviso se ve en el
# registro de creación del Codespace y en la terminal.
if ! bash deploy/railway/arrancar.sh --solo-validar; then
  echo "" >&2
  echo "Cargá los secretos de Codespaces de este repo (desde tu máquina:" >&2
  echo "bash deploy/codespaces/cargar_secretos.sh, o en github.com/settings/codespaces)" >&2
  echo "y reiniciá el Codespace (Stop y Start): GitHub los pasa al prender." >&2
  exit 1
fi
[ "$MODO" = validar ] && exit 0

mkdir -p .logs .temp-db
# De a uno: el postStartCommand y alguien que lo corre a mano al mismo tiempo.
# Los procesos de la demo no heredan el candado (9>&-).
if command -v flock >/dev/null 2>&1; then
  exec 9> .temp-db/codespaces.lock
  flock -w 60 9 || true
fi
if andando; then
  echo "La demo ya está prendida (log en $LOG)."
  urls
  exit 0
fi

: > "$LOG"
setsid nohup bash deploy/railway/arrancar.sh >> "$LOG" 2>&1 < /dev/null 9>&- &
echo "$!" > "$PID"
setsid nohup bash deploy/codespaces/prender.sh --publicar >> "$LOG" 2>&1 < /dev/null 9>&- &

echo "Prendiendo la demo (uno o dos minutos). Seguila con: tail -f $LOG"
urls
