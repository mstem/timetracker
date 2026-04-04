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
