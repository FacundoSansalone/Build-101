#!/usr/bin/env bash
# La simulación detrás del motor en Vercel, con un solo comando:
#
#   bash world/run_world.sh general          # en otra terminal: comercio :8001 y catálogo :8011
#   bash deploy/vercel/tunel_demo.sh         # enrutador :8090 + túnel rápido de cloudflared
#
# El túnel rápido (trycloudflare.com) no pide cuenta, pero cambia de URL en
# cada arranque. Este script la lee, reescribe GATEWAY_COMERCIOS_JSON con el
# dominio nuevo (mismo prefijo secreto de .temp-db/vercel/TUNEL_PREFIJO.txt),
# la carga en Vercel y redespliega producción. Ctrl+C corta túnel y enrutador.
#
# Requiere: `npx vercel login` y `npx vercel link` hechos, y los secretos de
# deploy/vercel/generar_secretos.py en .temp-db/vercel/. No imprime secretos.
set -euo pipefail

RAIZ="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$RAIZ"
SECRETOS="${SECRETOS:-.temp-db/vercel}"
PUERTO="${TUNEL_PUERTO:-8090}"
if [ -z "${PY:-}" ]; then
  if [ -x world/ap2/.venv/Scripts/python ]; then PY=world/ap2/.venv/Scripts/python; else PY=world/ap2/.venv/bin/python; fi
fi
CLOUDFLARED="$(command -v cloudflared || true)"
[ -n "$CLOUDFLARED" ] || CLOUDFLARED="/c/Program Files (x86)/cloudflared/cloudflared.exe"
[ -x "$CLOUDFLARED" ] || { echo "Falta cloudflared (winget install Cloudflare.cloudflared)."; exit 1; }
[ -s "$SECRETOS/TUNEL_PREFIJO.txt" ] || { echo "Falta $SECRETOS/TUNEL_PREFIJO.txt (generar_secretos.py --dominio …)."; exit 1; }
for p in 8001 8011; do
  curl -s -o /dev/null -m 5 "http://127.0.0.1:$p/" || { echo "La simulación no responde en :$p (bash world/run_world.sh general)."; exit 1; }
done

mkdir -p .logs
TUNEL_PREFIJO="$(tr -d '\r\n' < "$SECRETOS/TUNEL_PREFIJO.txt")" PYTHONUTF8=1 \
  "$PY" deploy/vercel/tunel_router.py --puerto "$PUERTO" > .logs/tunel_router.log 2>&1 &
ROUTER=$!
"$CLOUDFLARED" tunnel --no-autoupdate --url "http://127.0.0.1:$PUERTO" > .logs/cloudflared.log 2>&1 &
TUNEL=$!
trap 'kill $TUNEL $ROUTER 2>/dev/null || true' EXIT INT TERM

DOMINIO=""
for _ in $(seq 1 60); do
  DOMINIO="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' .logs/cloudflared.log | head -1 || true)"
  [ -n "$DOMINIO" ] && break
  sleep 1
done
[ -n "$DOMINIO" ] || { echo "cloudflared no dio una URL (ver .logs/cloudflared.log)."; exit 1; }
echo "túnel: $DOMINIO"

DOMINIO="$DOMINIO" SECRETOS="$SECRETOS" "$PY" - <<'EOF'
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

tr -d '\r\n' < "$SECRETOS/GATEWAY_COMERCIOS_JSON.json" \
  | npx vercel env add GATEWAY_COMERCIOS_JSON production --force > /dev/null
echo "GATEWAY_COMERCIOS_JSON actualizado en Vercel; redespliego producción…"
npx vercel deploy --prod --yes 2>&1 | grep -E '"message"|Error' | head -2 || true
echo "Listo. Túnel y enrutador quedan corriendo; Ctrl+C para cortarlos."
wait $TUNEL
