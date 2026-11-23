#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/config.sh"

private_key="${1:-$HOME/.ssh/id_ed25519}"
[[ -f "$private_key" && ! -L "$private_key" ]] || {
  printf 'Private key must be a regular, non-symlink file: %s\n' "$private_key" >&2
  exit 1
}

worker_ip="$(tart ip "$WORKER_VM_NAME")"
guest_host_key="$(tart exec "$WORKER_VM_NAME" sudo -n cat /etc/ssh/ssh_host_ed25519_key.pub)"
[[ "$guest_host_key" == ssh-ed25519\ * ]] || {
  printf 'Could not obtain the guest SSH host key from the trusted Tart guest agent.\n' >&2
  exit 1
}
known_hosts="$(mktemp "${TMPDIR:-/tmp}/toothless-shutdown-known-hosts.XXXXXX")"
trap 'rm -f "$known_hosts"' EXIT
printf '%s %s\n' "$worker_ip" "$guest_host_key" > "$known_hosts"
chmod 600 "$known_hosts"
ssh_options=(
  -F /dev/null -T -i "$private_key"
  -o IdentitiesOnly=yes -o IdentityAgent=none -o ForwardAgent=no
  -o ClearAllForwardings=yes -o BatchMode=yes -o PasswordAuthentication=no
  -o KbdInteractiveAuthentication=no -o PreferredAuthentications=publickey
  -o StrictHostKeyChecking=yes -o GlobalKnownHostsFile=/dev/null
  -o "UserKnownHostsFile=$known_hosts" -o ProxyCommand=none -o ProxyJump=none
)
ssh "${ssh_options[@]}" "admin@$worker_ip" 'sudo -n sync && sudo -n systemctl poweroff'

for _ in {1..30}; do
  state="$(tart get "$WORKER_VM_NAME" | awk 'NR == 2 {print $NF}')"
  if [[ "$state" == "stopped" ]]; then
    printf 'Worker shut down cleanly.\n'
    exit 0
  fi
  sleep 2
done

printf 'Guest did not stop within 60 seconds; baseline sealing is blocked. Use stop.sh only as an emergency stop.\n' >&2
exit 1
