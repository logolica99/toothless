#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/config.sh"
"$script_dir/preflight.sh"

printf 'This replaces %s with the current hardened worker and recreates the worker.\n' "$WORKER_BASE_NAME"
worker_state="$(tart get "$WORKER_VM_NAME" | awk 'NR == 2 {print $NF}')"
[[ "$worker_state" == "stopped" ]] || {
  printf 'Worker must be gracefully stopped with ops/worker/shutdown.sh before sealing.\n' >&2
  exit 1
}
read -r -p 'Type SEAL-BASELINE to continue: ' confirmation
[[ "$confirmation" == "SEAL-BASELINE" ]] || { printf 'Baseline unchanged.\n'; exit 2; }

tart delete "$WORKER_BASE_NAME"
tart clone "$WORKER_VM_NAME" "$WORKER_BASE_NAME"
tart delete "$WORKER_VM_NAME"
tart clone "$WORKER_BASE_NAME" "$WORKER_VM_NAME"
printf 'Hardened baseline sealed and clean worker recreated.\n'
