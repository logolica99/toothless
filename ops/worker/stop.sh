#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/config.sh"

command -v tart >/dev/null || { printf 'Tart is not installed.\n' >&2; exit 1; }
tart stop "$WORKER_VM_NAME"

