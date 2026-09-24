"""Who was on a call, and the people profiles built up from calls."""
import re
from datetime import datetime

import db

# Profile sections, in page order. Neutral on purpose: these are notes about what
# people are doing and asking for, not performance judgments (that lives in the
# development profiles, which Earshot only links to).
SECTIONS = [
    ("working_on", "Working on"),
    ("raised", "Raised"),
    ("commitments", "Said they'd do"),
    ("observations", "Other notes"),
]


def short_name(name):
    return name.split()[0]


def names_of(person):
    """Every lowercase name a person answers to: full name, first name, aliases."""
    names = {person["name"].lower(), short_name(person["name"]).lower()}
    names |= {a.strip().lower() for a in (person["aliases"] or "").split(",") if a.strip()}
    return names


def all_people(con):
    return con.execute("SELECT * FROM people ORDER BY name COLLATE NOCASE").fetchall()


def lookup(con, name):
    """The person answering to this name (full, first or alias), or None."""
    key = name.strip().lower()
    return next((p for p in all_people(con) if key in names_of(p)), None)


def find_or_create(con, name):
    name = " ".join(name.split())
    person = lookup(con, name)
    if person:
        return person["id"]
    cur = con.execute("INSERT INTO people (name, created_at) VALUES (?, ?)",
                      (name, datetime.now().isoformat(timespec="seconds")))
    return cur.lastrowid


def on_call(con, meeting_id):
    return con.execute("""SELECT p.* FROM people p JOIN meeting_people mp ON mp.person_id = p.id
                          WHERE mp.meeting_id = ? ORDER BY p.name COLLATE NOCASE""",
                       (meeting_id,)).fetchall()


def set_on_call(meeting_id, names):
    """Replace who was on this call. Unknown names become new people. Marks the
    meeting so its profile notes get rebuilt."""
    with db.connect() as con:
        ids = {find_or_create(con, n) for n in names if n.strip()}
        con.execute("DELETE FROM meeting_people WHERE meeting_id = ?", (meeting_id,))
        con.executemany("INSERT INTO meeting_people (meeting_id, person_id) VALUES (?, ?)",
                        [(meeting_id, pid) for pid in ids])
        # Edited by hand now, so it no longer counts as tagged from the Teams window.
        con.execute("UPDATE meetings SET people_dirty = 1, tagged_from = NULL WHERE id = ?",
                    (meeting_id,))
        return on_call(con, meeting_id)


def mentioned(con, meeting_id):
    """Known people whose name comes up in the call but who aren't tagged yet:
    one-tap suggestions, so he rarely has to type a name."""
    text = " ".join(r["text"] for r in con.execute(
        "SELECT text FROM segments WHERE meeting_id = ?", (meeting_id,))).lower()
    m = con.execute("SELECT summary FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    text += " " + ((m["summary"] if m else "") or "").lower()
    tagged = {p["id"] for p in on_call(con, meeting_id)}
    out = []
    for p in all_people(con):
        if p["id"] in tagged:
            continue
        if any(re.search(rf"\b{re.escape(n)}\b", text) for n in names_of(p)):
            out.append(p)
    return out


def them_label(people_on_call):
    """With exactly one other person on the call, "Them" is that person."""
    return short_name(people_on_call[0]["name"]) if len(people_on_call) == 1 else "Them"


def match(people_on_call, name):
    """Map a name the model wrote back to one of the tagged people. Notes about anyone
    else (someone only mentioned, like Priya in Maya's 1:1) are refused, never filed
    under the tagged person."""
    key = (name or "").strip().lower()
    for p in people_on_call:
        if key in names_of(p) or (key and short_name(key) in names_of(p)):
            return p
    if len(people_on_call) == 1 and key in ("", "them", "the other person"):
        return people_on_call[0]
    return None


def owns(person, owner):
    owner = (owner or "").strip().lower()
    return bool(owner) and (owner in names_of(person) or short_name(owner) in names_of(person))


def profile(con, person_id):
    """Everything the person page and the MCP show about one person."""
    person = con.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
    if person is None:
        return None
    meetings = con.execute("""
        SELECT m.* FROM meetings m JOIN meeting_people mp ON mp.meeting_id = m.id
        WHERE mp.person_id = ? ORDER BY m.started_at DESC""", (person_id,)).fetchall()
    rows = con.execute("""
        SELECT n.section, n.text, n.meeting_id, m.title, m.started_at
        FROM person_notes n JOIN meetings m ON m.id = n.meeting_id
        WHERE n.person_id = ? ORDER BY m.started_at DESC, n.id""", (person_id,)).fetchall()
    notes = {key: [r for r in rows if r["section"] == key] for key, _ in SECTIONS}
    open_actions = con.execute("""
        SELECT a.*, m.title, m.started_at FROM action_items a JOIN meetings m ON m.id = a.meeting_id
        WHERE a.done = 0 ORDER BY m.started_at DESC, a.id""").fetchall()
    call_ids = {m["id"] for m in meetings}
    theirs = [a for a in open_actions if owns(person, a["owner"])]
    mine = [a for a in open_actions if a["meeting_id"] in call_ids and a["owner"] == "Sam"]
    return {"person": person, "meetings": meetings, "notes": notes,
            "their_actions": theirs, "my_actions": mine}
