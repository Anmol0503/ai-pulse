#!/bin/zsh
# Turn the morning job on or off for this Mac.
#
#   ./schedule-mac.sh        on: ./daily.sh runs at 7:30 every morning (publish.py's MAC_MORNING).
#                            If the Mac is asleep then, launchd runs it as soon as the Mac wakes.
#                            The runs at 10:00, 13:00, 16:00, 19:00 and 22:00 do nothing once the
#                            day's pull is done: they matter only when the Mac was off, or a pull failed.
#   ./schedule-mac.sh --now  run it once, right now (still once a day)
#   ./schedule-mac.sh --off  off
set -eu
here="${0:A:h}"
label="io.github.anmol0503.ai-pulse"
plist="$HOME/Library/LaunchAgents/$label.plist"
domain="gui/$(id -u)"

case "${1:-}" in
  --off)
    launchctl bootout "$domain/$label" 2>/dev/null || true
    rm -f "$plist"
    print "The morning job is off."
    exit 0 ;;
  --now)
    launchctl kickstart "$domain/$label"
    print "Running. Follow it with: tail -f $here/data/daily.log"
    exit 0 ;;
esac

# launchd starts jobs with a bare PATH: add the folders uv, claude and gh live in, after the system ones.
# (Not called "path": in zsh, $path is tied to $PATH, and assigning it would hide every tool.)
job_path="/usr/bin:/bin:/usr/sbin:/sbin"
for tool in uv claude gh; do
  found="$(command -v $tool)" || { print "Can't find $tool. Install it, then run this again."; exit 1; }
  [[ ":$job_path:" == *":${found:h}:"* ]] || job_path="$job_path:${found:h}"
done

times=""
for hm in 7:30 10:00 13:00 16:00 19:00 22:00; do
  times="$times
    <dict><key>Hour</key><integer>${hm%%:*}</integer><key>Minute</key><integer>$(( ${hm##*:} ))</integer></dict>"
done

mkdir -p "$HOME/Library/LaunchAgents" "$here/data"
cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>/bin/zsh</string><string>$here/daily.sh</string></array>
  <key>WorkingDirectory</key><string>$here</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$job_path</string>
    <key>HOME</key><string>$HOME</string>
    <key>LANG</key><string>en_US.UTF-8</string>
  </dict>
  <key>StartCalendarInterval</key>
  <array>$times
  </array>
  <key>StandardOutPath</key><string>$here/data/daily.log</string>
  <key>StandardErrorPath</key><string>$here/data/daily.log</string>
</dict>
</plist>
EOF
plutil -lint -s "$plist"
launchctl bootout "$domain/$label" 2>/dev/null || true
for _ in 1 2 3 4 5; do launchctl bootstrap "$domain" "$plist" 2>/dev/null && break; sleep 1; done
launchctl print "$domain/$label" >/dev/null
print "The morning job is on: every day at 7:30, or as soon as the Mac wakes after that."
