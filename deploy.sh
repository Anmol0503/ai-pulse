#!/usr/bin/env bash
# Publish what `pulse-publish` built:
#   site/            -> the gh-pages branch   (GitHub Pages serves it to your phone)
#   data/cloud.db    -> the pulse-data branch (the seen-list the next run starts from)
# Each branch holds exactly one commit, force-pushed, so no history piles up.
#
#   ./deploy.sh <push URL>
# GitHub Actions passes a URL carrying its own token.
set -euo pipefail
url="${1:?usage: ./deploy.sh <git push URL>}"
db="${PULSE_DB:-data/cloud.db}"
[ -f site/index.html ] || { echo "site/ is missing: run pulse-publish first" >&2; exit 1; }

push_dir() {  # push_dir <directory> <branch> <commit message>
  local tmp
  tmp="$(mktemp -d)"
  cp -R "$1"/. "$tmp"/
  git -C "$tmp" init --quiet --initial-branch="$2"
  git -C "$tmp" add --all
  # a fixed bot identity, so no personal email ends up in the public history
  git -C "$tmp" -c user.name="AI Pulse" -c user.email="ai-pulse@users.noreply.github.com" commit --quiet --message "$3"
  git -C "$tmp" push --quiet --force "$url" "$2"
  rm -rf "$tmp"
}

when="$(date -u +'%Y-%m-%d %H:%M UTC')"
push_dir site gh-pages "Digest of $when"
if [ -f "$db" ]; then
  state="$(mktemp -d)"
  cp "$db" "$state/pulse.db"
  push_dir "$state" pulse-data "Seen-list after the $when pull"
  rm -rf "$state"
fi
echo "Published."
