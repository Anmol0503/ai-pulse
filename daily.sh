#!/bin/zsh
# The morning job: one pull a day on this Mac, published to the phone page.
# launchd runs it (turn it on or off with ./schedule-mac.sh). Its log: data/daily.log.
#
#   ./daily.sh            pull (once a day), then publish
#   ./daily.sh --rebuild  no pull: rebuild the page from what's stored, then publish (after a page change)
set -u
cd "${0:A:h}" || exit 1
repo="Anmol0503/ai-pulse"
export PULSE_DB=data/cloud.db
# The page promises a "Next pull" only while the morning job is really on.
[[ -f "$HOME/Library/LaunchAgents/io.github.anmol0503.ai-pulse.plist" ]] && export PULSE_SCHEDULE=mac
print -- "--- $(date '+%a %d %b %Y, %H:%M')"

if [[ "${1:-}" == "--rebuild" ]]; then
  uv run pulse-publish --out site --repo "$repo" --no-pull || exit 1
else
  # Once a day: the retries later in the day do nothing after a good pull.
  if [[ -n "$(sqlite3 -readonly "$PULSE_DB" "SELECT 1 FROM pulls WHERE status = 'done'
        AND date(started_at, 'localtime') = date('now', 'localtime') LIMIT 1" 2>/dev/null)" ]]; then
    print "Today's pull is already done."
    exit 0
  fi
  # A Mac that has just woken can need a minute to get back on Wi-Fi.
  for _ in {1..24}; do curl -s -m 5 -o /dev/null https://github.com && break; sleep 5; done
  if ! uv run pulse-publish --out site --repo "$repo"; then
    print "The pull failed. The phone keeps the last digest, and the next retry today tries again."
    exit 1
  fi
fi

# Push with the GitHub login the gh command already holds, without touching your git config.
gh="$(command -v gh)" || { print "Can't find gh."; exit 1; }
GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=credential.helper GIT_CONFIG_VALUE_0= \
  GIT_CONFIG_KEY_1=credential.helper GIT_CONFIG_VALUE_1="!$gh auth git-credential" \
  ./deploy.sh "https://github.com/$repo.git"
