#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/config.sh"
"$script_dir/preflight.sh"
mkdir -p "$TART_HOME"

list_local_vms() {
  tart list | awk '$1 == "local" {print $2}'
}

if ! list_local_vms | grep -Fxq "$WORKER_BASE_NAME"; then
  tart clone "$WORKER_IMAGE" "$WORKER_BASE_NAME"
  tart set "$WORKER_BASE_NAME" --cpu "$WORKER_CPUS" --memory "$WORKER_MEMORY_MB" --disk-size "$WORKER_DISK_GB"
fi

if ! list_local_vms | grep -Fxq "$WORKER_VM_NAME"; then
  tart clone "$WORKER_BASE_NAME" "$WORKER_VM_NAME"
fi

printf 'Worker created. Change the published default password before taking the baseline.\n'
printf 'Do not start it without ops/worker/start.sh; that script enforces Softnet.\n'
