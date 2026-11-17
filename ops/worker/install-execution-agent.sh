#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/config.sh"
"$script_dir/preflight.sh"

worker_ip="$(tart ip "$WORKER_VM_NAME")"
guest_host_key="$(tart exec "$WORKER_VM_NAME" sudo -n cat /etc/ssh/ssh_host_ed25519_key.pub)"
[[ "$guest_host_key" == ssh-ed25519\ * ]] || {
  printf 'Could not obtain the guest host key from the trusted Tart guest agent.\n' >&2
  exit 1
}
known_hosts="$(mktemp "${TMPDIR:-/tmp}/toothless-worker-known-hosts.XXXXXX")"
trap 'rm -f "$known_hosts"' EXIT
printf '%s %s\n' "$worker_ip" "$guest_host_key" > "$known_hosts"
chmod 600 "$known_hosts"
ssh_options=(
  -F /dev/null
  -T
  -i "$HOME/.ssh/id_ed25519"
  -o IdentitiesOnly=yes
  -o IdentityAgent=none
  -o ForwardAgent=no
  -o ClearAllForwardings=yes
  -o BatchMode=yes
  -o PasswordAuthentication=no
  -o KbdInteractiveAuthentication=no
  -o PreferredAuthentications=publickey
  -o StrictHostKeyChecking=yes
  -o GlobalKnownHostsFile=/dev/null
  -o "UserKnownHostsFile=$known_hosts"
  -o ProxyCommand=none
  -o ProxyJump=none
)

ssh "${ssh_options[@]}" "admin@$worker_ip" \
  'sudo -n install -d -o root -g root -m 0755 /usr/local/libexec/toothless'
ssh "${ssh_options[@]}" "admin@$worker_ip" \
  'sudo -n tee /tmp/toothless-worker-agent >/dev/null' < "$script_dir/guest_worker_agent.py"
ssh "${ssh_options[@]}" "admin@$worker_ip" \
  'sudo -n install -o root -g root -m 0755 /tmp/toothless-worker-agent /usr/local/libexec/toothless/worker-agent && sudo -n rm -f /tmp/toothless-worker-agent && sudo -n install -d -o admin -g admin -m 0750 /home/admin/workspace /home/admin/.local/state/toothless-worker/executions'
printf 'Installed the fixed guest worker helper with strict, pinned key-only SSH.\n'
