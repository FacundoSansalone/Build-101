#!/usr/bin/env bash
# Apunta el motor de Vercel a la simulación en https://<dominio>:
#
#   bash deploy/vercel/apuntar_vercel.sh https://<dominio>
#
# 1. Reescribe GATEWAY_COMERCIOS_JSON con el dominio nuevo y el mismo prefijo
#    secreto de .temp-db/vercel/TUNEL_PREFIJO.txt
#    (generar_secretos.py --apuntar).
# 2. La carga en Vercel (vercel env add … production --force).
# 3. Redespliega producción (vercel deploy --prod) y espera a que quede listo.
#
# Lo usan tunel_demo.sh (cada túnel nuevo) y deploy/railway/apuntar_vercel.sh
# (el comercio fijo en Railway). Si un paso falla, muestra el error y sale con
# 1 sin seguir: Vercel queda como estaba (o con la variable nueva y el deploy
# anterior, si falló el redeploy). No imprime secretos.
#
# Requiere `npx vercel login` y `npx vercel link` hechos. Variables:
#   SECRETOS  carpeta de los secretos (.temp-db/vercel)
#   PY        el Python del venv de world/ap2 (lo busca solo)
set -euo pipefail

RAIZ="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$RAIZ"
SECRETOS="${SECRETOS:-.temp-db/vercel}"
if [ -z "${PY:-}" ]; then
  if [ -x world/ap2/.venv/Scripts/python ]; then PY=world/ap2/.venv/Scripts/python; else PY=world/ap2/.venv/bin/python; fi
fi
DOMINIO="${1:-}"
if [ -z "$DOMINIO" ]; then
  echo "Uso: bash deploy/vercel/apuntar_vercel.sh https://<dominio>" >&2
  exit 2
fi

# La salida de cada paso se muestra solo si falla (la de la CLI no trae el
# valor de la variable; la de generar_secretos no trae el prefijo).
if ! salida="$(PYTHONUTF8=1 "$PY" deploy/vercel/generar_secretos.py --carpeta "$SECRETOS" --apuntar "$DOMINIO" 2>&1)"; then
  printf '%s\n' "$salida" >&2
  echo "No pude reescribir GATEWAY_COMERCIOS_JSON." >&2
  exit 1
fi
if ! salida="$(tr -d '\r\n' < "$SECRETOS/GATEWAY_COMERCIOS_JSON.json" \
    | npx vercel env add GATEWAY_COMERCIOS_JSON production --force 2>&1)"; then
  printf '%s\n' "$salida" | tail -n 20 >&2
  echo "No pude cargar GATEWAY_COMERCIOS_JSON en Vercel (¿npx vercel login y npx vercel link?)." >&2
  exit 1
fi
echo "$(date +%H:%M:%S) GATEWAY_COMERCIOS_JSON → $DOMINIO; redespliego producción…"
# `vercel deploy` espera a que el deploy quede listo y sale con error si el
# build falla; si sale bien, imprime la URL del deploy.
if ! salida="$(npx vercel deploy --prod --yes 2>&1)"; then
  printf '%s\n' "$salida" | tail -n 20 >&2
  echo "El redeploy de producción falló: Vercel sigue con el comercio anterior." >&2
  exit 1
fi
url="$(printf '%s\n' "$salida" | grep -oE 'https://[A-Za-z0-9.-]+\.vercel\.app' | tail -n 1 || true)"
if [ -z "$url" ]; then
  printf '%s\n' "$salida" | tail -n 20 >&2
  echo "No encontré la URL del deploy en la salida de vercel: revisalo con npx vercel ls." >&2
  exit 1
fi
echo "$(date +%H:%M:%S) producción redesplegada ($url)."
