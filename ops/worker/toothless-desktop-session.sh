#!/usr/bin/env bash

set -euo pipefail
umask 077

export HOME=/var/lib/toothless-desktop
export DISPLAY=:99
export XAUTHORITY=/run/toothless-desktop/Xauthority
export MOZ_ENABLE_WAYLAND=0

mkdir -p "$HOME" /tmp/.X11-unix
chmod 0700 /tmp/.X11-unix
/usr/bin/xauth -f "$XAUTHORITY" add "$DISPLAY" . "$(/usr/bin/mcookie)"

children=()
cleanup() {
  for child in "${children[@]}"; do
    kill "$child" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  rm -f /run/toothless-desktop/Xauthority
}
trap cleanup EXIT TERM INT

/usr/bin/Xvfb "$DISPLAY" -screen 0 1280x800x24 -nolisten tcp -auth "$XAUTHORITY" &
children+=("$!")
for _ in $(seq 1 50); do
  [[ -S /tmp/.X11-unix/X99 ]] && break
  sleep 0.1
done
[[ -S /tmp/.X11-unix/X99 ]]

/usr/bin/openbox &
children+=("$!")
sleep 0.5
/usr/bin/xsetroot -solid "#202a3a"
/opt/toothless/visible-browser-runtime/bin/python /usr/local/libexec/toothless/visible_browser_broker.py &
children+=("$!")
/usr/bin/xterm -fa "DejaVu Sans Mono" -fs 13 -bg "#f3f4f6" -fg "#172033" -geometry 96x28+12+12 -title "Toothless Ubuntu Workspace" -e /usr/bin/env bash -lc 'printf "Toothless isolated Ubuntu desktop\\nView-only session; no remote input is enabled.\\n"; exec bash' &
children+=("$!")
wait -n "${children[@]}"
