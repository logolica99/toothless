#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
source "$script_dir/config.sh"
"$script_dir/preflight.sh"

task_db="${TOOTHLESS_TASK_DB:-$HOME/.local/share/toothless/telegram/tasks.sqlite3}"
execution_db="${TOOTHLESS_EXECUTION_DB:-$HOME/.nanobot/worker/executions.sqlite3}"
schedule_db="${TOOTHLESS_SCHEDULE_DB:-$(dirname "$task_db")/schedules.sqlite3}"
python3 - "$task_db" "$execution_db" "$schedule_db" <<'PY'
from datetime import UTC, datetime, timedelta
import sqlite3
import sys
from pathlib import Path

def connect(path_text: str) -> sqlite3.Connection:
    path = Path(path_text)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise SystemExit("Required host task/execution database is unavailable; refusing deployment.")
    return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)

with connect(sys.argv[1]) as database:
    active_tasks = database.execute(
        "SELECT task_id FROM tasks WHERE status IN ('dispatching','running','cancelling') LIMIT 1"
    ).fetchone()
with connect(sys.argv[2]) as database:
    uncertain = database.execute(
        "SELECT execution_id FROM worker_executions "
        "WHERE status IN ('running','unknown') OR dispatch_state='uncertain' LIMIT 1"
    ).fetchone()
schedule_path = Path(sys.argv[3])
if (not schedule_path.is_absolute() or schedule_path.is_symlink() or not schedule_path.is_file()
        or schedule_path.with_name(schedule_path.name + "-wal").exists()
        and schedule_path.with_name(schedule_path.name + "-wal").stat().st_size > 0):
    raise SystemExit("Schedule database is unavailable or has uncheckpointed writes; refusing deployment.")
with sqlite3.connect(f"{schedule_path.as_uri()}?immutable=1", uri=True) as database:
    active_occurrences = database.execute(
        "SELECT occurrence_id FROM schedule_occurrences WHERE status IN ('claimed','dispatching','running') LIMIT 1"
    ).fetchone()
    due_soon = database.execute(
        "SELECT schedule_id,next_run_at FROM schedules WHERE state='active' AND next_run_at IS NOT NULL"
    ).fetchall()
cutoff = datetime.now(UTC) + timedelta(minutes=2)
due_schedule = next(
    (schedule_id for schedule_id, next_run_at in due_soon
     if datetime.fromisoformat(next_run_at).astimezone(UTC) <= cutoff),
    None,
)
if active_tasks or uncertain or active_occurrences or due_schedule:
    raise SystemExit("Active or uncertain work exists; refusing desktop service deployment.")
print("No active tasks or unresolved worker executions.")
print("No active or near-due schedule occurrence.")
PY

readiness="$(cd "$repo_root" && PYTHONPATH="$repo_root" UV_CACHE_DIR=/tmp/toothless-uv-cache uv run --no-sync python -c 'import json; from nanobot.personal_agent.worker_readiness import worker_readiness; print(json.dumps(worker_readiness(),sort_keys=True))')"
[[ "$(printf '%s' "$readiness" | /usr/bin/python3 -c 'import json,sys; print("yes" if json.load(sys.stdin).get("worker_ready") is True else "no")')" == yes ]] || {
  printf 'Protected worker readiness is unavailable; no guest files were changed.\n' >&2
  exit 1
}
[[ "$(tart get "$WORKER_VM_NAME" | awk 'NR == 2 { print tolower($NF) }')" == running ]] || {
  printf 'Worker is not running under its protected launcher; refusing hotfix deployment.\n' >&2
  exit 1
}

worker_ip="$(tart ip "$WORKER_VM_NAME")"
guest_host_key="$(tart exec "$WORKER_VM_NAME" sudo -n cat /etc/ssh/ssh_host_ed25519_key.pub)"
[[ "$guest_host_key" == ssh-ed25519\ * ]] || { printf 'Guest host key is unavailable.\n' >&2; exit 1; }
known_hosts="$(mktemp "${TMPDIR:-/tmp}/toothless-browser-auth-known-hosts.XXXXXX")"
remote_dir="/tmp/toothless-browser-auth-$(/usr/bin/uuidgen | tr '[:upper:]' '[:lower:]')"
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

ssh "${ssh_options[@]}" "admin@$worker_ip" \
  'sudo -n /usr/bin/python3 -c '\''from pathlib import Path; import stat,sys; p=Path("/run/toothless-browser-authorizations"); s=p.lstat() if p.exists() or p.is_symlink() else None; s is None or (stat.S_ISDIR(s.st_mode) and not list(p.glob("opencode-*.json"))) or sys.exit("Authorization directory is unsafe or contains records; refusing deployment")'\'''
ssh "${ssh_options[@]}" "admin@$worker_ip" "mkdir -m 0700 -- '$remote_dir'"
for file in toothless-browser-authorizations.conf toothless-browser-auth-dir.service toothless-desktop.service opencode_task_runner.py visible_browser_broker.py browser_mcp.py; do
  ssh "${ssh_options[@]}" "admin@$worker_ip" "tee '$remote_dir/$file' >/dev/null" < "$script_dir/$file"
done

ssh "${ssh_options[@]}" "admin@$worker_ip" \
  "sudo -n install -d -o root -g root -m 0755 /etc/tmpfiles.d /etc/systemd/system /usr/local/libexec/toothless && \
   sudo -n install -o root -g root -m 0644 '$remote_dir/toothless-browser-authorizations.conf' /etc/tmpfiles.d/toothless-browser-authorizations.conf && \
   sudo -n install -o root -g root -m 0644 '$remote_dir/toothless-browser-auth-dir.service' /etc/systemd/system/toothless-browser-auth-dir.service && \
   sudo -n install -o root -g root -m 0644 '$remote_dir/toothless-desktop.service' /etc/systemd/system/toothless-desktop.service && \
   sudo -n install -o root -g root -m 0755 '$remote_dir/opencode_task_runner.py' /usr/local/libexec/toothless/opencode-task-runner && \
   sudo -n install -o root -g root -m 0755 '$remote_dir/visible_browser_broker.py' /usr/local/libexec/toothless/visible_browser_broker.py && \
   sudo -n install -o root -g root -m 0755 '$remote_dir/browser_mcp.py' /usr/local/libexec/toothless/browser_mcp.py && \
   sudo -n /usr/bin/systemd-analyze verify /etc/systemd/system/toothless-browser-auth-dir.service /etc/systemd/system/toothless-desktop.service && \
   sudo -n /usr/bin/systemctl daemon-reload && \
   sudo -n /usr/bin/systemctl restart toothless-desktop.service"

ssh "${ssh_options[@]}" "admin@$worker_ip" \
  'sudo -n /usr/bin/python3 -c '\''import grp,os,stat; p="/run/toothless-browser-authorizations"; s=os.lstat(p); assert stat.S_ISDIR(s.st_mode) and s.st_uid==0 and s.st_gid==grp.getgrnam("toothless-browser").gr_gid and stat.S_IMODE(s.st_mode)==0o750; assert not list(__import__("pathlib").Path(p).glob("opencode-*.json")); print("PASS: empty root-owned 0750 authorization directory created")'\'' && sudo -n /usr/bin/bash -euc '\''for attempt in $(seq 1 30); do if systemctl is-active --quiet toothless-desktop.service && test -S /run/toothless-desktop/visible-browser.sock; then exit 0; fi; sleep 1; done; echo "Desktop service did not become ready" >&2; systemctl status toothless-desktop.service --no-pager >&2; exit 1'\'''

restart_count="$(ssh "${ssh_options[@]}" "admin@$worker_ip" 'sudo -n /usr/bin/systemctl show --property=NRestarts --value toothless-desktop.service')"
sleep 2
[[ "$(ssh "${ssh_options[@]}" "admin@$worker_ip" 'sudo -n /usr/bin/systemctl is-active toothless-desktop.service')" == active ]]
[[ "$(ssh "${ssh_options[@]}" "admin@$worker_ip" 'sudo -n /usr/bin/systemctl show --property=NRestarts --value toothless-desktop.service')" == "$restart_count" ]]

ssh "${ssh_options[@]}" "admin@$worker_ip" \
  'sudo -n /usr/bin/systemctl restart toothless-desktop.service && sudo -n /usr/bin/systemctl is-active --quiet toothless-desktop.service && sudo -n /usr/bin/test -S /run/toothless-desktop/visible-browser.sock && sudo -n /usr/bin/python3 -c '\''import grp,os,stat; s=os.lstat("/run/toothless-browser-authorizations"); assert stat.S_ISDIR(s.st_mode) and s.st_uid==0 and s.st_gid==grp.getgrnam("toothless-browser").gr_gid and stat.S_IMODE(s.st_mode)==0o750'\'''

ssh "${ssh_options[@]}" "admin@$worker_ip" \
  'sudo -n /usr/bin/systemctl stop toothless-desktop.service && sudo -n /usr/bin/rmdir /run/toothless-browser-authorizations && sudo -n /usr/bin/systemctl start toothless-desktop.service && sudo -n /usr/bin/systemctl is-active --quiet toothless-desktop.service && sudo -n /usr/bin/test -S /run/toothless-desktop/visible-browser.sock && sudo -n /usr/bin/python3 -c '\''import grp,os,stat; p="/run/toothless-browser-authorizations"; s=os.lstat(p); assert stat.S_ISDIR(s.st_mode) and s.st_uid==0 and s.st_gid==grp.getgrnam("toothless-browser").gr_gid and stat.S_IMODE(s.st_mode)==0o750; assert not list(__import__("pathlib").Path(p).glob("opencode-*.json")); print("PASS: missing runtime directory recreated empty on desktop start")'\'''

printf 'Browser authorization runtime hotfix deployed and service lifecycle checks passed.\n'
