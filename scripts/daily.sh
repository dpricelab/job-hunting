#!/bin/sh
# Fetch every board, then rebuild the digest. Meant to be run unattended by
# cron or launchd, and safe to run by hand.
#
# Locates the repository from its own path rather than relying on the working
# directory, because a scheduler starts it from somewhere unpredictable and
# the config and database are both looked up relative to the repo root.

set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(dirname -- "$script_dir")
cd "$repo_dir"

# Prefer the virtual environment. A scheduler does not run a login shell, so
# nothing has been activated and PATH is minimal.
if [ -x "$repo_dir/.venv/bin/jobhunt" ]; then
    jobhunt="$repo_dir/.venv/bin/jobhunt"
elif [ -x "$repo_dir/.venv/bin/python" ]; then
    jobhunt="$repo_dir/.venv/bin/python -m jobhunt"
else
    jobhunt="python3 -m jobhunt"
fi

echo "=== $(date '+%Y-%m-%d %H:%M:%S') fetching ==="
# launchd runs a missed job the moment the machine wakes, which can be before
# Wi-Fi has reconnected. fetch exits non-zero only when every board failed, so
# that is the case worth one more try once the network has had time to return.
if ! $jobhunt fetch; then
    echo "--- every board failed; retrying in 60s ---"
    sleep 60
    $jobhunt fetch || echo "--- still failing; the digest shows what was already known ---"
fi
# --no-mark: a scheduled run is not you reading the list. The "New" badges
# should mean new since you last looked, not since 07:00 this morning.
$jobhunt digest --no-mark
echo "=== done ==="
