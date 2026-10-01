#!/usr/bin/env bash
# knotifier-install
# Installs everything knotifier needs and installs knotifier itself.
# Missing dependencies are installed automatically, without asking.
# Usage: ./knotifier-install.sh
# Optional install folder (default: ~/knotifier):
#   KNOTIFIER_DIR=/some/folder ./knotifier-install.sh

# --- Version ------------------------------------------------------------
VERSION="knotifier-install 1.0.1"

# --- Version history ----------------------------------------------------
# v1.0.1: Install the latest release of knotifier instead of the main branch
# v1.0.0: Initial release

set -u

LATEST_URL="https://api.github.com/repos/rob-vandenberg/knotifier/releases/latest"
REPO_RAW_BASE="https://raw.githubusercontent.com/rob-vandenberg/knotifier"
INSTALL_DIR="${KNOTIFIER_DIR:-$HOME/knotifier}"
FILES="knotifier.py knotifier.sh"
SOUND_FILES="/usr/share/sounds/freedesktop/stereo/message.oga
/usr/share/sounds/freedesktop/stereo/message-new-instant.oga
/usr/share/sounds/freedesktop/stereo/bell.oga"

MISSING=""

echo "$VERSION"

if ! command -v apt-get >/dev/null 2>&1; then
    echo "This installer needs apt-get (Kubuntu, Ubuntu, Debian)." >&2
    exit 1
fi


# --- Dependency checks --------------------------------------------------
need() {
    case " $MISSING " in
        *" $1 "*) ;;
        *) MISSING="$MISSING $1" ;;
    esac
}

report() {
    printf '  %-8s %s\n' "$1" "$2"
}

check_dependencies() {
    local python_ok=0
    local found=0
    local f

    MISSING=""

    if command -v python3 >/dev/null 2>&1; then
        python_ok=1
        report ok "python3"
    else
        report missing "python3"
        need python3
    fi

    if [ "$python_ok" = 1 ] &&
       python3 -c "import PyQt6.QtCore, PyQt6.QtGui, PyQt6.QtWidgets" >/dev/null 2>&1; then
        report ok "Python module PyQt6"
    else
        report missing "Python module PyQt6 (python3-pyqt6)"
        need python3-pyqt6
    fi

    if [ "$python_ok" = 1 ] &&
       python3 -c "import PyQt6.QtSvg" >/dev/null 2>&1; then
        report ok "Python module PyQt6 QtSvg"
    else
        report missing "Python module PyQt6 QtSvg (python3-pyqt6.qtsvg)"
        need python3-pyqt6.qtsvg
    fi

    if command -v notify-send >/dev/null 2>&1; then
        report ok "notify-send"
    else
        report missing "notify-send (libnotify-bin)"
        need libnotify-bin
    fi

    if command -v paplay >/dev/null 2>&1; then
        report ok "paplay"
    else
        report missing "paplay (pulseaudio-utils)"
        need pulseaudio-utils
    fi

    for f in $SOUND_FILES; do
        [ -f "$f" ] && found=1
    done
    if [ "$found" = 1 ]; then
        report ok "notification sound file"
    else
        report missing "notification sound file (sound-theme-freedesktop)"
        need sound-theme-freedesktop
    fi

    if command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1; then
        report ok "curl or wget"
    else
        report missing "curl or wget (curl)"
        need curl
    fi
}

install_packages() {
    local sudo_cmd=""

    if [ "$(id -u)" -ne 0 ]; then
        if command -v sudo >/dev/null 2>&1; then
            sudo_cmd="sudo"
        else
            echo "Root rights are needed to install:$MISSING" >&2
            exit 1
        fi
    fi

    echo "Installing:$MISSING"
    if ! $sudo_cmd apt-get update; then
        echo "apt-get update failed." >&2
        exit 1
    fi
    # shellcheck disable=SC2086
    if ! $sudo_cmd apt-get install -y $MISSING; then
        echo "Installation of$MISSING failed." >&2
        exit 1
    fi
}

echo "Checking dependencies:"
check_dependencies

if [ -n "$MISSING" ]; then
    install_packages
    echo "Checking dependencies again:"
    check_dependencies
    if [ -n "$MISSING" ]; then
        echo "Still missing:$MISSING" >&2
        exit 1
    fi
fi


# --- Install knotifier --------------------------------------------------
download() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$1" -o "$2"
    else
        wget -q -O "$2" "$1"
    fi
}

fetch_text() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$1"
    else
        wget -q -O - "$1"
    fi
}

echo "Looking up the latest release of knotifier"
TAG="$(fetch_text "$LATEST_URL" | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n 1)"
if [ -z "$TAG" ]; then
    echo "Could not find the latest release of knotifier." >&2
    exit 1
fi
echo "Latest release: $TAG"
REPO_RAW="$REPO_RAW_BASE/$TAG"

echo "Installing knotifier in $INSTALL_DIR"
mkdir -p "$INSTALL_DIR" || exit 1

TMP="$(mktemp -d)" || exit 1
trap 'rm -rf "$TMP"' EXIT

for f in $FILES; do
    if ! download "$REPO_RAW/$f" "$TMP/$f" || [ ! -s "$TMP/$f" ]; then
        echo "Download of $f failed." >&2
        exit 1
    fi
done

if ! python3 -m py_compile "$TMP/knotifier.py"; then
    echo "The downloaded knotifier.py is not valid Python." >&2
    exit 1
fi

install -m 644 "$TMP/knotifier.py" "$INSTALL_DIR/knotifier.py" || exit 1
install -m 755 "$TMP/knotifier.sh" "$INSTALL_DIR/knotifier.sh" || exit 1

echo "knotifier is installed in $INSTALL_DIR"
echo "Start it with: $INSTALL_DIR/knotifier.sh"
echo "To start it at login, open About / Settings in the tray menu and tick Start at login."
