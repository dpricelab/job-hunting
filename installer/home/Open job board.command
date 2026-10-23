#!/bin/sh
# Double-click to check every careers page and open your job board.
# Close this window, or press Ctrl+C in it, when you have finished.

home=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
py=$(cat "$home/.program" 2>/dev/null)

if [ -z "$py" ] || [ ! -x "$py" ]; then
    echo "The program is missing. Double-click Install in the download again."
    printf "Press Enter to close this window. "
    read -r _
    exit 1
fi

run() {
    "$py" -m jobhunt --config "$home/config.yaml" --data-dir "$home/data" "$@"
}

echo "Checking careers pages - this takes a minute or two..."
echo
run fetch
echo
echo "Opening your job board in the browser."
echo "Leave this window open while you use it; close it when you are done."
echo
run serve
