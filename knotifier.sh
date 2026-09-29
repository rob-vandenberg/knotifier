#!/usr/bin/env bash
# knotifier-start
# Starts knotifier.py in this folder detached from the terminal.

# --- Version ------------------------------------------------------------
VERSION="knotifier-start 1.0.1"

# --- Version history ----------------------------------------------------
# v1.0.1: Start knotifier.py instead of the newest knotifier_*.py
# v1.0.0: Initial release

cd "$(dirname "$(readlink -f "$0")")" || exit 1

target="knotifier.py"

if [ ! -f "$target" ]; then
    echo "$target not found in $(pwd)" >&2
    exit 1
fi

setsid python3 "$target" >/dev/null 2>&1 &
echo "Started $target"
