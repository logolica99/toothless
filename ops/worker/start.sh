#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/config.sh"
"$script_dir/preflight.sh"

# The wrapper blocks new guest-to-host flows through Softnet's acknowledged,
# stateful control policy before accepting the startup as usable.
exec uv run --project "$script_dir/../.." --no-sync python "$script_dir/softnet_launcher.py" headless

