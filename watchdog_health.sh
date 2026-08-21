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

# The tracker can't log while the machine is asleep, and launchd runs the whole
# missed schedule the moment it wakes — so an overnight sleep looks exactly like
# a stuck tracker. Only count time the machine was actually awake.
LAST_WAKE=$(pmset -g log 2>/dev/null | awk '$4 == "Wake" {ts = $1 " " $2} END {print ts}')
if [ -n "$LAST_WAKE" ]; then
  WAKE_TS=$(date -j -f "%Y-%m-%d %H:%M:%S" "$LAST_WAKE" +%s 2>/dev/null)
  if [ -n "$WAKE_TS" ]; then
    AWAKE_MIN=$(( (NOW - WAKE_TS) / 60 ))
    if [ "$AWAKE_MIN" -lt "$AGE_MIN" ]; then
      AGE_MIN=$AWAKE_MIN
    fi
  fi
fi

# Same problem for idle time while awake: since the tracker stopped billing
# idle, a long stretch away from the keyboard writes nothing to the log and
# looks exactly like a stuck tracker. Discount however long input has been idle.
IDLE_SEC=$(ioreg -c IOHIDSystem 2>/dev/null | awk '/HIDIdleTime/ {print int($NF/1000000000); exit}')
if [ -n "$IDLE_SEC" ]; then
  IDLE_MIN=$(( IDLE_SEC / 60 ))
  AGE_MIN=$(( AGE_MIN - IDLE_MIN ))
  [ "$AGE_MIN" -lt 0 ] && AGE_MIN=0
fi

if [ "$AGE_MIN" -gt 90 ]; then
  "$DIR/notify_telegram.sh" "🟡 Timetracker: today's log hasn't updated in ${AGE_MIN} minutes. It may be stuck (e.g. lost Accessibility/AppleScript permission)."
fi
