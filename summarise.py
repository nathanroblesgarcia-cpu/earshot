"""Meeting notes from a transcript via local Ollama, as structured JSON."""
import json
import math
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from config import CHUNK_CHARS, OLLAMA_KEEP_ALIVE, OLLAMA_MAX_CTX, OLLAMA_MODEL, OLLAMA_URL

MAX_OUTPUT_TOKENS = 2000   # real notes are a few hundred tokens; more means the model is looping
REQUEST_TIMEOUT = 900      # seconds; a stuck call fails into "Redo notes" instead of blocking the queue
STALL_TIMEOUT = 420        # seconds with no new text from Ollama = its model worker has died
OLLAMA_APP = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama app.exe"
_last_restart = 0.0

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "array", "items": {"type": "string"}},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "action_items": {"type": "array", "items": {
            "type": "object",
            "properties": {"task": {"type": "string"}, "owner": {"type": "string"},
                           "due": {"type": "string"}},
            "required": ["task", "owner", "due"]}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "summary", "decisions", "action_items", "open_questions"],
}

NOTES_PROMPT = """You write meeting notes for Sam, the owner of Ember & Oak Coffee, a small coffee roastery and café.

The transcript is machine-generated from his call. Lines marked "Me" are Sam. Lines marked "Them" are everyone else on the call (possibly several people; use a name only if someone says it). The call may mix English and Tagalog (Taglish): write the notes in plain English. Expect some misheard words. Never invent facts that are not in the transcript.

Return:
- title: 3 to 8 words naming what the call was about (include the client if one is clearly named)
- summary: 3 to 8 short bullets of what was discussed, in the order it came up
- decisions: only the approach that was finally agreed. If an idea was considered and then dropped, it is NOT a decision (you may mention it in the summary as "considered, dropped because...")
- action_items: every follow-up for EVERY person, not just Sam. task, owner, due (as said, like "Friday", or "" if none). Owner is "Sam" when Me offers to do it OR when Them asks Me to do it ("can you...", "please..."). When Them says they will do something ("I'll scope it", "sige, ask ko si Jess"), the owner is that person (their name if known). Leave out anything the transcript shows was already done during the call
- open_questions: only questions still unanswered at the END of the call. If someone answered it later in the call, leave it out
Empty lists are fine. Keep every bullet short and plain. Only treat a word as a client or project name when it is clearly used as one; garbled words are transcription errors, not names."""

PEOPLE_SCHEMA = {
    "type": "object",
    "properties": {"people": {"type": "array", "items": {
        "type": "object",
        "properties": {"name": {"type": "string"},
                       "working_on": {"type": "array", "items": {"type": "string"}},
                       "raised": {"type": "array", "items": {"type": "string"}},
                       "commitments": {"type": "array", "items": {"type": "string"}},
                       "observations": {"type": "array", "items": {"type": "string"}}},
        "required": ["name", "working_on", "raised", "commitments", "observations"]}}},
    "required": ["people"],
}

PEOPLE_PROMPT = """From Sam's call transcript, write short profile notes about each person listed below (never about Sam himself, who is "Me").

For each person:
- working_on: projects, clients or tasks they are working on
- raised: issues, questions, concerns or requests they brought up
- commitments: things they said they will do
- observations: anything else useful to remember (what they need from Sam, preferences, context)

Rules: only facts the transcript supports. Plain factual notes, never judgments about how well someone performs. If you can't tell which person said something, leave it out. Something Sam ("Me") said he would do is NOT their commitment. Never invent client or project names: garbled words are transcription errors, not names. The call may be Taglish: write in plain English. Empty lists are fine. Short bullets."""

MERGE_PROMPT ="""These are partial notes from consecutive parts of ONE meeting. Merge them into a single set of notes in the same format: one title for the whole call, combine and de-duplicate bullets, keep every distinct action item and decision."""


def _who(people_names):
    """Prompt lines telling the model who "Them" is, when he has tagged the call."""
    if not people_names:
        return ""
    if len(people_names) == 1:
        return (f'\n\nThe only other person on this call is {people_names[0]}: every "Them" line '
                "is them. Use their name as owner for things they will do.")
    return ("\n\nOther people on this call: " + ", ".join(people_names) + '. "Them" lines can be '
            "any of them; use a name as owner only when the transcript makes it clear.")


def _ollama_up(timeout=3):
    try:
        urllib.request.urlopen(OLLAMA_URL.replace("/api/chat", "/api/tags"), timeout=timeout).close()
        return True
    except Exception:
        return False


def restart_ollama():
    """Kill and relaunch Ollama. Its model worker dies under memory pressure (1.7 GB free of
    15 GB on 2026-09-24) and the server then waits forever for it. At most once per 10 min,
    so a machine that really can't run the model doesn't restart it in a loop."""
    global _last_restart
    if time.time() - _last_restart < 600:
        return False
    _last_restart = time.time()
    for image in ("ollama app.exe", "ollama.exe"):
        subprocess.run(["taskkill", "/IM", image, "/F", "/T"], capture_output=True)
    time.sleep(2)
    subprocess.Popen([str(OLLAMA_APP)], creationflags=0x00000008 | 0x08000000)  # detached, no window
    for _ in range(60):
        if _ollama_up():
            return True
        time.sleep(1)
    return False


def _chat(system, user, chars, schema=SCHEMA):
    """One structured request, retried once after restarting Ollama if it stalls or drops."""
    try:
        return _chat_once(system, user, chars, schema)
    except (TimeoutError, ConnectionError, urllib.error.URLError, OSError) as first:
        if not restart_ollama():
            raise
        try:
            return _chat_once(system, user, chars, schema)
        except Exception as second:
            raise RuntimeError(f"Ollama failed twice ({first}; then {second})") from second


def _chat_once(system, user, chars, schema=SCHEMA):
    tokens = chars / 3 + 3000  # rough: ~3 chars per token for Taglish, plus prompt and output
    num_ctx = min(OLLAMA_MAX_CTX, max(8192, 2048 * math.ceil(tokens / 2048)))
    body = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "format": schema,
        # Streamed, so a dead model worker shows up as silence (no new text for STALL_TIMEOUT)
        # instead of a request that hangs until the overall timeout.
        "stream": True,
        "keep_alive": OLLAMA_KEEP_ALIVE,  # frees ~5.5 GB of RAM soon after the queue is done
        # A fixed seed makes the same call give the same notes every run (the owner of an
        # action item flipped between two QA runs without it). Not temperature 0: with the
        # JSON format that made qwen loop forever and blocked the queue for 20+ minutes on
        # 2026-09-24. num_predict caps the output so a runaway can never block it again.
        "options": {"temperature": 0.2, "seed": 42, "num_ctx": num_ctx, "num_predict": MAX_OUTPUT_TOKENS},
    }
    req = urllib.request.Request(OLLAMA_URL, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    parts, started = [], time.time()
    # The socket timeout applies to each read: reading the prompt can legitimately take a few
    # minutes on CPU before the first word, so STALL_TIMEOUT is generous.
    with urllib.request.urlopen(req, timeout=STALL_TIMEOUT) as resp:
        for line in resp:
            if time.time() - started > REQUEST_TIMEOUT:
                raise TimeoutError(f"notes took over {REQUEST_TIMEOUT} s")
            if not line.strip():
                continue
            chunk = json.loads(line)
            if chunk.get("error"):
                raise ConnectionError(f"Ollama: {chunk['error']}")
            parts.append(chunk.get("message", {}).get("content", ""))
            if chunk.get("done"):
                break
    return json.loads("".join(parts))


def _chunks(lines):
    chunk, size = [], 0
    for line in lines:
        if size + len(line) > CHUNK_CHARS and chunk:
            yield "\n".join(chunk)
            chunk, size = [], 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        yield "\n".join(chunk)


def notes(transcript_lines, people_names=()):
    """transcript_lines: ["[mm:ss] Me: ...", ...]. Returns the SCHEMA dict."""
    system = NOTES_PROMPT + _who(people_names)
    parts = list(_chunks(transcript_lines))
    if len(parts) == 1:
        return _chat(system, parts[0], len(parts[0]))
    partial = [_chat(system, p, len(p)) for p in parts]
    merged_input = json.dumps(partial, ensure_ascii=False)
    return _chat(MERGE_PROMPT, merged_input, len(merged_input))


def people_notes(transcript_lines, people_names):
    """Profile bullets per tagged person: {"people": [{name, working_on, ...}]}.
    Long calls are read in chunks and the bullets simply added together."""
    system = PEOPLE_PROMPT + "\n\nPeople: " + ", ".join(people_names) + _who(people_names)
    merged = {}
    for part in _chunks(transcript_lines):
        for p in _chat(system, part, len(part), PEOPLE_SCHEMA)["people"]:
            into = merged.setdefault(p["name"].strip(), {k: [] for k in
                                     ("working_on", "raised", "commitments", "observations")})
            for key in into:
                into[key] += [b.strip() for b in p[key] if b.strip() and b.strip() not in into[key]]
    return [{"name": name, **sections} for name, sections in merged.items()]
