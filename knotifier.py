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
import ctypes
import datetime
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from PyQt6.QtCore import (QByteArray, QDir, QFileSystemWatcher, QLockFile, QObject, QPoint,
                          QSocketNotifier, Qt, QTimer, pyqtClassInfo, pyqtProperty, pyqtSignal, pyqtSlot)
from PyQt6.QtDBus import (QDBusConnection, QDBusMessage, QDBusObjectPath,
                          QDBusServiceWatcher)
from PyQt6.QtGui import QAction, QColor, QCursor, QFont, QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (QApplication, QCheckBox, QDialog, QLabel, QMenu,
                             QPushButton, QSystemTrayIcon, QVBoxLayout)

__version__ = 'knotifier 1.0.12'


def version():
    return __version__


# --- Version history ----------------------------------------------------
# v1.0.12: Under X11 the menu closes on a click outside it or on the tray icon,
#          also while another program (such as KRDC) holds the mouse and keyboard
# v1.0.11: Under X11 the app registers its own tray item, so a right click opens
#          the same menu as a left click, above the panel
# v1.0.10: Under X11 a left click opens the app's own menu again, above the panel
# v1.0.9: Under X11 too a left click opens the menu through Plasma, like a right click
# v1.0.8: No code change: released with the new Chrono-style start menu icon
# v1.0.7: Under Wayland a left click opens the menu through Plasma, like a right click
# v1.0.6: A hyphen between the time and the message in the tray menu
# v1.0.5: Own icon knotifier.svg for the start menu entry made by the installer
# v1.0.4: Notifications in the tray menu no longer grey, the time in front of
#         the message without indent, the inside of the bell yellow while unread,
#         the left-click menu opens above the panel instead of over it, and the
#         menu is rebuilt only when a notification arrives, not when it opens
# v1.0.3: Bell icon enlarged to 17.5 x 20 pixels inside the 22 x 22 pixel area
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
# drawn, in yellow, in the alert icon.
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
BELL_COLOR_ALERT = "#ffcc00"
BELL_SVG_NORMAL = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="3.2 3.2 17.6 17.6">'
    f'<path d="{BELL_OUTLINE}" fill="{BELL_COLOR_LINE}" fill-rule="evenodd"/>'
    '</svg>'
)
BELL_SVG_ALERT = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="3.2 3.2 17.6 17.6">'
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


def let_plasma_open_menu_on_left_click(tray):
    """Under Wayland the app may not open a menu itself after a click on the
    panel: Qt refuses a popup without a window of its own that was clicked.
    Marking the tray item as a menu (ItemIsMenu) makes Plasma open the
    exported menu on a left click, as it does on a right click. KDE's tray
    plugin has no call for this, so the KStatusNotifierItem it created is
    found on its D-Bus connection and its setIsMenu is called directly.
    Returns False when that is not possible; the left click then stays as it
    is."""
    try:
        from PyQt6 import sip
        from PyQt6.QtDBus import QDBusConnection
        conn = QDBusConnection(f"org.kde.StatusNotifierItem-{os.getpid()}-1")
        if not conn.isConnected():
            return False
        dbus_object = conn.objectRegisteredAt("/StatusNotifierItem")
        item = dbus_object.parent() if dbus_object is not None else None
        if item is None or item.metaObject().className() != "KStatusNotifierItem":
            return False
        library = ctypes.CDLL("libKF6StatusNotifierItem.so.6")
        set_is_menu = library._ZN19KStatusNotifierItem9setIsMenuEb
        set_is_menu.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        set_is_menu.restype = None
        set_is_menu(sip.unwrapinstance(item), True)
    except (ImportError, OSError, AttributeError):
        return False
    # A changed tooltip makes Plasma read the item's properties again
    tip = tray.toolTip()
    tray.setToolTip(tip + " ")
    tray.setToolTip(tip)
    return True


def make_icon(alert=False):
    return svg_icon(BELL_SVG_ALERT if alert else BELL_SVG_NORMAL)


TRAY_ICON_DIR = os.path.expanduser("~/.cache/knotifier/icons")
TRAY_ICON_NAMES = {False: "knotifierbell", True: "knotifierbellalert"}
SNI_INTERFACE = "org.kde.StatusNotifierItem"
SNI_PATH = "/StatusNotifierItem"
SNI_WATCHER = "org.kde.StatusNotifierWatcher"


def write_tray_icons():
    folder = os.path.join(TRAY_ICON_DIR, "hicolor", "scalable", "apps")
    os.makedirs(folder, exist_ok=True)
    for alert, name in TRAY_ICON_NAMES.items():
        with open(os.path.join(folder, name + ".svg"), "w", encoding="utf-8") as f:
            f.write(BELL_SVG_ALERT if alert else BELL_SVG_NORMAL)


@pyqtClassInfo("D-Bus Interface", SNI_INTERFACE)
class PlasmaTrayItem(QObject):
    """The tray icon under X11, registered with Plasma directly.
    KDE's own tray item always hands Plasma a menu: Plasma then draws it
    itself at the click, over the panel, and a right click never reaches the
    app. This item has no menu, so Plasma passes a left and a right click on
    (Activate, ContextMenu) and the app opens its menu itself, above the
    panel."""
    clicked = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.alert = False
        self.bus = QDBusConnection.sessionBus()
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.watch = None

    def start(self):
        """Returns False when Plasma's tray cannot be reached this way."""
        try:
            write_tray_icons()
        except OSError:
            return False
        if not self.bus.isConnected() or not self.bus.registerService(self.name):
            return False
        options = (QDBusConnection.RegisterOption.ExportAllSlots
                   | QDBusConnection.RegisterOption.ExportAllProperties)
        if not self.bus.registerObject(SNI_PATH, self, options):
            self.bus.unregisterService(self.name)
            return False
        if not self.register():
            self.bus.unregisterObject(SNI_PATH)
            self.bus.unregisterService(self.name)
            return False
        # After a restart of plasmashell the tray has to be told again
        self.watch = QDBusServiceWatcher(
            SNI_WATCHER, self.bus,
            QDBusServiceWatcher.WatchModeFlag.WatchForRegistration, self)
        self.watch.serviceRegistered.connect(lambda _name: self.register())
        return True

    def register(self):
        msg = QDBusMessage.createMethodCall(SNI_WATCHER, "/StatusNotifierWatcher",
                                            SNI_WATCHER, "RegisterStatusNotifierItem")
        msg.setArguments([self.name])
        return self.bus.call(msg).type() == QDBusMessage.MessageType.ReplyMessage

    def set_alert(self, alert):
        if alert != self.alert:
            self.alert = alert
            self.bus.send(QDBusMessage.createSignal(SNI_PATH, SNI_INTERFACE, "NewIcon"))

    # -- D-Bus properties and methods read and called by Plasma --------------
    @pyqtProperty(str)
    def Category(self):
        return "ApplicationStatus"

    @pyqtProperty(str)
    def Id(self):
        return APP_NAME

    @pyqtProperty(str)
    def Title(self):
        return APP_NAME

    @pyqtProperty(str)
    def Status(self):
        return "Active"

    @pyqtProperty(str)
    def IconName(self):
        return TRAY_ICON_NAMES[self.alert]

    @pyqtProperty(str)
    def IconThemePath(self):
        return TRAY_ICON_DIR

    @pyqtProperty(bool)
    def ItemIsMenu(self):
        return False

    @pyqtProperty(QDBusObjectPath)
    def Menu(self):
        return QDBusObjectPath("/NO_DBUSMENU")

    @pyqtSlot(int, int)
    def Activate(self, _x, _y):
        self.clicked.emit()

    @pyqtSlot(int, int)
    def ContextMenu(self, _x, _y):
        self.clicked.emit()

    @pyqtSlot(int, int)
    def SecondaryActivate(self, _x, _y):
        pass


class _XIEventMask(ctypes.Structure):
    _fields_ = [("deviceid", ctypes.c_int), ("mask_len", ctypes.c_int),
                ("mask", ctypes.POINTER(ctypes.c_ubyte))]


class _XGenericEventCookie(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong),
                ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
                ("extension", ctypes.c_int), ("evtype", ctypes.c_int),
                ("cookie", ctypes.c_uint), ("data", ctypes.c_void_p)]


class _XIRawEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong),
                ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
                ("extension", ctypes.c_int), ("evtype", ctypes.c_int),
                ("time", ctypes.c_ulong), ("deviceid", ctypes.c_int),
                ("sourceid", ctypes.c_int), ("detail", ctypes.c_int)]


class _XEvent(ctypes.Union):
    _fields_ = [("type", ctypes.c_int), ("cookie", _XGenericEventCookie),
                ("pad", ctypes.c_long * 24)]


class MousePressWatch(QObject):
    """Reports every mouse button press on the X11 screen, wherever it lands.
    A menu the app opens itself only sees a click outside it while it holds
    the mouse (an X11 grab), and X11 allows one grab at a time: while another
    program holds one, such as a remote desktop window that grabs the
    keyboard, the menu opens without it and never closes on a click outside
    it. Raw input events reach every program that asks for them, grab or no
    grab, so the app can close its menu itself."""
    pressed = pyqtSignal()

    GENERIC_EVENT = 35
    XI_RAW_BUTTON_PRESS = 15
    XI_ALL_MASTER_DEVICES = 1
    WHEEL_BUTTONS = (4, 5, 6, 7)

    def __init__(self):
        super().__init__()
        self.display = None
        self.notifier = None

    def start(self):
        """Returns False when raw input cannot be watched."""
        try:
            self.x11 = ctypes.CDLL("libX11.so.6")
            self.xi = ctypes.CDLL("libXi.so.6")
        except OSError:
            return False
        x11, xi = self.x11, self.xi
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
        x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x11.XConnectionNumber.argtypes = [ctypes.c_void_p]
        x11.XPending.argtypes = [ctypes.c_void_p]
        x11.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.POINTER(_XEvent)]
        x11.XGetEventData.argtypes = [ctypes.c_void_p, ctypes.POINTER(_XGenericEventCookie)]
        x11.XFreeEventData.argtypes = [ctypes.c_void_p, ctypes.POINTER(_XGenericEventCookie)]
        x11.XQueryExtension.argtypes = [ctypes.c_void_p, ctypes.c_char_p] + [ctypes.POINTER(ctypes.c_int)] * 3
        x11.XFlush.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        xi.XIQueryVersion.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_int)] * 2
        xi.XISelectEvents.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                      ctypes.POINTER(_XIEventMask), ctypes.c_int]

        display = x11.XOpenDisplay(None)
        if not display:
            return False
        opcode, event, error = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        major, minor = ctypes.c_int(2), ctypes.c_int(2)
        # Raw events reach a program while another one holds a grab only
        # from XInput 2.1 on
        if (not x11.XQueryExtension(display, b"XInputExtension", ctypes.byref(opcode),
                                    ctypes.byref(event), ctypes.byref(error))
                or xi.XIQueryVersion(display, ctypes.byref(major), ctypes.byref(minor)) != 0
                or (major.value, minor.value) < (2, 1)):
            x11.XCloseDisplay(display)
            return False
        self.opcode = opcode.value
        self.mask_bytes = (ctypes.c_ubyte * 4)()
        self.mask_bytes[self.XI_RAW_BUTTON_PRESS // 8] |= 1 << (self.XI_RAW_BUTTON_PRESS % 8)
        mask = _XIEventMask(self.XI_ALL_MASTER_DEVICES, len(self.mask_bytes),
                            ctypes.cast(self.mask_bytes, ctypes.POINTER(ctypes.c_ubyte)))
        xi.XISelectEvents(display, x11.XDefaultRootWindow(display), ctypes.byref(mask), 1)
        x11.XFlush(display)
        self.display = display
        self.notifier = QSocketNotifier(x11.XConnectionNumber(display),
                                        QSocketNotifier.Type.Read, self)
        self.notifier.activated.connect(self.read_events)
        return True

    def read_events(self, *_args):
        """Handles every event already received. Also called before the app
        acts on a click on its tray icon, so a press that came before that
        click is always handled first."""
        if self.display is None:
            return
        x11 = self.x11
        event = _XEvent()
        while x11.XPending(self.display):
            x11.XNextEvent(self.display, ctypes.byref(event))
            cookie = event.cookie
            if cookie.type != self.GENERIC_EVENT or cookie.extension != self.opcode:
                continue
            if not x11.XGetEventData(self.display, ctypes.byref(cookie)):
                continue
            try:
                if cookie.evtype == self.XI_RAW_BUTTON_PRESS:
                    raw = ctypes.cast(cookie.data, ctypes.POINTER(_XIRawEvent)).contents
                    if raw.detail not in self.WHEEL_BUTTONS:
                        self.pressed.emit()
            finally:
                x11.XFreeEventData(self.display, ctypes.byref(cookie))


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
        # The menu is rebuilt only when its content changes. Rebuilding it
        # while it opens sends Plasma a new menu while Plasma places it.
        self.rebuild_menu()
        self.menu.aboutToShow.connect(self.on_menu_about_to_show)
        # Under X11 the app's own tray item opens the menu on a left and a
        # right click, above the panel. Elsewhere, or when that item cannot
        # be registered, Qt's tray icon is used as before.
        self.item = None
        if self.app.platformName() != "wayland":
            item = PlasmaTrayItem()
            if item.start():
                self.item = item
                self.item.clicked.connect(self.open_menu)
        if self.item is None:
            self.tray.setContextMenu(self.menu)
            self.tray.activated.connect(self.on_activated)
            self.tray.show()
            if self.app.platformName() == "wayland":
                let_plasma_open_menu_on_left_click(self.tray)

        # Under X11 the menu the app opens itself is closed by the app on a
        # press outside it, also when it could not get hold of the mouse.
        self.closed_by_press = False
        self.presses = None
        if self.app.platformName() == "xcb":
            presses = MousePressWatch()
            if presses.start():
                self.presses = presses
                self.presses.pressed.connect(self.on_mouse_press)

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
        self.rebuild_menu()
        self.set_alert(True)

    def set_alert(self, alert):
        if self.item is not None:
            self.item.set_alert(alert)
        else:
            self.tray.setIcon(self.icon_alert if alert else self.icon_normal)

    # -- menu ---------------------------------------------------------------
    def on_menu_about_to_show(self):
        self.set_alert(False)

    def rebuild_menu(self):
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
                a1 = QAction(f"{bell}{title}", self.menu)
                self.menu.addAction(a1)
                msg = " ".join(message.split())
                if len(msg) > MAX_MESSAGE_CHARS:
                    msg = msg[:MAX_MESSAGE_CHARS - 1] + "\u2026"
                a2 = QAction(f"{stamp} - {msg}", self.menu)
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
            self.open_menu()

    def on_mouse_press(self):
        # A press outside the open menu closes it. When that press was on
        # the tray icon, the click Plasma passes on afterwards must not open
        # the menu again: closed_by_press tells open_menu so. Any later press
        # starts afresh.
        if self.menu.isVisible() and not self.menu.geometry().contains(QCursor.pos()):
            self.closed_by_press = True
            self.menu.hide()
        else:
            self.closed_by_press = False

    def open_menu(self):
        if self.presses is not None:
            self.presses.read_events()
        if self.closed_by_press:
            self.closed_by_press = False
            return
        if self.menu.isVisible():
            # The menu is open without hold of the mouse: a click on the
            # tray icon closes it, as it does when the menu holds the mouse
            self.menu.hide()
            return
        self.rebuild_menu()
        # Open the menu above the click, with its bottom at the mouse or
        # at the top of the panel, whichever is higher. Left to itself,
        # Qt only keeps the menu inside the whole screen, so it covers
        # the panel.
        cursor = QCursor.pos()
        screen = QApplication.screenAt(cursor) or QApplication.primaryScreen()
        free = screen.availableGeometry()
        self.menu.ensurePolished()
        height = self.menu.sizeHint().height()
        bottom = min(cursor.y(), free.bottom() + 1)
        self.menu.popup(QPoint(cursor.x(), bottom - height))

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
