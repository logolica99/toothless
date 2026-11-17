#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/config.sh"

private_key="${1:-$HOME/.ssh/id_ed25519}"
[[ -f "$private_key" && ! -L "$private_key" ]] || {
  printf 'Private key must be a regular, non-symlink file: %s\n' "$private_key" >&2
  exit 1
}

worker_ip="$(tart ip "$WORKER_VM_NAME")"
ssh -o BatchMode=yes -o IdentitiesOnly=yes -i "$private_key" "admin@$worker_ip" "sudo sync; sudo systemctl poweroff" || true

for _ in {1..30}; do
  state="$(tart get "$WORKER_VM_NAME" | awk 'NR == 2 {print $NF}')"
  if [[ "$state" == "stopped" ]]; then
    printf 'Worker shut down cleanly.\n'
    exit 0
  fi
  sleep 2
done

printf 'Guest did not stop within 60 seconds; baseline sealing is blocked. Use stop.sh only as an emergency stop.\n' >&2
exit 1
