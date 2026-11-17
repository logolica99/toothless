#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/config.sh"
"$script_dir/preflight.sh"

worker_ip="$(tart ip "$WORKER_VM_NAME")"
guest_host_key="$(tart exec "$WORKER_VM_NAME" sudo -n cat /etc/ssh/ssh_host_ed25519_key.pub)"
[[ "$guest_host_key" == ssh-ed25519\ * ]] || { printf 'Could not obtain guest host key from Tart agent.\n' >&2; exit 1; }
known_hosts="$(mktemp "${TMPDIR:-/tmp}/toothless-dns-known-hosts.XXXXXX")"
trap 'rm -f "$known_hosts"' EXIT
printf '%s %s\n' "$worker_ip" "$guest_host_key" > "$known_hosts"
chmod 600 "$known_hosts"
ssh_options=(-i "$HOME/.ssh/id_ed25519" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$known_hosts")

install_remote() {
  local source_file="$1" destination="$2" mode="$3" encoded
  encoded="$(base64 < "$source_file" | tr -d '\n')"
  ssh "${ssh_options[@]}" "admin@$worker_ip" \
    "printf '%s' '$encoded' | base64 -d | sudo tee /tmp/toothless-dns-install >/dev/null && sudo install -o root -g root -m '$mode' /tmp/toothless-dns-install '$destination' && sudo rm -f /tmp/toothless-dns-install"
}

ssh "${ssh_options[@]}" "admin@$worker_ip" 'sudo install -d -o root -g root -m 0755 /usr/local/libexec/toothless /etc/systemd/system /etc/netplan'
install_remote "$script_dir/configure_dns_client.sh" /usr/local/libexec/toothless/configure-dns-client 0755
install_remote "$script_dir/toothless-dns-configure.service" /etc/systemd/system/toothless-dns-configure.service 0644
install_remote "$script_dir/toothless-netplan-dns.yaml" /etc/netplan/99-toothless-dns.yaml 0600
ssh "${ssh_options[@]}" "admin@$worker_ip" \
  'sudo netplan generate && sudo systemctl daemon-reload && sudo systemctl enable toothless-dns-configure.service && sudo systemctl restart systemd-resolved && sudo /usr/local/libexec/toothless/configure-dns-client && resolvectl status enp0s1'
printf 'Trusted DNS client installed. Gracefully stop and seal the baseline before reset testing.\n'
