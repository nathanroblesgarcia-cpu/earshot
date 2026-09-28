"""Earshot QA, sandboxed. Every module runs against a throwaway DB, audio folder and Recall
folder, with Whisper and Ollama stubbed, so it is fast and never touches real data.

Run:  py -3.14 tests\\qa_unit.py
"""
import json
from datetime import datetime, timedelta
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path
from types import SimpleNamespace as S

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SANDBOX = Path(tempfile.mkdtemp(prefix="earshot_qa_"))

import config  # noqa: E402  (patched BEFORE anything else imports it)

config.LOCAL_DIR = SANDBOX / "local"
config.DB_PATH = config.LOCAL_DIR / "earshot.db"
config.AUDIO_DIR = config.LOCAL_DIR / "audio"
config.RECALL_NOTES_DIR = SANDBOX / "earshot_meetings"
config.RECALL_REFRESH_URL = "http://127.0.0.1:9/refresh"  # nothing listens: pings fail quietly
config.PORT = 5999                                          # MCP recorder calls find no app
config.DETECT_CALLS = False
config.CALL_LOG = config.LOCAL_DIR / "calls.log"
# The seeded people link to the sample development profiles. Point every one
# into the sandbox, or a 1:1 test writes into a real performance record (it did once,
# 2026-09-24: a fake entry landed in Leo's profile).
config.PEOPLE_SEED = [{**p, "eval_profile": str(SANDBOX / "evals" / p["name"].split()[0] / "profile.md")}
                      if p.get("eval_profile") else p for p in config.PEOPLE_SEED]
for p in config.PEOPLE_SEED:
    if p.get("eval_profile"):
        Path(p["eval_profile"]).parent.mkdir(parents=True, exist_ok=True)
        Path(p["eval_profile"]).write_text("# Sandbox\n\n## Interactions & Observations\n\n---\n", encoding="utf-8")
assert not any(str(ROOT / "sample_profiles") in str(p.get("eval_profile") or "") for p in config.PEOPLE_SEED)

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

import db  # noqa: E402
import evals  # noqa: E402
import notes  # noqa: E402
import people  # noqa: E402
import pipeline  # noqa: E402
import recall_feed  # noqa: E402
import summarise  # noqa: E402
import transcript  # noqa: E402

RESULTS = []


def check(name, fn):
    try:
        fn()
        RESULTS.append((True, name, ""))
    except Exception as e:  # noqa: BLE001
        RESULTS.append((False, name, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"))


def seg(start, end, text, nsp=0.05, lp=-0.3, cr=1.3):
    return S(start=start, end=end, text=text, no_speech_prob=nsp, avg_logprob=lp, compression_ratio=cr)


def new_meeting(con, title=None, status="done", started="2026-09-24T10:00:00", duration=600):
    return con.execute("INSERT INTO meetings (title, started_at, status, duration_s) VALUES (?, ?, ?, ?)",
                       (title, started, status, duration)).lastrowid


def add_segments(con, mid, rows):
    con.executemany("INSERT INTO segments (meeting_id, start, end, speaker, text) VALUES (?, ?, ?, ?, ?)",
                    [(mid, *r) for r in rows])


# ---------------------------------------------------------------- database
def t_db_init():
    db.init()
    db.init()  # idempotent: migrations must not fail on a second run
    with db.connect() as con:
        cols = {r["name"] for r in con.execute("PRAGMA table_info(meetings)")}
        for c in ("source", "people_dirty", "eval_logged_at", "notes_source"):
            assert c in cols, f"missing column {c}"
        names = {p["name"] for p in people.all_people(con)}
        assert {"Maya Santos", "Leo Reyes", "Priya Nair", "Omar"} <= names, names
        assert "AUTOINCREMENT" in con.execute("SELECT sql FROM sqlite_master WHERE name='meetings'").fetchone()[0]


def t_ids_never_reused():
    with db.connect() as con:
        a = new_meeting(con)
        con.execute("DELETE FROM meetings WHERE id = ?", (a,))
        b = new_meeting(con)
        con.execute("DELETE FROM meetings WHERE id = ?", (b,))
    assert b > a, (a, b)


# ---------------------------------------------------------------- people
def t_people_lookup_and_create():
    with db.connect() as con:
        assert people.lookup(con, "maya")["name"] == "Maya Santos"
        assert people.lookup(con, "PRI")["name"] == "Priya Nair"
        assert people.lookup(con, "nobody") is None
        pid = people.find_or_create(con, "  Carl   Santos ")
        assert con.execute("SELECT name FROM people WHERE id = ?", (pid,)).fetchone()[0] == "Carl Santos"
        assert people.find_or_create(con, "carl") == pid  # first name finds the same person


def t_people_tagging_and_labels():
    with db.connect() as con:
        mid = new_meeting(con)
        add_segments(con, mid, [(0, 2, "Them", "Hi, I spoke to Ana and Anastasia yesterday."),
                                (3, 4, "Me", "Okay, let's loop in Priya.")])
    tagged = people.set_on_call(mid, ["Maya", "maya santos"])  # duplicates collapse
    assert [p["name"] for p in tagged] == ["Maya Santos"]
    with db.connect() as con:
        assert con.execute("SELECT people_dirty FROM meetings WHERE id = ?", (mid,)).fetchone()[0] == 1
        sugg = {p["name"] for p in people.mentioned(con, mid)}
        assert sugg == {"Ana", "Priya Nair"}, sugg  # "Anastasia" must not match "Ana"
        assert people.them_label(people.on_call(con, mid)) == "Maya"
    two = people.set_on_call(mid, ["Maya", "Leo"])
    assert people.them_label(two) == "Them"
    assert people.match(two, "Leo")["name"] == "Leo Reyes"
    assert people.match(two, "Stranger") is None
    assert people.set_on_call(mid, []) == []


def t_people_owns():
    with db.connect() as con:
        k = people.lookup(con, "Maya")
    assert people.owns(k, "Maya") and people.owns(k, "Maya Santos") and people.owns(k, "maya l.")
    assert not people.owns(k, "Sam") and not people.owns(k, "")


# ---------------------------------------------------------------- notes
def t_notes_save_rules():
    with db.connect() as con:
        mid = new_meeting(con, title="My own title")
        notes.save(con, mid, {"title": "Model title", "summary": ["a", " ", "b"], "decisions": ["d"],
                              "action_items": [{"task": "Send sheet", "owner": "", "due": ""},
                                               {"task": " ", "owner": "x", "due": ""}],
                              "open_questions": []}, "local")
        m = con.execute("SELECT * FROM meetings WHERE id = ?", (mid,)).fetchone()
        assert m["title"] == "My own title", "local notes must not overwrite his title"
        assert m["summary"] == "- a\n- b" and m["notes_source"] == "local"
        acts = con.execute("SELECT * FROM action_items WHERE meeting_id = ?", (mid,)).fetchall()
        assert [(a["task"], a["owner"]) for a in acts] == [("Send sheet", "Unassigned")]
        con.execute("UPDATE action_items SET done = 1 WHERE meeting_id = ?", (mid,))
        notes.save(con, mid, {"title": "Claude title", "summary": ["c"], "decisions": [],
                              "action_items": [{"task": "send sheet", "owner": "Sam", "due": "Fri"},
                                               {"task": "New task", "owner": "Omar", "due": ""}],
                              "open_questions": ["q"]}, "claude")
        m = con.execute("SELECT * FROM meetings WHERE id = ?", (mid,)).fetchone()
        assert m["title"] == "Claude title" and m["notes_source"] == "claude"
        done = {a["task"]: a["done"] for a in con.execute("SELECT * FROM action_items WHERE meeting_id = ?", (mid,))}
        assert done == {"send sheet": 1, "New task": 0}, done  # tick survives, case-insensitive


def t_notes_save_people():
    with db.connect() as con:
        mid = new_meeting(con)
    people.set_on_call(mid, ["Omar"])
    with db.connect() as con:
        changed = notes.save_people(con, mid, [{"name": "Omar", "working_on": ["x", ""], "raised": ["y"]},
                                               {"name": "Ghost", "working_on": ["z"]}])
        rows = con.execute("SELECT section, text FROM person_notes WHERE meeting_id = ?", (mid,)).fetchall()
        assert sorted(map(tuple, rows)) == [("raised", "y"), ("working_on", "x")], rows
        assert con.execute("SELECT people_dirty FROM meetings WHERE id = ?", (mid,)).fetchone()[0] == 0
        assert len(changed) == 1


def t_commitments_to_actions():
    with db.connect() as con:
        mid = new_meeting(con)
        notes.save(con, mid, {"summary": ["s"], "decisions": [], "open_questions": [],
                              "action_items": [{"task": "Send menu reference by Friday", "owner": "Sam", "due": "Friday"}]},
                   "local")
    people.set_on_call(mid, ["Omar"])
    with db.connect() as con:
        notes.save_people(con, mid, [{"name": "Omar", "commitments": [
            "scope the metrics with Mark tomorrow", "send menu reference by Friday"]}])
        assert notes.commitments_to_actions(con, mid) == 1  # the duplicate is skipped
        acts = {(a["task"], a["owner"], a["due"]) for a in con.execute("SELECT * FROM action_items WHERE meeting_id = ?", (mid,))}
        assert ("Scope the metrics with Mark tomorrow", "Omar", "Tomorrow") in acts, acts
        assert notes.commitments_to_actions(con, mid) == 0  # idempotent
        con.execute("UPDATE meetings SET notes_source = 'claude' WHERE id = ?", (mid,))
        con.execute("INSERT INTO person_notes (person_id, meeting_id, section, text) VALUES "
                    "((SELECT id FROM people WHERE name='Omar'), ?, 'commitments', 'something new')", (mid,))
        assert notes.commitments_to_actions(con, mid) == 0  # Claude's notes are never changed


def t_fix_owners():
    with db.connect() as con:
        mid = new_meeting(con)
        add_segments(con, mid, [
            (0, 8, "Them", "Hi Sam. I will scope the metrics with Mark tomorrow. Can you send me the menu reference by Friday?"),
            (9, 12, "Me", "Sige, i-check ko yung stock sheet mamaya."),
            (13, 15, "Them", "Paki-update na lang yung tracker.")])
        notes.save(con, mid, {"summary": ["s"], "decisions": [], "open_questions": [], "action_items": [
            {"task": "Send menu reference by Friday", "owner": "Omar", "due": "Friday"},      # wrong: he asked me
            {"task": "Scope metrics with Mark", "owner": "Sam", "due": "Tomorrow"},   # wrong: Omar promised
            {"task": "Check the stock sheet", "owner": "Omar", "due": ""},         # wrong: I promised
            {"task": "Update the tracker", "owner": "Omar", "due": ""},                    # wrong: he asked me (paki)
            {"task": "Something unrelated entirely", "owner": "Omar", "due": ""}]}, "local")  # no source: keep
    people.set_on_call(mid, ["Omar"])
    with db.connect() as con:
        assert notes.fix_owners(con, mid) == 4
        owners = {a["task"]: a["owner"] for a in con.execute("SELECT * FROM action_items WHERE meeting_id = ?", (mid,))}
    assert owners == {"Send menu reference by Friday": "Sam", "Scope metrics with Mark": "Omar",
                      "Check the stock sheet": "Sam", "Update the tracker": "Sam",
                      "Something unrelated entirely": "Omar"}, owners
    people.set_on_call(mid, ["Omar", "Jess"])  # group call: no rewrites
    with db.connect() as con:
        assert notes.fix_owners(con, mid) == 0


# ---------------------------------------------------------------- transcript cleaning
def t_hallucinations():
    segs = ([seg(0, 1, "Okay."), seg(2, 3, "Tapos yung team mapping lang.")]
            + [seg(10 + i, 10.5 + i, "ma,") for i in range(30)]
            + [seg(50 + 2 * i, 51 + 2 * i, "It's not available.") for i in range(5)]
            + [seg(70, 71, "Okay."), seg(90, 91, "Okay."), seg(95, 96, "junk", nsp=0.9, lp=-1.5),
               seg(99, 100, "aaaa " * 6, cr=3.0), seg(101, 102, "   ")])
    assert [s.text for s in pipeline.drop_hallucinations(segs)] == \
        ["Okay.", "Tapos yung team mapping lang.", "Okay.", "Okay."]


def t_merge_and_lines():
    rows = [{"start": 0, "end": 1, "speaker": "Me", "text": "parang,"}, {"start": 1.5, "end": 2, "speaker": "Me", "text": "ano"},
            {"start": 9, "end": 10, "speaker": "Me", "text": "later"}, {"start": 10, "end": 11, "speaker": "Them", "text": "yes"}]
    m = transcript.merged(rows)
    assert [(r["speaker"], r["text"]) for r in m] == [("Me", "parang, ano"), ("Me", "later"), ("Them", "yes")], m
    assert transcript.fmt(3725) == "1:02:05" and transcript.fmt(65) == "01:05"
    with db.connect() as con:
        mid = new_meeting(con)
        add_segments(con, mid, [(0, 1, "Them", "a"), (1.2, 2, "Them", "b"), (5, 6, "Me", "c")])
        assert pipeline.transcript_lines(con, mid) == ["[00:00] Them: a b", "[00:05] Me: c"]


def t_text_bleed():
    them = [seg(0, 3, "Can you send me the team mapping sheet by Friday?")]
    assert pipeline.text_bleed(seg(0.5, 3.5, "send me the team mapping sheet"), them)
    assert pipeline.text_bleed(seg(2.8, 3.2, "Friday?"), them)
    assert not pipeline.text_bleed(seg(10, 12, "Yes I will send it"), them)  # outside the window


def _signals(rate=16000, seconds=6.0, seed=1):
    rng = np.random.default_rng(seed)
    n = int(rate * seconds)
    t = np.arange(n) / rate
    wobble = lambda f, ph: np.clip(np.sin(2 * np.pi * f * t + ph), 0, None)  # noqa: E731
    them = rng.normal(0, 1, n) * 0.3 * wobble(3.1, 0.3) * ((t > 0.5) & (t < 2.8))
    own = rng.normal(0, 1, n) * 0.3 * wobble(2.3, 1.1) * ((t > 3.4) & (t < 5.2))
    delay = int(0.55 * rate)
    bleed = np.zeros(n)
    bleed[delay:] = 0.2 * them[:-delay]
    me = bleed + own + rng.normal(0, 0.001, n)
    return me.astype("float32"), them.astype("float32"), rate


def t_sound_bleed():
    me, them, rate = _signals()
    d = SANDBOX / "sig"
    d.mkdir(exist_ok=True)
    sf.write(d / "me.wav", me, rate)
    sf.write(d / "them.wav", them, rate)
    env_me, env_them = pipeline.envelope(d / "me.wav"), pipeline.envelope(d / "them.wav")
    assert abs(len(env_me) - 6.0 / config.ENVELOPE_FRAME) <= 2, len(env_me)
    bleed_seg = seg(1.1, 3.3, "helo prom dem")  # garbled copy of them, 550 ms late
    own_seg = seg(3.5, 5.1, "this is really me")
    assert pipeline.sounds_like_bleed(bleed_seg, env_me, env_them)[0] is True
    by, active = pipeline.sounds_like_bleed(own_seg, env_me, env_them)
    assert by is False and active < config.THEM_ACTIVE, (by, active)
    assert pipeline.is_bleed(bleed_seg, [], env_me, env_them)
    # text similarity must never drop his words while the other side is silent
    assert not pipeline.is_bleed(own_seg, [seg(3.5, 5.1, "this is really me")], env_me, env_them)


def t_opus_roundtrip():
    me, them, rate = _signals(rate=48000)
    d = config.AUDIO_DIR
    d.mkdir(parents=True, exist_ok=True)
    sf.write(d / "rt_me.flac", me, 48000)
    sf.write(d / "rt_them.flac", them, 48000)
    assert pipeline.mix_to_opus(d / "rt_me.flac", d / "rt_them.flac", d / "rt.ogg")
    assert pipeline.to_opus(d / "rt_me.flac", d / "rt_me.ogg")
    info = sf.info(d / "rt.ogg")
    assert abs(info.duration - 6.0) < 0.2 and info.samplerate == 48000, info
    sf.write(d / "odd.wav", me[:44100], 44100)
    assert pipeline.to_opus(d / "odd.wav", d / "odd.ogg") is False  # 44.1 kHz can't be Opus


# ---------------------------------------------------------------- worker pipeline (stubbed)
class FakeModel:
    def transcribe(self, path, **kw):
        assert kw.get("condition_on_previous_text") is False
        name = Path(path).name
        if "_me" in name:
            segs = [seg(1.1, 3.3, "helo prom dem"), seg(3.5, 5.1, "this is really me"),
                    *[seg(5.3 + 0.1 * i, 5.35 + 0.1 * i, "ma,") for i in range(5)]]
        else:
            segs = [seg(0.5, 2.8, "Hello from them, can you send the sheet?")]
        return iter(segs), S(duration=6.0)


def run_job(worker, kind, mid):
    worker.jobs.put((kind, mid))
    kind, mid = worker.jobs.get()
    # the body of Worker._loop for one job, without the thread
    try:
        if kind == "people":
            worker._people(mid)
            recall_feed.write(mid)
            return
        if kind in ("process", "retranscribe"):
            worker._transcribe(mid, force=kind == "retranscribe")
        worker._summarise(mid)
        worker._people(mid)
        db.set_status(mid, "done")
        recall_feed.write(mid)
    except Exception as e:  # noqa: BLE001
        db.set_status(mid, "error", error=f"{type(e).__name__}: {e}")
    finally:
        worker.progress = None


FAKE_NOTES = {"title": "Sheet request", "summary": ["They asked for the sheet"], "decisions": ["Use a sheet"],
              "action_items": [{"task": "Send the sheet", "owner": "Sam", "due": "Friday"}],
              "open_questions": []}


def t_worker_full_flow():
    summarise.notes = lambda lines, names=(): FAKE_NOTES
    summarise.people_notes = lambda lines, names: [{"name": names[0], "working_on": ["the sheet"], "raised": [],
                                                    "commitments": [], "observations": []}]
    me, them, _ = _signals(rate=48000)
    with db.connect() as con:
        mid = new_meeting(con, status="recording")
    me_raw, them_raw = pipeline.raw_paths(mid)
    sf.write(me_raw, me, 48000, format="FLAC")
    sf.write(them_raw, them, 48000, format="FLAC")
    people.set_on_call(mid, ["Omar"])
    w = pipeline.Worker()
    w._model = FakeModel()
    run_job(w, "process", mid)
    with db.connect() as con:
        m = con.execute("SELECT * FROM meetings WHERE id = ?", (mid,)).fetchone()
        segs = [(r["speaker"], r["text"]) for r in con.execute("SELECT * FROM segments WHERE meeting_id = ? ORDER BY start", (mid,))]
        pn = con.execute("SELECT COUNT(*) FROM person_notes WHERE meeting_id = ?", (mid,)).fetchone()[0]
    assert m["status"] == "done" and not m["error"], dict(m)
    assert segs == [("Them", "Hello from them, can you send the sheet?"), ("Me", "this is really me")], segs
    assert m["title"] == "Sheet request" and m["notes_source"] == "local" and pn == 1
    files = sorted(p.name for p in config.AUDIO_DIR.iterdir() if p.name.startswith(f"{mid}"))
    assert files == [f"{mid}.ogg", f"{mid}_me.ogg", f"{mid}_them.ogg"], files
    assert (config.RECALL_NOTES_DIR / f"meeting-{mid}.md").exists()
    # Redo transcript works from the kept Opus tracks
    w.redo_transcript(mid)
    w.jobs.get()
    run_job(w, "retranscribe", mid)
    with db.connect() as con:
        assert con.execute("SELECT status FROM meetings WHERE id = ?", (mid,)).fetchone()[0] == "done"
        assert con.execute("SELECT COUNT(*) FROM segments WHERE meeting_id = ?", (mid,)).fetchone()[0] == 2
    globals()["FLOW_MEETING"] = mid


def t_worker_ollama_down():
    def boom(*a, **k):
        raise ConnectionRefusedError("Ollama not running")
    summarise.notes = boom
    with db.connect() as con:
        mid = new_meeting(con, status="queued")
        add_segments(con, mid, [(0, 1, "Them", "hello there")])
    w = pipeline.Worker()
    run_job(w, "summary", mid)
    with db.connect() as con:
        m = con.execute("SELECT * FROM meetings WHERE id = ?", (mid,)).fetchone()
    assert m["status"] == "done" and "Redo notes" in (m["summary_error"] or ""), dict(m)
    summarise.notes = lambda lines, names=(): FAKE_NOTES


def t_worker_empty_and_missing():
    w = pipeline.Worker()
    with db.connect() as con:
        empty = new_meeting(con, status="queued")
        missing = new_meeting(con, status="queued")
    run_job(w, "summary", empty)
    run_job(w, "process", missing)
    with db.connect() as con:
        e = con.execute("SELECT * FROM meetings WHERE id = ?", (empty,)).fetchone()
        m = con.execute("SELECT * FROM meetings WHERE id = ?", (missing,)).fetchone()
    assert e["status"] == "done" and "Nothing was said" in e["summary_error"], dict(e)
    assert m["status"] == "error" and "missing" in m["error"], dict(m)
    try:
        w.redo_transcript(missing)
        raise AssertionError("redo_transcript should refuse without kept tracks")
    except FileNotFoundError:
        pass


def t_worker_recover():
    with db.connect() as con:
        crashed_no_audio = new_meeting(con, status="recording")
        crashed_with_audio = new_meeting(con, status="recording")
        half_done = new_meeting(con, status="summarising")
    for p in pipeline.raw_paths(crashed_with_audio):
        sf.write(p, np.zeros(4800, dtype="float32"), 48000, format="FLAC")
    w = pipeline.Worker()
    w._recover()
    queued = []
    while not w.jobs.empty():
        queued.append(w.jobs.get()[1])
    with db.connect() as con:
        st = lambda i: con.execute("SELECT status FROM meetings WHERE id = ?", (i,)).fetchone()[0]  # noqa: E731
        assert st(crashed_no_audio) == "error"
        assert crashed_with_audio in queued and half_done in queued and crashed_no_audio not in queued, queued


def t_worker_purge():
    mid = FLOW_MEETING
    with db.connect() as con:
        con.execute("UPDATE meetings SET started_at = '2020-01-01T09:00:00' WHERE id = ?", (mid,))
    w = pipeline.Worker()
    w._purge_old_audio()
    with db.connect() as con:
        m = con.execute("SELECT audio_path, audio_purged FROM meetings WHERE id = ?", (mid,)).fetchone()
        assert m["audio_path"] is None and m["audio_purged"] == 1
        assert con.execute("SELECT COUNT(*) FROM segments WHERE meeting_id = ?", (mid,)).fetchone()[0] > 0, \
            "transcript must survive the audio purge"
    assert not [p for p in config.AUDIO_DIR.iterdir() if p.name.startswith(f"{mid}")]


def t_people_job_and_dirty_sweep():
    with db.connect() as con:
        mid = new_meeting(con)
        add_segments(con, mid, [(0, 1, "Them", "I'm on the cold brew docs")])
    people.set_on_call(mid, ["Maya"])
    w = pipeline.Worker()
    w._queue_dirty_people()
    kinds = []
    while not w.jobs.empty():
        kinds.append(w.jobs.get())
    assert ("people", mid) in kinds, kinds
    run_job(w, "people", mid)
    with db.connect() as con:
        assert con.execute("SELECT people_dirty FROM meetings WHERE id = ?", (mid,)).fetchone()[0] == 0
        k = people.lookup(con, "Maya")
    assert (config.RECALL_NOTES_DIR / "people" / f"person-{k['id']}.md").exists()


# ---------------------------------------------------------------- summarise helpers
def t_summarise_helpers():
    lines = [f"[00:{i:02d}] Me: " + "x" * 1000 for i in range(150)]
    parts = list(summarise._chunks(lines))
    total = sum(len(line) + 1 for line in lines)
    assert len(parts) >= -(-total // config.CHUNK_CHARS), [len(p) for p in parts]
    assert all(len(p) <= config.CHUNK_CHARS + 1100 for p in parts), [len(p) for p in parts]
    assert "".join(parts).count("Me:") == len(lines), "no line may be lost or duplicated across chunks"
    assert summarise.OLLAMA_MAX_CTX <= 16384, "a bigger context ran the laptop out of memory"
    assert "only other person" in summarise._who(["Omar"]) and "Other people" in summarise._who(["A", "B"])
    assert summarise._who([]) == ""
    for key in ("title", "summary", "decisions", "action_items", "open_questions"):
        assert key in summarise.SCHEMA["required"]


# ---------------------------------------------------------------- recall feed
def t_recall_feed():
    with db.connect() as con:
        mid = new_meeting(con, title="Feed test")
        notes.save(con, mid, FAKE_NOTES, "local")
    people.set_on_call(mid, ["Jess"])
    recall_feed.write(mid, refresh=False)
    text = (config.RECALL_NOTES_DIR / f"meeting-{mid}.md").read_text(encoding="utf-8")
    assert "With: Jess" in text and "Send the sheet" in text and "Transcript" not in text.split("With:")[0][:0] + "x"
    with db.connect() as con:
        blank = new_meeting(con)
    recall_feed.write(blank, refresh=False)
    assert not (config.RECALL_NOTES_DIR / f"meeting-{blank}.md").exists(), "meetings with no notes are skipped"
    recall_feed.remove(mid)
    assert not (config.RECALL_NOTES_DIR / f"meeting-{mid}.md").exists()


# ---------------------------------------------------------------- 1:1 eval link
def t_evals_keeps_line_endings():
    # A one-line addition must not rewrite a whole performance record's line endings.
    lf, crlf = SANDBOX / "lf.md", SANDBOX / "crlf.md"
    lf.write_bytes(b"# A\n\n- one\n")
    crlf.write_bytes(b"# B\r\n\r\n- one\r\n")
    evals._save(lf, "# A\n\n- one\n- two\n")
    evals._save(crlf, "# B\n\n- one\n- two\n")
    assert lf.read_bytes() == b"# A\n\n- one\n- two\n", lf.read_bytes()
    assert crlf.read_bytes() == b"# B\r\n\r\n- one\r\n- two\r\n", crlf.read_bytes()


def t_evals_auto_sync():
    prof = SANDBOX / "evals" / "Maya" / "profile.md"
    prof.parent.mkdir(parents=True, exist_ok=True)
    prof.write_text("# Maya\n\n## Interactions & Observations\n\n- **2026-07-10:** old\n\n---\n", encoding="utf-8")
    with db.connect() as con:
        k = people.lookup(con, "Maya")
        con.execute("UPDATE people SET eval_profile = ? WHERE id = ?", (str(prof), k["id"]))
        mid = new_meeting(con)
        add_segments(con, mid, [(0, 2, "Them", "Done with the docs.")])
        notes.save(con, mid, {"summary": ["Docs done."], "decisions": ["Ship it."], "open_questions": [],
                              "action_items": [{"task": "Share docs.", "owner": "Maya", "due": ""}]}, "local")
    people.set_on_call(mid, ["Maya"])
    assert evals.sync(mid) == "added" and evals.sync(mid) == "updated"
    line = next(l for l in prof.read_text(encoding="utf-8").splitlines() if f"[Earshot meeting {mid}]" in l)
    assert ".;" not in line and ".." not in line and "notes by local model" in line, line
    with db.connect() as con:
        notes.save(con, mid, {"summary": ["Docs done"], "decisions": [], "open_questions": [], "action_items": []}, "claude")
    evals.sync(mid)
    assert "notes checked by Claude" in prof.read_text(encoding="utf-8")
    people.set_on_call(mid, ["Maya", "Leo"])  # no longer a 1:1: entry and transcript go
    assert evals.sync(mid) == "removed"
    text = prof.read_text(encoding="utf-8")
    assert f"[Earshot meeting {mid}]" not in text and "- **2026-07-10:** old" in text
    assert not list(prof.parent.glob(f"1on1_*_earshot{mid}_transcript.txt"))
    people.set_on_call(mid, ["Maya"])
    with db.connect() as con:
        con.execute("UPDATE meetings SET status = 'transcribing' WHERE id = ?", (mid,))
    assert evals.sync(mid) is None  # not finished yet: nothing written
    with db.connect() as con:
        con.execute("UPDATE meetings SET status = 'done', chat = 'Maya Santos' WHERE id = ?", (mid,))
    assert evals.sync(mid) is None  # chat weeks stay on the button
    with db.connect() as con:
        con.execute("UPDATE meetings SET chat = NULL WHERE id = ?", (mid,))
    assert evals.sync(mid) == "added"
    assert evals.remove_from_profile(mid) == 1 and f"[Earshot meeting {mid}]" not in prof.read_text(encoding="utf-8")


def t_ollama_stall_recovery():
    """A stalled or dropped Ollama request restarts Ollama once and retries; a second
    failure surfaces (so the meeting gets 'Redo notes', never a silent hang)."""
    calls, restarts = [], []
    real_once, real_restart = summarise._chat_once, summarise.restart_ollama
    try:
        def flaky(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                raise TimeoutError("no new text for 420 s")
            return {"ok": True}
        summarise._chat_once = flaky
        summarise.restart_ollama = lambda: restarts.append(1) or True
        assert summarise._chat("s", "u", 10) == {"ok": True} and len(calls) == 2 and len(restarts) == 1
        calls.clear(), restarts.clear()
        summarise._chat_once = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("dropped"))
        try:
            summarise._chat("s", "u", 10)
            raise AssertionError("two failures must raise")
        except RuntimeError as e:
            assert "failed twice" in str(e) and len(restarts) == 1
        summarise.restart_ollama = lambda: False  # restarted too recently: give up at once
        try:
            summarise._chat("s", "u", 10)
            raise AssertionError("must raise when Ollama can't be restarted")
        except ConnectionError:
            pass
    finally:
        summarise._chat_once, summarise.restart_ollama = real_once, real_restart

    # Every request asks Ollama to free the model soon after, not hold it 5 min.
    sent, real_urlopen = [], summarise.urllib.request.urlopen

    class Reply:
        def __enter__(self):
            return iter([b'{"message": {"content": "{}"}, "done": true}'])

        def __exit__(self, *a):
            return False
    try:
        summarise.urllib.request.urlopen = lambda req, timeout: sent.append(json.loads(req.data)) or Reply()
        assert summarise._chat_once("s", "u", 10) == {}
        assert sent[0]["keep_alive"] == config.OLLAMA_KEEP_ALIVE and sent[0]["model"] == "qwen2.5:7b"
    finally:
        summarise.urllib.request.urlopen = real_urlopen


def t_prep():
    import prep
    with db.connect() as con:
        v = people.lookup(con, "Priya")
    Path(v["eval_profile"]).write_text(
        "# V\n\n## Areas for Development\n\n- **Speed** of delivery\n\n---\n\n## Coaching & Goals\n\n"
        "- Own one client end to end\n- Present at the team huddle\n", encoding="utf-8")
    now = datetime.now()
    with db.connect() as con:
        old = new_meeting(con, started=(now - timedelta(days=20)).isoformat(timespec="seconds"))
        one = new_meeting(con, title="Pri 1:1", started=(now - timedelta(days=3)).isoformat(timespec="seconds"))
        chat = new_meeting(con, title="Pri chat week", started=(now - timedelta(days=5)).isoformat(timespec="seconds"))
        con.execute("UPDATE meetings SET chat = 'Priya Nair' WHERE id = ?", (chat,))
        notes.save(con, one, {"summary": ["menu docs"], "decisions": [], "open_questions": ["Who signs off?"],
                              "action_items": [{"task": "Share menu docs", "owner": "Priya", "due": "Fri"},
                                               {"task": "Book review", "owner": "Sam", "due": ""},
                                               {"task": "Fix the stock join", "owner": "Priya", "due": ""}]},
                   "local")
        con.execute("UPDATE action_items SET done = 1 WHERE meeting_id = ? AND task LIKE 'Fix%'", (one,))
    for mid in (old, one, chat):
        people.set_on_call(mid, ["Pri"])
    with db.connect() as con:
        for mid in (old, one, chat):
            con.execute("INSERT INTO person_notes (person_id, meeting_id, section, text) VALUES (?, ?, 'raised', ?)",
                        (v["id"], mid, f"raised in {mid}"))
        con.execute("INSERT INTO person_notes (person_id, meeting_id, section, text) VALUES (?, ?, 'commitments', ?)",
                    (v["id"], one, "fix the stock join"))
        for i in range(12):
            con.execute("INSERT INTO person_notes (person_id, meeting_id, section, text) VALUES (?, ?, 'commitments', ?)",
                        (v["id"], one, f"promise number {i} about something"))
        sheet = prep.build(con, v["id"])
    assert sheet["last"]["id"] == one, sheet["last"]
    raised = {n["text"] for n in sheet["raised"]}
    assert raised == {f"raised in {one}", f"raised in {chat}"}, raised  # 20-day-old call out, chat week in
    assert [a["task"] for a in sheet["their_actions"]] == ["Share menu docs"], sheet["their_actions"]
    assert [a["task"] for a in sheet["my_actions"]] == ["Book review"]
    assert all("stock join" not in n["text"] for n in sheet["commitments"]), "a done promise must drop"
    assert len(sheet["commitments"]) == prep.MAX_NOTES and sheet["more"]["commitments"] == 12 - prep.MAX_NOTES
    assert sheet["goals"]["Areas for Development"] == ["Speed of delivery"]  # bold markers stripped
    assert sheet["topics"][0].startswith("Progress on Pri 1:1") and any("Who signs off" in t for t in sheet["topics"])
    assert sheet["topics"][-1].startswith("Coaching check-in: Own one client"), sheet["topics"]
    text = prep.as_text(sheet)
    assert "Priya still owes you" in text and "...and 4 older" in text and "Coaching & Goals" in text
    import web
    r = web.app.test_client().get(f"/people/{v['id']}/prep")
    assert r.status_code == 200 and b"Prep: 1:1 with Priya Nair" in r.data and b"Print" in r.data


def t_evals():
    prof = SANDBOX / "evals" / "Priya" / "profile.md"
    prof.parent.mkdir(parents=True, exist_ok=True)
    prof.write_text("# Priya\n\n## Strengths\n\n- good\n\n---\n\n## Interactions & Observations\n\n"
                    "- **2026-07-10:** old entry\n\n---\n\n## Coaching & Goals\n\n- goal\n", encoding="utf-8")
    with db.connect() as con:
        v = people.lookup(con, "Priya")
        con.execute("UPDATE people SET eval_profile = ? WHERE id = ?", (str(prof), v["id"]))
        mid = new_meeting(con, duration=1500)
        add_segments(con, mid, [(0, 2, "Them", "I finished the menu docs"), (3, 4, "Me", "Great")])
        notes.save(con, mid, {"summary": ["menu docs done"], "decisions": [], "open_questions": [],
                              "action_items": [{"task": "Share docs", "owner": "Priya", "due": "Monday"},
                                               {"task": "Review docs", "owner": "Sam", "due": ""}]}, "local")
    people.set_on_call(mid, ["Pri"])
    with db.connect() as con:
        assert evals.one_on_one_with(con, mid)["name"] == "Priya Nair"
    r1 = evals.log_to_profile(mid)
    r2 = evals.log_to_profile(mid)
    text = prof.read_text(encoding="utf-8")
    assert not r1["updated"] and r2["updated"] and text.count(f"[Earshot meeting {mid}]") == 1
    section = text.split("## Interactions & Observations")[1].split("---")[0]
    assert "old entry" in section and "**Priya to:** Share docs (by Monday)" in section and "**Sam to:** Review docs" in section
    assert "## Coaching & Goals" in text and "- goal" in text, "other sections must be untouched"
    tfile = Path(r2["transcript"])
    assert tfile.parent == prof.parent and "Priya: I finished" in tfile.read_text(encoding="utf-8")
    # not a 1:1 once a second person is tagged
    people.set_on_call(mid, ["Pri", "Leo"])
    with db.connect() as con:
        assert evals.one_on_one_with(con, mid) is None
    try:
        evals.log_to_profile(mid)
        raise AssertionError("group call must be refused")
    except ValueError:
        pass
    # profile without the section gets one appended
    assert "## Interactions & Observations\n\n- x" in evals._upsert("# P\n\n## Strengths\n", "- x", "[m]")
    html = evals.render_markdown("## A\n- **b** <script>alert(1)</script>\n---\ntext `code`")
    assert "<script>" not in html and "&lt;script&gt;" in html and "<strong>b</strong>" in html


# ---------------------------------------------------------------- web
def t_web():
    import control
    import web
    control.worker = pipeline.Worker()  # no thread: queued jobs just sit there
    c = web.app.test_client()
    with db.connect() as con:
        mid = new_meeting(con, title='<img src=x onerror=alert(1)>')
        add_segments(con, mid, [(0, 2, "Them", "<script>alert('xss')</script> join key"), (3, 4, "Me", "ok")])
        notes.save(con, mid, FAKE_NOTES, "local")
        k = people.lookup(con, "Maya")
    for url in ("/", f"/m/{mid}", "/actions", "/actions?done=1", "/people", f"/people/{k['id']}",
                "/search?q=join", "/search?q=%22%27%29%28*", "/search?q=", "/api/status"):
        r = c.get(url)
        assert r.status_code == 200, (url, r.status_code)
        assert b"<script>alert" not in r.data and b"<img src=x" not in r.data, f"unescaped HTML on {url}"
    assert c.get("/m/999999").status_code == 404 and c.get("/people/999999").status_code == 404
    assert b"<mark>join</mark>" in c.get("/search?q=join").data
    r = c.post(f"/api/meetings/{mid}/people", json={"names": ["Maya", "Brand New Person"]})
    assert r.status_code == 200 and {p["name"] for p in r.json["people"]} == {"Maya Santos", "Brand New Person"}
    page = c.get(f"/m/{mid}").data.decode()
    assert "Brand New Person" in page and ">Them<" in page  # two people: stays "Them"
    c.post(f"/api/meetings/{mid}/people", json={"names": ["Maya"]})
    page = c.get(f"/m/{mid}").data.decode()
    assert ">Maya<" in page and "1:1 with Maya" in page
    with db.connect() as con:
        aid = con.execute("SELECT id FROM action_items WHERE meeting_id = ?", (mid,)).fetchone()[0]
    assert c.post(f"/api/actions/{aid}/toggle").json["done"] is True
    assert c.post(f"/api/actions/{aid}/toggle").json["done"] is False
    assert c.post("/api/actions/999999/toggle").status_code == 404
    assert c.post(f"/m/{mid}/title", data={"title": "Renamed"}).status_code == 302
    assert "Renamed" in c.get(f"/m/{mid}").data.decode()
    assert c.post(f"/m/{mid}/retranscribe").status_code == 409  # no kept tracks
    r = c.post("/people/new", data={"name": "QA Person"})
    new_id = int(r.headers["Location"].rstrip("/").split("/")[-1])
    assert c.post(f"/people/{new_id}", data={"name": "Maya Santos"}).status_code == 409  # name taken
    assert c.post(f"/people/{new_id}", data={"name": "QA Person", "role": "Tester", "about": "notes"}).status_code == 302
    assert c.post(f"/people/{new_id}/delete").status_code == 302 and c.get(f"/people/{new_id}").status_code == 404
    # audio: range requests for seeking
    ogg = config.AUDIO_DIR / f"{mid}.ogg"
    shutil.copy(config.AUDIO_DIR / "rt.ogg", ogg)
    with db.connect() as con:
        con.execute("UPDATE meetings SET audio_path = ? WHERE id = ?", (str(ogg), mid))
    r = c.get(f"/audio/{mid}", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.data) == 100
    r.close()
    # delete cleans DB, audio and the Recall file
    recall_feed.write(mid, refresh=False)
    assert c.post(f"/m/{mid}/delete").status_code == 302
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM segments WHERE meeting_id = ?", (mid,)).fetchone()[0] == 0
    assert not ogg.exists() and not (config.RECALL_NOTES_DIR / f"meeting-{mid}.md").exists()


# ---------------------------------------------------------------- MCP tools
def t_mcp():
    import mcp_server as s
    with db.connect() as con:
        mid = new_meeting(con, title="MCP test", started="2026-09-24T11:00:00")
        add_segments(con, mid, [(0, 1, "Them", "part one"), (1.2, 2, "Them", "part two"), (5, 6, "Me", "reply")])
        notes.save(con, mid, FAKE_NOTES, "local")
    assert "MCP test" in s.earshot_meetings(days=3650)
    out = s.earshot_meeting(mid)
    assert "part one part two" in out and "Notes by: local model" in out and "With: not tagged" in out
    assert "(1:1)" not in out
    print(s.earshot_set_people(mid, ["Leo"]))
    out = s.earshot_meeting(mid)
    assert "With: Leo Reyes (1:1)" in out and "Leo: part one part two" in out, out
    assert "part two" in s.earshot_search("part two") and "Give some words" in s.earshot_search("!!!")
    assert "Send the sheet" in s.earshot_actions() and "No matching" in s.earshot_actions(owner="Nobody")
    aid = int(s.earshot_actions().split("(action ")[1].split(")")[0])
    assert "marked done" in s.earshot_set_action(aid, True) and "marked open" in s.earshot_set_action(aid, False)
    assert "No action item" in s.earshot_set_action(999999)
    assert "Maya Santos" in s.earshot_people() and "No one called" in s.earshot_person("zzz")
    assert "# Leo Reyes" in s.earshot_person("leo")
    r = s.earshot_update_notes(mid, ["fixed"], ["d1"], [{"task": "T", "owner": "Leo", "due": ""}], [],
                               title="Fixed title", people_notes=[{"name": "Leo", "working_on": ["w"]}])
    assert "notes replaced" in r and "profile notes updated for 1" in r
    out = s.earshot_meeting(mid)
    assert "Fixed title" in out and "Claude (corrected)" in out and "T | Leo" in out
    assert "No meeting" in s.earshot_update_notes(999999, [], [], [], [])
    assert "isn't running" in s.earshot_recording("status") and "must be" in s.earshot_recording("dance")


# ---------------------------------------------------------------- call detection + weekly review
def t_detect():
    import control
    import detect
    apps = detect.call_apps_using_mic()  # real registry, read-only
    assert isinstance(apps, set)

    class Rec:
        active = False

        def elapsed(self):
            return 999  # past the auto-tag window: this test is about asking and stopping
    events = []
    control.recorder = Rec()
    control.stop_recording = lambda: (events.append("stop"), setattr(control.recorder, "active", False))
    control.announce = lambda text: events.append("announce")
    detect.AUTO_STOP_AFTER = 0.05
    w = detect.CallWatcher()
    w._ask = lambda app: events.append(f"ask {app}")
    w._tick(set()); w._tick({"Teams"}); w._tick({"Teams"})
    control.recorder.active = True
    w._tick({"Teams"}); w._tick(set()); time.sleep(0.1); w._tick(set())
    w._tick({"Zoom"})
    assert events == ["ask Teams", "stop", "announce", "ask Zoom"], events


def t_callwho():
    import callwho
    import control
    import detect
    assert callwho.parts("Maya Santos | Microsoft Teams") == ["Maya Santos"]
    assert callwho.parts("Meeting with Omar | Microsoft Teams") == ["Omar"]
    assert callwho.is_main("Chat | Leo Reyes | Microsoft Teams")
    assert not callwho.is_main("Leo Reyes | Microsoft Teams")
    with db.connect() as con:
        find = lambda t: callwho.people_in(con, [t])
        assert find("Leo Reyes | Microsoft Teams") == ["Leo Reyes"]  # Teams chat name
        assert find("Maya Santos | Microsoft Teams") == ["Maya Santos"]
        assert find("Café team | Microsoft Teams") == []                   # group chat
        assert sorted(find("Sync with Omar and Jess | Microsoft Teams")) == ["Jess", "Omar"]
        assert find("Priority review | Microsoft Teams") == []                     # "Pri" alias
        assert find("Anastasia catch-up | Microsoft Teams") == []
    wins = {1: "Chat | Leo Reyes | Microsoft Teams", 2: "Maya Santos | Microsoft Teams"}
    assert callwho.call_titles(wins, {1: 0, 2: 100}, 50) == ["Maya Santos | Microsoft Teams"]
    assert callwho.call_titles(wins, {1: 0, 2: 10}, 50) == []                         # an old pop-out

    with db.connect() as con:
        mid = new_meeting(con, status="recording")
    assert callwho.tag(mid, ["Maya Santos"], ["Maya Santos | Microsoft Teams"])
    assert not callwho.tag(mid, ["Omar"], ["x"])                                   # already tagged
    with db.connect() as con:
        assert [p["name"] for p in people.on_call(con, mid)] == ["Maya Santos"]
        assert con.execute("SELECT tagged_from FROM meetings WHERE id = ?", (mid,)).fetchone()[0]
    people.set_on_call(mid, ["Maya Santos", "Omar"])                             # he edits it
    with db.connect() as con:
        assert con.execute("SELECT tagged_from FROM meetings WHERE id = ?", (mid,)).fetchone()[0] is None

    # The watcher: main window open at start; the call window opens while it rings, then the mic.
    windows = {1: "Chat | Leo Reyes | Microsoft Teams"}
    callwho.teams_windows = lambda: dict(windows)

    class Rec:
        active = False

        def elapsed(self):
            return 5
    with db.connect() as con:
        mid2 = new_meeting(con, status="recording")
    events, asked = [], []
    control.recorder = Rec()
    control.status = lambda: {"meeting_id": mid2}
    control.announce = events.append
    w = detect.CallWatcher()
    w._ask = lambda app: asked.append((app, list(w._who)))
    w._tick(set())
    windows[2] = "Leo Reyes | Microsoft Teams"
    w._tick(set())
    w._tick({"Teams"})
    assert asked == [("Teams", ["Leo Reyes"])], asked
    control.recorder.active = True
    w._tick({"Teams"})
    with db.connect() as con:
        assert [p["name"] for p in people.on_call(con, mid2)] == ["Leo Reyes"]
    assert len(events) == 1 and "1:1" in events[0], events
    w._tick({"Teams"})
    assert len(events) == 1                                                          # once only
    assert "call window" in config.CALL_LOG.read_text(encoding="utf-8")

    # Earshot started mid-call: the call window counts as already open, so nothing is guessed.
    w2 = detect.CallWatcher()
    w2._ask = lambda app: asked.append((app, list(w2._who)))
    control.recorder.active = False
    w2._tick({"Teams"})
    assert asked[-1] == ("Teams", []), asked


def t_meeting_windows():
    """Replays the 28 Sep stand-up: the meeting window opened at the join screen minutes
    before the mic, and the only window that appeared with the call was titled just
    "Microsoft Teams"."""
    import callwho
    import control
    import detect
    assert callwho.usual_people("Morning Huddle Meeting")[:2] == ["Jess", "Omar"]
    assert callwho.usual_people("Weekly Ops Meeting") == []

    class Rec:
        active = False

        def elapsed(self):
            return 5
    windows = {1: "Chat | Front of house | Microsoft Teams"}
    callwho.teams_windows = lambda: dict(windows)
    with db.connect() as con:
        mid = new_meeting(con, status="recording")
    events, asked = [], []
    control.recorder = Rec()
    control.status = lambda: {"meeting_id": mid}
    control.announce = events.append
    w = detect.CallWatcher()
    w._ask = lambda app: asked.append((app, list(w._who), w._meeting))
    w._tick(set())
    windows[3] = "Morning Huddle Meeting | Microsoft Teams"
    w._tick(set())
    w._first_seen[3] -= 300                      # he sat on the join screen for 5 minutes
    windows[4] = "Microsoft Teams"
    w._tick({"Teams"})
    assert asked == [("Teams", [], "Morning Huddle Meeting")], asked
    control.recorder.active = True
    w._tick({"Teams"})
    with db.connect() as con:
        m = con.execute("SELECT title FROM meetings WHERE id = ?", (mid,)).fetchone()
        tagged = sorted(p["name"] for p in people.on_call(con, mid))
    assert m["title"] == "Morning Huddle Meeting"
    assert len(tagged) == 7 and {"Omar", "Jess", "Rosa", "Tomas Cruz", "Priya Nair",
                                 "Leo Reyes"} <= set(tagged), tagged
    assert any(t.startswith("Carl") for t in tagged), tagged
    assert len(events) == 1 and "usual 7 people" in events[0], events
    with db.connect() as con:
        assert people.lookup(con, "Carl")["name"] != "Leo Reyes"  # Carl is not Leo Carl

    # A meeting window from over 15 minutes ago is not this call's; an unlisted meeting gets a title only.
    for age, name, want_title, want_tags in ((1200, "Old Sync Meeting", None, 0), (60, "Weekly Ops Meeting", "Weekly Ops Meeting", 0)):
        windows.clear()
        windows.update({1: "Chat | Front of house | Microsoft Teams"})
        with db.connect() as con:
            mid = new_meeting(con, status="recording")
        control.status = lambda: {"meeting_id": mid}
        control.recorder.active = False
        w = detect.CallWatcher()
        w._ask = lambda app: None
        w._tick(set())
        windows[5] = f"{name} | Microsoft Teams"
        w._tick(set())
        w._first_seen[5] -= age
        w._tick({"Teams"})
        control.recorder.active = True
        w._tick({"Teams"})
        with db.connect() as con:
            got = con.execute("SELECT title FROM meetings WHERE id = ?", (mid,)).fetchone()["title"]
            n = len(people.on_call(con, mid))
        assert (got, n) == (want_title, want_tags), (name, got, n)


def t_dues():
    import dues
    from datetime import date
    thu = date(2026, 9, 24)
    cases = {"": None, "Friday": "2026-09-25", "Friday PM or Monday": "2026-09-25", "tomorrow": "2026-09-25",
             "Immediate": "2026-09-24", "Soon": None, "ongoing": None, "After deployment": None,
             "Oct 1": "2026-10-01", "1 October": "2026-10-01", "2/10": "2026-10-02", "next week": "2026-09-28",
             "end of month": "2026-09-30", "end of the week": "2026-09-25", "next Friday": "2026-10-02",
             "Thursday": "2026-10-01", "bukas": "2026-09-25", "this month": None, "Monitor it": None,
             "Jan 5": "2027-01-05", "Sep 1": "2026-09-01"}
    for text, want in cases.items():
        got = dues.read(text, thu)
        assert (got.isoformat() if got else None) == want, (text, got, want)
    assert dues.nice("2026-09-25", thu) == "tomorrow" and dues.nice("2026-09-29", thu) == "Tue 29 Sep"
    assert dues.bucket("2026-09-23", thu) == "overdue" and dues.bucket("", thu) == "none"

    with db.connect() as con:
        con.execute("DELETE FROM action_items")
        con.execute("DELETE FROM kv")
        mid = new_meeting(con, title="Dues", started="2026-09-22T10:00:00")  # a Tuesday
        notes.save(con, mid, {"summary": ["x"], "decisions": [], "open_questions": [], "action_items": [
            {"task": "Send the budget sheet", "owner": "Sam", "due": "Friday"},        # 25 Sep
            {"task": "Check the mapping", "owner": "Sam", "due": "tomorrow"},         # 23 Sep
            {"task": "Scope the metrics", "owner": "Maya", "due": "today"},               # 22 Sep
            {"task": "Think about it", "owner": "Sam", "due": ""}]}, "local")
        assert dues.fill(con) == 4 and dues.fill(con) == 0
        text = dues.digest(con, date(2026, 9, 25))
    assert text.startswith("Your action items: 1 due today, 1 overdue, 1 overdue from others."), text
    assert '"Send the budget sheet"' in text

    # He moves one date by hand; Redo notes keeps it.
    with db.connect() as con:
        aid = con.execute("SELECT id FROM action_items WHERE task = 'Check the mapping'").fetchone()[0]
        dues.set_date(con, aid, "2026-10-09")
        try:
            dues.set_date(con, aid, "not a date")
            raise AssertionError("bad date accepted")
        except ValueError:
            pass
        notes.save(con, mid, {"summary": ["x"], "decisions": [], "open_questions": [], "action_items": [
            {"task": "Check the mapping", "owner": "Sam", "due": "tomorrow"}]}, "local")
        dues.fill(con)
        row = con.execute("SELECT due_date, due_manual FROM action_items WHERE meeting_id = ?", (mid,)).fetchone()
    assert tuple(row) == ("2026-10-09", 1), tuple(row)

    # Once per weekday, not before 9, never at the weekend; a Monday adds old undated items.
    sent = []
    with db.connect() as con:
        notes.save(con, mid, {"summary": ["x"], "decisions": [], "open_questions": [], "action_items": [
            {"task": "Send the budget sheet", "owner": "Sam", "due": "Friday"},
            {"task": "Think about it", "owner": "Sam", "due": ""}]}, "local")
    assert not dues.remind_if_due(sent.append, datetime(2026, 9, 25, 8, 30))
    assert dues.remind_if_due(sent.append, datetime(2026, 9, 25, 9, 5))
    assert not dues.remind_if_due(sent.append, datetime(2026, 9, 25, 14, 0))
    assert not dues.remind_if_due(sent.append, datetime(2026, 9, 26, 9, 5))          # Saturday
    assert dues.remind_if_due(sent.append, datetime(2026, 10, 5, 9, 0))              # Monday
    assert len(sent) == 2 and "1 due today" in sent[0] and "no date open over a week" in sent[1], sent

    import web
    c = web.app.test_client()
    page = c.get("/actions").data.decode()
    assert "No date" in page and 'type="date"' in page and "<h2 class=\"due-head due-" in page
    with db.connect() as con:
        aid = con.execute("SELECT id FROM action_items WHERE task = 'Think about it'").fetchone()[0]
    r = c.post(f"/api/actions/{aid}/due", json={"date": "2026-12-01"})
    assert r.status_code == 200 and r.json["due_date"] == "2026-12-01"
    assert c.post(f"/api/actions/{aid}/due", json={"date": "nope"}).status_code == 400
    assert c.post("/api/actions/999999/due", json={"date": ""}).status_code == 404
    assert "Think about it" not in c.get("/actions?who=me").data.decode().split("No date")[-1]

    import mcp_server as s
    assert "due 2026-12-01" in s.earshot_set_action(aid, False, "2026-12-01")
    assert "isn't a date" in s.earshot_set_action(aid, False, "31/12")
    assert "due 2026-12-01" in s.earshot_actions(owner="Sam")


def t_voices():
    import numpy as np
    import voices
    import web
    # Features look like the model's training input: 80 mel bands every 10 ms, mean removed.
    tone = np.sin(2 * np.pi * 440 * np.arange(16000) / 16000).astype("float32") * 0.3
    f = voices.fbank(tone)
    assert f.shape == (98, 80) and abs(float(f.mean())) < 1e-3
    assert voices.embed(tone[:8000]) is None                                   # under 1.5 s: no guess

    def vec(*xs):
        v = np.zeros(256, dtype="float32")
        for i, x in enumerate(xs):
            v[i] = x
        return v / np.linalg.norm(v)
    K, J = vec(1, 0.1), vec(0.1, 1)
    assert voices.decide(vec(1, 0.15), {1: K, 2: J})[0] == 1
    assert voices.decide(vec(1, 1), {1: K, 2: J})[0] is None                  # halfway: stays Them
    assert voices.decide(vec(0, 0, 1), {1: K, 2: J})[0] is None               # nobody we know

    VEC = {}
    real_lp, real_load = voices.line_prints, voices.load
    voices.load = lambda path: np.zeros(10, dtype="float32")
    voices.line_prints = lambda wave, rows: [(r, VEC[r["text"]]) for r in rows
                                             if r["end"] - r["start"] >= config.VOICE_MIN_SECONDS]
    try:
        def call(tagged, lines):
            with db.connect() as con:
                mid = new_meeting(con)
                add_segments(con, mid, lines)
            people.set_on_call(mid, tagged)
            return mid

        def rows(mid):
            with db.connect() as con:
                return {r["text"]: (r["speaker"], r["voice"]) for r in con.execute(
                    "SELECT text, speaker, voice FROM segments WHERE meeting_id = ?", (mid,))}
        VEC.update({"k1": vec(1, 0.05), "k2": vec(1, 0.12), "j1": vec(0.05, 1), "j2": vec(0.1, 1),
                    "k3": vec(1, 0.1), "j3": vec(0.12, 1), "mid": vec(1, 1), "oo": vec(0, 0, 1)})
        a = call(["Maya"], [(0, 3, "Them", "k1"), (4, 7, "Them", "k2"), (8, 9, "Me", "hi")])
        b = call(["Leo"], [(0, 3, "Them", "j1"), (4, 7, "Them", "j2")])
        for mid in (a, b):
            assert voices.update(mid, "unused.ogg") == 0                      # 1:1s: learn only
        with db.connect() as con:
            k, j = people.lookup(con, "Maya")["id"], people.lookup(con, "Leo")["id"]
            assert voices.learned(con, k) == (1, 6.0) and voices.learned(con, j) == (1, 6.0)
        g = call(["Maya", "Leo"], [(0, 3, "Them", "k3"), (3.5, 4, "Them", "oo"), (4.2, 7, "Them", "k1"),
                                      (8, 11, "Them", "j3"), (12, 15, "Them", "mid"), (16, 17, "Me", "ok")])
        assert voices.update(g, "unused.ogg") == 4
        got = rows(g)
        assert got["k3"] == ("Maya Santos", "auto") and got["k1"] == ("Maya Santos", "auto")
        assert got["oo"] == ("Maya Santos", "auto")                        # short, between two Maya lines
        assert got["j3"] == ("Leo Reyes", "auto")
        assert got["mid"] == ("Them", None) and got["ok"] == ("Me", None)     # unsure: never guessed
        with db.connect() as con:
            lines = pipeline.transcript_lines(con, g)
        assert any(line.endswith("Maya: k3 oo k1") for line in lines), lines

        # He says the unsure line was Leo: kept, and it teaches Leo's voice from this call.
        with db.connect() as con:
            sid = con.execute("SELECT id FROM segments WHERE meeting_id = ? AND text = 'mid'", (g,)).fetchone()[0]
            voices.set_line(con, [sid], "Leo Reyes")
        voices.update(g, "unused.ogg")
        assert rows(g)["mid"] == ("Leo Reyes", "manual")
        with db.connect() as con:
            assert voices.learned(con, j)[0] == 2

        # Down to one person tagged: the voice names go, his own label stays.
        people.set_on_call(g, ["Maya"])
        voices.update(g, "unused.ogg")
        got = rows(g)
        assert got["k3"] == ("Them", None) and got["mid"] == ("Leo Reyes", "manual")

        # The page and the API.
        people.set_on_call(g, ["Maya", "Leo"])
        voices.update(g, "unused.ogg")
        control_worker = web.control.worker
        web.control.worker = pipeline.Worker()
        c = web.app.test_client()
        page = c.get(f"/m/{g}").data.decode()
        assert 'class="who pick by-voice"' in page and "Recognised by voice" in page
        assert c.post(f"/api/meetings/{g}/lines", json={"ids": [sid], "name": "Omar"}).status_code == 400
        assert c.post(f"/api/meetings/{g}/lines", json={"ids": [sid], "name": None}).json["changed"] == 1
        assert rows(g)["mid"] == ("Them", None)
        assert 'class="who pick' not in c.get(f"/m/{a}").data.decode()          # 1:1: nothing to pick
        web.control.worker = control_worker
    finally:
        voices.line_prints, voices.load = real_lp, real_load


TESTS = [t_db_init, t_evals_keeps_line_endings, t_ids_never_reused, t_people_lookup_and_create, t_people_tagging_and_labels, t_people_owns,
         t_notes_save_rules, t_notes_save_people, t_commitments_to_actions, t_fix_owners, t_hallucinations, t_merge_and_lines, t_text_bleed, t_sound_bleed,
         t_opus_roundtrip, t_worker_full_flow, t_worker_ollama_down, t_worker_empty_and_missing, t_worker_recover,
         t_worker_purge, t_people_job_and_dirty_sweep, t_summarise_helpers, t_recall_feed, t_evals_auto_sync, t_ollama_stall_recovery, t_prep, t_evals, t_web, t_mcp,
         t_detect, t_callwho, t_meeting_windows, t_dues, t_voices]

def real_evals_fingerprint():
    """Every file in the sample profiles folder, so the run can prove it touched none."""
    root = ROOT / "sample_profiles"
    return {str(f): f.stat().st_mtime_ns for f in root.rglob("*") if f.is_file()} if root.exists() else {}


if __name__ == "__main__":
    evals_before = real_evals_fingerprint()
    for t in TESTS:
        check(t.__name__[2:], t)
    evals_after = real_evals_fingerprint()
    changed = sorted(set(evals_before.items()) ^ set(evals_after.items()))
    RESULTS.append((not changed, "sample profiles untouched", f"changed: {changed}"))
    for ok, name, err in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            print("      " + err.replace("\n", "\n      "))
    passed = sum(ok for ok, _, _ in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} passed   (sandbox: {SANDBOX})")
    shutil.rmtree(SANDBOX, ignore_errors=True)
    sys.exit(0 if passed == len(RESULTS) else 1)
