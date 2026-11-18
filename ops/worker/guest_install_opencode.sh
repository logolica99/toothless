#!/usr/bin/env bash

set -euo pipefail
trap 'status=$?; printf "OpenCode guest install failed at line %s.\n" "$LINENO" >&2; exit "$status"' ERR

version='2.3.0'
asset='opencode-linux-arm64.tar.gz'
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
if ! getent group toothless-agent >/dev/null; then
  groupadd --system toothless-agent
fi
if ! id toothless-agent >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /home/toothless-agent --shell /bin/bash --gid toothless-agent toothless-agent
fi
[[ "$(getent passwd toothless-agent | cut -d: -f6)" == '/home/toothless-agent' ]]
[[ "$(id -gn toothless-agent)" == 'toothless-agent' ]]
getent group admin >/dev/null || {
  printf 'The existing worker admin group is unavailable; refusing the shared artifact path.\n' >&2
  exit 1
}
if id -nG toothless-agent | tr ' ' '\n' | grep -Eq '^(sudo|adm|docker|admin|root)$'; then
  printf 'The OpenCode account belongs to a privileged or management group; refusing installation.\n' >&2
  exit 1
fi
install -d -o toothless-agent -g admin -m 0710 /home/toothless-agent
install -d -o toothless-agent -g toothless-agent -m 0700 \
  /home/toothless-agent/.config \
  /home/toothless-agent/.local/share /home/toothless-agent/.local/state \
  /home/toothless-agent/.cache
install -d -o toothless-agent -g admin -m 2770 /home/toothless-agent/workspace
if runuser -u toothless-agent -- test -r /home/admin/.toothless-opencode-data/opencode; then
  printf 'The OpenCode account can read the prior admin credential store; refusing installation.\n' >&2
  exit 1
fi
install -o root -g root -m 0755 "$source_dir/opencode_task_runner.py" /usr/local/libexec/toothless/opencode-task-runner
install -o root -g root -m 0644 "$source_dir/opencode.json" /etc/toothless/opencode.json
install -o root -g root -m 0755 "$source_dir/browser_mcp.py" /usr/local/libexec/toothless/browser_mcp.py
install -o root -g root -m 0755 "$source_dir/browser_mcp_bridge.py" /usr/local/libexec/toothless/browser_mcp_bridge.py
install -o root -g root -m 0644 "$source_dir/browser.json" /etc/toothless/browser.json
install -o root -g root -m 0644 "$source_dir/browser-network-policy.json" /etc/toothless/browser-network-policy.json
install -o root -g root -m 0644 "$source_dir/browser-control-network-policy.json" /etc/toothless/browser-control-network-policy.json
config_sha256="$(sha256sum "$source_dir/opencode.json" | awk '{print $1}')"
[[ "$(sha256sum /etc/toothless/opencode.json | awk '{print $1}')" == "$config_sha256" ]]
[[ "$(sha256sum /usr/local/bin/opencode | awk '{print $1}')" == "$binary_sha256" ]]
runuser -u toothless-agent -- env HOME=/home/toothless-agent XDG_CONFIG_HOME=/home/toothless-agent/.config XDG_DATA_HOME=/home/toothless-agent/.local/share XDG_STATE_HOME=/home/toothless-agent/.local/state XDG_CACHE_HOME=/home/toothless-agent/.cache OPENCODE_CONFIG=/etc/toothless/opencode.json /usr/local/bin/opencode service set port 49375
/usr/bin/python3 /usr/local/libexec/toothless/opencode-task-runner --verify-config
/usr/bin/python3 /usr/local/libexec/toothless/opencode-task-runner --verify-browser-config
[[ -x /home/toothless-agent/.local/share/toothless/browser-smoke/venv/bin/python ]]
[[ -x /home/toothless-agent/.local/share/toothless/browser-smoke/chromium/chrome-headless-shell-linux-arm64/chrome-headless-shell ]]
printf 'OpenCode CLI binary SHA-256: %s\n' "$(sha256sum /usr/local/bin/opencode | awk '{print $1}')"
printf 'OpenCode CLI %s installed; browser-only config, Playwright, and Chromium paths verified.\n' "$version"
