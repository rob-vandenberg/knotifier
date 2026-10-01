# knotifier

A system tray application for Linux (KDE Plasma) that listens on an HTTP port and shows a desktop notification for every request it receives with the right parameters. It is a port of the Windows application Notifier and was written for receiving notifications from Home Assistant.

## Features

1. Lives in the system tray.
2. Listens on port 8765 by default. The port can be changed in `Notifier.ini`, which is reloaded while the app is running.
3. Shows a desktop notification for each request that contains a `message`.
4. Plays a sound for every priority except `low`. With `priority=low` the notification is silent.
5. Keeps the last 11 notifications and shows them in the tray menu. Both a left click and a right click on the tray icon open the menu.
6. About dialog with a clickable example URL and a "Start at login" checkbox.
7. `?cmd=quit` shuts the app down, but only when the request comes from the local machine (127.0.0.1). Requests from elsewhere get HTTP 403.
8. Only one instance can run at a time.
9. If the port is in use, the app retries every 3 seconds until it can bind.

## Requirements

1. Linux with a desktop that provides a system tray (developed for Kubuntu / KDE Plasma).
2. Python 3.
3. The packages named in the header of the script:

```
sudo apt install python3-pyqt6 python3-pyqt6.qtsvg libnotify-bin pulseaudio-utils
```

`libnotify-bin` provides `notify-send`, which is used to show the notification. If `notify-send` is not installed, the app falls back to the Qt tray balloon. `pulseaudio-utils` provides `paplay`, which plays the sound.

## Installation

1. Put `knotifier.py` in a folder of your choice, for example `~/knotifier`.
2. Install the requirements (see above).

## Running

Run in a terminal:

```
python3 knotifier.py
```

Run detached from the terminal:

```
setsid python3 knotifier.py >/dev/null 2>&1 &
```

Or use the start script `knotifier.sh`. Put it in the same folder as `knotifier.py`, make it executable with `chmod +x`, and run it. It starts `knotifier.py` detached from the terminal.

To stop the app, choose Quit in the tray menu, or run:

```
curl "http://localhost:8765/?cmd=quit"
```

## Start at login

Open the tray menu, choose About / Settings and tick "Start at login". This creates `~/.config/autostart/notifier.desktop`, which starts the app with the full path of the script. Unticking the box removes that file. If you move the script, untick and tick the box again.

## Configuration

Create a file named `Notifier.ini` in the same folder as `knotifier.py`:

```
[general]
port=8765
```

If the file is missing, or the port is not a number between 1 and 65535, port 8765 is used. Changes to the file are picked up while the app is running.

## Usage

Send an HTTP GET request with these parameters:

| Parameter | Description |
|-----------|-------------|
| `message` | The notification text. Required: a request without a message shows nothing. |
| `title` | The notification title. Defaults to `Home Assistant`. |
| `priority` | `low` gives a silent notification. Any other value, or no value, plays a sound. |
| `cmd` | `quit` shuts the app down (local machine only). |

Examples:

```
curl "http://localhost:8765/?title=Test&message=Hello+world&priority=high"
curl "http://localhost:8765/?title=Test&message=Quiet+one&priority=low"
curl "http://localhost:8765/?message=No+title+given"
```

Keep the quotes around the URL, otherwise the shell treats the `&` as a command separator. Use `+` for spaces in the values.

To send from another machine, use the address of the machine that runs knotifier:

```
curl "http://<ip-address>:8765/?title=Test&message=Remote+test"
```

The app listens on all network interfaces. If a firewall such as `ufw` is active, allow the port:

```
sudo ufw allow 8765/tcp
```

## Home Assistant

Home Assistant is the intended sender. Configure it to make an HTTP GET request to `http://<ip-address>:8765/` with the parameters described under Usage.

## Status

The application has received over 100 notifications in use without a problem.

## License

GNU Affero General Public License v3.0. See the LICENSE file.
