"""Earshot settings. Everything tunable lives here."""
import os
from pathlib import Path

APP_DIR = Path(__file__).parent
PORT = int(os.environ.get("EARSHOT_PORT", 5175))

# Keep the live DB and audio on local disk, outside any cloud-synced folder: a sync client
# grabbing a live SQLite file mid-write can corrupt it. Audio is private and kept 30 days.
LOCAL_DIR = Path(os.environ.get("EARSHOT_DATA", APP_DIR / "local"))
DB_PATH = LOCAL_DIR / "earshot.db"
AUDIO_DIR = LOCAL_DIR / "audio"

AUDIO_RETENTION_DAYS = 30
MAX_RECORDING_HOURS = 3  # auto-stop if a recording is forgotten

# Transcription: same settings that handled Taglish well in the 1:1 review workflow.
WHISPER_MODEL = "small"
WHISPER_COMPUTE = "int8"
# Names and jargon Whisper would otherwise mishear. Whisper reads this as the text that came
# before, so it also carries one Taglish sentence to nudge it toward English/Tagalog
# code-switching. Put your own team, suppliers and jargon here.
VOCAB = ("Earshot, Ember & Oak Coffee, Sam, Maya, Leo, Priya, Omar, Jess, Rosa, Ben, "
         "Harbour Beans, Northside Kiosk, cold brew, pour-over, espresso, roast profile, green beans, "
         "oat milk, POS, loyalty app, rota, stocktake, wholesale, pastry order. "
         "Okay, so yung roast schedule natin, i-check ko muna yung stock sheet tapos i-order natin.")

# Transcript cleaning (tuned on the first real call, a speaker setup).
MERGE_GAP = 3.0              # join same-speaker lines closer than this (seconds)
HALLUCINATION_REPEATS = 4    # a short phrase this many times within 30 s is Whisper looping
ENVELOPE_FRAME = 0.05        # loudness is compared in 50 ms steps
THEM_ACTIVE = 0.3            # the other side is "talking" when >30% of a line's frames are audible
BLEED_CORR = 0.6             # mic loudness tracking theirs this closely = the speaker bleeding in
BLEED_MAX_LAG = 20           # frames (1 s) the mic may lag the call audio; measured 550 ms on his speaker
BLEED_MIN_LAG = -5           # and a little the other way, as the two streams start a few ms apart

# People Earshot knows from the first run (more are added by tagging a call).
# eval_profile links a direct report to a markdown development profile; each 1:1 call with
# them is logged there automatically (evals.py).
PROFILES_DIR = APP_DIR / "sample_profiles"
PEOPLE_SEED = [
    {"name": "Maya Santos", "aliases": "Maya", "role": "Head Barista",
     "eval_profile": str(PROFILES_DIR / "Maya" / "profile.md")},
    {"name": "Leo Reyes", "aliases": "Leo", "role": "Roaster",
     "eval_profile": str(PROFILES_DIR / "Leo" / "profile.md")},
    {"name": "Priya Nair", "aliases": "Priya, Pri", "role": "Shift Lead",
     "eval_profile": str(PROFILES_DIR / "Priya" / "profile.md")},
    {"name": "Jess", "aliases": "", "role": "Bookkeeper"},
    {"name": "Rosa", "aliases": "", "role": "Pastry supplier"},
    {"name": "Ben", "aliases": "", "role": "Barista"},
    {"name": "Ana", "aliases": "", "role": "Barista"},
    {"name": "Omar", "aliases": "", "role": "Account manager, Harbour Beans"},
]

# Text-based bleed backup: drop "me" segments that repeat what "them" said.
BLEED_WINDOW = 2.0      # seconds either side to look for the original
BLEED_RUN = 3           # this many consecutive shared words means bleed
BLEED_SIMILARITY = 0.4  # or this overall word similarity

# Teams chat names mapped to people, so a 1:1 call window titled with the chat name is
# tagged with the right person ({"chat": "Maya S.", "person": "Maya Santos"}).
TEAMS_CHATS = []

# Call detection: Windows records which apps are using the mic right now in the
# registry. When one of these starts, Earshot asks whether to record; when it lets
# go of the mic for AUTO_STOP_AFTER seconds, the recording stops on its own.
DETECT_CALLS = True
DETECT_EVERY = 3  # seconds between registry checks
CALL_APPS = {     # lowercase fragment of the registry key -> name shown in the prompt
    "msteams": "Teams", "teams.exe": "Teams", "zoom": "Zoom", "chrome.exe": "Chrome",
    "msedge.exe": "Edge", "slack": "Slack", "webex": "Webex",
}
AUTO_STOP_AFTER = 30
PROMPT_TIMEOUT = 90  # the "Record this call?" pop-up closes itself as "Not now" after this
# Auto-tagging: who the call is with comes from the Teams call window's title. It keeps
# looking this long into a recording (the title can fill in after the call connects).
TAG_LOOK_FOR = 120
RING_TIME = 90  # a window that opened this long before the mic came on can still be the call's
CALL_LOG = LOCAL_DIR / "calls.log"  # every call-window title seen, to check new title formats

# Reminders: each weekday at or after this time the tray lists his action items due today,
# overdue and due tomorrow (plus, on Mondays, undated ones left open this many days).
REMIND_AT = "09:00"
REMIND_STALE_DAYS = 7

# Markdown export: one file of notes per meeting and per person, for a notes app or a RAG
# index to pick up. Set a URL to ping it after each write (None = don't).
RECALL_NOTES_DIR = LOCAL_DIR / "markdown"
RECALL_REFRESH_URL = None

# Voices: who said each "Them" line on a group call (voices.py). Voiceprints are learned
# from 1:1 calls. A line gets a name only when its voice matches one tagged person by at
# least VOICE_MATCH and beats the next best by VOICE_MARGIN; otherwise it stays "Them".
VOICE_MODEL_REPO = "Wespeaker/wespeaker-voxceleb-resnet34-LM"  # 26 MB, CC BY 4.0
VOICE_MODEL_FILE = "voxceleb_resnet34_LM.onnx"
VOICE_MIN_SECONDS = 1.5  # shorter lines ("okay", "sige") are too short to tell apart
VOICE_MATCH = 0.45  # on two real voices: 51 of 53 lines named, 0 wrong
VOICE_MARGIN = 0.1

# Summaries
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
OLLAMA_MODEL = "qwen2.5:7b"
# Memory: the model is ~5 GB and a 32k context adds ~2 GB more. On a laptop with
# little free RAM that killed Ollama's model worker, so cap at 16k and read long
# transcripts in smaller chunks (merged afterwards).
OLLAMA_MAX_CTX = 16384
CHUNK_CHARS = 36000           # longer transcripts are summarised in chunks, then merged
# Ollama keeps a model in memory 5 min after each request by default, and it held qwen
# (5.5 GB) and llama3.2 (3.1 GB) at once in testing. Earshot's next request follows
# within seconds while the queue has work, so let the model go soon after the queue empties.
OLLAMA_KEEP_ALIVE = "90s"
# llama3.2:3b was tried as a faster notes model: 2.4x faster, but it put "Friday" on every
# action item, used "Me" as an owner, and listed "Discuss X" as actions. Not used.
