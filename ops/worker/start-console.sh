#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/config.sh"
"$script_dir/preflight.sh"

# Operator-only recovery console. Host clipboard, USB, audio, shares, and forwarding remain disabled.
exec uv run --project "$script_dir/../.." --no-sync python "$script_dir/softnet_launcher.py" console
