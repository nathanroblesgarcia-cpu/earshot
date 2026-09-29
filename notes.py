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
    Claude's title replaces it. Ticked action items stay ticked when the task is the same
    or reworded."""
    clean = lambda items: [i.strip() for i in items if i and i.strip()]  # noqa: E731
    title = (notes.get("title") or "").strip() or None
    title_sql = "COALESCE(title, ?)" if source == "local" else "COALESCE(?, title)"
    con.execute(
        f"UPDATE meetings SET title = {title_sql}, summary = ?, decisions = ?, questions = ?, "
        "summary_error = NULL, notes_source = ?, notes_behind = 0 WHERE id = ?",
        (title, "\n".join(f"- {b}" for b in clean(notes.get("summary", []))),
         json.dumps(clean(notes.get("decisions", [])), ensure_ascii=False),
         json.dumps(clean(notes.get("open_questions", [])), ensure_ascii=False),
         source, meeting_id))
    # Ticks and dates he set by hand survive a rewrite of the notes, even when the model words
    # the task a little differently ("Send Leo the sheet" -> "Send the sheet to Leo").
    old = con.execute("SELECT task, done, due_date, due_manual FROM action_items WHERE meeting_id = ? "
                      "AND (done = 1 OR due_manual = 1)", (meeting_id,)).fetchall()
    con.execute("DELETE FROM action_items WHERE meeting_id = ?", (meeting_id,))
    kept = set()
    for a in notes.get("action_items", []):
        task = (a.get("task") or "").strip()
        if not task:
            continue
        i = _same_task(task, [(i, o) for i, o in enumerate(old) if i not in kept])
        was = None if i is None else old[i]
        if i is not None:
            kept.add(i)  # one old item carries over to one new one
        con.execute(
            "INSERT INTO action_items (meeting_id, task, owner, due, done, due_date, due_manual) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (meeting_id, task, (a.get("owner") or "").strip() or "Unassigned", (a.get("due") or "").strip(),
             int(bool(was and was["done"])), was["due_date"] if was and was["due_manual"] else None,
             int(bool(was and was["due_manual"]))))


def _same_task(task, old):
    """Index of the old action item this task is a rewording of, or None: nearly the same
    text, or the same key words (at least 3, and most of both). old: [(index, row)]."""
    low, words = task.lower(), _key_words(task)
    best, score = None, 0.0
    for i, o in old:
        ratio = SequenceMatcher(None, low, o["task"].strip().lower()).ratio()
        theirs = _key_words(o["task"])
        shared = len(words & theirs)
        if shared >= 3 and shared >= 0.6 * max(len(words), len(theirs)):
            ratio = max(ratio, 0.85)
        if ratio > score:
            best, score = i, ratio
    return best if score >= 0.85 else None


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


def drop_my_commitments(con, meeting_id):
    """The profile pass sometimes puts HIS promise under someone else ("send it over" as
    Maya's commitment in QA). Each "said they'd do" note is checked against the line it
    came from: when that line is his own promise ("I'll send it over"), the note is dropped,
    before it can become their action item. Only for local notes. Returns how many went."""
    from transcript import merged
    m = con.execute("SELECT notes_source FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    if m is None or m["notes_source"] == "claude":
        return 0
    sentences = [(line["speaker"], s) for line in merged(con.execute(
        "SELECT start, end, speaker, text FROM segments WHERE meeting_id = ? ORDER BY start", (meeting_id,)).fetchall())
        for s in re.split(r"(?<=[.?!])\s+", line["text"]) if s.strip()]
    def says(speaker, text):
        """His promise = his; their promise = theirs. Their requests aren't counted: "I'm
        asking Claude to check" reads as a request but was Maya's own task (real notes,
        29 Sep), and these notes go into eval profiles, so only a clear promise decides."""
        if not PROMISES.search(text) or ASKS.search(text):
            return None
        return "his" if speaker == "Me" else "theirs"

    dropped = 0
    for n in con.execute("SELECT id, text FROM person_notes WHERE meeting_id = ? AND section = 'commitments'",
                         (meeting_id,)).fetchall():
        want = _key_words(n["text"])
        scored = [(len(want & _key_words(text)), speaker, text) for speaker, text in sentences]
        top = max((s for s, _, _ in scored), default=0)
        if top < 2:
            continue  # nothing clearly said it: keep the model's note
        verdicts = {says(sp, t) for s, sp, t in scored if s == top} - {None}
        if verdicts == {"his"}:
            con.execute("DELETE FROM person_notes WHERE id = ?", (n["id"],))
            dropped += 1
    return dropped


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
