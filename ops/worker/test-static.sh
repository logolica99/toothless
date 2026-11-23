#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/config.sh"

[[ "$WORKER_IMAGE" == *@sha256:* ]]
grep -Fq 'uv run --project "$script_dir/../.." --no-sync python "$script_dir/softnet_launcher.py" headless' "$script_dir/start.sh"
grep -Fq 'uv run --project "$script_dir/../.." --no-sync python "$script_dir/softnet_launcher.py" console' "$script_dir/start-console.sh"
grep -Fq 'uv run --project "$script_dir/../.." --no-sync python "$script_dir/softnet_launcher.py" serial' "$script_dir/start-serial.sh"
grep -Fq '"out @host"' "$script_dir/softnet_launcher.py"
grep -Fq 'out proto udp @host port 55353' "$script_dir/softnet_launcher.py"
grep -Fq 'out proto tcp @host port 55353' "$script_dir/softnet_launcher.py"
! grep -Fq 'port 53"' "$script_dir/softnet_launcher.py"
grep -Fq 'dns_forwarder.py' "$script_dir/softnet_launcher.py"
grep -Fq 'softnet.policy.set' "$script_dir/softnet_launcher.py"
! grep -Eq -- '--dir|--net-bridged|--net-host|--net-softnet-allow|--net-softnet-expose' "$script_dir/softnet_launcher.py"
grep -Fq 'read -r -p' "$script_dir/reset.sh"
grep -Fq 'SEAL-BASELINE' "$script_dir/seal-baseline.sh"
grep -Fq 'tart stop "$WORKER_VM_NAME"' "$script_dir/stop.sh"
grep -Fq 'StrictHostKeyChecking=yes' "$script_dir/install-opencode-runner.sh"
grep -Fq 'IdentityAgent=none' "$script_dir/install-opencode-runner.sh"
grep -Fq 'installed_hash' "$script_dir/install-opencode-runner.sh"
grep -Fq 'StrictHostKeyChecking=yes' "$script_dir/shutdown.sh"
grep -Fq 'IdentityAgent=none' "$script_dir/shutdown.sh"
grep -Fq 'ForwardAgent=no' "$script_dir/shutdown.sh"
grep -Fq 'UserKnownHostsFile=$known_hosts' "$script_dir/shutdown.sh"

printf 'Static worker safety checks passed.\n'
