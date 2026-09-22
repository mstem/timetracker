#!/bin/bash
# Daily wrapper for exist_sync.py, run by com.mstem.exist-done-working.
#
# The exist.io write token is daily-briefing's: it mints and refreshes it with
# reauth-exist.py. Reading it out of that file at run time rather than copying
# the value here means a refresh there is picked up on the next run instead of
# silently breaking this one.
set -euo pipefail

TOKEN_FILE="$HOME/.config/daily-briefing/exist-token.json"
if [ ! -f "$TOKEN_FILE" ]; then
  echo "$(date '+%Y-%m-%d %H:%M') no token at $TOKEN_FILE — run reauth-exist.py" >&2
  exit 1
fi

EXIST_WRITE_TOKEN=$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["access_token"])' "$TOKEN_FILE")
export EXIST_WRITE_TOKEN

cd "$(dirname "$0")"
exec /usr/bin/python3 exist_sync.py "$@"
