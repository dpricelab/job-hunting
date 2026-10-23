#!/bin/sh
# Double-click to install, or to update after downloading a newer version.
#
# Your own files (settings, what you have applied to) live in
# Documents/Job hunt and are never overwritten. The program itself goes in
# ~/Library/Application Support/jobhunt, out of the way of iCloud syncing.

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
home="$HOME/Documents/Job hunt"
app="$HOME/Library/Application Support/jobhunt"

finish() {
    echo
    if [ -z "${JOBHUNT_NO_PAUSE:-}" ]; then
        printf "Press Enter to close this window. "
        read -r _
    fi
    exit "$1"
}

echo "Job hunt - installer"
echo "===================="
echo

if [ ! -d "$here/program" ]; then
    echo "Can't find the 'program' folder next to this file."
    echo "Unzip the download first, then double-click Install inside the unzipped folder."
    finish 1
fi

# Files downloaded from the internet are marked as such. Clearing the mark on
# the rest of the download now means the other launchers open without asking.
if command -v xattr >/dev/null 2>&1; then
    xattr -dr com.apple.quarantine "$here" 2>/dev/null || true
fi

# Find Python 3.10 or newer. /usr/bin/python3 on a Mac without Apple's
# developer tools is only a stub that pops up an installer, so it is skipped
# unless those tools are present.
py=""
for candidate in \
    /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
    /usr/local/bin/python3 \
    /opt/homebrew/bin/python3 \
    python3
do
    if [ "$candidate" = python3 ]; then
        found=$(command -v python3 2>/dev/null) || continue
        if [ "$found" = /usr/bin/python3 ] && command -v xcode-select >/dev/null 2>&1 \
            && ! xcode-select -p >/dev/null 2>&1; then
            continue
        fi
    elif [ ! -x "$candidate" ]; then
        continue
    fi
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        py=$candidate
        break
    fi
done

if [ -z "$py" ]; then
    echo "Python 3.10 or newer is needed and was not found."
    echo
    echo "1. Install it from python.org (opening the page now)."
    echo "   Click the big yellow 'Download Python' button and run the installer."
    echo "2. Then double-click this Install file again."
    command -v open >/dev/null 2>&1 && open "https://www.python.org/downloads/"
    finish 1
fi
echo "Using $("$py" --version 2>&1)"

echo "Setting up the program (this takes a minute)..."
mkdir -p "$app" "$home/data" || finish 1
if [ ! -x "$app/venv/bin/python" ]; then
    "$py" -m venv "$app/venv" || { echo "Could not set up Python."; finish 1; }
fi
"$app/venv/bin/python" -m pip install --quiet --disable-pip-version-check \
    --upgrade --force-reinstall --no-deps "$here/program" \
    && "$app/venv/bin/python" -m pip install --quiet --disable-pip-version-check \
    "$here/program" \
    || { echo; echo "The install did not finish. Check you are online and try again."; finish 1; }

if [ -f "$home/config.yaml" ]; then
    echo "Keeping your existing settings."
else
    cp "$here/program/config.yaml" "$home/config.yaml"
    echo "Created your settings file."
fi

for launcher in "Open job board.command" "Edit my settings.command"; do
    cp "$here/program/installer/home/$launcher" "$home/$launcher"
    chmod +x "$home/$launcher"
done
cp "$here/START HERE.html" "$home/START HERE.html" 2>/dev/null || true
command -v xattr >/dev/null 2>&1 && xattr -dr com.apple.quarantine "$home" 2>/dev/null

# Remember where the program is, so the launchers find it.
printf '%s\n' "$app/venv/bin/python" > "$home/.program"

echo
echo "Done. Your Job hunt folder is in Documents and is opening now."
echo "From now on, double-click 'Open job board' in that folder."
echo "You can delete the download."
command -v open >/dev/null 2>&1 && open "$home"
finish 0
