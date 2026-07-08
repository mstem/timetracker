#!/bin/bash
# Sends a message to your Telegram bot. Usage: notify_telegram.sh "message text"
# Reads telegram_bot_token / telegram_chat_id from config.json (sibling to this script).

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$DIR/config.json"
MESSAGE="$1"

TOKEN=$(jq -r '.telegram_bot_token // empty' "$CONFIG")
CHAT_ID=$(jq -r '.telegram_chat_id // empty' "$CONFIG")

if [ -z "$TOKEN" ] || [ -z "$CHAT_ID" ]; then
  echo "notify_telegram.sh: telegram_bot_token/telegram_chat_id not set in config.json" >&2
  exit 1
fi

curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  -d chat_id="$CHAT_ID" \
  --data-urlencode text="$MESSAGE" > /dev/null
