#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/config.sh"
"$script_dir/preflight.sh"

public_key="${1:-$HOME/.ssh/id_ed25519.pub}"
[[ -f "$public_key" && ! -L "$public_key" ]] || {
  printf 'Public key must be a regular, non-symlink file: %s\n' "$public_key" >&2
  exit 1
}

key_type="$(awk 'NR == 1 {print $1}' "$public_key")"
case "$key_type" in
  ssh-ed25519|sk-ssh-ed25519@openssh.com) ;;
  *) printf 'Use an Ed25519 SSH public key; found %s.\n' "$key_type" >&2; exit 1 ;;
esac

private_key="${public_key%.pub}"
[[ -f "$private_key" && ! -L "$private_key" ]] || {
  printf 'Matching private key is unavailable: %s\n' "$private_key" >&2
  exit 1
}

public_key_b64="$(base64 < "$public_key" | tr -d '\n')"

worker_ip="$(tart ip "$WORKER_VM_NAME")"
printf 'Installing %s for admin@%s. Enter the temporary vendor password when prompted.\n' "$public_key" "$worker_ip"
ssh-copy-id -i "$public_key" -o IdentitiesOnly=yes "admin@$worker_ip"

ssh -o BatchMode=yes -o IdentitiesOnly=yes -i "$private_key" "admin@$worker_ip" true

printf 'Installing the persistent key path and disabling password authentication.\n'
ssh -o IdentitiesOnly=yes -i "$private_key" "admin@$worker_ip" \
  "printf '%s' '$public_key_b64' | base64 -d | sudo tee /tmp/toothless-admin-key >/dev/null && sudo sh -c 'install -d -o root -g root -m 0755 /etc/ssh/authorized_keys; install -o root -g root -m 0644 /tmp/toothless-admin-key /etc/ssh/authorized_keys/admin; rm -f /tmp/toothless-admin-key; rm -f /etc/ssh/sshd_config.d/99-toothless-hardening.conf; umask 022; printf \"AuthorizedKeysFile /etc/ssh/authorized_keys/admin\\nPasswordAuthentication no\\nKbdInteractiveAuthentication no\\nPermitRootLogin no\\nAllowAgentForwarding no\\nAllowTcpForwarding no\\nX11Forwarding no\\n\" > /etc/ssh/sshd_config.d/00-toothless-hardening.conf; sshd -t; systemctl reload ssh'"

# Prove a fresh key-only session through the persistent path before locking the password.
ssh -o BatchMode=yes -o IdentitiesOnly=yes -i "$private_key" "admin@$worker_ip" \
  "sudo sshd -T | grep -E '^(authorizedkeysfile /etc/ssh/authorized_keys/admin|passwordauthentication no|kbdinteractiveauthentication no|permitrootlogin no|allowagentforwarding no|allowtcpforwarding no|x11forwarding no)$'; sudo test \"\$(cat /etc/ssh/authorized_keys/admin)\" = \"\$(cat ~/.ssh/authorized_keys)\"; sudo passwd -l admin; sudo passwd -S admin | grep '^admin L '; sudo sync"

printf 'Worker SSH hardening verified. Stop and seal the baseline next.\n'
