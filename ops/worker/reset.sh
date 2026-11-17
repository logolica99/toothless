#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/config.sh"
"$script_dir/preflight.sh"

printf 'This permanently replaces %s with a clean clone of %s.\n' "$WORKER_VM_NAME" "$WORKER_BASE_NAME"
read -r -p 'Type RESET-WORKER to continue: ' confirmation
[[ "$confirmation" == "RESET-WORKER" ]] || { printf 'Reset cancelled.\n'; exit 2; }

tart stop "$WORKER_VM_NAME" 2>/dev/null || true
tart delete "$WORKER_VM_NAME"
tart clone "$WORKER_BASE_NAME" "$WORKER_VM_NAME"
printf 'Worker reset complete. Start with ops/worker/start.sh, then run ops/worker/install-execution-agent.sh to restore the worker command helper.\n'

