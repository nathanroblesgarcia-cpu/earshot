"""Telling voices apart: who said each "Them" line on a group call.

A small speaker model (WeSpeaker ResNet34, trained on VoxCeleb, 26 MB, CPU) turns a few
seconds of speech into a voiceprint: 256 numbers where the same voice lands close
together. Earshot learns each person's voiceprint by itself from 1:1 calls, where every
"Them" line is that one person (and from any line he labels by hand). On a call with
several people tagged, each "Them" line is compared with the tagged people's voiceprints
and gets their name only when the match is clear; otherwise it stays "Them".

Everything stays local: voiceprints live in the Earshot database, next to the audio.
"""
import math

import numpy as np
import soundfile as sf

import db
from config import (VOICE_MARGIN, VOICE_MATCH, VOICE_MIN_SECONDS, VOICE_MODEL_FILE,
                    VOICE_MODEL_REPO)

RATE = 16000
_session = None


# -- features: Kaldi-style log mel filterbank, as the model was trained on -------------
def _mel(f):
    return 1127.0 * np.log(1.0 + f / 700.0)


def _mel_banks(n_mels=80, n_fft=512, low=20.0, high=RATE / 2):
    bins = np.arange(n_fft // 2) * RATE / n_fft  # Kaldi leaves out the Nyquist bin
    m_lo, m_hi = _mel(low), _mel(high)
    centers = m_lo + (m_hi - m_lo) * np.arange(n_mels + 2) / (n_mels + 1)
    mb = _mel(bins)
    banks = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float32)
    for i in range(n_mels):
        left, mid, right = centers[i], centers[i + 1], centers[i + 2]
        up = (mb - left) / (mid - left)
        down = (right - mb) / (right - mid)
        banks[i, :n_fft // 2] = np.maximum(0.0, np.minimum(up, down))
    return banks


_BANKS = _mel_banks()


def fbank(wave):
    """80 log mel energies every 10 ms (25 ms windows), mean-normalised over time.
    wave: float32 mono at 16 kHz in [-1, 1]."""
    x = wave.astype(np.float64) * 32768.0  # the model was trained on int16-scaled audio
    frame, shift, n_fft = 400, 160, 512
    if len(x) < frame:
        return np.zeros((0, 80), dtype=np.float32)
    n = 1 + (len(x) - frame) // shift
    idx = np.arange(frame)[None, :] + shift * np.arange(n)[:, None]
    frames = x[idx]
    frames = frames - frames.mean(axis=1, keepdims=True)                  # remove DC
    frames = np.concatenate([frames[:, :1] - 0.97 * frames[:, :1],        # pre-emphasis
                             frames[:, 1:] - 0.97 * frames[:, :-1]], axis=1)
    window = (0.5 - 0.5 * np.cos(2 * math.pi * np.arange(frame) / (frame - 1))) ** 0.85  # Povey
    power = np.abs(np.fft.rfft(frames * window, n=n_fft)) ** 2
    feats = np.log(np.maximum(power @ _BANKS.T, np.finfo(np.float64).eps))
    return (feats - feats.mean(axis=0)).astype(np.float32)


# -- audio ---------------------------------------------------------------------------
def _lowpass(rate_in, taps=101):
    cutoff = 0.9 * (RATE / 2) / rate_in
    n = np.arange(taps) - (taps - 1) / 2
    h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(taps)
    return h / h.sum()


def load(path):
    """A track as 16 kHz mono float32."""
    wave, rate = sf.read(str(path), dtype="float32", always_2d=True)
    wave = wave.mean(axis=1)
    if rate != RATE:
        wave = np.convolve(wave, _lowpass(rate), mode="same")
        if rate % RATE == 0:
            wave = wave[::rate // RATE]
        else:
            t = np.arange(0, len(wave) / rate, 1 / RATE)
            wave = np.interp(t, np.arange(len(wave)) / rate, wave)
    return wave.astype(np.float32)


# -- voiceprints -----------------------------------------------------------------------
def _model():
    global _session
    if _session is None:
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        path = hf_hub_download(VOICE_MODEL_REPO, VOICE_MODEL_FILE)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2  # leave the rest of the laptop alone
        _session = ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"])
    return _session


def embed(wave):
    """Voiceprint of a stretch of speech (unit length), or None if it's too short."""
    if len(wave) < VOICE_MIN_SECONDS * RATE:
        return None
    feats = fbank(wave)
    out = _model().run(None, {"feats": feats[None]})[0][0]
    return out / (np.linalg.norm(out) + 1e-9)


def similarity(a, b):
    return float(np.dot(a, b))


def centroid(prints, weights=None):
    v = np.average(np.stack(prints), axis=0, weights=weights)
    return v / (np.linalg.norm(v) + 1e-9)


def line_prints(wave, rows):
    """[(line, voiceprint)] for the lines long enough to judge."""
    out = []
    for r in rows:
        e = embed(wave[int(r["start"] * RATE):int(r["end"] * RATE)])
        if e is not None:
            out.append((r, e))
    return out


def decide(e, prints):
    """(person id, score) when one voiceprint clearly matches, else (None, best score).
    prints: {person_id: voiceprint}."""
    scored = sorted(((similarity(e, v), pid) for pid, v in prints.items()), reverse=True)
    if not scored:
        return None, 0.0
    best, pid = scored[0]
    second = scored[1][0] if len(scored) > 1 else -1.0
    if best >= VOICE_MATCH and best - second >= VOICE_MARGIN:
        return pid, best
    return None, best


def to_blob(v):
    return np.asarray(v, dtype=np.float32).tobytes()


def from_blob(b):
    return np.frombuffer(b, dtype=np.float32)


def known_prints(con, person_ids=None):
    """{person_id: voiceprint} from every call they've been learned on."""
    rows = con.execute("SELECT person_id, emb, seconds FROM voiceprints").fetchall()
    by = {}
    for r in rows:
        if person_ids is None or r["person_id"] in person_ids:
            by.setdefault(r["person_id"], ([], []))
            by[r["person_id"]][0].append(from_blob(r["emb"]))
            by[r["person_id"]][1].append(r["seconds"])
    return {pid: centroid(p, w) for pid, (p, w) in by.items()}


# -- learning and labelling a call ------------------------------------------------------
def _tagged(con, meeting_id):
    return con.execute("""SELECT p.id, p.name FROM people p JOIN meeting_people mp ON mp.person_id = p.id
                          WHERE mp.meeting_id = ?""", (meeting_id,)).fetchall()


def _learn(con, meeting_id, tagged, wave, lines):
    """Store this call's voiceprint for each person it can teach: on a 1:1, every "Them"
    line is the one person; on any call, lines he labelled by hand are who he said."""
    names = {p["name"]: p["id"] for p in tagged}
    teach = {}
    for r in lines:
        if r["voice"] == "manual" and r["speaker"] in names:
            teach.setdefault(names[r["speaker"]], []).append(r)
        elif len(tagged) == 1 and r["speaker"] == "Them":
            teach.setdefault(tagged[0]["id"], []).append(r)
    con.execute("DELETE FROM voiceprints WHERE meeting_id = ?", (meeting_id,))
    for pid, rows in teach.items():
        prints = line_prints(wave, rows)
        if prints:
            secs = [r["end"] - r["start"] for r, _ in prints]
            con.execute("INSERT INTO voiceprints (person_id, meeting_id, emb, seconds) VALUES (?, ?, ?, ?)",
                        (pid, meeting_id, to_blob(centroid([e for _, e in prints], secs)), sum(secs)))
    return len(teach)


def _label(con, meeting_id, tagged, wave, lines):
    """Name the "Them" lines of a call with two or more people tagged. Returns how many
    lines changed. Lines he labelled by hand are left as he set them."""
    before = {r["id"]: r["speaker"] for r in lines}
    auto = [r for r in lines if r["speaker"] != "Me" and r["voice"] != "manual"]
    con.execute("UPDATE segments SET speaker = 'Them', voice = NULL, voice_score = NULL "
                "WHERE meeting_id = ? AND voice = 'auto'", (meeting_id,))
    if len(tagged) < 2:
        return sum(before[r["id"]] != "Them" for r in auto)
    names = {p["id"]: p["name"] for p in tagged}
    prints = known_prints(con, set(names))
    if not prints:
        return sum(before[r["id"]] != "Them" for r in auto)
    # A voiceprint learned on another call only: compare against the tagged people only,
    # so a voice can never be named as someone who wasn't there.
    got = {}
    judged = [r for r in auto if r["end"] - r["start"] >= VOICE_MIN_SECONDS]
    for r, e in line_prints(wave, judged):
        pid, score = decide(e, prints)
        if pid is not None:
            got[r["id"]] = (names[pid], score)
    # A short line ("oo", "sige") between two lines of the same voice is that voice too.
    ordered = sorted(auto, key=lambda r: r["start"])
    for i, r in enumerate(ordered):
        if r["id"] in got or r["end"] - r["start"] >= VOICE_MIN_SECONDS:
            continue
        prev = next((got[x["id"]][0] for x in reversed(ordered[:i]) if x["id"] in got), None)
        nxt = next((got[x["id"]][0] for x in ordered[i + 1:] if x["id"] in got), None)
        if prev and prev == nxt:
            got[r["id"]] = (prev, None)
    con.executemany("UPDATE segments SET speaker = ?, voice = 'auto', voice_score = ? WHERE id = ?",
                    [(name, score, sid) for sid, (name, score) in got.items()])
    return sum(before[r["id"]] != got.get(r["id"], ("Them",))[0] for r in auto)


def update(meeting_id, them_path):
    """Learn from this call and name its voices. Returns how many lines changed name."""
    with db.connect() as con:
        m = con.execute("SELECT chat FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        if m is None or m["chat"]:
            return 0
        tagged = _tagged(con, meeting_id)
        lines = con.execute("SELECT id, start, end, speaker, voice, text FROM segments WHERE meeting_id = ? "
                            "AND speaker != 'Me' ORDER BY start", (meeting_id,)).fetchall()
    if not lines:
        return 0
    wave = load(them_path)
    with db.connect() as con:
        _learn(con, meeting_id, tagged, wave, lines)
        return _label(con, meeting_id, tagged, wave, lines)


def set_line(con, segment_ids, name):
    """He says who said these lines (name, or None for "Them"). Hand labels are kept and
    teach Earshot that voice."""
    for sid in segment_ids:
        if name:
            con.execute("UPDATE segments SET speaker = ?, voice = 'manual', voice_score = NULL "
                        "WHERE id = ? AND speaker != 'Me'", (name, sid))
        else:
            con.execute("UPDATE segments SET speaker = 'Them', voice = NULL, voice_score = NULL "
                        "WHERE id = ? AND speaker != 'Me'", (sid,))


def learned(con, person_id):
    """(calls, seconds) of speech a person's voiceprint was learned from."""
    r = con.execute("SELECT COUNT(*) AS n, COALESCE(SUM(seconds), 0) AS s FROM voiceprints WHERE person_id = ?",
                    (person_id,)).fetchone()
    return r["n"], r["s"]
