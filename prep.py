"""The 1:1 prep sheet: one page to read before a 1:1, so nothing has to be remembered or
typed going in. Built straight from Earshot's own data and the Team Eval profile, with no
model call, so it opens instantly and never invents anything.

Sections: what they still owe him, what he owes them, what they raised and said they'd do
since the last 1:1, calls and Teams chat weeks since then, open questions, their coaching
goals and development areas, and suggested topics drawn from all of that.
"""
import json
import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

import people

MAX_SINCE = 10        # most recent calls / chat weeks listed
MAX_NOTES = 8         # newest raised / said-they'd-do items shown (the rest are counted)
MAX_EVAL_ITEMS = 5    # bullets taken from each eval profile section
NO_ONE_ON_ONE_DAYS = 30  # with no 1:1 call yet, cover this many days
EVAL_SECTIONS = ("Coaching & Goals", "Areas for Development")


def _bullets(summary, n=2):
    return [line[2:].strip() for line in (summary or "").splitlines() if line.startswith("- ")][:n]


def eval_sections(path):
    """{"Coaching & Goals": [...], "Areas for Development": [...]} from a profile.md, plain text."""
    out = {}
    if not path or not Path(path).exists():
        return out
    current = None
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        if raw.startswith("## "):
            current = raw[3:].strip() if raw[3:].strip() in EVAL_SECTIONS else None
            continue
        if current and raw.lstrip().startswith(("- ", "* ")):
            text = re.sub(r"\*\*(.+?)\*\*", r"\1", raw.lstrip()[2:]).replace("`", "").strip()
            if len(text) > 240:
                text = text[:237].rsplit(" ", 1)[0] + "..."
            items = out.setdefault(current, [])
            if len(items) < MAX_EVAL_ITEMS:
                items.append(text)
    return out


def last_one_on_one(con, person_id):
    """Their most recent 1:1 CALL: a recorded call tagged with only this person."""
    return con.execute("""
        SELECT m.* FROM meetings m JOIN meeting_people mp ON mp.meeting_id = m.id
        WHERE mp.person_id = ? AND m.chat IS NULL AND m.status = 'done'
          AND (SELECT COUNT(*) FROM meeting_people x WHERE x.meeting_id = m.id) = 1
        ORDER BY m.started_at DESC LIMIT 1""", (person_id,)).fetchone()


def build(con, person_id):
    prof = people.profile(con, person_id)
    if prof is None:
        return None
    p = prof["person"]
    last = last_one_on_one(con, person_id)
    since = last["started_at"] if last else \
        (datetime.now() - timedelta(days=NO_ONE_ON_ONE_DAYS)).isoformat(timespec="seconds")
    # A chat week counts if any of its 7 days is on or after `since`.
    week_before = (datetime.fromisoformat(since) - timedelta(days=6)).isoformat(timespec="seconds")

    # Everything with them from the last 1:1 on (that 1:1 included): calls and chat weeks.
    recent = [m for m in prof["meetings"] if m["status"] == "done"
              and m["started_at"] >= (week_before if m["chat"] else since)]
    recent_ids = {m["id"] for m in recent}
    notes = {key: [n for n in prof["notes"][key] if n["meeting_id"] in recent_ids]
             for key in ("raised", "commitments", "working_on")}
    # A promise that is already an action ticked done isn't worth raising again.
    done = [r["task"].lower() for r in con.execute(
        f"SELECT task FROM action_items WHERE done = 1 AND meeting_id IN ({','.join('?' * len(recent_ids)) or 'NULL'})",
        tuple(recent_ids))]
    notes["commitments"] = [n for n in notes["commitments"]
                            if not any(SequenceMatcher(None, n["text"].lower(), d).ratio() >= 0.55 for d in done)]
    more = {key: max(0, len(notes[key]) - MAX_NOTES) for key in notes}
    notes = {key: items[:MAX_NOTES] for key, items in notes.items()}
    questions = []
    for m in recent:
        for q in json.loads(m["questions"] or "[]"):
            questions.append({"text": q, "meeting_id": m["id"], "started_at": m["started_at"],
                              "title": m["title"]})
    goals = eval_sections(p["eval_profile"])

    topics = []
    by_meeting = {}
    for a in prof["their_actions"]:
        by_meeting.setdefault((a["meeting_id"], a["title"]), []).append(a)
    for (mid, title), items in list(by_meeting.items())[:3]:
        topics.append(f"Progress on {title or 'their open items'} ({len(items)} open)")
    if prof["my_actions"]:
        topics.append(f"Your {len(prof['my_actions'])} open item(s) for them: done, or say when")
    for q in questions[:2]:
        topics.append(f"Still open: {q['text']}")
    for n in notes["raised"][:2]:
        topics.append(f"Follow up: {n['text']}")
    for g in goals.get("Coaching & Goals", [])[:1]:
        topics.append(f"Coaching check-in: {g}")

    return {
        "person": p, "last": last,
        "their_actions": prof["their_actions"], "my_actions": prof["my_actions"],
        "raised": notes["raised"], "commitments": notes["commitments"], "working_on": notes["working_on"],
        "more": more, "since": since,
        "recent": [{"m": m, "gist": _bullets(m["summary"])} for m in recent[:MAX_SINCE]],
        "recent_more": max(0, len(recent) - MAX_SINCE),
        "questions": questions, "goals": goals, "topics": topics,
        "is_report": bool(p["eval_profile"]),
    }


def as_text(prep):
    """Plain-text sheet for the MCP ("prep my 1:1 with Maya")."""
    p, first = prep["person"], people.short_name(prep["person"]["name"])
    last = prep["last"]
    out = [f"# Prep: 1:1 with {p['name']}",
           f"Last 1:1: {last['started_at'][:10]} ({last['title'] or 'Untitled'})" if last
           else f"No 1:1 call recorded yet, so this covers the last {NO_ONE_ON_ONE_DAYS} days."]

    def section(title, rows):
        out.extend(["", f"## {title}", *(rows or ["- (nothing)"])])

    section(f"{first} still owes you", [f"- {a['task']}{' (due ' + a['due'] + ')' if a['due'] else ''}"
                                        f" [from {a['started_at'][:10]}, meeting {a['meeting_id']}]"
                                        for a in prep["their_actions"]])
    section(f"You owe {first}", [f"- {a['task']}{' (due ' + a['due'] + ')' if a['due'] else ''}"
                                 f" [from {a['started_at'][:10]}, meeting {a['meeting_id']}]" for a in prep["my_actions"]])
    for key, title in (("raised", f"{first} raised since the last 1:1"), ("commitments", f"{first} said they'd do")):
        rows = [f"- {n['text']} [{n['started_at'][:10]}]" for n in prep[key]]
        if prep["more"][key]:
            rows.append(f"- ...and {prep['more'][key]} older")
        section(title, rows)
    section("Still-open questions", [f"- {q['text']} [{q['started_at'][:10]}]" for q in prep["questions"]])
    section("Calls and chat weeks since then", [f"- {r['m']['started_at'][:10]} {r['m']['title'] or 'Untitled'}"
                                                 + (f": {'; '.join(r['gist'])}" if r["gist"] else "")
                                                 for r in prep["recent"]])
    for name, items in prep["goals"].items():
        section(f"{name} (eval profile)", [f"- {g}" for g in items])
    section("Suggested topics", [f"{i}. {t}" for i, t in enumerate(prep["topics"], 1)])
    return "\n".join(out)
