#!/usr/bin/env bash
# knotifier-start
# Starts the newest knotifier_*.py in this folder detached from the terminal.

# --- Version ------------------------------------------------------------
VERSION="knotifier-start 1.0.0"

# --- Version history ----------------------------------------------------
# v1.0.0: Initial release

cd "$(dirname "$(readlink -f "$0")")" || exit 1

target="$(ls knotifier_*.py 2>/dev/null | sort -V | tail -n 1)"

if [ -z "$target" ]; then
    echo "No knotifier_*.py found in $(pwd)" >&2
    exit 1
fi

setsid python3 "$target" >/dev/null 2>&1 &
echo "Started $target"
