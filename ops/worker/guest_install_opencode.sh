#!/usr/bin/env bash

set -euo pipefail
trap 'status=$?; printf "OpenCode guest install failed at line %s.\n" "$LINENO" >&2; exit "$status"' ERR

version='2.3.0'
asset='opencode-linux-arm64.tar.gz'
config_sha256='85f4457e7ed330262e3797bceef759b68938042dc757977f764365fa5aa20eff'
binary_sha256='5c7d89bc2d5fa5d83cc7768b813ca671368e70b8b9eda9d1ca9e6ec6b66d8ce0'
release="https://github.com/anomalyco/opencode/releases/download/v${version}"
source_dir="${1:?staged source directory is required}"
tmpdir="$(mktemp -d /tmp/toothless-opencode.XXXXXX)"
trap 'rm -rf "$tmpdir"' EXIT

[[ "$(uname -m)" == 'aarch64' ]]
grep -q '^ID=ubuntu$' /etc/os-release
if [[ -x /usr/local/bin/opencode ]]; then
  installed="$(/usr/local/bin/opencode --version 2>/dev/null || true)"
  [[ "$installed" =~ (^|[^0-9])2[.]3[.]0([^0-9]|$) ]] || {
    printf 'An unexpected OpenCode version is already installed; refusing replacement.\n' >&2
    exit 1
  }
  [[ "$(sha256sum /usr/local/bin/opencode | awk '{print $1}')" == "$binary_sha256" ]] || {
    printf 'The installed OpenCode binary hash differs from the reviewed build; refusing replacement.\n' >&2
    exit 1
  }
else
  command -v curl >/dev/null
  command -v tar >/dev/null
  command -v sha256sum >/dev/null
  curl --fail --location --silent --show-error "$release/SHA256SUMS" -o "$tmpdir/SHA256SUMS"
  curl --fail --location --silent --show-error "$release/$asset" -o "$tmpdir/$asset"
  expected="$(awk -v name="$asset" '$2 == name { print $1 }' "$tmpdir/SHA256SUMS")"
  [[ "$expected" =~ ^[0-9a-f]{64}$ ]]
  printf '%s  %s\n' "$expected" "$tmpdir/$asset" | sha256sum --check --status
  tar -tzf "$tmpdir/$asset" | awk '$0 == "opencode" { found = 1 } END { exit !found }'
  tar -xzf "$tmpdir/$asset" -C "$tmpdir" opencode
  actual_version="$("$tmpdir/opencode" --version 2>&1 || true)"
  [[ "$actual_version" =~ (^|[^0-9])2[.]3[.]0([^0-9]|$) ]] || {
    printf 'Downloaded OpenCode reported an unexpected version: %s\n' "$actual_version" >&2
    exit 1
  }
  [[ "$(sha256sum "$tmpdir/opencode" | awk '{print $1}')" == "$binary_sha256" ]] || {
    printf 'The downloaded OpenCode binary hash differs from the reviewed build.\n' >&2
    exit 1
  }
  install -o root -g root -m 0755 "$tmpdir/opencode" /usr/local/bin/opencode
fi

install -d -o root -g root -m 0755 /etc/toothless
install -d -o root -g root -m 0555 /home/admin/opencode-task-workspace
install -d -o admin -g admin -m 0700 /home/admin/.toothless-opencode-config /home/admin/.toothless-opencode-data /home/admin/.toothless-opencode-state /home/admin/.toothless-opencode-cache
install -o root -g root -m 0755 "$source_dir/opencode_task_runner.py" /usr/local/libexec/toothless/opencode-task-runner
install -o root -g root -m 0644 "$source_dir/opencode.json" /etc/toothless/opencode.json
[[ "$(sha256sum /etc/toothless/opencode.json | awk '{print $1}')" == "$config_sha256" ]]
[[ "$(sha256sum /usr/local/bin/opencode | awk '{print $1}')" == "$binary_sha256" ]]
runuser -u admin -- env HOME=/home/admin XDG_CONFIG_HOME=/home/admin/.toothless-opencode-config XDG_DATA_HOME=/home/admin/.toothless-opencode-data XDG_STATE_HOME=/home/admin/.toothless-opencode-state XDG_CACHE_HOME=/home/admin/.toothless-opencode-cache OPENCODE_CONFIG=/etc/toothless/opencode.json /usr/bin/python3 /usr/local/libexec/toothless/opencode-task-runner --verify-config
printf 'OpenCode CLI binary SHA-256: %s\n' "$(sha256sum /usr/local/bin/opencode | awk '{print $1}')"
printf 'OpenCode CLI %s installed; task runner and deny-all config verified.\n' "$version"
