#!/usr/bin/env bash

set -euo pipefail

label="com.toothless.telegram-controller"
uid="$(id -u)"
plist="$HOME/Library/LaunchAgents/$label.plist"
launchctl bootout "gui/$uid" "$plist" >/dev/null 2>&1 || true
rm -f "$plist"
printf 'Removed the current-user Toothless LaunchAgent. Logs and databases were retained.\n'
