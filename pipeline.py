"""Background worker: transcribe -> clean -> keep audio -> summarise -> profiles -> purge.

One job at a time on a single thread, so a long transcription never competes with another
for the CPU. Jobs survive a restart: on startup, anything unfinished is queued again.
"""
import queue
import re
import threading
import time
import traceback
from datetime import datetime, timedelta
from difflib import SequenceMatcher

import numpy as np
import soundfile as sf

import db
import evals
import notes as notes_store
import people
import recall_feed
import voices
import summarise
from config import (AUDIO_DIR, AUDIO_RETENTION_DAYS, BLEED_CORR, BLEED_MAX_LAG, BLEED_MIN_LAG, BLEED_RUN,
                    BLEED_SIMILARITY, BLEED_WINDOW, ENVELOPE_FRAME, HALLUCINATION_REPEATS,
                    THEM_ACTIVE, VOCAB, WHISPER_COMPUTE, WHISPER_MODEL)
from transcript import fmt, merged, stamp, who  # noqa: F401  (fmt is re-exported for web and evals)

OPUS_RATES = {8000, 12000, 16000, 24000, 48000}
PURGE_EVERY = 6 * 3600
SILENT_RMS = 0.003  # below this a 50 ms frame of the call audio counts as silence


def raw_paths(meeting_id):
    """The FLAC tracks written while recording."""
    return AUDIO_DIR / f"{meeting_id}_me.flac", AUDIO_DIR / f"{meeting_id}_them.flac"


def track_paths(meeting_id):
    """Compressed copies of each track, kept 30 days so a call can be re-transcribed."""
    return AUDIO_DIR / f"{meeting_id}_me.ogg", AUDIO_DIR / f"{meeting_id}_them.ogg"


def audio_files(meeting_id):
    return [*raw_paths(meeting_id), *track_paths(meeting_id), AUDIO_DIR / f"{meeting_id}.ogg"]


def transcript_lines(con, meeting_id, them="Them"):
    m = con.execute("SELECT started_at, chat FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    rows = con.execute("SELECT start, end, speaker, text FROM segments WHERE meeting_id = ? "
                       "ORDER BY start", (meeting_id,)).fetchall()
    lines = [f"[{stamp(m, r['start'])}] {who(r['speaker'], them)}: {r['text']}" for r in merged(rows)]
    if m is not None and m["chat"]:
        # The prompts talk about a call; say this is typed text so nothing is "misheard".
        lines.insert(0, f"(This is a week of the Teams chat '{m['chat']}', typed messages, not a call.)")
    return lines


def _words(text):
    return re.findall(r"\w+", text.lower())


# -- cleaning a raw transcript -------------------------------------------------
def drop_hallucinations(segs):
    """Whisper invents text on silence and noise, and can get stuck repeating it
    ("ma, ma, ma" x30, "It's not available." x5 in the first real call). Drop segments
    Whisper itself is unsure are speech, overly repetitive ones, and short phrases that
    repeat HALLUCINATION_REPEATS+ times within 30 seconds."""
    kept = [s for s in segs if s.text.strip()
            and not (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0)
            and s.compression_ratio <= 2.4]
    norms = [" ".join(_words(s.text)) for s in kept]
    out = []
    for i, s in enumerate(kept):
        if len(norms[i].split()) <= 4:
            near = sum(1 for j, t in enumerate(kept) if norms[j] == norms[i] and abs(t.start - s.start) <= 30)
            if near >= HALLUCINATION_REPEATS:
                continue
        out.append(s)
    return out


def envelope(path):
    """Loudness (RMS) of a track in ENVELOPE_FRAME steps, read in blocks so an hour-long
    call stays small in memory."""
    with sf.SoundFile(path) as f:
        hop = max(1, int(f.samplerate * ENVELOPE_FRAME))
        parts = []
        for block in f.blocks(blocksize=hop * 400, dtype="float32", always_2d=True):
            mono = block.mean(axis=1)
            n = len(mono) // hop
            if n:
                parts.append(np.sqrt((mono[:n * hop].reshape(n, hop) ** 2).mean(axis=1)))
    return np.concatenate(parts) if parts else np.zeros(0, dtype="float32")


def sounds_like_bleed(seg, env_me, env_them):
    """(bleed?, them_active) from the audio itself. With a speaker, the mic hears the other
    person through the room, so during bleed the mic's loudness follows the call audio's
    loudness a few frames later. Words can't show that in Taglish: the two copies get
    transcribed differently. bleed? is None when the segment is too short to judge."""
    i0, i1 = int(seg.start / ENVELOPE_FRAME), int(seg.end / ENVELOPE_FRAME) + 1
    me = env_me[i0:i1]
    them_now = env_them[i0:i1]
    if len(me) == 0 or len(them_now) == 0:
        return None, 0.0
    active = float((them_now > SILENT_RMS).mean())
    if active < THEM_ACTIVE:
        return False, active  # they were quiet, so this is really him
    if len(me) < 6:
        return None, active
    return bleed_score(me, env_them, i0) >= BLEED_CORR, active


def bleed_score(me, env_them, i0):
    """Best correlation between the mic's loudness and the call audio's loudness, allowing
    for the speaker-to-mic delay (measured at 550 ms on his setup). Square-rooted loudness
    compared best in testing (0.95 at the true delay)."""
    a = np.sqrt(me)
    if a.std() < 1e-6:
        return 0.0
    best = 0.0
    for lag in range(BLEED_MIN_LAG, BLEED_MAX_LAG + 1):
        lo = i0 - lag
        if lo < 0 or lo + len(me) > len(env_them):
            continue
        b = np.sqrt(env_them[lo:lo + len(me)])
        if b.std() < 1e-6:
            continue
        best = max(best, float(np.corrcoef(a, b)[0, 1]))
    return best


def text_bleed(me_seg, them_segs):
    """Word-based backup: a "me" line that repeats what they said around the same time."""
    nearby = " ".join(t.text for t in them_segs
                      if t.start < me_seg.end + BLEED_WINDOW and t.end > me_seg.start - BLEED_WINDOW)
    if not nearby:
        return False
    a, b = _words(me_seg.text), _words(nearby)
    if len(a) <= BLEED_RUN and set(a) <= set(b):
        return True  # a short fragment like "demo?" that only echoes what they just said
    matcher = SequenceMatcher(None, a, b)
    return (matcher.find_longest_match(0, len(a), 0, len(b)).size >= BLEED_RUN
            or matcher.ratio() >= BLEED_SIMILARITY)


def is_bleed(me_seg, them_segs, env_me=None, env_them=None):
    if env_me is not None and env_them is not None:
        by_sound, active = sounds_like_bleed(me_seg, env_me, env_them)
        if by_sound:
            return True
        if active < THEM_ACTIVE:
            return False  # they were silent: never drop his own words on text similarity alone
    return text_bleed(me_seg, them_segs)


# -- audio files ---------------------------------------------------------------
def to_opus(src, dst):
    """Re-encode one track to Opus. Returns False if its sample rate can't be Opus."""
    with sf.SoundFile(src) as f:
        if f.samplerate not in OPUS_RATES:
            return False
        with sf.SoundFile(dst, "w", samplerate=f.samplerate, channels=1, format="OGG", subtype="OPUS") as out:
            for block in f.blocks(blocksize=f.samplerate * 10, dtype="float32", always_2d=True):
                out.write(block.mean(axis=1))
    return True


def mix_to_opus(me_path, them_path, out_path):
    """Mix both tracks into one small Opus file for playback. Streams in blocks so a
    3-hour call never has to fit in memory. Returns False if the tracks can't be mixed."""
    with sf.SoundFile(me_path) as me, sf.SoundFile(them_path) as them:
        if me.samplerate != them.samplerate or them.samplerate not in OPUS_RATES:
            return False
        block = them.samplerate * 10
        with sf.SoundFile(out_path, "w", samplerate=them.samplerate, channels=1,
                          format="OGG", subtype="OPUS") as out:
            while True:
                a, b = me.read(block, dtype="float32"), them.read(block, dtype="float32")
                if not len(a) and not len(b):
                    break
                n = max(len(a), len(b))
                mixed = np.zeros(n, dtype="float32")
                mixed[:len(a)] += a
                mixed[:len(b)] += b
                out.write(np.clip(mixed, -1.0, 1.0))
    return True


class Worker:
    def __init__(self):
        self.jobs = queue.Queue()
        self.progress = None  # {"meeting_id", "stage", "pct"} while a job runs
        self._model = None
        self._last_purge = 0.0

    # -- public ------------------------------------------------------------
    def start(self):
        self._recover()
        threading.Thread(target=self._loop, name="earshot-worker", daemon=True).start()

    def process(self, meeting_id):
        db.set_status(meeting_id, "queued")
        self.jobs.put(("process", meeting_id))

    def redo_summary(self, meeting_id):
        db.set_status(meeting_id, "queued")
        self.jobs.put(("summary", meeting_id))

    def redo_transcript(self, meeting_id):
        """Transcribe again from the kept tracks (after tuning), then notes and profiles."""
        if not all(p.exists() for p in track_paths(meeting_id)):
            raise FileNotFoundError("The separate tracks for this call aren't kept")
        db.set_status(meeting_id, "queued")
        self.jobs.put(("retranscribe", meeting_id))

    def refresh_people(self, meeting_id):
        """Rebuild profile notes after who-was-on-the-call changed. Meetings still being
        processed are skipped: their own job does this at the end."""
        with db.connect() as con:
            m = con.execute("SELECT status FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        if m and m["status"] in ("done", "error"):
            self.jobs.put(("people", meeting_id))

    # -- internals ---------------------------------------------------------
    def _recover(self):
        with db.connect() as con:
            rows = con.execute("SELECT id, status FROM meetings WHERE status IN "
                               "('recording', 'queued', 'transcribing', 'summarising')").fetchall()
        for r in rows:
            me, them = raw_paths(r["id"])
            if r["status"] == "recording" and not (me.exists() and them.exists()):
                db.set_status(r["id"], "error", error="App closed while recording and no audio was saved")
            else:
                self.process(r["id"])
        self._queue_dirty_people()

    def _queue_dirty_people(self):
        with db.connect() as con:
            ids = [r["id"] for r in con.execute(
                "SELECT id FROM meetings WHERE people_dirty = 1 AND status = 'done'")]
        for meeting_id in ids:
            self.jobs.put(("people", meeting_id))
        # Teams chat weeks saved by the MCP (a separate process) wait here as 'queued'.
        # The sweep only runs once the queue has sat empty, so nothing is queued twice.
        with db.connect() as con:
            chats = [r["id"] for r in con.execute(
                "SELECT id FROM meetings WHERE chat IS NOT NULL AND status = 'queued' ORDER BY started_at")]
        for meeting_id in chats:
            self.jobs.put(("summary", meeting_id))

    def _stale(self, kind, meeting_id):
        """A leftover job for notes Claude has checked: skip it rather than let the local model
        overwrite them. Redo notes and a Teams rebuild set the status back to 'queued', and a tag
        change sets people_dirty, so real work still runs."""
        with db.connect() as con:
            m = con.execute("SELECT status, notes_source, people_dirty FROM meetings WHERE id = ?",
                            (meeting_id,)).fetchone()
        if m is None or m["status"] != "done" or m["notes_source"] != "claude":
            return False
        return kind == "summary" or (kind == "people" and not m["people_dirty"])

    def _loop(self):
        while True:
            if time.time() - self._last_purge > PURGE_EVERY:
                self._purge_old_audio()
            try:
                kind, meeting_id = self.jobs.get(timeout=60)
            except queue.Empty:
                self._queue_dirty_people()  # tags changed from outside the app (the MCP)
                continue
            try:
                if self._stale(kind, meeting_id):
                    continue
                if kind == "people":
                    renamed = self._voices(meeting_id)  # who's tagged decides whose voices to look for
                    self._people(meeting_id)
                    if renamed and self._notes_by_local_model(meeting_id):
                        self.redo_summary(meeting_id)  # so the notes use the names too
                    recall_feed.write(meeting_id)
                    self._sync_eval(meeting_id)
                    continue
                if kind in ("process", "retranscribe"):
                    self._transcribe(meeting_id, force=kind == "retranscribe")
                    self._voices(meeting_id)
                self._summarise(meeting_id)
                self._people(meeting_id)
                db.set_status(meeting_id, "done")
                recall_feed.write(meeting_id)
                self._sync_eval(meeting_id)
            except Exception as e:
                traceback.print_exc()
                db.set_status(meeting_id, "error", error=f"{type(e).__name__}: {e}")
            finally:
                self.progress = None

    def _voices(self, meeting_id):
        """Learn voices from this call and name its "Them" lines. Never fails the job: a
        call whose voices can't be read just keeps "Them"."""
        them = next((p[1] for p in (raw_paths(meeting_id), track_paths(meeting_id)) if p[1].exists()), None)
        if them is None:
            return 0
        self.progress = {"meeting_id": meeting_id, "stage": "Recognising voices", "pct": None}
        try:
            return voices.update(meeting_id, them)
        except Exception:
            traceback.print_exc()
            return 0

    @staticmethod
    def _notes_by_local_model(meeting_id):
        with db.connect() as con:
            m = con.execute("SELECT notes_source FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        return m is not None and m["notes_source"] != "claude"

    @staticmethod
    def _sync_eval(meeting_id):
        """Add/refresh/remove the 1:1 entry in the eval profile. A problem here must never
        mark a finished meeting as failed, so it only lands in the meeting's summary_error."""
        try:
            evals.sync(meeting_id)
        except Exception as e:
            traceback.print_exc()
            db.set_status(meeting_id, "done", summary_error=f"Couldn't update the eval profile: {e}")

    def _whisper(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # slow import, only when first needed
            self._model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type=WHISPER_COMPUTE)
        return self._model

    def _transcribe(self, meeting_id, force=False):
        raw = raw_paths(meeting_id)
        kept = track_paths(meeting_id)
        if all(p.exists() for p in raw):
            me_path, them_path = raw
        elif force and all(p.exists() for p in kept):
            me_path, them_path = kept
        else:
            with db.connect() as con:
                has_text = con.execute("SELECT 1 FROM segments WHERE meeting_id = ? LIMIT 1",
                                       (meeting_id,)).fetchone()
            if has_text and not force:
                return  # already transcribed before a restart; just summarise
            raise FileNotFoundError("Recording files are missing")

        db.set_status(meeting_id, "transcribing")
        self.progress = {"meeting_id": meeting_id, "stage": "Transcribing", "pct": 0}
        model = self._whisper()
        total = sf.info(me_path).duration + sf.info(them_path).duration or 1
        done = 0.0
        tracks = {}
        for speaker, path in (("me", me_path), ("them", them_path)):
            # condition_on_previous_text=False stops one misheard line feeding the next,
            # which is what turns a pause into "ma, ma, ma..." loops.
            segments, info = model.transcribe(str(path), vad_filter=True, initial_prompt=VOCAB,
                                              condition_on_previous_text=False)
            tracks[speaker] = []
            for seg in segments:
                tracks[speaker].append(seg)
                self.progress["pct"] = int(100 * (done + seg.end) / total)
            done += info.duration

        self.progress = {"meeting_id": meeting_id, "stage": "Cleaning transcript", "pct": 100}
        env_me, env_them = envelope(me_path), envelope(them_path)
        them = drop_hallucinations(tracks["them"])
        me = [s for s in drop_hallucinations(tracks["me"]) if not is_bleed(s, them, env_me, env_them)]
        rows = [(meeting_id, s.start, s.end, "Me", s.text.strip()) for s in me]
        rows += [(meeting_id, s.start, s.end, "Them", s.text.strip()) for s in them]
        with db.connect() as con:
            con.execute("DELETE FROM segments WHERE meeting_id = ?", (meeting_id,))
            con.executemany("INSERT INTO segments (meeting_id, start, end, speaker, text) "
                            "VALUES (?, ?, ?, ?, ?)", rows)

        if (me_path, them_path) == raw:
            self._save_audio(meeting_id)

    def _save_audio(self, meeting_id):
        """One mixed file for playback plus each track compressed (so the call can be
        re-transcribed later), then drop the big FLACs."""
        self.progress = {"meeting_id": meeting_id, "stage": "Saving audio", "pct": 100}
        me_raw, them_raw = raw_paths(meeting_id)
        me_ogg, them_ogg = track_paths(meeting_id)
        mixed = AUDIO_DIR / f"{meeting_id}.ogg"
        if mix_to_opus(me_raw, them_raw, mixed) and to_opus(me_raw, me_ogg) and to_opus(them_raw, them_ogg):
            me_raw.unlink()
            them_raw.unlink()
            audio = mixed
        else:
            audio = them_raw
        with db.connect() as con:
            con.execute("UPDATE meetings SET audio_path = ? WHERE id = ?", (str(audio), meeting_id))

    def _summarise(self, meeting_id):
        db.set_status(meeting_id, "summarising")
        self.progress = {"meeting_id": meeting_id, "stage": "Writing notes", "pct": None}
        with db.connect() as con:
            lines = transcript_lines(con, meeting_id)
            names = [p["name"] for p in people.on_call(con, meeting_id)]
        if not lines:
            db.set_status(meeting_id, "summarising", summary_error="Nothing was said in this recording")
            return
        try:
            notes = summarise.notes(lines, names)
        except Exception as e:
            # The transcript is the valuable part; keep it and let the notes be retried.
            db.set_status(meeting_id, "summarising", summary_error=f"Notes failed: {e}. "
                          "Check Ollama is running, then press Redo notes.")
            return
        with db.connect() as con:
            notes_store.save(con, meeting_id, notes, "local")

    def _people(self, meeting_id):
        """Replace this call's profile notes for everyone tagged on it."""
        with db.connect() as con:
            tagged = people.on_call(con, meeting_id)
            lines = transcript_lines(con, meeting_id)
        found = []
        if tagged and lines:
            self.progress = {"meeting_id": meeting_id, "stage": "Updating profiles", "pct": None}
            try:
                found = summarise.people_notes(lines, [p["name"] for p in tagged])
            except Exception:
                traceback.print_exc()
                return  # leave people_dirty set so the next idle sweep retries
        with db.connect() as con:
            changed = notes_store.save_people(con, meeting_id, found)
            notes_store.commitments_to_actions(con, meeting_id)
            notes_store.fix_owners(con, meeting_id)
        for person_id in changed:
            recall_feed.write_person(person_id, refresh=False)
        recall_feed.refresh_recall()

    def _purge_old_audio(self):
        self._last_purge = time.time()
        cutoff = (datetime.now() - timedelta(days=AUDIO_RETENTION_DAYS)).isoformat(timespec="seconds")
        with db.connect() as con:
            rows = con.execute("SELECT id, audio_path FROM meetings WHERE audio_path IS NOT NULL "
                               "AND started_at < ?", (cutoff,)).fetchall()
            for r in rows:
                try:
                    for p in {*audio_files(r["id"]), AUDIO_DIR.joinpath(r["audio_path"])}:
                        p.unlink(missing_ok=True)
                except OSError:
                    continue  # locked by the browser; try again next pass
                con.execute("UPDATE meetings SET audio_path = NULL, audio_purged = 1 WHERE id = ?",
                            (r["id"],))
