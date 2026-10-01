#!/usr/bin/env python3
# knotifier.py
# System tray application for KDE Plasma / Linux (port of the Windows Notifier).
# Listens for HTTP GET requests (e.g. from Home Assistant) and shows desktop
# notifications with sound (priority != low) or silently (priority=low).
# A GET with ?cmd=quit from loopback shuts the app down cleanly.
# Features: last 11 notifications in the tray menu, About dialog with
# "Start at login" checkbox, port configurable in Notifier.ini (live reload),
# single instance.
#
# Dependencies (Kubuntu):
#   sudo apt install python3-pyqt6 python3-pyqt6.qtsvg libnotify-bin pulseaudio-utils
# Run:
#   python3 knotifier.py

import configparser
import datetime
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from PyQt6.QtCore import (QByteArray, QDir, QFileSystemWatcher, QLockFile, QObject, Qt,
                          QTimer, pyqtSignal)
from PyQt6.QtGui import QAction, QColor, QCursor, QFont, QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (QApplication, QCheckBox, QDialog, QLabel, QMenu,
                             QPushButton, QSystemTrayIcon, QVBoxLayout)

__version__ = 'knotifier 1.0.2'


def version():
    return __version__


# --- Version history ----------------------------------------------------
# v1.0.2: Application name shown as knotifier instead of Notifier
# v1.0.1: Tray icon turns red when a new notification arrives and returns to
#         normal when the menu is opened
# v1.0.0: Initial release of the Linux/KDE port of Notifier (Windows)

VERSION = __version__.split()[-1]
APP_NAME = "knotifier"
HTTP_PORT_DEF = 8765
CLIENT_TIMEOUT_S = 5
MAX_NOTIFICATIONS = 11
MAX_MESSAGE_CHARS = 70

SCRIPT_PATH = os.path.abspath(__file__)
INI_PATH = os.path.join(os.path.dirname(SCRIPT_PATH), "Notifier.ini")
AUTOSTART_FILE = os.path.expanduser("~/.config/autostart/notifier.desktop")

SOUND_CANDIDATES = [
    "/usr/share/sounds/freedesktop/stereo/message.oga",
    "/usr/share/sounds/freedesktop/stereo/message-new-instant.oga",
    "/usr/share/sounds/freedesktop/stereo/bell.oga",
]


# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
def load_port():
    cp = configparser.ConfigParser()
    try:
        cp.read(INI_PATH, encoding="utf-8")
        port = cp.getint("general", "port", fallback=HTTP_PORT_DEF)
    except (ValueError, configparser.Error):
        port = HTTP_PORT_DEF
    if port <= 0 or port > 65535:
        port = HTTP_PORT_DEF
    return port


# ----------------------------------------------------------------------------
# Autostart (XDG autostart entry)
# ----------------------------------------------------------------------------
def autostart_get():
    return os.path.exists(AUTOSTART_FILE)


def autostart_set(enable):
    if enable:
        os.makedirs(os.path.dirname(AUTOSTART_FILE), exist_ok=True)
        with open(AUTOSTART_FILE, "w", encoding="utf-8") as f:
            f.write(
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=knotifier\n"
                "Comment=Home Assistant notification receiver\n"
                f"Exec={sys.executable} {SCRIPT_PATH}\n"
                "Icon=preferences-desktop-notification\n"
                "X-GNOME-Autostart-enabled=true\n"
            )
    else:
        try:
            os.remove(AUTOSTART_FILE)
        except FileNotFoundError:
            pass


# ----------------------------------------------------------------------------
# HTTP server (runs in worker threads, talks to the GUI via signals)
# ----------------------------------------------------------------------------
class Bridge(QObject):
    notification = pyqtSignal(str, str, bool)   # title, message, silent
    quit_requested = pyqtSignal()


BRIDGE = Bridge()


class Handler(BaseHTTPRequestHandler):
    timeout = CLIENT_TIMEOUT_S        # bound how long a client may hold a thread
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _reply(self, code):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def do_GET(self):
        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)

        def param(name):
            vals = query.get(name)
            return vals[0] if vals else ""

        message = param("message")
        title = param("title")
        priority = param("priority")
        cmd = param("cmd")

        # cmd=quit: honoured from loopback only.
        if cmd == "quit":
            if self.client_address[0] == "127.0.0.1":
                self._reply(200)
                BRIDGE.quit_requested.emit()
            else:
                self._reply(403)
            return

        if not title:
            title = "Home Assistant"
        silent = priority.lower() == "low"

        self._reply(200)

        if message:
            BRIDGE.notification.emit(title, message, silent)


class HttpService:
    def __init__(self):
        self.server = None
        self.thread = None
        self.port = None

    def start(self, port):
        """Returns True on success, False if the port could not be bound."""
        try:
            server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        except OSError:
            return False
        server.daemon_threads = True
        self.server = server
        self.port = port
        self.thread = threading.Thread(target=server.serve_forever,
                                       kwargs={"poll_interval": 0.5},
                                       daemon=True)
        self.thread.start()
        return True

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.thread:
            self.thread.join(timeout=3)
            self.thread = None
        self.port = None


# ----------------------------------------------------------------------------
# Desktop notification + sound
# ----------------------------------------------------------------------------
def play_sound():
    player = shutil.which("paplay")
    if not player:
        return
    for path in SOUND_CANDIDATES:
        if os.path.exists(path):
            subprocess.Popen([player, path], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return


def show_desktop_notification(title, message, silent, tray):
    notify_send = shutil.which("notify-send")
    if notify_send:
        # The notification daemon's own sound is always suppressed so that
        # the sound behaviour is identical everywhere: we play it ourselves.
        cmd = [notify_send, "-a", APP_NAME,
               "-u", "low" if silent else "normal",
               "-h", "boolean:suppress-sound:true",
               title, message]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 5000)
    if not silent:
        play_sound()


# ----------------------------------------------------------------------------
# About dialog
# ----------------------------------------------------------------------------
class AboutDialog(QDialog):
    def __init__(self, port):
        super().__init__()
        self.setWindowTitle(f"About {APP_NAME}")
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        name = QLabel(APP_NAME)
        f = QFont()
        f.setBold(True)
        f.setPointSize(f.pointSize() + 4)
        name.setFont(f)
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(name)

        ver = QLabel(f"Version {VERSION}")
        ver.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(ver)

        desc = QLabel(f"Receives notifications on port {port} and displays them "
                      "as desktop notifications with optional sound control.")
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(desc)

        example = QLabel("Example:")
        example.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(example)

        url = f"http://localhost:{port}/?title=knotifier&message=Hello+world!&priority=high"
        link = QLabel(f'<a href="{url}">{url.replace("&", "&amp;")}</a>')
        link.setTextFormat(Qt.TextFormat.RichText)
        link.setOpenExternalLinks(True)      # clicking fires a real notification
        link.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(link)

        self.check = QCheckBox("Start at login")
        self.check.setChecked(autostart_get())
        self.check.toggled.connect(autostart_set)
        layout.addWidget(self.check, alignment=Qt.AlignmentFlag.AlignCenter)

        ok = QPushButton("OK")
        ok.setDefault(True)
        ok.clicked.connect(self.close)
        layout.addWidget(ok, alignment=Qt.AlignmentFlag.AlignCenter)


# ----------------------------------------------------------------------------
# Tray application
# ----------------------------------------------------------------------------
# Bell icon. The outline is the "notifications" bell of the KDE Breeze icon
# theme (Copyright (C) 2014 Uri Herrera and others, LGPL-3.0-or-later).
# It is always drawn in white. BELL_FILL is the inside of the bell; it is only
# drawn, in red, in the alert icon.
BELL_OUTLINE = (
    "m10.269531 17a2 2 0 0 0-0.2695312 1 2 2 0 0 0 2 2 2 2 0 0 0 2-2 "
    "2 2 0 0 0-0.271484-1z"
    "m1.7304688-13a1 1 0 0 0-1 1 1 1 0 0 0 0.0098 0.1289062 3.9999999 "
    "3.9999999 0 0 0-3.0098 3.8710938c0 3-1 4-3 6v1h14v-1c-2-2-3-3-3-6a"
    "3.9999999 3.9999999 0 0 0-3.009766-3.8710938 1 1 0 0 0 0.009766-0.1289062 "
    "1 1 0 0 0-1-1z"
    "m0 2a3 3 0 0 1 3 3c0 3 0.585938 4 2.585938 6h-11.171876c2-2 2.5859375-3 "
    "2.5859375-6a3 3 0 0 1 3-3z"
)
BELL_FILL = ("M12 6a3 3 0 0 1 3 3c0 3 0.585938 4 2.585938 6h-11.171876c2-2 "
             "2.5859375-3 2.5859375-6a3 3 0 0 1 3-3z")
BELL_COLOR_LINE = "#ffffff"
BELL_COLOR_ALERT = "#e03131"
BELL_SVG_NORMAL = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 22 22">'
    f'<path d="{BELL_OUTLINE}" fill="{BELL_COLOR_LINE}" fill-rule="evenodd"/>'
    '</svg>'
)
BELL_SVG_ALERT = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 22 22">'
    f'<path d="{BELL_FILL}" fill="{BELL_COLOR_ALERT}"/>'
    f'<path d="{BELL_OUTLINE}" fill="{BELL_COLOR_LINE}" fill-rule="evenodd"/>'
    '</svg>'
)


def svg_icon(svg):
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    icon = QIcon()
    for size in (16, 22, 24, 32, 48, 64):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(p)
        p.end()
        icon.addPixmap(pm)
    return icon


def make_icon(alert=False):
    return svg_icon(BELL_SVG_ALERT if alert else BELL_SVG_NORMAL)


class TrayApp(QObject):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.notifications = []          # newest first: (title, message, time, silent)
        self.about = None
        self.service = HttpService()
        self.port = load_port()

        self.icon_normal = make_icon()
        self.icon_alert = make_icon(alert=True)
        self.tray = QSystemTrayIcon(self.icon_normal)
        self.tray.setToolTip(APP_NAME)
        self.menu = QMenu()
        self.menu.aboutToShow.connect(self.rebuild_menu)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self.on_activated)
        self.tray.show()

        BRIDGE.notification.connect(self.on_notification)
        BRIDGE.quit_requested.connect(self.app.quit)
        self.app.aboutToQuit.connect(self.service.stop)

        # Retry binding every 3 s until it works (mirrors the Windows version)
        self.bind_timer = QTimer(self)
        self.bind_timer.setInterval(3000)
        self.bind_timer.timeout.connect(self.try_start)
        self.try_start()

        # Live reload of Notifier.ini (watch the directory too, because
        # editors often replace the file, which drops a file-level watch)
        self.watcher = QFileSystemWatcher(self)
        self.watcher.addPath(os.path.dirname(INI_PATH))
        if os.path.exists(INI_PATH):
            self.watcher.addPath(INI_PATH)
        self.watcher.fileChanged.connect(self.on_ini_changed)
        self.watcher.directoryChanged.connect(self.on_ini_changed)

    # -- server -------------------------------------------------------------
    def try_start(self):
        if self.service.start(self.port):
            self.bind_timer.stop()
        elif not self.bind_timer.isActive():
            self.bind_timer.start()

    def on_ini_changed(self, _path):
        QTimer.singleShot(200, self.reload_settings)

    def reload_settings(self):
        if os.path.exists(INI_PATH) and INI_PATH not in self.watcher.files():
            self.watcher.addPath(INI_PATH)
        port = load_port()
        if port != self.port:
            self.port = port
            self.service.stop()
            self.try_start()

    # -- notifications ------------------------------------------------------
    def on_notification(self, title, message, silent):
        stamp = datetime.datetime.now().strftime("%H:%M")
        self.notifications.insert(0, (title, message, stamp, silent))
        del self.notifications[MAX_NOTIFICATIONS:]
        show_desktop_notification(title, message, silent, self.tray)
        self.tray.setIcon(self.icon_alert)

    # -- menu ---------------------------------------------------------------
    def rebuild_menu(self):
        self.tray.setIcon(self.icon_normal)
        self.menu.clear()
        header = QAction(f"{APP_NAME} {VERSION}", self.menu)
        header.setEnabled(False)
        f = header.font()
        f.setBold(True)
        header.setFont(f)
        self.menu.addAction(header)
        self.menu.addSeparator()

        if not self.notifications:
            empty = QAction("No notifications yet", self.menu)
            empty.setEnabled(False)
            self.menu.addAction(empty)
        else:
            for i, (title, message, stamp, silent) in enumerate(self.notifications):
                bell = "" if silent else "\U0001F514 "
                a1 = QAction(f"{bell}{title}   [{stamp}]", self.menu)
                a1.setEnabled(False)
                self.menu.addAction(a1)
                msg = " ".join(message.split())
                if len(msg) > MAX_MESSAGE_CHARS:
                    msg = msg[:MAX_MESSAGE_CHARS - 1] + "\u2026"
                a2 = QAction("    " + msg, self.menu)
                a2.setEnabled(False)
                self.menu.addAction(a2)
                if i < len(self.notifications) - 1:
                    self.menu.addSeparator()

        self.menu.addSeparator()
        about = QAction("About / Settings...", self.menu)
        about.triggered.connect(self.show_about)
        self.menu.addAction(about)
        self.menu.addSeparator()
        quit_action = QAction("&Quit", self.menu)
        quit_action.triggered.connect(self.app.quit)
        self.menu.addAction(quit_action)

    def on_activated(self, reason):
        # Right-click is handled by the context menu; a plain left-click
        # opens the same menu.
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.rebuild_menu()
            self.menu.popup(QCursor.pos())

    def show_about(self):
        if self.about is not None and self.about.isVisible():
            self.about.raise_()
            self.about.activateWindow()
            return
        self.about = AboutDialog(self.port)
        self.about.show()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)

    # Single instance
    lock = QLockFile(os.path.join(QDir.tempPath(), "notifier-tray.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        print(f"{APP_NAME} is already running.", file=sys.stderr)
        return 0

    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("No system tray available.", file=sys.stderr)
        return 1

    tray_app = TrayApp(app)   # keep a reference alive
    rc = app.exec()
    lock.unlock()
    return rc


if __name__ == "__main__":
    sys.exit(main())
