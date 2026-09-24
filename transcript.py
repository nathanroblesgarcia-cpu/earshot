"""Light transcript helpers shared by the app and the MCP (no audio libraries)."""
from config import MERGE_GAP


def fmt(seconds):
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def who(speaker, them="Them"):
    """Label for a line. Calls have Me/Them; a Teams group chat keeps each sender's name."""
    if speaker == "Me":
        return "Me"
    return them if speaker == "Them" else speaker.split()[0]


def stamp(m, seconds):
    """A line's time: offset into the call, or the date and time for a Teams chat week
    (its lines are seconds after the week's first message)."""
    if m is not None and m["chat"]:
        from datetime import datetime, timedelta
        return (datetime.fromisoformat(m["started_at"]) + timedelta(seconds=seconds)).strftime("%a %d %b %H:%M")
    return fmt(seconds)


def merged(rows):
    """Join consecutive lines from the same speaker. The transcriber splits at every pause,
    which leaves one-word lines ("parang,", "ano,") that read badly and confuse the notes."""
    out = []
    for r in rows:
        if out and out[-1]["speaker"] == r["speaker"] and r["start"] - out[-1]["end"] < MERGE_GAP:
            out[-1]["text"] += " " + r["text"]
            out[-1]["end"] = r["end"]
        else:
            out.append({"start": r["start"], "end": r["end"], "speaker": r["speaker"], "text": r["text"]})
    return out
