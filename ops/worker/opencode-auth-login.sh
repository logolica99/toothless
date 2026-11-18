#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/config.sh"
"$script_dir/preflight.sh"

worker_state="$(tart get "$WORKER_VM_NAME" | awk 'NR == 2 { print tolower($NF) }')"
[[ "$worker_state" == 'running' ]] || {
  printf 'Start the worker with ops/worker/start.sh first; this script does not start it.\n' >&2
  exit 1
}
worker_ip="$(tart ip "$WORKER_VM_NAME")"
guest_host_key="$(tart exec "$WORKER_VM_NAME" sudo -n cat /etc/ssh/ssh_host_ed25519_key.pub)"
[[ "$guest_host_key" == ssh-ed25519\ * ]] || {
  printf 'Could not obtain the guest host key from the trusted Tart guest agent.\n' >&2
  exit 1
}
known_hosts="$(mktemp "${TMPDIR:-/tmp}/toothless-opencode-auth-hosts.XXXXXX")"
trap 'rm -f "$known_hosts"' EXIT
printf '%s %s\n' "$worker_ip" "$guest_host_key" > "$known_hosts"
chmod 600 "$known_hosts"
ssh_options=(
  -F /dev/null -tt -i "$HOME/.ssh/id_ed25519"
  -o IdentitiesOnly=yes -o IdentityAgent=none -o ForwardAgent=no
  -o ClearAllForwardings=yes -o BatchMode=yes -o PasswordAuthentication=no
  -o KbdInteractiveAuthentication=no -o PreferredAuthentications=publickey
  -o StrictHostKeyChecking=yes -o GlobalKnownHostsFile=/dev/null
  -o "UserKnownHostsFile=$known_hosts" -o ProxyCommand=none -o ProxyJump=none
)
printf 'OpenCode will prompt inside the Ubuntu guest. Enter the OpenCode Go credential only in this interactive terminal; it is not sent to Toothless logs or copied from the Mac.\n'
ssh "${ssh_options[@]}" "admin@$worker_ip" \
  "sudo -n -u toothless-agent -- env HOME=/home/toothless-agent XDG_CONFIG_HOME=/home/toothless-agent/.config XDG_DATA_HOME=/home/toothless-agent/.local/share XDG_STATE_HOME=/home/toothless-agent/.local/state XDG_CACHE_HOME=/home/toothless-agent/.cache OPENCODE_CONFIG=/etc/toothless/opencode.json /bin/sh -c 'cd /home/toothless-agent/workspace && exec /usr/local/bin/opencode auth login opencode-go --method key'"
printf 'OpenCode authentication command completed. Credentials remain guest-local under /home/toothless-agent/.local/share/opencode. The OpenCode shell tools can access that guest credential because they run as the same guest identity.\n'
