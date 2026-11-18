#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/config.sh"
"$script_dir/preflight.sh"

worker_state="$(tart get "$WORKER_VM_NAME" | awk 'NR == 2 { print tolower($NF) }')"
[[ "$worker_state" == 'running' ]] || {
  printf 'Start the worker with ops/worker/start.sh before updating its OpenCode runner.\n' >&2
  exit 1
}
worker_ip="$(tart ip "$WORKER_VM_NAME")"
[[ "$worker_ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]
guest_host_key="$(tart exec "$WORKER_VM_NAME" sudo -n cat /etc/ssh/ssh_host_ed25519_key.pub)"
[[ "$guest_host_key" == ssh-ed25519\ * ]] || {
  printf 'Could not obtain the guest host key from the trusted Tart guest agent.\n' >&2
  exit 1
}
known_hosts="$(mktemp "${TMPDIR:-/tmp}/toothless-runner-known-hosts.XXXXXX")"
remote_dir="/tmp/toothless-opencode-runner-$(/usr/bin/uuidgen | tr '[:upper:]' '[:lower:]')"
expected_hash="$(shasum -a 256 "$script_dir/opencode_task_runner.py" | awk '{print $1}')"
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
ssh "${ssh_options[@]}" "admin@$worker_ip" "tee '$remote_dir/opencode_task_runner.py' >/dev/null" < "$script_dir/opencode_task_runner.py"
actual_hash="$(ssh "${ssh_options[@]}" "admin@$worker_ip" "sha256sum '$remote_dir/opencode_task_runner.py' | awk '{print \$1}'" | tr -d '\r')"
[[ "$actual_hash" == "$expected_hash" ]] || {
  printf 'Staged OpenCode runner hash verification failed.\n' >&2
  exit 1
}
ssh "${ssh_options[@]}" "admin@$worker_ip" \
  "sudo -n install -o root -g root -m 0755 -- '$remote_dir/opencode_task_runner.py' /usr/local/libexec/toothless/opencode-task-runner"
installed_hash="$(ssh "${ssh_options[@]}" "admin@$worker_ip" "sudo -n sha256sum /usr/local/libexec/toothless/opencode-task-runner | awk '{print \$1}'" | tr -d '\r')"
[[ "$installed_hash" == "$expected_hash" ]] || {
  printf 'Installed OpenCode runner hash verification failed.\n' >&2
  exit 1
}
ssh "${ssh_options[@]}" "admin@$worker_ip" \
  'sudo -n /usr/bin/python3 /usr/local/libexec/toothless/opencode-task-runner --verify-config && sudo -n /usr/bin/python3 /usr/local/libexec/toothless/opencode-task-runner --verify-browser-config'
printf 'Installed the reviewed OpenCode runner update over pinned-key SSH. SHA-256: %s\n' "$expected_hash"
