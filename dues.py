"""Due dates and reminders for action items.

The notes say when something is due the way people said it ("Friday", "tomorrow",
"Oct 1", "immediately"). read() turns that into a real date, counted from the day of the
call, and stores it in action_items.due_date (NULL = not read yet, "" = no date said).
A date he sets by hand (due_manual = 1) is never overwritten, even by Redo notes.

Once each weekday morning the tray shows what's due today, what's overdue and what's
due tomorrow; on Mondays it also counts his undated items left open over a week.
"""
import re
import sys
import threading
import time
from datetime import date, datetime, timedelta

import db
from config import REMIND_AT, REMIND_STALE_DAYS

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
TODAY_WORDS = r"\b(today|immediate|immediately|asap|now|right away|eod|end of (the )?day|tonight|this (afternoon|morning))\b"
ME = {"sam", "sam carter", "sam", "me"}


def _weekday(base, name, nxt=False):
    """The next `name` day after base ("Friday" said on a Friday means next week's).
    "next Friday" is the Friday of next week."""
    target = WEEKDAYS.index(name)
    if nxt:
        return base + timedelta(days=7 - base.weekday() + target)
    return base + timedelta(days=(target - base.weekday()) % 7 or 7)


def _month_day(base, month, day):
    try:
        d = date(base.year, month, day)
    except ValueError:
        return None
    return d.replace(year=d.year + 1) if d < base - timedelta(days=60) else d


def read(text, base):
    """The date `text` means, said on `base` (a date), or None."""
    t = " ".join((text or "").lower().replace(",", " ").split())
    if not t:
        return None
    if m := re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t):
        try:
            return date(*map(int, m.groups()))
        except ValueError:
            return None
    mon = "|".join(MONTHS)
    if m := re.search(rf"\b({mon})[a-z]*\.? (\d{{1,2}})(st|nd|rd|th)?\b", t):
        return _month_day(base, MONTHS.index(m.group(1)) + 1, int(m.group(2)))
    if m := re.search(rf"\b(\d{{1,2}})(st|nd|rd|th)? (of )?({mon})[a-z]*\b", t):
        return _month_day(base, MONTHS.index(m.group(4)) + 1, int(m.group(1)))
    if m := re.search(r"\b(\d{1,2})/(\d{1,2})\b", t):  # day/month, as in PH and AU
        return _month_day(base, int(m.group(2)), int(m.group(1)))
    if re.search(r"\btomorrow\b|\bbukas\b", t):
        return base + timedelta(days=1)
    if re.search(TODAY_WORDS, t) or re.search(r"\bngayon\b|\bmamaya\b", t):
        return base
    if m := re.search(r"\bin (\d{1,2}) days?\b|\bwithin (\d{1,2}) days?\b", t):
        return base + timedelta(days=int(m.group(1) or m.group(2)))
    if re.search(r"\b(end of (the )?week|eow|this week|within the week)\b", t):
        friday = base + timedelta(days=4 - base.weekday())
        return friday if friday >= base else base
    if re.search(r"\bnext week\b", t):
        return base + timedelta(days=7 - base.weekday())
    if re.search(r"\b(end of (the )?month|eom)\b", t):
        first_next = (base.replace(day=1) + timedelta(days=32)).replace(day=1)
        return first_next - timedelta(days=1)
    if re.search(r"\bnext month\b", t):
        return (base.replace(day=1) + timedelta(days=32)).replace(day=1)
    # After the month checks, so "month" is never read as "Mon".
    if m := re.search(r"\b(next )?(monday|tuesday|wednesday|thursday|friday|saturday|sunday"
                      r"|mon|tues?|wed|thu|thurs?|fri|sat|sun)\b", t):
        name = next(w for w in WEEKDAYS if w.startswith(m.group(2)[:3]))
        return _weekday(base, name, nxt=bool(m.group(1)))
    return None  # "soon", "ongoing", "after deployment": no date to hold anyone to


def fill(con):
    """Read the due text of every action item not read yet. Cheap; run before any listing."""
    rows = con.execute("""SELECT a.id, a.due, m.started_at FROM action_items a
                          JOIN meetings m ON m.id = a.meeting_id WHERE a.due_date IS NULL""").fetchall()
    for r in rows:
        d = read(r["due"], datetime.fromisoformat(r["started_at"]).date())
        con.execute("UPDATE action_items SET due_date = ? WHERE id = ?", (d.isoformat() if d else "", r["id"]))
    return len(rows)


def set_date(con, action_id, value):
    """He sets (or clears, with "") the date by hand. Returns the row, or None."""
    value = (value or "").strip()
    if value:
        date.fromisoformat(value)  # raises ValueError on a bad date
    con.execute("UPDATE action_items SET due_date = ?, due_manual = 1 WHERE id = ?", (value, action_id))
    return con.execute("SELECT * FROM action_items WHERE id = ?", (action_id,)).fetchone()


def is_mine(owner):
    return (owner or "").strip().lower() in ME


def bucket(due_date, today):
    """Where an open item goes on the actions page."""
    if not due_date:
        return "none"
    d = date.fromisoformat(due_date)
    if d < today:
        return "overdue"
    if d == today:
        return "today"
    if d <= today + timedelta(days=7):
        return "week"
    return "later"


def nice(due_date, today=None):
    """"Fri 26 Sep", or "today" / "tomorrow" / "yesterday"."""
    if not due_date:
        return ""
    d, today = date.fromisoformat(due_date), today or date.today()
    words = {0: "today", 1: "tomorrow", -1: "yesterday"}
    return words.get((d - today).days) or f"{d:%a} {d.day} {d:%b}"


def digest(con, today=None):
    """The morning reminder, or None when there's nothing to say."""
    today = today or date.today()
    fill(con)
    rows = con.execute("""SELECT a.*, m.started_at FROM action_items a
                          JOIN meetings m ON m.id = a.meeting_id WHERE a.done = 0""").fetchall()
    mine = [r for r in rows if is_mine(r["owner"])]
    dated = lambda rs, test: [r for r in rs if r["due_date"] and test(date.fromisoformat(r["due_date"]))]  # noqa: E731
    overdue = sorted(dated(mine, lambda d: d < today), key=lambda r: r["due_date"])
    due_today = dated(mine, lambda d: d == today)
    tomorrow = dated(mine, lambda d: d == today + timedelta(days=1))
    theirs_overdue = dated([r for r in rows if not is_mine(r["owner"])], lambda d: d < today)
    parts = []
    if due_today:
        parts.append(f"{len(due_today)} due today")
    if overdue:
        parts.append(f"{len(overdue)} overdue")
    if tomorrow:
        parts.append(f"{len(tomorrow)} due tomorrow")
    if today.weekday() == 0:
        cutoff = (today - timedelta(days=REMIND_STALE_DAYS)).isoformat()
        stale = [r for r in mine if not r["due_date"] and r["started_at"] < cutoff]
        if stale:
            parts.append(f"{len(stale)} with no date open over a week")
    if theirs_overdue:
        parts.append(f"{len(theirs_overdue)} overdue from others")
    if not parts:
        return None
    first = (due_today or overdue or tomorrow or [None])[0]
    text = "Your action items: " + ", ".join(parts) + "."
    if first:
        text += f' First: "{first["task"][:90]}"'
        if first in overdue:
            text += f" (was due {nice(first['due_date'], today)})"
        text += "."
    return text


def _already_sent(con, today):
    row = con.execute("SELECT value FROM kv WHERE key = 'reminded_on'").fetchone()
    return row is not None and row["value"] == today.isoformat()


def remind_if_due(announce, now=None):
    """Send the morning reminder once per weekday, at or after REMIND_AT."""
    now = now or datetime.now()
    if now.weekday() > 4 or now.strftime("%H:%M") < REMIND_AT:
        return False
    with db.connect() as con:
        if _already_sent(con, now.date()):
            return False
        text = digest(con, now.date())
        con.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('reminded_on', ?)", (now.date().isoformat(),))
    if text:
        announce(text)
    return bool(text)


def start(announce):
    def loop():
        time.sleep(60)  # let the tray icon come up first, or the notification is lost
        while True:
            try:
                remind_if_due(announce)
            except Exception as e:
                print("reminders:", e, file=sys.stderr)
            time.sleep(300)
    threading.Thread(target=loop, name="earshot-reminders", daemon=True).start()
