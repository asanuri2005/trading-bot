#!/usr/bin/env bash
# Publish watchlist.txt to the live SSM parameter — the halal universe gate.
#
# The bot may only ever signal or trade tickers in this list, so this script
# refuses to publish an empty one and prints the resulting value before
# writing. Preview without publishing:  DRY_RUN=1 make watchlist
#
# Also rewrites samconfig.toml's Watchlist override. The SSM parameter is a
# CloudFormation-managed resource (template.yaml WatchlistParam), so editing
# it out of band creates drift that a later `sam deploy` can silently revert.
# Keeping both in step means there is one source of truth: watchlist.txt.
set -euo pipefail
export MSYS_NO_PATHCONV=1   # Git Bash rewrites /trading-bot/... into a Windows path

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
FILE="$ROOT/watchlist.txt"
CONFIG="$ROOT/aws/samconfig.toml"
PARAM="${WATCHLIST_PARAM:-/trading-bot/watchlist}"

# Strip comments, remove whitespace within each line, keep line boundaries so
# tickers stay separate, uppercase, then join with commas.
TICKERS=$(awk '{ sub(/#.*/, ""); gsub(/[[:space:]]/, ""); if ($0 != "") print toupper($0) }' \
          "$FILE" | paste -sd, -)

if [ -z "$TICKERS" ]; then
  echo "ERROR: no tickers parsed from $FILE — refusing to publish an empty gate." >&2
  exit 1
fi

echo "watchlist.txt  ->  $TICKERS"
if [ -n "${DRY_RUN:-}" ]; then
  echo "(DRY_RUN set — nothing published)"
  exit 0
fi

aws ssm put-parameter --name "$PARAM" --type String --overwrite --value "$TICKERS" >/dev/null
echo "published: SSM $PARAM"

if grep -q 'Watchlist=' "$CONFIG" 2>/dev/null; then
  sed -i -E 's/Watchlist=\\"[^\]*\\"/Watchlist=\\"'"$TICKERS"'\\"/' "$CONFIG"
  echo "synced:    samconfig.toml -> $(grep -o 'Watchlist=[^ ]*' "$CONFIG")"
fi

echo
echo "For a changed universe, set a NEW ExperimentId in aws/samconfig.toml and redeploy."
echo "The current experiment refuses changed settings so its benchmark stays comparable."
