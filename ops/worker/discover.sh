#!/usr/bin/env bash

set -euo pipefail

architecture="$(uname -m)"
macos_version="$(sw_vers -productVersion)"
memory_bytes="$(sysctl -n hw.memsize)"
cpu_count="$(sysctl -n hw.ncpu)"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/config.sh"
free_kb="$(df -Pk "$WORKER_STORAGE_VOLUME" | awk 'NR == 2 {print $4}')"

python3 - "$architecture" "$macos_version" "$memory_bytes" "$cpu_count" "$free_kb" <<'PY'
import json
import sys

architecture, macos_version, memory_bytes, cpu_count, free_kb = sys.argv[1:]
print(json.dumps({
    "architecture": architecture,
    "macos_version": macos_version,
    "memory_gib": round(int(memory_bytes) / 1024**3, 1),
    "logical_cpus": int(cpu_count),
    "worker_volume": "/Volumes/toothless",
    "worker_volume_free_gib": round(int(free_kb) / 1024**2, 1),
}, indent=2, sort_keys=True))
PY
