#!/bin/sh
# Instala la feature kaiai-arranque (al construir la imagen del Codespace,
# como root): el entrypoint, el vigía y el usuario con el que prende la demo
# (_REMOTE_USER, el del Codespace: vscode).
set -e
DIR=/usr/local/share/kaiai-arranque
mkdir -p "$DIR"
cp entrypoint.sh vigia.sh "$DIR/"
chmod 755 "$DIR/entrypoint.sh" "$DIR/vigia.sh"
printf '%s\n' "${_REMOTE_USER:-vscode}" > "$DIR/usuario"
