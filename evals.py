"""Link 1:1s with direct reports to their development profiles.

A call tagged with exactly one person who has an eval_profile counts as a 1:1. For a
1:1 CALL this happens automatically once its notes are done (he asked for it on
2026-09-24); a 1:1 Teams chat week uses the button, so weekly chats don't flood the
profile. Earshot:
  - saves the transcript next to their profile, like the review_<date>_transcript.txt files
  - adds a dated entry under "## Interactions & Observations" in their profile.md
The entry is found again by its "[Earshot meeting N]" tag, so redone or corrected notes
replace it (never a duplicate), and it is removed if the call stops being a 1:1 or the
meeting is deleted.
"""
import json
import re
from datetime import datetime
from pathlib import Path

import db
import people
from transcript import stamp

SECTION = "## Interactions & Observations"


def one_on_one_with(con, meeting_id):
    """The direct report this call was a 1:1 with, or None."""
    m = con.execute("SELECT chat_group FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    if m is not None and m["chat_group"]:
        return None  # a group chat week is never a 1:1, even if only one of them posted
    tagged = people.on_call(con, meeting_id)
    if len(tagged) == 1 and tagged[0]["eval_profile"] and Path(tagged[0]["eval_profile"]).exists():
        return tagged[0]
    return None


def _marker(meeting_id):
    return f"[Earshot meeting {meeting_id}]"


def _join(items):
    """"a; b; c." without the ".;" and ".." that bullets ending in a full stop produced."""
    return "; ".join(i.strip().rstrip(".;: ") for i in items if i.strip()) + "."


def _entry(m, person, actions, raised, transcript_name):
    first = people.short_name(person["name"])
    day = m["started_at"][:10]
    mins = f", {max(1, round(m['duration_s'] / 60))} min" if m["duration_s"] else ""
    parts = []
    summary = [line[2:].strip() for line in (m["summary"] or "").splitlines() if line.startswith("- ")]
    if summary:
        parts.append("**Covered:** " + _join(summary))
    decisions = json.loads(m["decisions"] or "[]")
    if decisions:
        parts.append("**Agreed:** " + _join(decisions))
    theirs = [a for a in actions if people.owns(person, a["owner"])]
    mine = [a for a in actions if a["owner"] == "Sam"]
    for label, items in ((f"{first} to", theirs), ("Sam to", mine)):
        if items:
            parts.append(f"**{label}:** " + _join(
                [a["task"].rstrip(". ") + (f" (by {a['due']})" if a["due"] else "") for a in items]))
    if raised:
        parts.append(f"**{first} raised:** " + _join(raised))
    questions = json.loads(m["questions"] or "[]")
    if questions:
        parts.append("**Open:** " + _join(questions))
    parts.append(f"Transcript: `{transcript_name}`.")
    how = "Teams chat that week, pulled by Earshot" if m["chat"] else f"recorded with Earshot{mins}"
    who = ("notes checked by Claude" if m["notes_source"] == "claude"
           else "notes by local model, check before relying on detail")
    return f"- **{day} (1:1, {how}; {who}):** " + " ".join(parts) + f" {_marker(m['id'])}"


def _write_transcript(con, m, person, folder):
    first = people.short_name(person["name"])
    rows = con.execute("SELECT start, speaker, text FROM segments WHERE meeting_id = ? ORDER BY start",
                       (m["id"],)).fetchall()
    name = f"1on1_{m['started_at'][:10]}_earshot{m['id']}_transcript.txt"
    header = [f"1:1 with {person['name']}, {m['started_at'][:16].replace('T', ' ')}",
              ("Teams chat messages pulled by Earshot." if m["chat"] else
               "Recorded and transcribed locally with Earshot (faster-whisper).") + "", ""]
    lines = [f"[{stamp(m, r['start'])}] {'Sam' if r['speaker'] == 'Me' else first}: {r['text']}" for r in rows]
    (folder / name).write_text("\n".join(header + lines) + "\n", encoding="utf-8")
    return name


def _upsert(profile_text, entry, marker):
    lines = profile_text.splitlines()
    for i, line in enumerate(lines):
        if marker in line:
            lines[i] = entry
            return "\n".join(lines) + "\n"
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == SECTION)
    except StopIteration:
        return profile_text.rstrip() + f"\n\n---\n\n{SECTION}\n\n{entry}\n"
    # End of the section: the next "---" or "## " heading.
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip() == "---" or lines[i].startswith("## ")), len(lines))
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1  # sit right after the last entry, before the blank line
    lines.insert(insert_at, entry)
    return "\n".join(lines) + "\n"


def log_to_profile(meeting_id):
    """Add or refresh this 1:1's entry in the direct report's eval profile. Returns
    {"person", "profile", "transcript", "updated"} or raises ValueError."""
    with db.connect() as con:
        m = con.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        person = one_on_one_with(con, meeting_id)
        if m is None or person is None:
            raise ValueError("This isn't a 1:1 with someone who has an eval profile.")
        if not m["summary"]:
            raise ValueError("There are no notes yet to add.")
        actions = con.execute("SELECT * FROM action_items WHERE meeting_id = ? ORDER BY id",
                              (meeting_id,)).fetchall()
        raised = [r["text"] for r in con.execute(
            "SELECT text FROM person_notes WHERE meeting_id = ? AND person_id = ? AND section = 'raised'",
            (meeting_id, person["id"]))]
        profile = Path(person["eval_profile"])
        transcript_name = _write_transcript(con, m, person, profile.parent)
    text = profile.read_text(encoding="utf-8")
    updated = _marker(meeting_id) in text
    _save(profile, _upsert(text, _entry(m, person, actions, raised, transcript_name), _marker(meeting_id)))
    with db.connect() as con:
        con.execute("UPDATE meetings SET eval_logged_at = ? WHERE id = ?",
                    (datetime.now().isoformat(timespec="seconds"), meeting_id))
    return {"person": person["name"], "profile": str(profile),
            "transcript": str(profile.parent / transcript_name), "updated": updated}


def _save(path, text):
    """Write a profile back with the line endings it already had. Path.write_text on Windows
    turns every LF into CRLF, which made a one-line addition look like the whole performance
    record had changed (seen on Priya's profile, 2026-09-24)."""
    crlf = path.exists() and b"\r\n" in path.read_bytes()
    with open(path, "w", encoding="utf-8", newline="\r\n" if crlf else "\n") as f:
        f.write(text)


def remove_from_profile(meeting_id):
    """Take a meeting's entry (and its saved transcript) out of every eval profile.
    Used when a call stops being a 1:1 or is deleted. Returns how many entries went."""
    marker = _marker(meeting_id)
    removed = 0
    with db.connect() as con:
        profiles = {r["eval_profile"] for r in con.execute("SELECT eval_profile FROM people WHERE eval_profile IS NOT NULL")}
    for path in map(Path, profiles):
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if marker in text:
            _save(path, "\n".join(line for line in text.splitlines() if marker not in line) + "\n")
            removed += 1
        for f in path.parent.glob(f"1on1_*_earshot{meeting_id}_transcript.txt"):
            f.unlink(missing_ok=True)
    with db.connect() as con:
        con.execute("UPDATE meetings SET eval_logged_at = NULL WHERE id = ?", (meeting_id,))
    return removed


def sync(meeting_id):
    """Keep the eval profile in step with the meeting, automatically. A finished 1:1 call
    with notes gets its entry added or refreshed. A call that was logged but is no longer a
    1:1 (a second person tagged) has its entry removed. Chat weeks are left to the button.
    Returns "added" | "updated" | "removed" | None."""
    with db.connect() as con:
        m = con.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        if m is None:
            return None
        person = one_on_one_with(con, meeting_id)
    if person is not None and m["summary"] and m["status"] == "done" and not m["chat"]:
        return "updated" if log_to_profile(meeting_id)["updated"] else "added"
    if person is None and m["eval_logged_at"]:
        remove_from_profile(meeting_id)
        return "removed"
    return None


def render_markdown(text):
    """Just enough markdown for the eval profiles (headings, bullets, bold, rules),
    with everything escaped first."""
    from markupsafe import escape
    html, in_list = [], False
    for raw in text.splitlines():
        line = str(escape(raw.rstrip()))
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        line = re.sub(r"`(.+?)`", r"<code>\1</code>", line)
        is_item = raw.lstrip().startswith(("- ", "* "))
        if in_list and not is_item:
            html.append("</ul>")
            in_list = False
        if is_item:
            if not in_list:
                html.append("<ul>")
                in_list = True
            html.append(f"<li>{line.lstrip()[2:]}</li>")
        elif raw.startswith("### "):
            html.append(f"<h4>{line[4:]}</h4>")
        elif raw.startswith("## "):
            html.append(f"<h3>{line[3:]}</h3>")
        elif raw.startswith("# "):
            continue  # the page already shows the name
        elif raw.strip() == "---":
            html.append("<hr>")
        elif line.strip():
            html.append(f"<p>{line}</p>")
    if in_list:
        html.append("</ul>")
    return "\n".join(html)
