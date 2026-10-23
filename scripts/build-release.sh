#!/bin/sh
# Build the download for people who will never open a terminal:
#
#   dist/jobhunt-<version>.zip
#     jobhunt-<version>/
#       START HERE.html
#       Install (Mac).command
#       Install (Windows).bat
#       program/            the package, config, licence and home launchers
#
# Built from the last commit rather than the working tree, so stray local
# files never ship and the .bat files carry the CRLF endings cmd.exe needs.

set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_dir"

version=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
name="jobhunt-$version"
stage="dist/$name"

rm -rf dist
mkdir -p "$stage/program"

git archive --format=tar HEAD \
    jobhunt pyproject.toml config.yaml LICENSE README.md installer \
    | tar -x -C "$stage/program"

mv "$stage/program/installer/START HERE.html" \
   "$stage/program/installer/Install (Mac).command" \
   "$stage/program/installer/Install (Windows).bat" \
   "$stage/"
chmod +x "$stage/Install (Mac).command" "$stage/program/installer/home/"*.command

(cd dist && zip -qr "$name.zip" "$name")
echo "dist/$name.zip"
