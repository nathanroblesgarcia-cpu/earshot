"""Earshot: local meeting note-taker. Tray icon + web UI in one process.

Run:  run.bat  (or  pyw -3.14 earshot.py)  ->  http://127.0.0.1:5175
Tray: click the icon to open Earshot; right-click to start or stop recording.
"""
import logging
import socket
import sys
import threading
import webbrowser

import pystray
from PIL import Image, ImageDraw
from werkzeug.serving import make_server

import control
from config import PORT
from web import app

URL = f"http://127.0.0.1:{PORT}"


def already_running():
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def icon_image(recording):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(220, 38, 38) if recording else (15, 118, 110))
    d.ellipse((22, 22, 42, 42), fill=(255, 255, 255))
    return img


def main():
    if already_running():
        if "--no-browser" not in sys.argv:  # a login start stays silent
            webbrowser.open(URL)
        return

    control.start()
    logging.getLogger("werkzeug").setLevel(logging.WARNING)  # the page polls status every second
    server = make_server("127.0.0.1", PORT, app, threaded=True)
    threading.Thread(target=server.serve_forever, name="earshot-web", daemon=True).start()

    def recording(_item=None):
        return control.recorder.active

    def start(icon, _item):
        try:
            control.start_recording()
        except Exception as e:
            icon.notify(f"Could not start recording: {e}", "Earshot")

    def quit_(icon, _item):
        control.stop_recording()  # queues the last call so it is processed on next launch
        server.shutdown()
        icon.stop()

    icon = pystray.Icon("earshot", icon_image(False), "Earshot", menu=pystray.Menu(
        pystray.MenuItem("Open Earshot", lambda: webbrowser.open(URL), default=True),
        pystray.MenuItem("My action items", lambda: webbrowser.open(URL.rstrip("/") + "/actions?who=me")),
        pystray.MenuItem("Start recording", start, enabled=lambda item: not recording()),
        pystray.MenuItem("Stop recording", lambda: control.stop_recording(), enabled=recording),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_),
    ))

    def refresh():
        icon.icon = icon_image(recording())
        icon.title = "Earshot: recording" if recording() else "Earshot"
        icon.update_menu()

    control.on_change(refresh)
    control.on_message(lambda text: icon.notify(text, "Earshot"))
    if "--no-browser" not in sys.argv:
        webbrowser.open(URL)
    icon.run()


if __name__ == "__main__":
    main()
