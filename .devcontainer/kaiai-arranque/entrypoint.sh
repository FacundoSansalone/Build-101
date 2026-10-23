#!/bin/sh
# Entrypoint de la feature kaiai-arranque: lo corre el PID 1 del contenedor
# en cada arranque, como root (igual que el de la feature de sshd). No
# bloquea: deja el vigía en segundo plano, que vive lo que vive el
# contenedor. Lo que lanza el postStartCommand, en cambio, GitHub lo mata al
# terminar esa etapa (medido el 2026-10-23, aun con setsid y nohup).
DIR=/usr/local/share/kaiai-arranque
USUARIO="$(cat "$DIR/usuario" 2>/dev/null || true)"
[ -n "$USUARIO" ] || USUARIO=vscode
nohup setsid sh "$DIR/vigia.sh" "$USUARIO" > /tmp/kaiai-vigia.log 2>&1 < /dev/null &

exec "$@"
