#!/bin/sh
# Schedule the daily run on macOS, at 07:00 on weekdays by default.
#
#   sh scripts/install-macos-schedule.sh          # 07:00 weekdays
#   sh scripts/install-macos-schedule.sh 8 30     # 08:30 weekdays
#
# Uses launchd rather than cron deliberately. cron simply misses a run if the
# machine is asleep at the appointed time, which for a laptop is most
# mornings. launchd runs the job when the machine next wakes instead.

set -eu

hour=${1:-7}
minute=${2:-0}

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(dirname -- "$script_dir")
label="com.jobhunt.daily"
plist="$HOME/Library/LaunchAgents/$label.plist"
log="$repo_dir/out/schedule.log"

mkdir -p "$HOME/Library/LaunchAgents" "$repo_dir/out"
chmod +x "$repo_dir/scripts/daily.sh"

# One entry per weekday: launchd has no weekday range, unlike cron.
entries=""
for weekday in 1 2 3 4 5; do
    entries="$entries
        <dict>
            <key>Weekday</key><integer>$weekday</integer>
            <key>Hour</key><integer>$hour</integer>
            <key>Minute</key><integer>$minute</integer>
        </dict>"
done

cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$label</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>$repo_dir/scripts/daily.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$repo_dir</string>
    <key>StartCalendarInterval</key>
    <array>$entries
    </array>
    <key>StandardOutPath</key>
    <string>$log</string>
    <key>StandardErrorPath</key>
    <string>$log</string>
</dict>
</plist>
PLIST

# Replace any previous copy, ignoring the error when there is none.
launchctl unload "$plist" 2>/dev/null || true
launchctl load "$plist"

printf 'Scheduled: weekdays at %02d:%02d\n' "$hour" "$minute"
echo "Definition: $plist"
echo "Log:        $log"
echo
echo "Run it now to check:   launchctl start $label"
echo "Stop scheduling:       launchctl unload $plist"
