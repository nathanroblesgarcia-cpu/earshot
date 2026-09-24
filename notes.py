"""Saving a meeting's notes, whoever wrote them: the local model after a call, or Claude
correcting them through the Earshot MCP ("fix meeting 3's notes")."""
import json
import re
from difflib import SequenceMatcher

import people

SECTION_KEYS = [key for key, _ in people.SECTIONS]


def save(con, meeting_id, notes, source):
    """notes: {title?, summary, decisions, action_items [{task, owner, due}], open_questions}.
    source: "local" (Ollama) or "claude". The local model only names an untitled meeting;
    Claude's title replaces it. Ticked action items stay ticked when the task is unchanged."""
    clean = lambda items: [i.strip() for i in items if i and i.strip()]  # noqa: E731
    title = (notes.get("title") or "").strip() or None
    title_sql = "COALESCE(title, ?)" if source == "local" else "COALESCE(?, title)"
    con.execute(
        f"UPDATE meetings SET title = {title_sql}, summary = ?, decisions = ?, questions = ?, "
        "summary_error = NULL, notes_source = ? WHERE id = ?",
        (title, "\n".join(f"- {b}" for b in clean(notes.get("summary", []))),
         json.dumps(clean(notes.get("decisions", [])), ensure_ascii=False),
         json.dumps(clean(notes.get("open_questions", [])), ensure_ascii=False),
         source, meeting_id))
    done = {r["task"].strip().lower() for r in con.execute(
        "SELECT task FROM action_items WHERE meeting_id = ? AND done = 1", (meeting_id,))}
    # Dates he set by hand survive a rewrite of the notes, like ticks do.
    manual = {r["task"].strip().lower(): r["due_date"] for r in con.execute(
        "SELECT task, due_date FROM action_items WHERE meeting_id = ? AND due_manual = 1", (meeting_id,))}
    con.execute("DELETE FROM action_items WHERE meeting_id = ?", (meeting_id,))
    con.executemany(
        "INSERT INTO action_items (meeting_id, task, owner, due, done) VALUES (?, ?, ?, ?, ?)",
        [(meeting_id, a["task"].strip(), (a.get("owner") or "").strip() or "Unassigned",
          (a.get("due") or "").strip(), int(a["task"].strip().lower() in done))
         for a in notes.get("action_items", []) if (a.get("task") or "").strip()])
    for task, due_date in manual.items():
        con.execute("UPDATE action_items SET due_date = ?, due_manual = 1 WHERE meeting_id = ? "
                    "AND lower(trim(task)) = ?", (due_date, meeting_id, task))


DUE_WORDS = re.compile(r"\b(today|tomorrow|tonight|this week|next week|monday|tuesday|wednesday|thursday|"
                       r"friday|saturday|sunday|end of (?:the )?(?:day|week|month))\b", re.I)


def commitments_to_actions(con, meeting_id):
    """The notes model often misses what the OTHER person promised ("I will scope the
    metrics with Mark tomorrow" in QA). The profile pass looks for exactly that, so each
    of its "said they'd do" notes that isn't already an action item becomes one, owned by
    that person. Only for local-model notes; Claude-corrected notes are left alone."""
    m = con.execute("SELECT notes_source FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    if m is None or m["notes_source"] == "claude":
        return 0
    existing = [r["task"].lower() for r in con.execute(
        "SELECT task FROM action_items WHERE meeting_id = ?", (meeting_id,))]
    added = 0
    for r in con.execute("""SELECT p.name, n.text FROM person_notes n JOIN people p ON p.id = n.person_id
                            WHERE n.meeting_id = ? AND n.section = 'commitments'""", (meeting_id,)).fetchall():
        text = r["text"].strip()
        if any(SequenceMatcher(None, text.lower(), e).ratio() >= 0.6 or e in text.lower() or text.lower() in e
               for e in existing):
            continue
        due = DUE_WORDS.search(text)
        con.execute("INSERT INTO action_items (meeting_id, task, owner, due) VALUES (?, ?, ?, ?)",
                    (meeting_id, text[0].upper() + text[1:], r["name"], due.group(0).capitalize() if due else ""))
        existing.append(text.lower())
        added += 1
    return added


ASKS = re.compile(r"\b(can you|could you|would you|will you|please|pwede mo|pwede bang|paki\w*|ikaw na|"
                  r"send me|let me know|check mo|gawin mo)\b", re.I)
PROMISES = re.compile(r"\b(i will|i'll|i can|let me|ako na|gagawin ko|i-\w+ ko|\w+ ko (?:na|rin|din|mamaya)|"
                      r"ask ko|send ko|scope ko|check ko)\b", re.I)
STOP = {"the", "and", "for", "with", "that", "this", "from", "can", "you", "will", "send", "yung", "lang",
        "natin", "mo", "ko", "sa", "ng", "na", "by", "to", "of", "a", "an", "me"}


def _key_words(text):
    return {w for w in re.findall(r"\w+", text.lower()) if len(w) > 2 and w not in STOP}


def fix_owners(con, meeting_id):
    """Check each action item's owner against the line it came from, instead of trusting
    the small model's guess. "Can you send me the menu reference?" from them = Sam's
    task; "I will scope the metrics" from them = theirs. The same rules in reverse for his
    own lines. Only with local notes and exactly one other person tagged (in a group call
    "them" can't be tied to a name). Returns how many owners changed."""
    from transcript import merged
    m = con.execute("SELECT notes_source, chat_group FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    tagged = people.on_call(con, meeting_id)
    if m is None or m["notes_source"] == "claude" or m["chat_group"] or len(tagged) != 1:
        return 0
    other = tagged[0]["name"]
    # Sentences, not whole turns: one turn can hold both a promise and a request.
    sentences = [(line["speaker"], s) for line in merged(con.execute(
        "SELECT start, end, speaker, text FROM segments WHERE meeting_id = ? ORDER BY start", (meeting_id,)).fetchall())
        for s in re.split(r"(?<=[.?!])\s+", line["text"]) if s.strip()]
    changed = 0
    for a in con.execute("SELECT * FROM action_items WHERE meeting_id = ?", (meeting_id,)).fetchall():
        want = _key_words(a["task"])
        best, score = None, 1
        for speaker, text in sentences:
            s = len(want & _key_words(text))
            if s > score:
                best, score = (speaker, text), s
        if best is None:
            continue
        speaker, text = best
        asks, promises = bool(ASKS.search(text)), bool(PROMISES.search(text))
        if asks == promises:
            continue  # both or neither: can't tell, keep the model's call
        if speaker == "Them":
            owner = "Sam" if asks else other
        else:
            owner = other if asks else "Sam"
        if owner != a["owner"]:
            con.execute("UPDATE action_items SET owner = ? WHERE id = ?", (owner, a["id"]))
            changed += 1
    return changed


def save_people(con, meeting_id, entries):
    """Replace this call's profile notes. entries: [{name, working_on, raised, commitments,
    observations}], names matched to the people tagged on the call. Returns the ids of
    everyone whose profile changed."""
    tagged = people.on_call(con, meeting_id)
    before = {r["person_id"] for r in con.execute(
        "SELECT DISTINCT person_id FROM person_notes WHERE meeting_id = ?", (meeting_id,))}
    rows = []
    for entry in entries:
        person = people.match(tagged, entry.get("name", ""))
        if person is None:
            continue
        for key in SECTION_KEYS:
            rows += [(person["id"], meeting_id, key, t.strip()) for t in entry.get(key, []) if t and t.strip()]
    con.execute("DELETE FROM person_notes WHERE meeting_id = ?", (meeting_id,))
    con.executemany("INSERT INTO person_notes (person_id, meeting_id, section, text) VALUES (?, ?, ?, ?)", rows)
    con.execute("UPDATE meetings SET people_dirty = 0 WHERE id = ?", (meeting_id,))
    return before | {p["id"] for p in tagged}
