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
$jobhunt fetch
$jobhunt digest
echo "=== done ==="
