"""How well does Earshot tell speaker bleed from real speech? A repeatable test.

The problem: on a laptop speaker (no headphones) the mic also hears the other person, so
the "Me" track gets copies of their words. Word matching can't catch it reliably (the two
copies are transcribed differently, especially in Taglish), so Earshot compares LOUDNESS:
during bleed, the mic's loudness follows the call audio's loudness a fraction of a second
later (pipeline.bleed_score). This builds labelled test cases and measures that rule.

Each case is a "Them" track (one Windows voice) and a "Me" track, built three ways:
  bleed      the mic only hears the call audio through the room: delayed 300 to 800 ms,
             quieter, smeared by a short echo, plus room noise.        label: bleed (drop it)
  overlap    he talks over them: his own voice plus the same bleed.  label: real   (keep it)
  alone      he talks while they are silent.                          label: real   (keep it)

Run:  python evals\\bleed_eval.py            (Windows: the voices come from System.Speech)
Writes evals\\bleed_report.md. The speech is synthetic, so this is a controlled test of
the rule, not of any particular room. Real calls are in the README for comparison.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pipeline  # noqa: E402
from config import BLEED_CORR, ENVELOPE_FRAME  # noqa: E402

RATE = 16000
SEED = 7
PHRASES = [
    "The green beans are up about twelve percent from November.",
    "Can you send me the revised price list on Monday?",
    "We are throwing away almost two cartons of oat milk a day.",
    "The left group head on the espresso machine is dripping again.",
    "I will do three test roasts so we can cup them on Friday.",
    "Yung cinnamon oat latte medyo matamis pa, so less syrup.",
    "The loyalty app is ready to test for a week.",
    "Two wholesale invoices are still unpaid, both over thirty days.",
    "Let's switch the house blend to the Colombian for now.",
    "Sige, I'll message their manager today and chase it.",
    "The chaff collector needs a proper clean on Saturday.",
    "Kulang tayo sa isang malaking container for cold brew.",
    "Maple cold brew tested really well with the regulars.",
    "I can swap with you and take Sunday if you take Tuesday.",
    "The technician's earliest visit is next Wednesday.",
    "Do we have every receipt for the quarterly return?",
]
VOICES = ["Microsoft Zira Desktop", "Microsoft David Desktop"]  # them, me


def speak(text, voice, path):
    ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          f"$s.SelectVoice('{voice}'); $s.Rate = 1; "
          f"$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo({RATE}, 16, 1); "
          f"$s.SetOutputToWaveFile('{path}', $f); $s.Speak(\"{text}\"); $s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True)
    audio, _ = sf.read(path, dtype="float32")
    return audio


# Harder and harder rooms: how loud the bleed is, how noisy the room, how loud he is.
CONDITIONS = [
    ("typical laptop speaker", (0.05, 0.3), 0.002, 1.0),
    ("quiet bleed", (0.01, 0.03), 0.002, 1.0),
    ("noisy room", (0.05, 0.3), 0.02, 1.0),
    ("he talks softly", (0.05, 0.3), 0.002, 0.25),
]


def room(signal, rng, gains):
    """What a laptop mic hears from its own speaker: later, quieter, with a short echo."""
    delay = int(rng.uniform(0.3, 0.8) * RATE)
    gain = rng.uniform(*gains)
    tail = np.exp(-np.arange(int(0.15 * RATE)) / (0.03 * RATE)) * rng.normal(0, 1, int(0.15 * RATE))
    tail[0] = 1.0
    wet = np.convolve(signal, tail / np.abs(tail).sum() * 3)[:len(signal)]
    return np.concatenate([np.zeros(delay), wet])[:len(signal)] * gain, delay / RATE


def place(clip, at, length):
    out = np.zeros(length, dtype="float32")
    out[at:at + len(clip)] = clip[:length - at]
    return out


def speech_span(clip, at):
    """Start and end (seconds) of the audible part of a clip placed at `at` samples."""
    loud = np.flatnonzero(np.abs(clip) > 0.02)
    return (at + loud[0]) / RATE, (at + loud[-1]) / RATE


def score_case(me, them, span, tmp):
    me_path, them_path = tmp / "me.wav", tmp / "them.wav"
    sf.write(me_path, me, RATE)
    sf.write(them_path, them, RATE)
    env_me, env_them = pipeline.envelope(me_path), pipeline.envelope(them_path)
    seg = type("Seg", (), {"start": span[0], "end": span[1]})
    verdict, active = pipeline.sounds_like_bleed(seg, env_me, env_them)
    i0, i1 = int(span[0] / ENVELOPE_FRAME), int(span[1] / ENVELOPE_FRAME) + 1
    score = pipeline.bleed_score(env_me[i0:i1], env_them, i0) if active >= pipeline.THEM_ACTIVE else None
    return bool(verdict), score


def run(clips, condition, rng, tmp):
    _, gains, noise_level, my_gain = condition
    rows = []
    for i in range(len(PHRASES)):
        them_clip = clips[i, 0]
        mine = clips[(i + 5) % len(PHRASES), 1] * my_gain  # another sentence, the other voice
        length = int(RATE * 1.0) + max(len(them_clip), len(mine)) + RATE
        them = place(them_clip, RATE // 2, length)
        noise = rng.normal(0, noise_level, length).astype("float32")
        bled, delay = room(them, rng, gains)
        them_span = speech_span(them_clip, RATE // 2)
        my_span = speech_span(clips[(i + 5) % len(PHRASES), 1], RATE // 2)
        cases = [
            ("bleed", True, bled + noise, them, (them_span[0] + delay, them_span[1] + delay)),
            ("overlap", False, place(mine, RATE // 2, length) + bled + noise, them, my_span),
            ("alone", False, place(mine, RATE // 2, length) + noise, np.zeros(length, "float32"), my_span),
        ]
        for kind, is_bleed, me, them_track, span in cases:
            said_bleed, score = score_case(me.astype("float32"), them_track, span, tmp)
            rows.append({"kind": kind, "truth": is_bleed, "said": said_bleed, "score": score, "delay": delay})
    return rows


def spread(rows, kind):
    s = [r["score"] for r in rows if r["kind"] == kind and r["score"] is not None]
    return f"{min(s):.2f} to {max(s):.2f} (median {np.median(s):.2f})" if s else "not scored"


def main():
    rng = np.random.default_rng(SEED)
    report = ["# Bleed detection eval", "",
              f"{len(PHRASES)} sentences x 3 cases (bleed, overlap, alone) per condition, seed {SEED}. "
              f"Threshold BLEED_CORR = {BLEED_CORR}. Synthetic speech (two Windows voices).", "",
              "| Condition | Bleed caught | Real speech wrongly dropped | Bleed score | Overlap score |",
              "|---|---|---|---|---|"]
    misses = []
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clips = {(i, v): speak(text, voice, str(tmp / f"{i}_{v}.wav"))
                 for i, text in enumerate(PHRASES) for v, voice in enumerate(VOICES)}
        for condition in CONDITIONS:
            rows = run(clips, condition, rng, tmp)
            bleed = [r for r in rows if r["truth"]]
            real = [r for r in rows if not r["truth"]]
            caught = sum(r["said"] for r in bleed)
            dropped = sum(r["said"] for r in real)
            report.append(f"| {condition[0]} | {caught}/{len(bleed)} | {dropped}/{len(real)} | "
                          f"{spread(rows, 'bleed')} | {spread(rows, 'overlap')} |")
            misses += [f"{condition[0]}, {r['kind']}: score {r['score']}" for r in rows if r["truth"] != r["said"]]
    report += ["", "- **Bleed caught** (recall): bleed lines the rule dropped.",
               "- **Real speech wrongly dropped**: his own words lost. This is the costly mistake, "
               "because they vanish from the notes, so the threshold sits above what overlapping speech scores.",
               "- A bleed line the rule misses is not lost: the word-matching backup still gets a try.", ""]
    report.append(f"{len(misses)} misses in all, every one a bleed line let through; no real speech was dropped."
                  if misses and not any("bleed:" not in m for m in misses) else f"{len(misses)} misses in all.")
    out = ROOT / "evals" / "bleed_report.md"
    text = "\n".join(report) + "\n"
    out.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
