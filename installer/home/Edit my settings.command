#!/bin/sh
# Double-click to change which roles, places and companies are searched.
# Opens the settings in TextEdit, then checks them once you have saved.

home=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
py=$(cat "$home/.program" 2>/dev/null)

open -e "$home/config.yaml"
echo "Your settings are open in TextEdit."
echo "Make your changes, save (Cmd+S), and come back to this window."
echo
printf "Press Enter here once you have saved, to check the settings. "
read -r _
echo

if [ -n "$py" ] && [ -x "$py" ]; then
    if "$py" -m jobhunt --config "$home/config.yaml" --data-dir "$home/data" rescore; then
        echo
        echo "Settings look good. They apply next time you open the job board."
    else
        echo
        echo "Something in the settings could not be read (see above)."
        echo "Double-click Edit my settings again to fix it."
    fi
fi
echo
printf "Press Enter to close this window. "
read -r _
