#!/bin/sh
# El vigía de la feature kaiai-arranque (lo lanza entrypoint.sh, como root).
# Espera el aviso del postStartCommand (deploy/codespaces/prender.sh
# --avisar deja .temp-db/codespaces.aviso en el repo) y prende la demo como
# el usuario del Codespace, con una shell de login: ahí están CODESPACE_NAME,
# GITHUB_TOKEN y los secretos, ya renovados para este arranque.
#
# Un aviso de antes de este arranque no cuenta (las variables podrían ser
# viejas): lo borra. Sigue mirando cada 2 s, así un aviso nuevo (otra vez
# --avisar) vuelve a prenderla; prender.sh no la levanta dos veces.
USUARIO="${1:-vscode}"
CASA="$(getent passwd "$USUARIO" | cut -d: -f6)"
inicio="$(date +%s)"
echo "$$" > /tmp/kaiai-vigia.pid
echo "$(date -u '+%F %T') vigía listo (usuario $USUARIO)"
while :; do
  for aviso in /workspaces/*/.temp-db/codespaces.aviso; do
    [ -f "$aviso" ] || continue
    cuando="$(stat -c %Y "$aviso" 2>/dev/null || echo 0)"
    rm -f "$aviso"
    if [ "$cuando" -lt "$inicio" ]; then
      echo "$(date -u '+%F %T') aviso de antes de este arranque: no cuenta"
      continue
    fi
    raiz="$(dirname "$(dirname "$aviso")")"
    echo "$(date -u '+%F %T') prendo la demo en $raiz"
    # runuser -u conserva el entorno del contenedor (containerEnv: UV_FROZEN,
    # UV_PYTHON…) salvo HOME, que tiene que ser el del usuario (ahí está uv).
    runuser -u "$USUARIO" -- env HOME="${CASA:-/home/$USUARIO}" \
      bash -lc "cd '$raiz' && bash deploy/codespaces/prender.sh" \
      || echo "$(date -u '+%F %T') prender.sh salió con error (mirá $raiz/.logs/codespaces.log)"
  done
  sleep 2
done
