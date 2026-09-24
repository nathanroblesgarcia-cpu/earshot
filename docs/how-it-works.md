# How Earshot works, and what went wrong on the way

Earshot started as "Fireflies, but without a bot joining the call". Each section below is a problem real calls turned up, and the fix that survived.

## Recording without joining

A bot that joins a meeting needs the organiser's permission and shows up in the attendee list. Earshot records the laptop instead. The mic is the "Me" track, and the default speaker's output, captured with WASAPI loopback, is the "Them" track.

- **Keeping the loopback alive.** Loopback capture only runs while something is playing. During a silent stretch of a call it stops delivering audio, and the two tracks drift apart. Earshot plays a silent stream to the same device for the whole recording, so the loopback clock never stops.
- **Separate tracks.** Keeping the tracks separate makes the rest possible: who spoke, bleed detection, and a per-track redo of the transcript.

## Knowing when a call starts

Windows records every app that uses the mic under `CapabilityAccessManager\ConsentStore\microphone`, and a `LastUsedTimeStop` of 0 means the app is using it right now. Earshot reads that every 3 seconds.

- **Start and stop.** When Teams, Zoom or a browser takes the mic, a small pop-up asks whether to record. When the app lets go for 30 seconds, the recording stops. Nothing needs installing in the call app.
- **Who the call is with.** This comes from the title of the Teams call window. The main Teams window names whichever chat is open, which is often not the person on the call. So only a window that appeared while the call was ringing counts, and a window already open when Earshot started never does. A wrong tag on a 1:1 would write to the wrong person's profile, so the rules are strict.

## Whisper problems

- **Looping.** Over silence, Whisper sometimes repeats a phrase ("ma, ma, ma..." 30 times), or invents a sentence like "It's not available." There are three filters: a high no-speech probability with low confidence, a high compression ratio, and a short phrase repeated 4 or more times within 30 seconds. `condition_on_previous_text=False` stops one bad line from leading the next one astray.
- **Names.** Names come out wrong without help: "Maya" became "Mia", and jargon got garbled. The names list in `VOCAB` goes in as Whisper's `initial_prompt`. It ends with a Taglish sentence, which nudges the model toward code-switching instead of forcing everything into English.

## Speaker bleed

This was the biggest quality problem. On a laptop speaker the mic hears the other person, so their sentences turn up twice: once as "Them", and again, a bit garbled, as "Me".

1. **Matching words** (drop a "Me" line that repeats nearby "Them" words) worked in English. It failed in Taglish, because Whisper transcribed the two copies too differently.
2. **Loudness.** In 50 ms steps, compare the mic's loudness curve with the call audio's, shifted by up to a second. Bleed follows the call audio closely. Real speech doesn't, even when both people talk at once. The first version looked only 300 ms ahead and missed everything. The measured speaker-to-mic delay was 550 ms, so the window now runs from -250 ms to +1 s. Square-rooted loudness separates the two cases best.
3. **Evaluation.** `evals/bleed_eval.py` measures the rule on synthetic cases in four conditions (see the README). On a normal laptop speaker it catches all the bleed and never drops real speech. It fails, safely, when the bleed is very quiet or the room is noisy: bleed gets through, and real speech is still kept. Smoothing the loudness curves was tested as a fix and rejected, because it started dropping real speech.

## The local LLM

The notes come from `qwen2.5:7b` in Ollama, with a JSON schema passed as `format`, so every reply parses.

- **Temperature 0 looped.** With the JSON format, temperature 0 made the model repeat itself until the queue had been stuck for 20 minutes. It now runs at temperature 0.2 with a fixed seed, so the same call gives the same notes, and `num_predict` caps the output length.
- **The model worker dying.** On a laptop with little free memory, Ollama's model worker died mid-reply and the server waited forever. Replies are now streamed. No new text for 7 minutes counts as a stall: Earshot restarts Ollama once and retries. A second failure shows up as a **Redo notes** button, never as a silent hang.
- **Memory.** Ollama keeps a model loaded for 5 minutes after each request. It held two models at once, 8.5 GB. Earshot now asks it to unload 90 seconds after the queue empties.
- **A smaller model was tried and rejected.** `llama3.2:3b` was 2.4 times faster, but it gave every action item a due date of "Friday", used "Me" as an owner, and listed "Discuss X" as tasks. A faster model that writes wrong notes saves no time.
- **Owners.** Owners were the model's weakest spot. A rule-based pass fixes them afterwards, sentence by sentence, in English and Tagalog. "Can you send me..." from them means Sam owns it, and "I'll send it" from them means they do. A second pass reads each person's own commitments and turns any the notes missed into action items.

## Tests that protect real data

An early version of the test suite wrote a fake 1:1 entry into a real profile file, because the seeded people pointed at real paths. The suite now points every path into a throwaway folder. It also fingerprints the real profile folder before and after the run, and fails if anything changed.
