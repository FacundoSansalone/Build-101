#!/usr/bin/env bash
# Desde tu máquina, en la raíz del repo (Git Bash en Windows), con el
# Codespace prendido: apunta el motor de Vercel al comercio del Codespace.
# Una vez por Codespace: el dominio (<codespace>-8080.app.github.dev) no
# cambia al apagarlo y prenderlo, solo si se crea otro.
#
#   bash deploy/codespaces/apuntar_vercel.sh [<nombre del Codespace>]
#   bash deploy/codespaces/apuntar_vercel.sh --solo-publicar [<nombre>]   # solo 1 y 2
#
# Sin nombre, usa el único Codespace de este repo (gh codespace list).
# 1. Deja público el puerto 8080. prender.sh lo intenta al prender, pero
#    desde adentro del Codespace gh puede no tener permiso, y GitHub puede
#    volver a dejarlo privado al reiniciarlo: --solo-publicar hace solo esto
#    y la comprobación, sin tocar Vercel.
# 2. Comprueba que la demo responde: /salud da 200, y el catálogo con el
#    prefijo de .temp-db/vercel/TUNEL_PREFIJO.txt también (si da 401 lo
#    atendió el panel: el secreto TUNEL_PREFIJO del Codespace es otro).
# 3. deploy/vercel/apuntar_vercel.sh: GATEWAY_COMERCIOS_JSON con ese dominio
#    y el mismo prefijo, vercel env add y vercel deploy --prod.
#
# Dice "Listo" solo si todo salió bien. No imprime el prefijo. Requiere gh
# con permiso de Codespaces (gh auth login -s codespace) y `npx vercel login`
# y `npx vercel link` hechos (deploy/vercel/README.md).
set -euo pipefail

RAIZ="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$RAIZ"
SECRETOS="${SECRETOS:-.temp-db/vercel}"
PUERTO=8080
DOMINIO_GH="app.github.dev"

uso() {
  echo "Uso: bash deploy/codespaces/apuntar_vercel.sh [--solo-publicar] [<nombre del Codespace>]" >&2
  exit 2
}
SOLO_PUBLICAR=0
NOMBRE=""
for arg in "$@"; do
  case "$arg" in
    --solo-publicar) SOLO_PUBLICAR=1 ;;
    -*) uso ;;
    *) [ -z "$NOMBRE" ] || uso; NOMBRE="$arg" ;;
  esac
done
if [ -z "$NOMBRE" ]; then
  REPO="${REPO:-$(git remote get-url origin 2>/dev/null \
    | sed -E 's#^(https://github\.com/|git@github\.com:)##; s#\.git$##' || true)}"
  if ! nombres="$(gh codespace list --repo "$REPO" --json name --jq '.[].name' 2>&1)"; then
    printf '%s\n' "$nombres" | tail -n 5 >&2
    echo "No pude listar tus Codespaces (¿gh auth login -s codespace?). Pasá el nombre a mano." >&2
    exit 1
  fi
  cantidad="$(printf '%s\n' "$nombres" | grep -c . || true)"
  if [ "$cantidad" = 0 ]; then
    echo "No tenés ningún Codespace de $REPO: crealo primero (deploy/codespaces/README.md)." >&2
    exit 1
  elif [ "$cantidad" != 1 ]; then
    echo "Tenés $cantidad Codespaces de $REPO; pasá el nombre del que querés usar:" >&2
    printf '  %s\n' $nombres >&2
    exit 2
  fi
  NOMBRE="$nombres"
fi
if ! [[ "$NOMBRE" =~ ^[A-Za-z0-9-]{1,100}$ ]]; then
  echo "Eso no es el nombre de un Codespace (el de gh codespace list, sin https:// ni puerto)." >&2
  exit 2
fi
DOMINIO="$NOMBRE-$PUERTO.$DOMINIO_GH"
if [ ! -s "$SECRETOS/TUNEL_PREFIJO.txt" ]; then
  echo "Falta $SECRETOS/TUNEL_PREFIJO.txt (deploy/vercel/README.md, paso 3)." >&2
  exit 1
fi

# --- 1. El puerto público ----------------------------------------------------
if ! salida="$(gh codespace ports visibility "$PUERTO:public" -c "$NOMBRE" 2>&1)"; then
  printf '%s\n' "$salida" | tail -n 5 >&2
  echo "No pude dejar público el puerto $PUERTO de $NOMBRE: ¿está prendido? (gh codespace list)" >&2
  exit 1
fi

# --- 2. La demo responde -----------------------------------------------------
codigo="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://$DOMINIO/salud" || true)"
if [ "$codigo" != 200 ]; then
  echo "https://$DOMINIO/salud contestó ${codigo:-nada} (se esperaba 200): ¿terminó de prender la demo?" >&2
  echo "Mirá el log: gh codespace ssh -c $NOMBRE -- tail -n 40 .logs/codespaces.log" >&2
  exit 1
fi
prefijo="$(tr -d '\r\n' < "$SECRETOS/TUNEL_PREFIJO.txt")"
# -s sin -S: si falla, curl no muestra la URL (lleva el prefijo).
codigo="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://$DOMINIO/$prefijo/products" || true)"
if [ "$codigo" != 200 ]; then
  if [ "$codigo" = 401 ]; then
    echo "El catálogo con el prefijo de TUNEL_PREFIJO.txt lo contestó el panel (401): el secreto" >&2
    echo "TUNEL_PREFIJO del Codespace es otro. Cargalo con bash deploy/codespaces/cargar_secretos.sh" >&2
    echo "y reiniciá el Codespace." >&2
  else
    echo "El catálogo de la demo contestó ${codigo:-nada} (se esperaba 200)." >&2
  fi
  exit 1
fi
echo "La demo responde en https://$DOMINIO (salud y catálogo con el prefijo)."
if [ "$SOLO_PUBLICAR" = 1 ]; then
  echo "Listo: el puerto $PUERTO es público (el motor de Vercel no se tocó)."
  exit 0
fi

# --- 3. El motor de Vercel ---------------------------------------------------
if ! SECRETOS="$SECRETOS" bash deploy/vercel/apuntar_vercel.sh "https://$DOMINIO"; then
  echo "El motor de Vercel no quedó apuntado al Codespace: revisá el error de arriba." >&2
  exit 1
fi
echo "Listo. El motor de Vercel le habla al comercio en https://$DOMINIO (con el prefijo secreto)."
echo "Con el Codespace apagado, las compras por el motor se bloquean (falla cerrado) hasta que lo prendas."
