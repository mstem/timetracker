#!/bin/bash
# Sets up the terminal time tracker as a launchd agent (auto-starts on login).

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_NAME="com.terminaltracker"
PLIST_SRC="$SCRIPT_DIR/$PLIST_NAME.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"

echo "==> Setting up Terminal Time Tracker"
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
# Friday 4pm Clockify review reminder
# ---------------------------------------------------------------------------
REMINDER_PLIST_NAME="com.terminaltracker.reminder"
REMINDER_PLIST_SRC="$SCRIPT_DIR/$REMINDER_PLIST_NAME.plist"
REMINDER_PLIST_DST="$HOME/Library/LaunchAgents/$REMINDER_PLIST_NAME.plist"

cat > "$REMINDER_PLIST_SRC" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$REMINDER_PLIST_NAME</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$SCRIPT_DIR/clockify_reminder.sh</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>5</integer>
        <key>Hour</key>
        <integer>16</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
</dict>
</plist>
EOF

cp "$REMINDER_PLIST_SRC" "$REMINDER_PLIST_DST"
launchctl unload "$REMINDER_PLIST_DST" 2>/dev/null || true
launchctl load "$REMINDER_PLIST_DST"
echo "==> Friday 4pm reminder installed."

# ---------------------------------------------------------------------------
# Optional: AI project matcher (weekly launchd agent)
# ---------------------------------------------------------------------------
AI_PLIST_NAME="com.terminaltracker.aimatcher"
AI_PLIST_SRC="$SCRIPT_DIR/$AI_PLIST_NAME.plist"
AI_PLIST_DST="$HOME/Library/LaunchAgents/$AI_PLIST_NAME.plist"

echo ""
echo "==> Checking for AI matcher setup (optional)"

if python3 -c "
import json, sys
try:
    c = json.load(open('$SCRIPT_DIR/config.json'))
    key = c.get('anthropic_api_key', '')
    sys.exit(0 if key and not key.startswith('<') else 1)
except Exception:
    sys.exit(1)
" 2>/dev/null; then

    cat > "$AI_PLIST_SRC" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$AI_PLIST_NAME</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>$SCRIPT_DIR/ai_matcher.py</string>
        <string>--days</string>
        <string>7</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>2</integer>
        <key>Hour</key>
        <integer>9</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>$SCRIPT_DIR/ai_matcher_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$SCRIPT_DIR/ai_matcher_stderr.log</string>
</dict>
</plist>
EOF

    cp "$AI_PLIST_SRC" "$AI_PLIST_DST"
    launchctl unload "$AI_PLIST_DST" 2>/dev/null || true
    launchctl load "$AI_PLIST_DST"
    echo "==> AI matcher installed — runs every Monday at 9:00am."
    echo "  Manual run: python3 $SCRIPT_DIR/ai_matcher.py"
    echo "  Dry run:    python3 $SCRIPT_DIR/ai_matcher.py --dry-run"
    echo "  Interactive: python3 $SCRIPT_DIR/ai_matcher.py --interactive"
else
    echo "    anthropic_api_key not found in config.json — skipping AI matcher install."
    echo "    To enable: add 'anthropic_api_key' to config.json, then re-run setup.sh."
fi
