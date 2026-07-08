#!/bin/bash
# Alerts via Telegram if the main tracker launchd job isn't loaded/running.
# Installed as com.terminaltracker.watchdog (runs every 10 min) — see setup.sh.

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UID_NUM=$(id -u)

STATUS=$(launchctl print "gui/$UID_NUM/com.terminaltracker" 2>&1)

if ! echo "$STATUS" | grep -q "state = running"; then
  "$DIR/notify_telegram.sh" "🔴 Timetracker is NOT running. Check: launchctl print gui/$UID_NUM/com.terminaltracker"
fi
