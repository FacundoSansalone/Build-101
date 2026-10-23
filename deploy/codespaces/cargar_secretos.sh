#!/usr/bin/env bash
# Desde tu máquina, en la raíz del repo (Git Bash en Windows): carga en GitHub
# los tres secretos de Codespaces que usa deploy/codespaces/prender.sh. Son
# secretos de tu usuario, habilitados solo para este repo: no van al repo y
# no los ve nadie más.
#
#   bash deploy/codespaces/cargar_secretos.sh
#
#   GOOGLE_API_KEY          la del .env
#   DASHBOARD_ACCESS_TOKEN  la contraseña de /demo: la genera una vez en
#                           .temp-db/codespaces_dashboard_token (ignorado por
#                           git) y después la reusa
#   TUNEL_PREFIJO           el de .temp-db/vercel/TUNEL_PREFIJO.txt, el mismo
#                           que usa el motor de Vercel
#
# Cada valor le llega a gh por la entrada estándar: nunca en la línea de
# comandos ni en la pantalla. Requiere gh con permiso de Codespaces
# (gh auth login -s codespace, o gh auth refresh -s codespace). Un Codespace
# que ya estaba prendido los ve recién al reiniciarlo.
#
# Variables (para probar): ENV_ARCHIVO (.env), SECRETOS (.temp-db/vercel),
# CLAVE_PANEL (.temp-db/codespaces_dashboard_token), REPO (el de origin), PY.
set -euo pipefail

RAIZ="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$RAIZ"
ENV_ARCHIVO="${ENV_ARCHIVO:-.env}"
SECRETOS="${SECRETOS:-.temp-db/vercel}"
CLAVE_PANEL="${CLAVE_PANEL:-.temp-db/codespaces_dashboard_token}"
if [ -z "${PY:-}" ]; then
  if [ -x world/ap2/.venv/Scripts/python ]; then PY=world/ap2/.venv/Scripts/python; else PY=world/ap2/.venv/bin/python; fi
fi
if [ -z "${REPO:-}" ]; then
  REPO="$(git remote get-url origin 2>/dev/null \
    | sed -E 's#^(https://github\.com/|git@github\.com:)##; s#\.git$##' || true)"
fi
if ! [[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo "No encontré el repo de GitHub (git remote origin). Pasalo con REPO=<dueño>/<repo>." >&2
  exit 2
fi

# --- 1. Lo que hace falta, antes de tocar GitHub (sin mostrar valores) -------
errores=()
gemini=""
if [ -f "$ENV_ARCHIVO" ]; then
  gemini="$(grep -E '^[[:space:]]*GOOGLE_API_KEY=' "$ENV_ARCHIVO" | tail -n 1 | cut -d= -f2- \
    | tr -d "\r\n\"' " || true)"
fi
if [ -z "$gemini" ]; then
  errores+=("GOOGLE_API_KEY: no está en $ENV_ARCHIVO.")
fi
prefijo=""
if [ -s "$SECRETOS/TUNEL_PREFIJO.txt" ]; then
  prefijo="$(tr -d '\r\n' < "$SECRETOS/TUNEL_PREFIJO.txt")"
fi
if ! [[ "$prefijo" =~ ^[A-Za-z0-9_-]{8,128}$ ]]; then
  errores+=("TUNEL_PREFIJO: falta o no sirve $SECRETOS/TUNEL_PREFIJO.txt (deploy/vercel/README.md, paso 3).")
fi
if ! command -v gh >/dev/null 2>&1; then
  errores+=("gh: no está instalado (https://cli.github.com).")
fi
if [ "${#errores[@]}" -gt 0 ]; then
  echo "No cargué nada:" >&2
  for e in "${errores[@]}"; do echo "  - $e" >&2; done
  exit 2
fi

# --- 2. La contraseña de /demo: una vez, y después se reusa ------------------
if [ ! -s "$CLAVE_PANEL" ]; then
  mkdir -p "$(dirname "$CLAVE_PANEL")"
  (umask 077; "$PY" -c "import secrets; print(secrets.token_urlsafe(24))" > "$CLAVE_PANEL")
  echo "Generé la contraseña de /demo en $CLAVE_PANEL."
fi
panel="$(tr -d '\r\n' < "$CLAVE_PANEL")"
if [ "${#panel}" -lt 16 ]; then
  echo "La contraseña de $CLAVE_PANEL es demasiado corta (al menos 16 caracteres): borrala y volvé a correr esto." >&2
  exit 2
fi

# --- 3. A GitHub: secretos de Codespaces de tu usuario, solo para este repo --
cargar() {  # cargar <nombre> <valor>: el valor va por la entrada estándar
  local salida
  if ! salida="$(printf '%s' "$2" | gh secret set "$1" --user --app codespaces --repos "$REPO" 2>&1)"; then
    printf '%s\n' "$salida" | tail -n 5 >&2
    echo "No pude cargar $1 (¿gh auth login -s codespace?)." >&2
    exit 1
  fi
  echo "  $1: cargado."
}
echo "Secretos de Codespaces para $REPO:"
cargar GOOGLE_API_KEY "$gemini"
cargar DASHBOARD_ACCESS_TOKEN "$panel"
cargar TUNEL_PREFIJO "$prefijo"
echo "Listo. La contraseña de /demo está en $CLAVE_PANEL (usuario cualquiera)."
echo "Un Codespace que ya estaba prendido los ve recién al reiniciarlo (Stop y Start)."
