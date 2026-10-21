#!/usr/bin/env bash
# Apunta el motor de Vercel al comercio fijo en Railway. Desde tu máquina, en
# la raíz del repo, después del primer deploy (y cada vez que cambie el
# dominio del servicio):
#
#   bash deploy/railway/apuntar_vercel.sh <servicio>.up.railway.app
#   bash deploy/railway/apuntar_vercel.sh --sin-salud <dominio>   # sin el paso 1
#
# 1. Comprueba que la demo responde: https://<dominio>/salud da 200, y el
#    catálogo con el prefijo de .temp-db/vercel/TUNEL_PREFIJO.txt también (si
#    da 401 lo atendió el panel: el TUNEL_PREFIJO del servicio no es el mismo).
# 2. deploy/vercel/apuntar_vercel.sh: GATEWAY_COMERCIOS_JSON con ese dominio y
#    el mismo prefijo, vercel env add y vercel deploy --prod.
#
# Dice "Listo" solo si todo salió bien. No imprime el prefijo. Requiere
# `npx vercel login` y `npx vercel link` hechos (deploy/vercel/README.md).
set -euo pipefail

RAIZ="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$RAIZ"
SECRETOS="${SECRETOS:-.temp-db/vercel}"

CHEQUEAR=1
DOMINIO=""
for arg in "$@"; do
  case "$arg" in
    --sin-salud) CHEQUEAR=0 ;;
    -*) echo "Opción desconocida: $arg" >&2; exit 2 ;;
    *) DOMINIO="$arg" ;;
  esac
done
DOMINIO="$(printf '%s' "$DOMINIO" | tr '[:upper:]' '[:lower:]')"
DOMINIO="${DOMINIO#https://}"
DOMINIO="${DOMINIO%/}"
if ! [[ "$DOMINIO" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ ]]; then
  echo "Uso: bash deploy/railway/apuntar_vercel.sh [--sin-salud] <servicio>.up.railway.app" >&2
  echo "(el dominio solo, sin ruta: el de Settings → Networking del servicio)" >&2
  exit 2
fi

if [ "$CHEQUEAR" = 1 ]; then
  if [ ! -s "$SECRETOS/TUNEL_PREFIJO.txt" ]; then
    echo "Falta $SECRETOS/TUNEL_PREFIJO.txt (deploy/vercel/README.md, paso 3)." >&2
    exit 1
  fi
  codigo="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://$DOMINIO/salud" || true)"
  if [ "$codigo" != 200 ]; then
    echo "https://$DOMINIO/salud contestó ${codigo:-nada} (se esperaba 200): ¿terminó el deploy?" >&2
    echo "Mirá los logs: npx @railway/cli logs" >&2
    exit 1
  fi
  prefijo="$(tr -d '\r\n' < "$SECRETOS/TUNEL_PREFIJO.txt")"
  # -s sin -S: si falla, curl no muestra la URL (lleva el prefijo).
  codigo="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://$DOMINIO/$prefijo/products" || true)"
  if [ "$codigo" != 200 ]; then
    if [ "$codigo" = 401 ]; then
      echo "El catálogo con el prefijo de TUNEL_PREFIJO.txt lo contestó el panel (401): el" >&2
      echo "TUNEL_PREFIJO del servicio en Railway no es el mismo. Cargá el de $SECRETOS/TUNEL_PREFIJO.txt." >&2
    else
      echo "El catálogo de la demo contestó ${codigo:-nada} (se esperaba 200)." >&2
    fi
    exit 1
  fi
  echo "La demo responde en https://$DOMINIO (salud y catálogo con el prefijo)."
fi

if ! SECRETOS="$SECRETOS" bash deploy/vercel/apuntar_vercel.sh "https://$DOMINIO"; then
  echo "El motor de Vercel no quedó apuntado a Railway: revisá el error de arriba." >&2
  exit 1
fi
echo "Listo. El motor de Vercel le habla al comercio en https://$DOMINIO (con el prefijo secreto)."
