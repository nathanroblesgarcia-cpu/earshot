"""The one recorder and worker shared by the tray icon and the web UI."""
import threading
import time
from datetime import datetime

import db
import recall_feed
from config import AUDIO_DIR, DETECT_CALLS, MAX_RECORDING_HOURS
from pipeline import Worker, raw_paths
from recorder import Recorder

recorder = Recorder()
worker = Worker()
_lock = threading.Lock()
_meeting_id = None
_listeners = []  # called with no args whenever recording starts or stops
_messengers = []  # called with a message for the user (the tray shows it as a notification)


def on_change(fn):
    _listeners.append(fn)


def on_message(fn):
    _messengers.append(fn)


def announce(text):
    for fn in _messengers:
        try:
            fn(text)
        except Exception:
            pass


def _notify():
    for fn in _listeners:
        try:
            fn()
        except Exception:
            pass


def start_recording(title=None, source=None):
    global _meeting_id
    with _lock:
        if recorder.active:
            return _meeting_id
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        with db.connect() as con:
            cur = con.execute(
                "INSERT INTO meetings (title, started_at, status, source) VALUES (?, ?, 'recording', ?)",
                ((title or "").strip() or None, datetime.now().isoformat(timespec="seconds"), source))
            meeting_id = cur.lastrowid
        try:
            recorder.start(*raw_paths(meeting_id))
        except Exception:
            with db.connect() as con:
                con.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
            raise
        _meeting_id = meeting_id
    _notify()
    return meeting_id


def stop_recording():
    global _meeting_id
    with _lock:
        if not recorder.active:
            return None
        meeting_id = _meeting_id
        duration = recorder.stop()
        _meeting_id = None
        with db.connect() as con:
            con.execute("UPDATE meetings SET ended_at = ?, duration_s = ? WHERE id = ?",
                        (datetime.now().isoformat(timespec="seconds"), duration, meeting_id))
        worker.process(meeting_id)
    _notify()
    return meeting_id


def status():
    return {
        "recording": recorder.active,
        "meeting_id": _meeting_id,
        "elapsed": round(recorder.elapsed()),
        "levels": recorder.levels() if recorder.active else {},
        "devices": recorder.devices if recorder.active else {},
        "progress": worker.progress,
        "queued": worker.jobs.qsize(),
    }


def _watchdog():
    """Stops a recording that has been left running for MAX_RECORDING_HOURS."""
    while True:
        time.sleep(30)
        if recorder.active and recorder.elapsed() > MAX_RECORDING_HOURS * 3600:
            stop_recording()
            announce(f"Recording stopped after {MAX_RECORDING_HOURS} hours.")


def start():
    db.init()
    recall_feed.backfill()
    worker.start()
    threading.Thread(target=_watchdog, name="earshot-watchdog", daemon=True).start()
    import dues
    dues.start(announce)
    if DETECT_CALLS:
        from detect import CallWatcher  # imports control, so only after it is set up
        CallWatcher().start()
