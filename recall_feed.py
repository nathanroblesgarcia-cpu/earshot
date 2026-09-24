"""Keeps one markdown notes file per meeting in a folder Recall indexes.

Recall gets the notes (summary, decisions, action items, questions) so "what did we
decide about X" finds them. The transcript is left out on purpose: it would flood
Recall's search with chatter, and the Earshot MCP serves it when it is needed.
"""
import json
import threading
import urllib.request
from datetime import datetime

import db
import people
from config import RECALL_NOTES_DIR, RECALL_REFRESH_URL


def _path(meeting_id):
    return RECALL_NOTES_DIR / f"meeting-{meeting_id}.md"


def _person_path(person_id):
    return RECALL_NOTES_DIR / "people" / f"person-{person_id}.md"


def _render(m, actions, on_call):
    started = datetime.fromisoformat(m["started_at"])
    title = m["title"] or "Untitled meeting"
    meta = [started.strftime("%A %d %B %Y, %I:%M %p")]
    if m["duration_s"]:
        meta.append(f"{max(1, round(m['duration_s'] / 60))} min")
    if m["source"]:
        meta.append(f"on {m['source']}")
    out = [f"# Meeting: {title}", "",
           f"Earshot meeting {m['id']} ({', '.join(meta)}). Notes written by a local model from "
           "Sam's call recording. Full transcript: Earshot MCP, earshot_meeting.", ""]
    if on_call:
        out += [f"With: {', '.join(p['name'] for p in on_call)}", ""]
    if m["summary"]:
        out += ["## Summary", m["summary"], ""]
    for heading, items in (("Decisions", json.loads(m["decisions"] or "[]")),
                           ("Open questions", json.loads(m["questions"] or "[]"))):
        if items:
            out += [f"## {heading}", *[f"- {i}" for i in items], ""]
    if actions:
        out += ["## Action items"]
        for a in actions:
            due = f", due {a['due']}" if a["due"] else ""
            state = "done" if a["done"] else "open"
            out.append(f"- {a['task']} (owner {a['owner']}{due}; {state})")
        out.append("")
    return "\n".join(out)


def write(meeting_id, refresh=True):
    """Write (or rewrite) a meeting's notes file. Skips meetings with no notes yet."""
    with db.connect() as con:
        m = con.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        actions = con.execute("SELECT * FROM action_items WHERE meeting_id = ? ORDER BY id",
                              (meeting_id,)).fetchall()
        on_call = people.on_call(con, meeting_id)
    if m is None or not (m["summary"] or actions):
        return
    RECALL_NOTES_DIR.mkdir(parents=True, exist_ok=True)
    _path(meeting_id).write_text(_render(m, actions, on_call), encoding="utf-8")
    if refresh:
        refresh_recall()


def write_person(person_id, refresh=True):
    """One profile file per person: dated notes from calls, open actions, calls list."""
    with db.connect() as con:
        prof = people.profile(con, person_id)
    path = _person_path(person_id)
    if prof is None or not prof["meetings"]:
        path.unlink(missing_ok=True)
        if refresh:
            refresh_recall()
        return
    p = prof["person"]
    out = [f"# Person: {p['name']}", "",
           "Earshot profile built from Sam's calls with this person (notes by a local model)."]
    facts = [f"Role: {p['role']}" if p["role"] else "",
             f"Also called: {p['aliases']}" if p["aliases"] else "",
             f"development profiles profile: {p['eval_profile']}" if p["eval_profile"] else ""]
    out += [f for f in facts if f] + [""]
    if p["about"]:
        out += ["## Sam's notes", p["about"], ""]
    for key, heading in people.SECTIONS:
        if prof["notes"][key]:
            out.append(f"## {heading}")
            out += [f"- {n['started_at'][:10]}: {n['text']} (meeting {n['meeting_id']})"
                    for n in prof["notes"][key]]
            out.append("")
    if prof["their_actions"]:
        out += ["## Their open action items",
                *[f"- {a['task']}{', due ' + a['due'] if a['due'] else ''} (meeting {a['meeting_id']})"
                  for a in prof["their_actions"]], ""]
    if prof["my_actions"]:
        out += ["## Sam's open action items from calls with them",
                *[f"- {a['task']}{', due ' + a['due'] if a['due'] else ''} (meeting {a['meeting_id']})"
                  for a in prof["my_actions"]], ""]
    out += ["## Calls", *[f"- {m['started_at'][:10]} {m['title'] or 'Untitled meeting'} (meeting {m['id']})"
                          for m in prof["meetings"]], ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out), encoding="utf-8")
    if refresh:
        refresh_recall()


def remove(meeting_id):
    _path(meeting_id).unlink(missing_ok=True)
    refresh_recall()


def refresh_recall():
    """Ask a running Recall to re-index now. If Recall is closed, its own auto-refresh
    picks the file up later, so failure here is fine."""
    if not RECALL_REFRESH_URL:
        return

    def post():
        try:
            urllib.request.urlopen(urllib.request.Request(RECALL_REFRESH_URL, method="POST"), timeout=5)
        except Exception:
            pass
    threading.Thread(target=post, daemon=True).start()


def backfill():
    """Write notes files for every finished meeting (first run after upgrading)."""
    with db.connect() as con:
        ids = [r["id"] for r in con.execute("SELECT id FROM meetings WHERE status = 'done'")]
    for meeting_id in ids:
        if not _path(meeting_id).exists():
            write(meeting_id, refresh=False)
    if ids:
        refresh_recall()
