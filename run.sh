#!/bin/zsh
# Scheduled entry point (launchd). Loads only the keys this job needs from
# ~/.zshrc, runs the sync with --apply, and logs to sync.log.
#
# launchd fires weekly; this wrapper skips the run unless the last successful
# run was at least MIN_DAYS ago, so the sync happens every two weeks. A run
# that was missed (Mac asleep) just happens on the next wake.

MIN_DAYS=13
DIR="${0:A:h}"
LOG="$DIR/sync.log"
STAMP="$DIR/.last_success"

eval "$(grep -E '^[[:space:]]*export (KANDJI_API_URL|KANDJI_API_TOKEN|FRESHSERVICE_API_KEY|SLACK_DEVICE_SYNC_TOKEN)=' ~/.zshrc)"

if [[ -f "$STAMP" && "$1" != "--force" ]]; then
  age_days=$(( ($(date +%s) - $(stat -f %m "$STAMP")) / 86400 ))
  if (( age_days < MIN_DAYS )); then
    echo "$(date '+%F %T') skipped: last success ${age_days}d ago (< ${MIN_DAYS}d)" >> "$LOG"
    exit 0
  fi
fi

echo "=== $(date '+%F %T') run ===" >> "$LOG"
/usr/bin/python3 "$DIR/sync.py" --apply >> "$LOG" 2>&1
code=$?
echo "=== exit $code ===" >> "$LOG"

if (( code == 0 )); then
  touch "$STAMP"
else
  # The sync DMs its own summary; this covers crashes before it gets that far.
  # Exit 2 (safety cap) already sent its own alert.
  if (( code != 2 )); then
    user_id=$(/usr/bin/python3 -c "import json;print(json.load(open('$DIR/config.json'))['slack_user_id'])")
    /usr/bin/curl -s -m 30 -X POST https://slack.com/api/chat.postMessage \
      -H "Authorization: Bearer $SLACK_DEVICE_SYNC_TOKEN" \
      -H 'Content-Type: application/json; charset=utf-8' \
      -d "{\"channel\":\"$user_id\",\"text\":\"*Device sync failed* (exit $code). See $LOG\"}" > /dev/null
  fi
fi
exit $code
