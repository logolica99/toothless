#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/config.sh"

failures=0
fail() {
  printf 'FAIL: %s\n' "$1" >&2
  failures=$((failures + 1))
}

[[ "$(uname -s)" == "Darwin" ]] || fail "worker host must be macOS"
[[ "$(uname -m)" == "arm64" ]] || fail "pinned worker image requires Apple silicon"

if ! command -v uv >/dev/null; then
  fail "uv is required to run the worker controller and trusted DNS forwarder"
elif ! uv run --project "$script_dir/../.." --no-sync python -c 'import dns.message' >/dev/null 2>&1; then
  fail "the project environment must contain the locked dnspython dependency"
fi

macos_major="$(sw_vers -productVersion | cut -d. -f1)"
((macos_major >= 26)) || fail "Softnet requires macOS 26 or newer"

memory_bytes="$(sysctl -n hw.memsize)"
((memory_bytes >= 16 * 1024 * 1024 * 1024)) || fail "at least 16 GiB host RAM is required for this profile"

[[ -d "$WORKER_STORAGE_VOLUME" ]] || fail "dedicated worker volume is not mounted at $WORKER_STORAGE_VOLUME"
[[ -d "$WORKER_STORAGE_ROOT" ]] || fail "worker storage root is missing at $WORKER_STORAGE_ROOT"
[[ -w "$WORKER_STORAGE_ROOT" ]] || fail "worker storage root is not writable"
volume_filesystem="$(diskutil info "$WORKER_STORAGE_VOLUME" 2>/dev/null | awk -F: '/File System Personality/ {gsub(/^[ \t]+/, "", $2); print $2}')"
[[ "$volume_filesystem" == "APFS" ]] || fail "dedicated worker volume must use APFS"

free_kb="$(df -Pk "$WORKER_STORAGE_VOLUME" | awk 'NR == 2 {print $4}')"
required_kb=$((WORKER_MIN_FREE_GB * 1024 * 1024))
((free_kb >= required_kb)) || fail "at least ${WORKER_MIN_FREE_GB} GiB free disk is required (current: $((free_kb / 1024 / 1024)) GiB)"

if ! command -v tart >/dev/null; then
  fail "Tart is not installed; run the reviewed install steps in docs/worker-vm.md"
elif [[ "$(tart --version | awk '{print $NF}')" != "$REQUIRED_TART_VERSION" ]]; then
  fail "Tart $REQUIRED_TART_VERSION is required"
fi

if ! command -v softnet >/dev/null; then
  fail "Softnet is not installed; run the reviewed install steps in docs/worker-vm.md"
else
  softnet_path="$(command -v softnet)"
  softnet_resolved="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$softnet_path")"
  [[ "$softnet_resolved" == "$REQUIRED_SOFTNET_PATH" ]] || fail "Softnet must resolve to $REQUIRED_SOFTNET_PATH"
  [[ "$(stat -f '%Su:%Sg' "$REQUIRED_SOFTNET_PATH")" == "root:wheel" ]] || fail "Softnet must be owned by root:wheel"
  [[ "$(stat -f '%Mp' "$REQUIRED_SOFTNET_PATH")" == "4" ]] || fail "Softnet must have its setuid bit enabled"
  [[ "$(shasum -a 256 "$REQUIRED_SOFTNET_PATH" | awk '{print $1}')" == "$REQUIRED_SOFTNET_SHA256" ]] || fail "Softnet hash does not match the reviewed signed payload"
  codesign --verify --strict "$REQUIRED_SOFTNET_PATH" 2>/dev/null || fail "Softnet ad-hoc signature is invalid"
  softnet_version="$(softnet --version | awk '{print $NF}')"
  [[ "$softnet_version" == "$REQUIRED_SOFTNET_VERSION"-* || "$softnet_version" == "$REQUIRED_SOFTNET_VERSION" ]] || fail "Softnet $REQUIRED_SOFTNET_VERSION is required"
fi

[[ "$WORKER_IMAGE" == *@sha256:* ]] || fail "worker image must be pinned by digest"

if ((failures > 0)); then
  printf 'Preflight blocked with %d failure(s).\n' "$failures" >&2
  exit 1
fi

printf 'Phase 01 host preflight passed.\n'
