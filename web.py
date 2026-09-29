"""Flask UI: meeting list, live recording status, notes + transcript viewer, search."""
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for
from markupsafe import escape

import control
import db
import dues
import evals
import people
import prep
import recall_feed
import voices
from pipeline import audio_files, fmt, track_paths
from transcript import stamp, who

app = Flask(__name__)


@app.template_filter("when")
def when(iso):
    return datetime.fromisoformat(iso).strftime("%a %d %b, %I:%M %p").replace(" 0", " ") if iso else ""


@app.template_filter("clock")
def clock(seconds):
    return fmt(seconds or 0)


@app.template_filter("mins")
def mins(seconds):
    if not seconds:
        return ""
    m = round(seconds / 60)
    if m == 0:
        return "under 1 min"
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60} min"


def meeting_or_404(con, meeting_id):
    m = con.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    if m is None:
        abort(404)
    return m


def highlight(snippet):
    """Escape transcript text, then turn the FTS match markers into <mark> tags."""
    return str(escape(snippet)).replace("\x02", "<mark>").replace("\x03", "</mark>")


def paragraphs(segments, them="Them", m=None):
    """Merge consecutive lines from the same speaker so the transcript reads like a chat.
    `them` replaces the "Them" label when he has tagged a single person on the call.
    `m` (the meeting) makes a Teams chat week show dates and times, not call offsets."""
    out = []
    for s in segments:
        voice = s["voice"] if "voice" in s.keys() else None
        if out and out[-1]["speaker"] == s["speaker"] and s["start"] - out[-1]["end"] < 3:
            out[-1]["text"] += " " + s["text"]
            out[-1]["end"] = s["end"]
            out[-1]["ids"].append(s["id"])
            out[-1]["voice"] = out[-1]["voice"] or voice
        else:
            out.append({"speaker": s["speaker"], "start": s["start"], "end": s["end"], "text": s["text"],
                        "who": who(s["speaker"], them), "label": stamp(m, s["start"]), "ids": [s["id"]],
                        "kind": "me" if s["speaker"] == "Me" else "them", "voice": voice})
    return out


WITH_NAMES = """(SELECT group_concat(p.name, ', ') FROM meeting_people mp JOIN people p ON p.id = mp.person_id
                 WHERE mp.meeting_id = m.id) AS with_names"""


# -- pages ------------------------------------------------------------------
@app.get("/")
def index():
    """Calls and Teams chat weeks on separate tabs (?show=chats), calls by default."""
    show = "chats" if request.args.get("show") == "chats" else "calls"
    with db.connect() as con:
        meetings = con.execute(f"""
            SELECT m.*, (SELECT COUNT(*) FROM action_items a
                         WHERE a.meeting_id = m.id AND a.done = 0) AS open_actions, {WITH_NAMES}
            FROM meetings m WHERE m.chat IS {'NOT ' if show == 'chats' else ''}NULL
            ORDER BY m.started_at DESC LIMIT 200""").fetchall()
        counts = con.execute("SELECT SUM(chat IS NULL) AS calls, SUM(chat IS NOT NULL) AS chats FROM meetings").fetchone()
    return render_template("index.html", meetings=meetings, show=show,
                           calls=counts["calls"] or 0, chats=counts["chats"] or 0)


@app.get("/people")
def people_list():
    with db.connect() as con:
        rows = con.execute("""
            SELECT p.*, COUNT(mp.meeting_id) AS calls, MAX(m.started_at) AS last_call
            FROM people p LEFT JOIN meeting_people mp ON mp.person_id = p.id
            LEFT JOIN meetings m ON m.id = mp.meeting_id
            GROUP BY p.id ORDER BY last_call IS NULL, last_call DESC, p.name COLLATE NOCASE""").fetchall()
    return render_template("people.html", rows=rows)


@app.get("/people/<int:person_id>")
def person(person_id):
    with db.connect() as con:
        prof = people.profile(con, person_id)
        voice = voices.learned(con, person_id)
    if prof is None:
        abort(404)
    p = prof["person"]
    eval_html = None
    if p["eval_profile"] and Path(p["eval_profile"]).exists():
        eval_html = evals.render_markdown(Path(p["eval_profile"]).read_text(encoding="utf-8"))
    return render_template("person.html", prof=prof, p=p, sections=people.SECTIONS, eval_html=eval_html,
                           voice=voice)


@app.get("/people/<int:person_id>/prep")
def person_prep(person_id):
    """The 1:1 prep sheet: read before the call, prints to one page."""
    with db.connect() as con:
        sheet = prep.build(con, person_id)
    if sheet is None:
        abort(404)
    return render_template("prep.html", prep=sheet, p=sheet["person"])


@app.post("/people/new")
def person_new():
    name = request.form.get("name", "").strip()
    if not name:
        return redirect(url_for("people_list"))
    with db.connect() as con:
        person_id = people.find_or_create(con, name)
    return redirect(url_for("person", person_id=person_id))


@app.post("/people/<int:person_id>")
def person_save(person_id):
    f = request.form
    try:
        with db.connect() as con:
            con.execute("UPDATE people SET name = ?, role = ?, aliases = ?, about = ?, eval_profile = ? "
                        "WHERE id = ?",
                        (f.get("name", "").strip() or "Unnamed", f.get("role", "").strip() or None,
                         f.get("aliases", "").strip() or None, f.get("about", "").strip() or None,
                         f.get("eval_profile", "").strip() or None, person_id))
    except sqlite3.IntegrityError:
        abort(409, "Someone else already has that name.")
    recall_feed.write_person(person_id)
    return redirect(url_for("person", person_id=person_id))


@app.post("/people/<int:person_id>/delete")
def person_delete(person_id):
    with db.connect() as con:
        con.execute("DELETE FROM people WHERE id = ?", (person_id,))
    recall_feed.write_person(person_id)  # no longer exists, so this removes the file
    return redirect(url_for("people_list"))


@app.get("/m/<int:meeting_id>")
def meeting(meeting_id):
    with db.connect() as con:
        m = meeting_or_404(con, meeting_id)
        segments = con.execute("SELECT * FROM segments WHERE meeting_id = ? ORDER BY start",
                               (meeting_id,)).fetchall()
        actions = con.execute("SELECT * FROM action_items WHERE meeting_id = ? ORDER BY id",
                              (meeting_id,)).fetchall()
        on_call = people.on_call(con, meeting_id)
        suggested = people.mentioned(con, meeting_id)
        everyone = people.all_people(con)
        one_on_one = evals.one_on_one_with(con, meeting_id)
        can_name = len(on_call) >= 2 and not m["chat"]
        # Whose voice Earshot can't pick out yet: naming one of their lines here teaches it.
        no_voice = [p["name"] for p in on_call if can_name and voices.learned(con, p["id"])[0] == 0]
    return render_template(
        "meeting.html", m=m, actions=actions, on_call=on_call, suggested=suggested, everyone=everyone,
        can_name=can_name, no_voice=no_voice,
        one_on_one=one_on_one, eval_result=request.args.get("eval"),
        can_retranscribe=all(p.exists() for p in track_paths(meeting_id)),
        transcript=paragraphs(segments, people.them_label(on_call) if on_call else "Them", m),
        decisions=json.loads(m["decisions"] or "[]"), questions=json.loads(m["questions"] or "[]"),
        summary=[line[2:] for line in (m["summary"] or "").splitlines() if line.startswith("- ")])


@app.post("/api/meetings/<int:meeting_id>/people")
def api_set_people(meeting_id):
    names = (request.get_json(silent=True) or {}).get("names", [])
    with db.connect() as con:
        meeting_or_404(con, meeting_id)
    tagged = people.set_on_call(meeting_id, names)
    control.worker.refresh_people(meeting_id)  # no-op while the call is still being processed
    return jsonify(people=[{"id": p["id"], "name": p["name"]} for p in tagged])


@app.post("/api/meetings/<int:meeting_id>/lines")
def api_set_lines(meeting_id):
    """He says who said these transcript lines: {"ids": [...], "name": "Maya Santos"} or
    name null for "Them". Only people tagged on the call can be named."""
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip() or None
    with db.connect() as con:
        meeting_or_404(con, meeting_id)
        if name and name not in {p["name"] for p in people.on_call(con, meeting_id)}:
            return jsonify(error="Only someone tagged on this call can be picked"), 400
        ids = [int(i) for i in body.get("ids", []) if con.execute(
            "SELECT 1 FROM segments WHERE id = ? AND meeting_id = ?", (int(i), meeting_id)).fetchone()]
        voices.set_line(con, ids, name)
    control.worker.refresh_voices(meeting_id)  # relearns the voice and re-checks the other lines
    return jsonify(changed=len(ids))


@app.post("/m/<int:meeting_id>/eval")
def add_to_eval(meeting_id):
    try:
        result = evals.log_to_profile(meeting_id)
    except ValueError as e:
        return redirect(url_for("meeting", meeting_id=meeting_id, eval=f"error: {e}"))
    return redirect(url_for("meeting", meeting_id=meeting_id,
                            eval="updated" if result["updated"] else "added"))


BUCKETS = [("overdue", "Overdue"), ("today", "Due today"), ("week", "Next 7 days"),
           ("later", "Later"), ("none", "No date")]


@app.get("/actions")
def actions():
    show_done = request.args.get("done") == "1"
    only_mine = request.args.get("who") == "me"
    today = datetime.now().date()
    with db.connect() as con:
        dues.fill(con)
        rows = con.execute(f"""
            SELECT a.*, m.title, m.started_at FROM action_items a JOIN meetings m ON m.id = a.meeting_id
            {'' if show_done else 'WHERE a.done = 0'}
            ORDER BY a.done, COALESCE(NULLIF(a.due_date, ''), '9999'), m.started_at DESC, a.id""").fetchall()
    if only_mine:
        rows = [r for r in rows if dues.is_mine(r["owner"])]
    groups = [(key, label, [r for r in rows if not r["done"] and dues.bucket(r["due_date"], today) == key])
              for key, label in BUCKETS]
    return render_template("actions.html", groups=[g for g in groups if g[2]],
                           finished=[r for r in rows if r["done"]], any_open=any(g[2] for g in groups),
                           show_done=show_done, only_mine=only_mine, nice=dues.nice)


@app.get("/search")
def search():
    q = request.args.get("q", "").strip()
    results = []
    words = re.findall(r"\w+", q)
    if words:
        fts = " ".join(f'"{w}"' for w in words)
        like = f"%{q}%"
        with db.connect() as con:
            hits = con.execute("""
                SELECT s.meeting_id, s.start, s.speaker,
                       snippet(segments_fts, 0, char(2), char(3), '...', 16) AS snip
                FROM segments_fts JOIN segments s ON s.id = segments_fts.rowid
                WHERE segments_fts MATCH ? ORDER BY rank LIMIT 200""", (fts,)).fetchall()
            titled = con.execute("""
                SELECT id FROM meetings WHERE title LIKE ? OR summary LIKE ?""", (like, like)).fetchall()
            ids = {h["meeting_id"] for h in hits} | {t["id"] for t in titled}
            meetings = {r["id"]: r for r in con.execute(
                f"SELECT * FROM meetings WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))} if ids else {}
        for mid, m in sorted(meetings.items(), key=lambda kv: kv[1]["started_at"], reverse=True):
            results.append({"m": m, "hits": [
                {**h, "snip": highlight(h["snip"])} for h in hits if h["meeting_id"] == mid][:5]})
    return render_template("search.html", q=q, results=results)


# -- actions on a meeting -----------------------------------------------------
@app.post("/m/<int:meeting_id>/title")
def rename(meeting_id):
    title = request.form.get("title", "").strip() or None
    with db.connect() as con:
        con.execute("UPDATE meetings SET title = ? WHERE id = ?", (title, meeting_id))
    recall_feed.write(meeting_id)
    return redirect(url_for("meeting", meeting_id=meeting_id))


@app.post("/m/<int:meeting_id>/redo")
def redo(meeting_id):
    control.worker.redo_summary(meeting_id)
    return redirect(url_for("meeting", meeting_id=meeting_id))


@app.post("/m/<int:meeting_id>/retranscribe")
def retranscribe(meeting_id):
    try:
        control.worker.redo_transcript(meeting_id)
    except FileNotFoundError:
        abort(409, "This call's separate tracks aren't kept, so it can't be transcribed again.")
    return redirect(url_for("meeting", meeting_id=meeting_id))


@app.post("/m/<int:meeting_id>/delete")
def delete(meeting_id):
    if control.status()["meeting_id"] == meeting_id:
        abort(409)
    with db.connect() as con:
        meeting_or_404(con, meeting_id)
    evals.remove_from_profile(meeting_id)  # a deleted 1:1 leaves no entry in the eval profile
    with db.connect() as con:
        m = meeting_or_404(con, meeting_id)
        tagged = [p["id"] for p in people.on_call(con, meeting_id)]
        taught = {r[0] for r in con.execute("SELECT person_id FROM voiceprints WHERE meeting_id = ?", (meeting_id,))}
        for p in (*audio_files(meeting_id), Path(m["audio_path"]) if m["audio_path"] else None):
            if p:
                p.unlink(missing_ok=True)
        con.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        # The voices this call taught are gone with it: their other calls are named again.
        voices.mark_others(con, meeting_id, taught)
    for person_id in tagged:  # their profiles lose this call's notes
        recall_feed.write_person(person_id, refresh=False)
    recall_feed.remove(meeting_id)
    return redirect(url_for("index"))


@app.get("/audio/<int:meeting_id>")
def audio(meeting_id):
    with db.connect() as con:
        m = meeting_or_404(con, meeting_id)
    if not m["audio_path"] or not Path(m["audio_path"]).exists():
        abort(404)
    return send_file(m["audio_path"], conditional=True)  # conditional = range requests, so seeking works


# -- JSON API (used by the page script and the tray) -------------------------
@app.get("/api/status")
def api_status():
    return jsonify(control.status())


@app.post("/api/start")
def api_start():
    try:
        meeting_id = control.start_recording((request.get_json(silent=True) or {}).get("title"))
    except Exception as e:
        return jsonify(error=str(e)), 500
    return jsonify(meeting_id=meeting_id)


@app.post("/api/stop")
def api_stop():
    return jsonify(meeting_id=control.stop_recording())


@app.post("/api/actions/done_all")
def api_done_all():
    """Tick every open action item, or only one meeting's with {"meeting_id": N}."""
    meeting_id = (request.get_json(silent=True) or {}).get("meeting_id")
    with db.connect() as con:
        where, args = ("AND meeting_id = ?", (int(meeting_id),)) if meeting_id else ("", ())
        touched = [r["meeting_id"] for r in con.execute(
            f"SELECT DISTINCT meeting_id FROM action_items WHERE done = 0 {where}", args)]
        n = con.execute(f"UPDATE action_items SET done = 1 WHERE done = 0 {where}", args).rowcount
    for mid in touched:
        recall_feed.write(mid, refresh=False)
    return jsonify(ticked=n)


@app.post("/api/actions/<int:action_id>/due")
def api_due(action_id):
    """Set or clear an item's due date by hand: {"date": "2026-09-30"} or {"date": ""}."""
    with db.connect() as con:
        try:
            row = dues.set_date(con, action_id, (request.get_json(silent=True) or {}).get("date", ""))
        except ValueError:
            return jsonify(error="Not a date"), 400
    if row is None:
        abort(404)
    recall_feed.write(row["meeting_id"], refresh=False)
    return jsonify(due_date=row["due_date"], label=dues.nice(row["due_date"]))


@app.post("/api/actions/<int:action_id>/toggle")
def api_toggle(action_id):
    with db.connect() as con:
        con.execute("UPDATE action_items SET done = 1 - done WHERE id = ?", (action_id,))
        row = con.execute("SELECT done, meeting_id FROM action_items WHERE id = ?", (action_id,)).fetchone()
    if row is None:
        abort(404)
    recall_feed.write(row["meeting_id"], refresh=False)  # Recall's auto-refresh picks it up
    return jsonify(done=bool(row["done"]))
