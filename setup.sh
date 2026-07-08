#!/bin/bash
# Sets up the terminal time tracker as a launchd agent (auto-starts on login).

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_NAME="com.terminaltracker"
PLIST_SRC="$SCRIPT_DIR/$PLIST_NAME.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"

echo "==> Setting up Timetracker"
echo "    Project dir: $SCRIPT_DIR"

# Write plist with correct absolute paths
cat > "$PLIST_SRC" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_NAME</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>$SCRIPT_DIR/tracker.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$SCRIPT_DIR/launchd_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$SCRIPT_DIR/launchd_stderr.log</string>
</dict>
</plist>
EOF

echo "==> Written $PLIST_SRC"

# Install into LaunchAgents
cp "$PLIST_SRC" "$PLIST_DST"
echo "==> Installed to $PLIST_DST"

# Unload if already loaded (ignore errors)
launchctl unload "$PLIST_DST" 2>/dev/null || true

# Load
launchctl load "$PLIST_DST"
echo "==> Loaded launchd agent — tracker is now running."
echo ""
echo "Useful commands:"
echo "  Stop:    launchctl unload ~/Library/LaunchAgents/$PLIST_NAME.plist"
echo "  Start:   launchctl load   ~/Library/LaunchAgents/$PLIST_NAME.plist"
echo "  Logs:    tail -f $SCRIPT_DIR/tracker.log"
echo "  Send now: python3 $SCRIPT_DIR/tracker.py --send-today"

# ---------------------------------------------------------------------------
# AI project classification now runs automatically right before every send
# (see ai_matcher.classify_and_save, called from tracker.py's send_to_clockify).
# No separate weekly launchd agent is needed. ai_matcher.py's CLI is still
# available for manual review/corrections:
#   python3 ai_matcher.py                 # classify unmapped activity now
#   python3 ai_matcher.py --dry-run       # preview without saving
#   python3 ai_matcher.py --interactive   # confirm/override each match
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Optional: watchdogs that alert via Telegram if the tracker stops working
# ---------------------------------------------------------------------------
echo ""
echo "==> Checking for Telegram watchdog setup (optional)"

if python3 -c "
import json, sys
try:
    c = json.load(open('$SCRIPT_DIR/config.json'))
    token = c.get('telegram_bot_token', '')
    chat_id = c.get('telegram_chat_id', '')
    sys.exit(0 if token and chat_id and not token.startswith('<') else 1)
except Exception:
    sys.exit(1)
" 2>/dev/null; then

    WATCHDOG_PROCESS_NAME="com.terminaltracker.watchdog-process"
    WATCHDOG_PROCESS_SRC="$SCRIPT_DIR/$WATCHDOG_PROCESS_NAME.plist"
    WATCHDOG_PROCESS_DST="$HOME/Library/LaunchAgents/$WATCHDOG_PROCESS_NAME.plist"

    cat > "$WATCHDOG_PROCESS_SRC" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$WATCHDOG_PROCESS_NAME</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$SCRIPT_DIR/watchdog_process.sh</string>
    </array>
    <key>StartInterval</key>
    <integer>600</integer>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF

    cp "$WATCHDOG_PROCESS_SRC" "$WATCHDOG_PROCESS_DST"
    launchctl unload "$WATCHDOG_PROCESS_DST" 2>/dev/null || true
    launchctl load "$WATCHDOG_PROCESS_DST"

    WATCHDOG_HEALTH_NAME="com.terminaltracker.watchdog-health"
    WATCHDOG_HEALTH_SRC="$SCRIPT_DIR/$WATCHDOG_HEALTH_NAME.plist"
    WATCHDOG_HEALTH_DST="$HOME/Library/LaunchAgents/$WATCHDOG_HEALTH_NAME.plist"

    cat > "$WATCHDOG_HEALTH_SRC" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$WATCHDOG_HEALTH_NAME</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$SCRIPT_DIR/watchdog_health.sh</string>
    </array>
    <key>StartInterval</key>
    <integer>3600</integer>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF

    cp "$WATCHDOG_HEALTH_SRC" "$WATCHDOG_HEALTH_DST"
    launchctl unload "$WATCHDOG_HEALTH_DST" 2>/dev/null || true
    launchctl load "$WATCHDOG_HEALTH_DST"

    echo "==> Watchdogs installed:"
    echo "  com.terminaltracker.watchdog-process — checks every 10 min that the tracker is running"
    echo "  com.terminaltracker.watchdog-health  — checks hourly (9am-8pm) that today's log is fresh"
    echo "  Alerts are sent to your Telegram bot."
else
    echo "    telegram_bot_token/telegram_chat_id not found in config.json — skipping watchdog install."
    echo "    To enable: add them to config.json, then re-run setup.sh."
fi
