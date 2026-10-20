#!/usr/bin/env bash
# La simulación detrás del motor en Vercel, con un solo comando:
#
#   bash world/run_world.sh general          # en otra terminal: comercio :8001 y catálogo :8011
#   bash deploy/vercel/tunel_demo.sh         # enrutador :8090 + túnel
#
# Túnel (TUNEL=auto por defecto):
#   - cloudflared rápido (trycloudflare.com, sin cuenta). Necesita salida al
#     puerto 7844 de Cloudflare.
#   - pinggy (SSH por el 443, sin cuenta): para redes que solo dejan salir por
#     el 443. El túnel gratis dura 60 minutos: el script lo vuelve a abrir solo.
#   auto usa cloudflared si el 7844 está abierto y, si no, pinggy.
#
# Los dos cambian de URL en cada arranque. El script la lee, reescribe
# GATEWAY_COMERCIOS_JSON con el dominio nuevo (mismo prefijo secreto de
# .temp-db/vercel/TUNEL_PREFIJO.txt), la carga en Vercel y redespliega
# producción. Ctrl+C corta túnel y enrutador.
#
# Requiere: `npx vercel login` y `npx vercel link` hechos, y los secretos de
# deploy/vercel/generar_secretos.py en .temp-db/vercel/. No imprime secretos.
set -euo pipefail

RAIZ="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$RAIZ"
SECRETOS="${SECRETOS:-.temp-db/vercel}"
PUERTO="${TUNEL_PUERTO:-8090}"
TUNEL="${TUNEL:-auto}"
if [ -z "${PY:-}" ]; then
  if [ -x world/ap2/.venv/Scripts/python ]; then PY=world/ap2/.venv/Scripts/python; else PY=world/ap2/.venv/bin/python; fi
fi
[ -s "$SECRETOS/TUNEL_PREFIJO.txt" ] || { echo "Falta $SECRETOS/TUNEL_PREFIJO.txt (generar_secretos.py --dominio …)."; exit 1; }
for p in 8001 8011; do
  curl -s -o /dev/null -m 5 "http://127.0.0.1:$p/" || { echo "La simulación no responde en :$p (bash world/run_world.sh general)."; exit 1; }
done

abierto() { timeout 6 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null; }
if [ "$TUNEL" = auto ]; then
  if abierto region1.v2.argotunnel.com 7844; then TUNEL=cloudflared; else TUNEL=pinggy; fi
fi
echo "túnel: $TUNEL"

mkdir -p .logs
PIDS=()
cortar() {  # túnel y enrutador, una sola vez
  trap - EXIT INT TERM
  # ${PIDS[@]+...}: bash 3.2 (macOS) trata un array vacío como no definido.
  kill ${PIDS[@]+"${PIDS[@]}"} 2>/dev/null || true
}
trap cortar EXIT
# Ctrl+C o TERM: cortar y salir. Sin el exit, el bucle de pinggy reabría el
# túnel y volvía a apuntar Vercel con el enrutador ya muerto.
trap 'cortar; exit 130' INT
trap 'cortar; exit 143' TERM
TUNEL_PREFIJO="$(tr -d '\r\n' < "$SECRETOS/TUNEL_PREFIJO.txt")" PYTHONUTF8=1 \
  "$PY" deploy/vercel/tunel_router.py --puerto "$PUERTO" > .logs/tunel_router.log 2>&1 &
PIDS+=($!)

# Espera en el log $1 una URL que cumpla $2 (regex) y la deja en DOMINIO.
# Nunca la del panel de pinggy (dashboard.pinggy.io) ni la API con la que
# cloudflared pide el túnel (https://api.trycloudflare.com, sale en el log si
# falla): esas no son el túnel.
esperar_url() {
  DOMINIO=""
  for _ in $(seq 1 60); do
    DOMINIO="$(grep -oE "$2" "$1" | grep -vE 'dashboard|//api\.' | head -1 || true)"
    [ -n "$DOMINIO" ] && return 0
    sleep 1
  done
  return 1
}

# Reescribe GATEWAY_COMERCIOS_JSON con $DOMINIO, lo carga en Vercel y
# redespliega. Si algo falla, muestra el error y devuelve 1 (quien llama
# corta: no hay que decir "Listo" con Vercel apuntando a otro lado). Se llama
# con `||`, así que set -e no corre adentro: cada paso se chequea a mano.
apuntar_vercel() {
  local salida url
  DOMINIO="$DOMINIO" SECRETOS="$SECRETOS" "$PY" - <<'EOF' || { echo "No pude reescribir GATEWAY_COMERCIOS_JSON." >&2; return 1; }
import json, os, pathlib
base = pathlib.Path(os.environ["SECRETOS"])
prefijo = (base / "TUNEL_PREFIJO.txt").read_text(encoding="utf-8").strip()
raiz = f"{os.environ['DOMINIO']}/{prefijo}"
registro = {"por_defecto": "merchant_1", "comercios": [{
    "id": "merchant_1", "nombre": "Simulación (comercio general detrás del túnel)",
    "a2a_url": f"{raiz}/a2a/merchant_agent", "catalogo_url": raiz,
    "merchant_ids": ["merchant_1"], "respaldo_fixture": False,
}]}
(base / "GATEWAY_COMERCIOS_JSON.json").write_text(json.dumps(registro, separators=(",", ":")), encoding="utf-8")
EOF
  # La salida de la CLI no trae el valor: se muestra solo si falla.
  if ! salida="$(tr -d '\r\n' < "$SECRETOS/GATEWAY_COMERCIOS_JSON.json" \
      | npx vercel env add GATEWAY_COMERCIOS_JSON production --force 2>&1)"; then
    printf '%s\n' "$salida" | tail -n 20 >&2
    echo "No pude cargar GATEWAY_COMERCIOS_JSON en Vercel (¿npx vercel login y npx vercel link?)." >&2
    return 1
  fi
  echo "$(date +%H:%M:%S) GATEWAY_COMERCIOS_JSON → $DOMINIO; redespliego producción…"
  # `vercel deploy` espera a que el deploy quede listo y sale con error si el
  # build falla; si sale bien, imprime la URL del deploy.
  if ! salida="$(npx vercel deploy --prod --yes 2>&1)"; then
    printf '%s\n' "$salida" | tail -n 20 >&2
    echo "El redeploy de producción falló: Vercel sigue con el túnel anterior." >&2
    return 1
  fi
  url="$(printf '%s\n' "$salida" | grep -oE 'https://[A-Za-z0-9.-]+\.vercel\.app' | tail -n 1 || true)"
  if [ -z "$url" ]; then
    printf '%s\n' "$salida" | tail -n 20 >&2
    echo "No encontré la URL del deploy en la salida de vercel: revisalo con npx vercel ls." >&2
    return 1
  fi
  echo "$(date +%H:%M:%S) producción redesplegada ($url)."
}

if [ "$TUNEL" = cloudflared ]; then
  CLOUDFLARED="$(command -v cloudflared || true)"
  [ -n "$CLOUDFLARED" ] || CLOUDFLARED="/c/Program Files (x86)/cloudflared/cloudflared.exe"
  [ -x "$CLOUDFLARED" ] || { echo "Falta cloudflared (winget install Cloudflare.cloudflared)."; exit 1; }
  # http2 (TCP) y no QUIC (UDP): después de suspender la PC o en redes que
  # filtran UDP, QUIC no reconecta ("no recent network activity").
  "$CLOUDFLARED" tunnel --no-autoupdate --protocol "${TUNEL_PROTOCOLO:-http2}" \
    --url "http://127.0.0.1:$PUERTO" > .logs/cloudflared.log 2>&1 &
  PIDS+=($!)
  esperar_url .logs/cloudflared.log 'https://[a-z0-9-]+\.trycloudflare\.com' \
    || { echo "cloudflared no dio una URL (ver .logs/cloudflared.log)."; exit 1; }
  apuntar_vercel || exit 1
  echo "Listo. Túnel y enrutador quedan corriendo; Ctrl+C para cortarlos."
  wait "${PIDS[1]}"
  exit 0
fi

[ "$TUNEL" = pinggy ] || { echo "TUNEL tiene que ser auto, cloudflared o pinggy."; exit 1; }
command -v ssh > /dev/null || { echo "Falta ssh (OpenSSH)."; exit 1; }
while true; do
  ssh -p 443 -T -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$SECRETOS/../pinggy_known_hosts" \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes \
    -R0:127.0.0.1:"$PUERTO" a.pinggy.io > .logs/pinggy.log 2>&1 &
  SSH=$!
  PIDS+=($SSH)
  if esperar_url .logs/pinggy.log 'https://[a-z0-9.-]+\.pinggy\.(net|link|online)'; then
    apuntar_vercel || exit 1
    echo "Listo. El túnel gratis dura 60 min: al cortarse, lo vuelvo a abrir solo. Ctrl+C para cortar."
  else
    echo "pinggy no dio una URL (ver .logs/pinggy.log)."
  fi
  wait "$SSH" || true
  echo "$(date +%H:%M:%S) el túnel se cortó; lo vuelvo a abrir…"
  sleep 3
done
