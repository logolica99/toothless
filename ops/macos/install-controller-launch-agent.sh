#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python_path="$repo_root/.venv/bin/python"
env_file="$HOME/.config/toothless/telegram.env"
label="com.toothless.telegram-controller"
agent_dir="$HOME/Library/LaunchAgents"
state_dir="$HOME/.local/share/toothless/telegram"
plist_path="$agent_dir/$label.plist"
uid="$(id -u)"

[[ -x "$python_path" ]] || { printf 'Missing documented project interpreter: %s\n' "$python_path" >&2; exit 1; }
[[ -f "$env_file" && ! -L "$env_file" ]] || { printf 'Missing protected Telegram configuration: %s\n' "$env_file" >&2; exit 1; }
mode="$(stat -f '%Lp' "$env_file")"
[[ "$mode" == '600' ]] || { printf 'Telegram configuration must have mode 600.\n' >&2; exit 1; }
existing_controller="$(pgrep -f '[n]anobot.personal_agent.telegram_controller' || true)"
[[ -z "$existing_controller" ]] || {
  printf 'A manual Toothless controller is already running. Stop it with Ctrl+C, then rerun this installer.\n' >&2
  exit 1
}
mkdir -p "$agent_dir" "$state_dir/logs"
chmod 700 "$agent_dir" "$state_dir" "$state_dir/logs"

python3 - "$plist_path" "$repo_root" "$python_path" "$env_file" "$state_dir" <<'PY'
import os
import plistlib
import sys
import tempfile

destination, repo, python, env_file, state = sys.argv[1:]
label = "com.toothless.telegram-controller"
document = {
    "Label": label,
    "ProgramArguments": [python, "-m", "nanobot.personal_agent.telegram_controller"],
    "WorkingDirectory": repo,
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 30,
    "ProcessType": "Background",
    "Umask": 63,
    "LimitLoadToSessionType": "Aqua",
    "EnvironmentVariables": {
        "HOME": os.path.expanduser("~"),
        "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
        "PYTHONUNBUFFERED": "1",
        "TOOTHLESS_TELEGRAM_ENV_FILE": env_file,
        "TOOTHLESS_LOG_DIR": os.path.join(state, "logs"),
    },
    "StandardOutPath": "/dev/null",
    "StandardErrorPath": "/dev/null",
}
fd, temporary = tempfile.mkstemp(prefix=".toothless-launch-agent.", dir=os.path.dirname(destination))
try:
    with os.fdopen(fd, "wb") as stream:
        plistlib.dump(document, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, destination)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY

launchctl bootout "gui/$uid" "$plist_path" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$uid" "$plist_path"
launchctl kickstart "gui/$uid/$label"
printf 'Installed and started %s for the current user. Inspect with: launchctl print gui/%s/%s\n' "$label" "$uid" "$label"
