#!/bin/bash
# Alerts via Telegram if the tracker process is running but hasn't logged
# anything in a while (e.g. it lost AppleScript/Accessibility permission and
# is silently spinning). A process-alive check alone would miss this.
# Installed as com.terminaltracker.watchdog-health (runs hourly) — see setup.sh.
#
# An empty or stale log is only a fault if someone is using the Mac. The Mac
# never sleeps (caffeinate keeps it awake), so system wake time says nothing
# about use. The signals that do:
#   - the display is on (it turns off after `pmset displaysleep` minutes idle)
#   - HID input within the last ACTIVE_WINDOW_MIN minutes
# The clock for "should have logged by now" starts at whichever is latest:
# the last log write, the moment the display last came on, or the last wake.
# Only alerts during the day (9am-8pm).
#
# Every machine input has an env override so tests can drive the script:
#   WATCHDOG_NOW, WATCHDOG_IDLE_SEC, WATCHDOG_PMSET_LOG (file),
#   WATCHDOG_LOG_DIR, WATCHDOG_NOTIFY

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${WATCHDOG_LOG_DIR:-$DIR/logs}"
NOTIFY="${WATCHDOG_NOTIFY:-$DIR/notify_telegram.sh}"
NOW="${WATCHDOG_NOW:-$(date +%s)}"

STALE_MIN=90          # in-use minutes with no log write before alerting
ACTIVE_WINDOW_MIN=10  # input this recent means someone is at the Mac

TODAY=$(date -r "$NOW" +%Y-%m-%d)
TODAY_LOG="$LOG_DIR/$TODAY.json"
HOUR=$(date -r "$NOW" +%H)
HOUR=$((10#$HOUR))
if [ "$HOUR" -lt 9 ] || [ "$HOUR" -ge 20 ]; then
  exit 0
fi

# Nobody at the keyboard right now: nothing to log, nothing to alert about.
if [ -n "${WATCHDOG_IDLE_SEC+x}" ]; then
  IDLE_SEC="$WATCHDOG_IDLE_SEC"
else
  IDLE_SEC=$(ioreg -c IOHIDSystem 2>/dev/null | awk '/HIDIdleTime/ {print int($NF/1000000000); exit}')
fi
if [ -z "$IDLE_SEC" ]; then
  # The idle probe is what the tracker itself depends on; if it is unreadable
  # we cannot tell use from absence, so stay quiet rather than guess.
  exit 0
fi
IDLE_MIN=$(( IDLE_SEC / 60 ))
if [ "$IDLE_MIN" -ge "$ACTIVE_WINDOW_MIN" ]; then
  exit 0
fi

if [ -n "$WATCHDOG_PMSET_LOG" ]; then
  PMSET_LOG=$(cat "$WATCHDOG_PMSET_LOG" 2>/dev/null)
else
  PMSET_LOG=$(pmset -g log 2>/dev/null)
fi

to_epoch() {  # "YYYY-MM-DD HH:MM:SS +ZZZZ" -> epoch seconds
  date -j -f "%Y-%m-%d %H:%M:%S %z" "$1" +%s 2>/dev/null
}

# Display state and the moment the current on-stretch began.
LAST_DISPLAY=$(printf '%s\n' "$PMSET_LOG" | awk '/Display is turned (on|off)/ {ts = $1 " " $2 " " $3; st = $NF} END {if (ts) print st "|" ts}')
if [ "${LAST_DISPLAY%%|*}" = "off" ]; then
  exit 0
fi
START_TS=$(date -j -f "%Y-%m-%d %H:%M:%S" "$TODAY 00:00:00" +%s)

DISPLAY_ON_TS=""
[ -n "$LAST_DISPLAY" ] && DISPLAY_ON_TS=$(to_epoch "${LAST_DISPLAY#*|}")
[ -n "$DISPLAY_ON_TS" ] && [ "$DISPLAY_ON_TS" -gt "$START_TS" ] && START_TS=$DISPLAY_ON_TS

# launchd runs a missed schedule on wake, so a sleep still must not count.
LAST_WAKE=$(printf '%s\n' "$PMSET_LOG" | awk '$4 == "Wake" {ts = $1 " " $2 " " $3} END {print ts}')
WAKE_TS=""
[ -n "$LAST_WAKE" ] && WAKE_TS=$(to_epoch "$LAST_WAKE")
[ -n "$WAKE_TS" ] && [ "$WAKE_TS" -gt "$START_TS" ] && START_TS=$WAKE_TS

if [ -f "$TODAY_LOG" ]; then
  LAST_MODIFIED=$(stat -f %m "$TODAY_LOG")
  [ "$LAST_MODIFIED" -gt "$START_TS" ] && START_TS=$LAST_MODIFIED
fi

# Since the tracker stopped billing idle, the current idle stretch writes
# nothing either; don't count it.
AGE_MIN=$(( (NOW - START_TS) / 60 - IDLE_MIN ))
[ "$AGE_MIN" -lt 0 ] && AGE_MIN=0

if [ "$AGE_MIN" -le "$STALE_MIN" ]; then
  exit 0
fi

if [ ! -f "$TODAY_LOG" ]; then
  "$NOTIFY" "🟡 Timetracker: no log file for today after ${AGE_MIN} minutes of Mac use ($TODAY_LOG missing). It may be stuck or lost permissions."
else
  "$NOTIFY" "🟡 Timetracker: today's log hasn't updated in ${AGE_MIN} minutes of Mac use. It may be stuck (e.g. lost Accessibility/AppleScript permission)."
fi
