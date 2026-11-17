#!/usr/bin/env bash

set -euo pipefail

interface="enp0s1"
gateway=""
for _ in $(seq 1 60); do
  gateway="$(ip -4 route show default dev "$interface" | awk '$1 == "default" {print $3; exit}')"
  if [[ -n "$gateway" ]] && python3 -c 'import ipaddress, sys; ipaddress.IPv4Address(sys.argv[1])' "$gateway"; then
    break
  fi
  gateway=""
  sleep 1
done
[[ -n "$gateway" ]] || { printf 'Softnet IPv4 gateway did not become available.\n' >&2; exit 1; }

resolvectl dns "$interface" "$gateway:55353"
resolvectl domain "$interface" '~.'
resolvectl flush-caches
resolvectl status "$interface" | grep -F "$gateway:55353" >/dev/null
printf 'Trusted DNS relay configured: %s:55353\n' "$gateway"
