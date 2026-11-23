#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
source "$script_dir/config.sh"
"$script_dir/preflight.sh"

readiness="$(cd "$repo_root" && PYTHONPATH="$repo_root" UV_CACHE_DIR=/tmp/toothless-uv-cache uv run --no-sync python -c 'import json; from nanobot.personal_agent.worker_readiness import worker_readiness; print(json.dumps(worker_readiness(),sort_keys=True))')"
[[ "$(printf '%s' "$readiness" | /usr/bin/python3 -c 'import json,sys; print("yes" if json.load(sys.stdin).get("worker_ready") is True else "no")')" == yes ]] || {
  printf 'Protected worker readiness is not fully verified; no deployment files were sent.\n' >&2
  exit 1
}

worker_state="$(tart get "$WORKER_VM_NAME" | awk 'NR == 2 { print tolower($NF) }')"
[[ "$worker_state" == running ]] || {
  printf 'Worker is not running under its protected launcher; use ops/worker/start.sh first.\n' >&2
  exit 1
}
worker_ip="$(tart ip "$WORKER_VM_NAME")"
[[ "$worker_ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]
guest_host_key="$(tart exec "$WORKER_VM_NAME" sudo -n cat /etc/ssh/ssh_host_ed25519_key.pub)"
[[ "$guest_host_key" == ssh-ed25519\ * ]]

known_hosts="$(mktemp "${TMPDIR:-/tmp}/toothless-visible-browser-known-hosts.XXXXXX")"
remote_dir="/tmp/toothless-visible-browser-$(/usr/bin/uuidgen | tr '[:upper:]' '[:lower:]')"
printf '%s %s\n' "$worker_ip" "$guest_host_key" > "$known_hosts"
chmod 600 "$known_hosts"
ssh_options=(
  -F /dev/null -T -i "$HOME/.ssh/id_ed25519"
  -o IdentitiesOnly=yes -o IdentityAgent=none -o ForwardAgent=no
  -o ClearAllForwardings=yes -o BatchMode=yes -o PasswordAuthentication=no
  -o KbdInteractiveAuthentication=no -o PreferredAuthentications=publickey
  -o StrictHostKeyChecking=yes -o GlobalKnownHostsFile=/dev/null
  -o "UserKnownHostsFile=$known_hosts" -o ProxyCommand=none -o ProxyJump=none
)
cleanup() {
  rm -f "$known_hosts"
  ssh "${ssh_options[@]}" "admin@$worker_ip" "sudo -n rm -rf -- '$remote_dir'" >/dev/null 2>&1 || true
}
trap cleanup EXIT
ssh "${ssh_options[@]}" "admin@$worker_ip" "mkdir -m 0700 -- '$remote_dir'"
for file in guest_install_visible_browser.sh opencode_task_runner.py browser_mcp.py visible_browser_broker.py toothless-desktop-session.sh toothless-desktop.service toothless-browser-authorizations.conf toothless-browser-auth-dir.service; do
  ssh "${ssh_options[@]}" "admin@$worker_ip" "tee '$remote_dir/$file' >/dev/null" < "$script_dir/$file"
done
ssh "${ssh_options[@]}" "admin@$worker_ip" "sudo -n bash '$remote_dir/guest_install_visible_browser.sh' '$remote_dir'"
