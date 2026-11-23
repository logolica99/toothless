#!/usr/bin/env bash

set -euo pipefail

source_dir="${1:?temporary reviewed deployment directory is required}"
[[ -d "$source_dir" && ! -L "$source_dir" ]]
[[ "$(/usr/bin/id -u)" -eq 0 ]]

if /usr/bin/systemctl is-active --quiet toothless-desktop.service; then
  printf 'The desktop service is active; close its viewer and stop it before deploying the broker.\n' >&2
  exit 1
fi

/usr/bin/getent passwd toothless-desktop >/dev/null
/usr/bin/getent passwd toothless-agent >/dev/null
if ! /usr/bin/getent group toothless-browser >/dev/null; then
  /usr/sbin/groupadd --system toothless-browser
fi
group_members="$(/usr/bin/getent group toothless-browser | /usr/bin/awk -F: '{print $4}')"
if [[ -n "$group_members" ]]; then
  while IFS= read -r member; do
    [[ "$member" == toothless-agent || "$member" == toothless-desktop ]]
  done < <(printf '%s' "$group_members" | tr ',' '\n')
fi
/usr/sbin/usermod -aG toothless-browser toothless-desktop
/usr/sbin/usermod -aG toothless-browser toothless-agent
group_members="$(/usr/bin/getent group toothless-browser | /usr/bin/awk -F: '{print $4}')"
expected_members="$(printf '%s\n' toothless-agent toothless-desktop | /usr/bin/sort | /usr/bin/paste -sd, -)"
[[ "$(printf '%s' "$group_members" | tr ',' '\n' | sort | paste -sd, -)" == "$expected_members" ]]

runtime_source=/home/toothless-agent/.local/share/toothless/browser-smoke/venv
[[ -x "$runtime_source/bin/python" && -d "$runtime_source/lib/python3.12/site-packages/playwright" ]]
if [[ -e /opt/toothless/visible-browser-runtime || -L /opt/toothless/visible-browser-runtime ]]; then
  [[ -d /opt/toothless/visible-browser-runtime && ! -L /opt/toothless/visible-browser-runtime ]]
  [[ "$(/usr/bin/stat -c '%u:%g' /opt/toothless/visible-browser-runtime)" == 0:0 ]]
  /opt/toothless/visible-browser-runtime/bin/python -c 'import importlib.metadata as m; assert m.version("playwright")=="1.65.2"'
else
  /usr/bin/install -d -o root -g root -m 0755 /opt/toothless/visible-browser-runtime
  /usr/bin/cp -a "$runtime_source/." /opt/toothless/visible-browser-runtime/
  /usr/bin/chown -R root:root /opt/toothless/visible-browser-runtime
  /usr/bin/find /opt/toothless/visible-browser-runtime -type d -exec /usr/bin/chmod 0755 {} +
  /usr/bin/chmod -R go-w /opt/toothless/visible-browser-runtime
fi
/opt/toothless/visible-browser-runtime/bin/python -c 'import importlib.metadata as m; assert m.version("playwright")=="1.65.2"'
/usr/bin/install -d -o root -g root -m 0755 /usr/local/libexec/toothless
/usr/bin/install -d -o root -g root -m 0755 /etc/tmpfiles.d /etc/systemd/system
/usr/bin/install -o root -g root -m 0644 "$source_dir/toothless-browser-authorizations.conf" /etc/tmpfiles.d/toothless-browser-authorizations.conf
/usr/bin/install -o root -g root -m 0644 "$source_dir/toothless-browser-auth-dir.service" /etc/systemd/system/toothless-browser-auth-dir.service
/usr/bin/install -o root -g root -m 0755 "$source_dir/visible_browser_broker.py" /usr/local/libexec/toothless/visible_browser_broker.py
/usr/bin/install -o root -g root -m 0755 "$source_dir/browser_mcp.py" /usr/local/libexec/toothless/browser_mcp.py
/usr/bin/install -o root -g root -m 0755 "$source_dir/opencode_task_runner.py" /usr/local/libexec/toothless/opencode-task-runner
/usr/bin/install -o root -g root -m 0755 "$source_dir/toothless-desktop-session.sh" /usr/local/libexec/toothless/desktop-session
/usr/bin/install -o root -g root -m 0644 "$source_dir/toothless-desktop.service" /etc/systemd/system/toothless-desktop.service
/usr/bin/systemd-analyze verify /etc/systemd/system/toothless-browser-auth-dir.service /etc/systemd/system/toothless-desktop.service
/usr/bin/systemctl daemon-reload
/usr/bin/systemctl start toothless-browser-auth-dir.service
/usr/bin/python3 -c 'import grp,os,stat; p="/run/toothless-browser-authorizations"; s=os.lstat(p); assert stat.S_ISDIR(s.st_mode) and s.st_uid==0 and s.st_gid==grp.getgrnam("toothless-browser").gr_gid and stat.S_IMODE(s.st_mode)==0o750'
/usr/bin/systemctl start toothless-desktop.service

for _ in $(/usr/bin/seq 1 60); do
  if /usr/bin/systemctl is-active --quiet toothless-desktop.service \
      && [[ -S /run/toothless-desktop/visible-browser.sock ]]; then
    /usr/bin/python3 -c 'import grp,os,stat; p="/run/toothless-desktop/visible-browser.sock"; s=os.stat(p); assert stat.S_ISSOCK(s.st_mode) and s.st_gid==grp.getgrnam("toothless-browser").gr_gid and stat.S_IMODE(s.st_mode)==0o660'
    printf 'Visible browser broker is ready; browser remains closed until an owner-approved visible task.\n'
    exit 0
  fi
  /usr/bin/sleep 1
done
/usr/bin/systemctl status --no-pager toothless-desktop.service >&2 || true
printf 'Visible browser broker did not become ready; no task was dispatched.\n' >&2
exit 1
