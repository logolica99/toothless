#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/config.sh"
"$script_dir/preflight.sh"

# Operator-only recovery terminal; avoids GUI keyboard-capture permissions.
exec uv run --project "$script_dir/../.." --no-sync python "$script_dir/softnet_launcher.py" serial
