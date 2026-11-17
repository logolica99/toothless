#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/config.sh"
"$script_dir/preflight.sh"

worker_state="$(tart get "$WORKER_VM_NAME" | awk 'NR == 2 { print tolower($NF) }')"
[[ "$worker_state" == 'running' ]] || {
  printf 'Start the worker with ops/worker/start.sh before installing OpenCode.\n' >&2
  exit 1
}
worker_ip="$(tart ip "$WORKER_VM_NAME")"
[[ "$worker_ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]
guest_host_key="$(tart exec "$WORKER_VM_NAME" sudo -n cat /etc/ssh/ssh_host_ed25519_key.pub)"
[[ "$guest_host_key" == ssh-ed25519\ * ]] || {
  printf 'Could not obtain the guest host key from the trusted Tart guest agent.\n' >&2
  exit 1
}
known_hosts="$(mktemp "${TMPDIR:-/tmp}/toothless-opencode-known-hosts.XXXXXX")"
remote_dir="/tmp/toothless-opencode-install-$(/usr/bin/uuidgen | tr '[:upper:]' '[:lower:]')"
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
trap 'rm -f "$known_hosts"; ssh "${ssh_options[@]}" "admin@$worker_ip" "sudo -n rm -rf -- $remote_dir" >/dev/null 2>&1 || true' EXIT
ssh "${ssh_options[@]}" "admin@$worker_ip" "mkdir -m 0700 -- '$remote_dir'"
for file in guest_install_opencode.sh opencode_task_runner.py; do
  ssh "${ssh_options[@]}" "admin@$worker_ip" "tee '$remote_dir/$file' >/dev/null" < "$script_dir/$file"
done
ssh "${ssh_options[@]}" "admin@$worker_ip" "tee '$remote_dir/opencode.json' >/dev/null" < "$script_dir/../opencode/phase05/opencode.json"
ssh "${ssh_options[@]}" "admin@$worker_ip" "sudo -n bash '$remote_dir/guest_install_opencode.sh' '$remote_dir'"
printf 'OpenCode CLI and the fixed deny-all task runner are installed in Ubuntu. This installer does not read, copy, or change authentication.\n'
