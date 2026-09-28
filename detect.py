"""Notices calls starting and ending by watching which apps hold the microphone.

Windows keeps a per-app record under CapabilityAccessManager\\ConsentStore\\microphone:
LastUsedTimeStop is 0 while the app is using the mic. New Teams is a packaged app
(key "MSTeams_8wekyb3d8bbwe"); desktop apps sit under NonPackaged with their exe path.
"""
import subprocess
import sys
import threading
import time
import winreg
from pathlib import Path

import callwho
import control
import db
from config import AUTO_STOP_AFTER, CALL_APPS, DETECT_EVERY, MEETING_LOBBY, PORT, PROMPT_TIMEOUT, RING_TIME, TAG_LOOK_FOR

MIC_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
POPUP = Path(__file__).with_name("popup.py")


def _subkeys(key):
    i = 0
    while True:
        try:
            yield winreg.EnumKey(key, i)
        except OSError:
            return
        i += 1


def _in_use(key, name):
    try:
        with winreg.OpenKey(key, name) as k:
            start, _ = winreg.QueryValueEx(k, "LastUsedTimeStart")
            stop, _ = winreg.QueryValueEx(k, "LastUsedTimeStop")
    except OSError:
        return False
    return start > 0 and stop == 0


def call_apps_using_mic():
    """Names (from CALL_APPS) of call apps holding the mic right now. Never Earshot itself."""
    found = set()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, MIC_KEY) as root:
        names = [(root, n) for n in _subkeys(root) if n != "NonPackaged"]
        try:
            nonpackaged = winreg.OpenKey(root, "NonPackaged")
            names += [(nonpackaged, n) for n in _subkeys(nonpackaged)]
        except OSError:
            nonpackaged = None
        for key, name in names:
            low = name.lower()
            if "python" in low:
                continue  # Earshot's own recording
            app = next((label for frag, label in CALL_APPS.items() if frag in low), None)
            if app and _in_use(key, name):
                found.add(app)
        if nonpackaged:
            nonpackaged.Close()
    return found


class CallWatcher:
    def __init__(self):
        self._prev = set()
        self._popup = None          # the open "Record this call?" process
        self._popup_app = None
        self._seen_call = False     # a call app held the mic during this recording
        self._released_at = None
        self._first_seen = None     # Teams window -> when it appeared (0 = open when Earshot started)
        self._call_at = None        # when the mic came on for this call
        self._who = []              # people the call window names
        self._titles = None         # the call windows' titles (None = not looked yet)
        self._tagged = False        # this recording has been tagged (or he tagged it himself)
        self._meeting = None        # the scheduled meeting's name, when it's one

    def start(self):
        threading.Thread(target=self._loop, name="earshot-detect", daemon=True).start()

    def _loop(self):
        while True:
            try:
                self._tick(call_apps_using_mic())
            except Exception as e:
                print("detect:", e, file=sys.stderr)
            time.sleep(DETECT_EVERY)

    def _call_windows(self):
        """Titles of Teams windows that opened with this call, and who they name."""
        try:
            windows = callwho.teams_windows()
        except Exception:
            return [], []
        titles = callwho.call_titles(windows, self._first_seen or {}, self._call_at - RING_TIME)
        with db.connect() as con:
            who = callwho.people_in(con, titles)
            if not who:  # not a 1:1: maybe a scheduled meeting, whose window opened at the join screen
                self._meeting, meeting_title = callwho.meeting_name(
                    con, windows, self._first_seen or {}, self._call_at - MEETING_LOBBY)
                if self._meeting:
                    titles = [meeting_title]
        if titles != self._titles:
            callwho.log("call window" if titles else "no new window",
                        titles or sorted(windows.values()))
        return titles, who

    def _track_windows(self):
        try:
            windows = callwho.teams_windows()
        except Exception:
            return
        now = 0 if self._first_seen is None else time.time()
        seen = self._first_seen or {}
        self._first_seen = {h: seen.get(h, now) for h in windows}

    def _tick(self, apps):
        recording = control.recorder.active
        self._track_windows()
        if not apps and not recording:
            self._who, self._titles, self._call_at, self._meeting = [], None, None, None
        if apps and self._call_at is None:
            self._call_at = time.time()
        if not recording:
            self._seen_call, self._released_at, self._tagged = False, None, False
            new = apps - self._prev
            if new and self._popup is None:
                self._titles, self._who = self._call_windows()
                self._ask(sorted(new)[0])
        else:
            if not self._tagged and control.recorder.elapsed() < TAG_LOOK_FOR:
                self._auto_tag()
            if apps:
                self._seen_call, self._released_at = True, None
            elif self._seen_call:
                self._released_at = self._released_at or time.time()
                if time.time() - self._released_at >= AUTO_STOP_AFTER:
                    control.stop_recording()
                    control.announce("Call ended, so Earshot stopped recording. Notes are on the way.")
        # The call ended (or recording started another way) before anyone answered.
        popup = self._popup
        if popup and (recording or self._popup_app not in apps):
            popup.terminate()
        self._prev = apps

    def _auto_tag(self):
        meeting_id = control.status()["meeting_id"]
        if meeting_id is None:
            return
        self._call_at = self._call_at or time.time()  # recording started by hand mid-call
        if not self._who and not self._meeting:
            self._titles, self._who = self._call_windows()
        if self._meeting and not self._who:
            self._tagged = True
            callwho.set_title(meeting_id, self._meeting)
            usual = callwho.usual_people(self._meeting)
            if usual and callwho.tag(meeting_id, usual, self._titles):
                control.announce(f"Tagged {self._meeting} with its usual {len(usual)} people. "
                                 "Remove anyone who wasn't there on the meeting page.")
            return
        if self._who:
            self._tagged = True
            if callwho.tag(meeting_id, self._who, self._titles):
                with db.connect() as con:
                    report = len(self._who) == 1 and (callwho.people.lookup(con, self._who[0]) or {})
                    report = report and report["eval_profile"]
                extra = " It's a 1:1, so it goes to their eval profile once the notes are done." if report else ""
                control.announce("Tagged this call with " + ", ".join(self._who) + "." + extra
                                 + " Change it on the meeting page if that's wrong.")

    def _ask(self, app):
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        exe = str(pythonw if pythonw.exists() else sys.executable)
        self._popup_app = app
        who, prep = ", ".join(self._who) or (self._meeting or ""), ""
        if len(self._who) == 1:
            with db.connect() as con:
                person = callwho.people.lookup(con, self._who[0])
            if person:
                prep = f"http://127.0.0.1:{PORT}/people/{person['id']}/prep"
        kind = "meeting" if self._meeting and not self._who else "person"
        self._popup = subprocess.Popen([exe, str(POPUP), app, str(PROMPT_TIMEOUT), who, prep, kind],
                                       creationflags=subprocess.CREATE_NO_WINDOW)
        threading.Thread(target=self._await_answer, args=(self._popup, app), daemon=True).start()

    def _await_answer(self, proc, app):
        if proc.wait() == 0 and not control.recorder.active:
            try:
                control.start_recording(source=app)
            except Exception as e:
                control.announce(f"Could not start recording: {e}")
        self._popup = None
