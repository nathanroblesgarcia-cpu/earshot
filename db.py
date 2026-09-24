"""SQLite schema and helpers. One connection per call site; WAL so the web UI can
read while the worker writes."""
import sqlite3
from datetime import datetime

from config import DB_PATH, LOCAL_DIR, PEOPLE_SEED

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    -- AUTOINCREMENT: a deleted meeting's number is never reused, because eval profile
    -- entries and Recall files point at meetings by number ("[Earshot meeting 12]").
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT,
    started_at    TEXT NOT NULL,          -- local ISO time
    ended_at      TEXT,
    duration_s    REAL,
    -- recording -> queued -> transcribing -> summarising -> done | error
    status        TEXT NOT NULL,
    error         TEXT,
    summary       TEXT,                   -- markdown bullets
    decisions     TEXT,                   -- JSON list of strings
    questions     TEXT,                   -- JSON list of strings
    summary_error TEXT,                   -- transcript is kept even if Ollama fails
    audio_path    TEXT,                   -- mixed Opus file, NULL once purged
    audio_purged  INTEGER NOT NULL DEFAULT 0,
    source        TEXT                    -- call app that triggered it (Teams, Zoom...), NULL if manual
);
CREATE TABLE IF NOT EXISTS segments (
    id          INTEGER PRIMARY KEY,
    meeting_id  INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    start       REAL NOT NULL,
    end         REAL NOT NULL,
    speaker     TEXT NOT NULL,            -- Me | Them
    text        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_segments_meeting ON segments(meeting_id, start);
CREATE TABLE IF NOT EXISTS action_items (
    id          INTEGER PRIMARY KEY,
    meeting_id  INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    task        TEXT NOT NULL,
    owner       TEXT,
    due         TEXT,
    done        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS voiceprints (  -- what each person sounds like, learned per call (voices.py)
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    emb        BLOB NOT NULL,                 -- 256 float32, unit length
    seconds    REAL NOT NULL                  -- how much speech it was learned from
);
CREATE TABLE IF NOT EXISTS kv (          -- small bits of app state ("reminded_on")
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS people (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    aliases       TEXT,                   -- comma-separated other names ("Pri, Priya")
    role          TEXT,
    about         TEXT,                   -- his own free-text notes
    eval_profile  TEXT,                   -- path to a development profiles profile, if any
    created_at    TEXT
);
CREATE TABLE IF NOT EXISTS meeting_people (
    meeting_id  INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    PRIMARY KEY (meeting_id, person_id)
);
-- Dated profile entries drawn from each call, one row per bullet.
CREATE TABLE IF NOT EXISTS person_notes (
    id          INTEGER PRIMARY KEY,
    person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    meeting_id  INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    section     TEXT NOT NULL,            -- working_on | raised | commitments | observations
    text        TEXT NOT NULL
);
-- Teams chat messages, one row per Teams message id, so overlapping pulls never duplicate.
-- Each chat's messages are grouped into one chat "meeting" per week (Monday to Sunday).
CREATE TABLE IF NOT EXISTS teams_messages (
    msg_id      TEXT PRIMARY KEY,         -- Teams message id (its send time in ms)
    chat        TEXT NOT NULL,            -- chat name as in config.TEAMS_CHATS
    sent_at     TEXT NOT NULL,            -- local ISO time
    sender      TEXT NOT NULL,            -- Teams display name
    text        TEXT NOT NULL,
    pulled_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_teams_chat ON teams_messages(chat, sent_at);
-- One row per pull of a chat (which may arrive in several parts). The next pull only moves
-- forward once a pull arrived complete; until then it re-reads from the incomplete one's date.
CREATE TABLE IF NOT EXISTS teams_pulls (
    id          INTEGER PRIMARY KEY,
    chat        TEXT NOT NULL,
    since       TEXT NOT NULL,            -- the date the script read back to
    expected    INTEGER NOT NULL,         -- lines the script said it returned
    received    TEXT NOT NULL DEFAULT '[]',  -- JSON list of line ids that arrived intact
    masked      INTEGER NOT NULL DEFAULT 0,  -- lines the browser tool hid as "[BLOCKED]"
    reached     INTEGER NOT NULL DEFAULT 0,  -- the page loaded back to `since` (END line)
    started_at  TEXT NOT NULL,
    complete    INTEGER NOT NULL DEFAULT 0
);
CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
    text, content='segments', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON segments BEGIN
    INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS segments_ad AFTER DELETE ON segments BEGIN
    INSERT INTO segments_fts(segments_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
"""


def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init():
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        # Columns added after v1.0; CREATE TABLE IF NOT EXISTS won't add them to an old DB.
        have = {r["name"] for r in con.execute("PRAGMA table_info(meetings)")}
        if "source" not in have:
            con.execute("ALTER TABLE meetings ADD COLUMN source TEXT")
        if "people_dirty" not in have:  # 1 = who-was-on-the-call changed; rebuild profile notes
            con.execute("ALTER TABLE meetings ADD COLUMN people_dirty INTEGER NOT NULL DEFAULT 0")
        if "notes_source" not in have:  # "local" (Ollama) or "claude" (corrected via the MCP)
            con.execute("ALTER TABLE meetings ADD COLUMN notes_source TEXT")
        if "eval_logged_at" not in have:  # when a 1:1 was last added to an eval profile
            con.execute("ALTER TABLE meetings ADD COLUMN eval_logged_at TEXT")
        segs = {r["name"] for r in con.execute("PRAGMA table_info(segments)")}
        if "voice" not in segs:  # "auto" = named by voice, "manual" = he said who; NULL = as heard
            con.execute("ALTER TABLE segments ADD COLUMN voice TEXT")
            con.execute("ALTER TABLE segments ADD COLUMN voice_score REAL")
        acts = {r["name"] for r in con.execute("PRAGMA table_info(action_items)")}
        if "due_date" not in acts:  # the due text read as a date (dues.py); NULL = not read yet
            con.execute("ALTER TABLE action_items ADD COLUMN due_date TEXT")
            con.execute("ALTER TABLE action_items ADD COLUMN due_manual INTEGER NOT NULL DEFAULT 0")
        if "tagged_from" not in have:  # the Teams window title a call was auto-tagged from
            con.execute("ALTER TABLE meetings ADD COLUMN tagged_from TEXT")
        if "chat_week" not in have:  # Teams chat weeks: which chat, and the Monday the week starts
            con.execute("ALTER TABLE meetings ADD COLUMN chat TEXT")
            con.execute("ALTER TABLE meetings ADD COLUMN chat_week TEXT")
            con.execute("ALTER TABLE meetings ADD COLUMN chat_group INTEGER NOT NULL DEFAULT 0")
        if "masked" not in {r["name"] for r in con.execute("PRAGMA table_info(teams_pulls)")}:
            con.execute("ALTER TABLE teams_pulls ADD COLUMN masked INTEGER NOT NULL DEFAULT 0")
        if "reached" not in {r["name"] for r in con.execute("PRAGMA table_info(teams_pulls)")}:
            con.execute("ALTER TABLE teams_pulls ADD COLUMN reached INTEGER NOT NULL DEFAULT 0")
            # Pulls from before the END line can't prove they reached their start date.
            con.execute("UPDATE teams_pulls SET complete = 0")
        if con.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 0:
            now = datetime.now().isoformat(timespec="seconds")
            con.executemany(
                "INSERT INTO people (name, aliases, role, eval_profile, created_at) VALUES (?, ?, ?, ?, ?)",
                [(p["name"], p.get("aliases") or None, p.get("role") or None,
                  p.get("eval_profile"), now) for p in PEOPLE_SEED])


def set_status(meeting_id, status, **fields):
    fields["status"] = status
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as con:
        con.execute(f"UPDATE meetings SET {cols} WHERE id = ?", (*fields.values(), meeting_id))
