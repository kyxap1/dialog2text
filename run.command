#!/usr/bin/env bash
# Double-clickable wrapper: Finder starts this in the user's home folder, so cd first.
cd "$(dirname "$0")"
./run.sh "$@"
echo
read -rp "Press Enter to close this window."
