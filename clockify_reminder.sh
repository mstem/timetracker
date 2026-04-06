#!/bin/bash
# Sends a macOS notification reminding you to review Clockify time entries.
# Scheduled to run every Friday at 4pm via launchd — see setup.sh.

osascript -e 'display notification "Run: python3 ~/terminal-time-tracker/ai_matcher.py --interactive" with title "⏱ Clockify Review Time" sound name "Glass"'
