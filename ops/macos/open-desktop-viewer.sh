#!/usr/bin/env bash

set -euo pipefail

readonly token_file="$HOME/.local/share/toothless/desktop/viewer-token"
readonly viewer_url="http://127.0.0.1:8769/"

[[ -f "$token_file" && ! -L "$token_file" ]] || {
  printf 'No temporary desktop-viewer code is available. Request Open Desktop in Telegram first.\n' >&2
  exit 1
}
[[ "$(stat -f '%u %Lp' "$token_file")" == "$(id -u) 600" ]] || {
  printf 'The local viewer code file is not owner-private; refusing to use it.\n' >&2
  exit 1
}
[[ "$(curl --silent --show-error --max-time 2 --output /dev/null --write-out '%{http_code}' "$viewer_url")" == 200 ]] || {
  printf 'The local view-only service is not listening. Start a temporary viewing session from Telegram first.\n' >&2
  exit 1
}

token="$(cat "$token_file")"
[[ "$token" =~ ^[A-Za-z0-9_-]{40,64}$ ]] || {
  printf 'The temporary viewer code is malformed.\n' >&2
  exit 1
}
rm -f "$token_file"
open "$viewer_url"
printf 'Enter this one-time code in the local viewer page (valid for up to 10 minutes):\n%s\n' "$token"
