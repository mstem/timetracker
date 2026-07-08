#!/bin/bash
# Alerts via Telegram if the tracker process is running but hasn't logged
# anything in a while (e.g. it lost AppleScript/Accessibility permission and
# is silently spinning). A process-alive check alone would miss this.
# Installed as com.terminaltracker.watchdog-health (runs hourly) — see setup.sh.
#
# Only alerts during the day (9am-8pm) since no log activity overnight or on
# weekends is expected, not a failure.

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TODAY_LOG="$DIR/logs/$(date +%Y-%m-%d).json"

HOUR=$(date +%H)
if [ "$HOUR" -lt 9 ] || [ "$HOUR" -ge 20 ]; then
  exit 0
fi

if [ ! -f "$TODAY_LOG" ]; then
  # No log file yet today — only worth flagging later in the day.
  if [ "$HOUR" -ge 12 ]; then
    "$DIR/notify_telegram.sh" "🟡 Timetracker: no log file for today yet ($TODAY_LOG missing). It may be stuck or lost permissions."
  fi
  exit 0
fi

LAST_MODIFIED=$(stat -f %m "$TODAY_LOG")
NOW=$(date +%s)
AGE_MIN=$(( (NOW - LAST_MODIFIED) / 60 ))

if [ "$AGE_MIN" -gt 90 ]; then
  "$DIR/notify_telegram.sh" "🟡 Timetracker: today's log hasn't updated in ${AGE_MIN} minutes. It may be stuck (e.g. lost Accessibility/AppleScript permission)."
fi
