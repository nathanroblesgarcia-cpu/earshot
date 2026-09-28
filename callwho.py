"""Who a call is with, read from the Teams call window's title, so he never has to tag
a call by hand. Teams names its windows "<what> | Microsoft Teams" (the main window is
"Chat | Café team | Microsoft Teams"), and a call opens a window of its own.

Only windows that appeared with the call are read, never the main window: that one
shows whichever chat happens to be open, and a wrong tag on a 1:1 would write to the
wrong eval profile.
"""
import ctypes
import ctypes.wintypes as wt
import re
from datetime import datetime

import db
import people
from config import CALL_LOG, MEETINGS, TEAMS_CHATS

TEAMS_SUFFIX = "microsoft teams"
# Parts of a title that name a Teams screen, not a person.
GENERIC = {"chat", "chats", "calendar", "activity", "teams", "calls", "call", "meeting",
           "meet", "apps", "onedrive", "files", "people", "communities", "copilot",
           "settings", "notifications", "compact view", "meeting compact view"}
# The main window's title starts with the screen it's on ("Chat | ..."); a call window's doesn't.
MAIN_SCREENS = {"chat", "activity", "calendar", "teams", "calls", "apps", "onedrive", "files",
                "people", "communities", "copilot", "settings", "notifications"}
PREFIXES = ("meeting with ", "call with ", "calling ", "meeting in ", "in call with ")
MIN_NAME = 3  # never match on a name this short ("Al" inside a sentence)

_user32 = ctypes.windll.user32


def teams_windows():
    """{hwnd: title} for every visible top-level window Teams owns, desktop or web."""
    found = {}

    def each(hwnd, _):
        n = _user32.GetWindowTextLengthW(hwnd)
        if n and _user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(n + 1)
            _user32.GetWindowTextW(hwnd, buf, n + 1)
            if TEAMS_SUFFIX in buf.value.lower():
                found[hwnd] = buf.value
        return True

    _user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)(each), 0)
    return found


def is_main(title):
    return title.split("|")[0].strip().lower() in MAIN_SCREENS


def call_titles(windows, first_seen, since):
    """Titles of the Teams windows that first appeared after `since` (the call window opens
    while it rings, a little before the mic turns on) and aren't the main window."""
    return sorted(t for h, t in windows.items()
                  if first_seen.get(h, 0) >= since and not is_main(t))


def parts(title):
    """The meaningful pieces of a window title: "Maya Santos | Microsoft Teams" -> ["Maya Santos"]."""
    out = []
    for piece in title.split("|"):
        piece = " ".join(piece.split()).strip(" -")
        low = piece.lower()
        if not piece or TEAMS_SUFFIX in low or low in GENERIC:
            continue
        for prefix in PREFIXES:
            if low.startswith(prefix):
                piece = piece[len(prefix):]
                break
        out.append(piece)
    return out


def people_in(con, titles):
    """Names of known people a set of call-window titles points at. A Teams group chat
    name (from TEAMS_CHATS) is never read as a person."""
    chats = {c["chat"].lower(): c for c in TEAMS_CHATS}
    everyone = people.all_people(con)
    names = []
    for title in titles:
        for piece in parts(title):
            low = piece.lower()
            chat = chats.get(low)
            if chat:
                if chat.get("person"):
                    names.append(chat["person"])
                continue  # a group chat: nobody in particular
            exact = people.lookup(con, piece)
            if exact:
                names.append(exact["name"])
                continue
            for p in everyone:
                if any(len(n) >= MIN_NAME and re.search(rf"\b{re.escape(n)}\b", low)
                       for n in people.names_of(p)):
                    names.append(p["name"])
    return list(dict.fromkeys(names))  # de-duplicated, first seen first


def meeting_name(con, windows, first_seen, since):
    """(name, title) of a scheduled meeting's window that opened after `since`, or (None, None).
    A meeting window's title names the meeting, not a person ("Morning Huddle Meeting")."""
    newest = sorted(((first_seen.get(h, 0), t) for h, t in windows.items()
                     if first_seen.get(h, 0) >= since and not is_main(t)), reverse=True)
    for _, title in newest:
        for piece in parts(title):
            if not people_in(con, [piece]):
                return piece, title
    return None, None


def usual_people(name):
    """The usual people of a recurring meeting in config.MEETINGS, or []."""
    low = (name or "").lower()
    for m in MEETINGS:
        if m["meeting"].lower() in low:
            return list(m["people"])
    return []


def set_title(meeting_id, name):
    """Name the call after its meeting, unless it already has a title."""
    with db.connect() as con:
        con.execute("UPDATE meetings SET title = ? WHERE id = ? AND (title IS NULL OR title = '')",
                    (name, meeting_id))


def log(event, titles):
    """Every title seen, so a Teams title format this doesn't read yet can be fixed."""
    try:
        CALL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with CALL_LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {event}: "
                    + " || ".join(titles) + "\n")
    except OSError:
        pass


def tag(meeting_id, names, titles):
    """Tag the meeting with these people, unless he already tagged it himself.
    Returns True when it tagged."""
    if not names:
        return False
    with db.connect() as con:
        if people.on_call(con, meeting_id):
            return False
        ids = {people.find_or_create(con, n) for n in names}
        con.executemany("INSERT INTO meeting_people (meeting_id, person_id) VALUES (?, ?)",
                        [(meeting_id, pid) for pid in ids])
        con.execute("UPDATE meetings SET tagged_from = ? WHERE id = ?",
                    (" || ".join(titles), meeting_id))
    return True
