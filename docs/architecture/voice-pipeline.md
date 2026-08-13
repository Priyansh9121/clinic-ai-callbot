# Voice pipeline

Milestone 3 turns the text receptionist into a locally running, interruptible
voice agent. Everything runs on the machine: no speech leaves it.

```
microphone → VAD → endpointing → STT → ConversationEngine → TTS → speaker
             │                                                     │
             └──────────── barge-in: caller speaks over ───────────┘
```

The `ConversationEngine` is untouched. Voice calls it through the same text
contract the CLI and the API use — `handle_message(session_id, text)` — so there
is exactly one implementation of every clinic rule, and a spoken sentence has
precisely the reach of a typed one.

| Layer | Module | Engine |
|---|---|---|
| Audio format | `app/speech/audio.py` | numpy + soxr |
| Voice activity | `app/speech/vad/silero.py` | Silero VAD v5 (ONNX) |
| Transcription | `app/speech/stt/whisper.py` | faster-whisper `base.en`, int8, CPU |
| Speech | `app/speech/tts/piper.py` | Piper `en_GB-alan-medium` |
| Devices | `app/voice/devices.py` | sounddevice / PortAudio |
| Orchestration | `app/voice/session.py` | — |

## The audio contract

One format, declared once in `app/speech/providers.py` and enforced everywhere:

    16 kHz · mono · signed 16-bit little-endian · 20 ms frames (640 bytes)

`app/speech/audio.py` is the only place a conversion happens. Every provider
takes and returns this format, so a microphone at 48 kHz, a Piper voice at
22.05 kHz and a future 8 kHz phone line all converge in one function rather than
in five slightly different ones.

`audioop` was removed from the standard library in Python 3.13 (PEP 594).
Resampling uses `soxr` (measured at 0.24 ms per second of audio, HQ quality)
rather than the `audioop-lts` shim, because downsampling artefacts land directly
on transcription accuracy.

## Two state machines, deliberately separate

`ConversationState` (in the engine) tracks the *clinic* workflow — identifying
the caller, offering slots, confirming a booking. `VoiceState` (in
`app/voice/states.py`) tracks *audio mechanics* — listening, transcribing,
speaking, interrupted.

They are not merged because they change for unrelated reasons. A caller can
interrupt the agent (a `VoiceState` change) without altering where they are in
the booking (`ConversationState`). Fusing them would put audio concerns inside
the engine, which is the thing Milestone 3 is specifically not allowed to do.

## Barge-in

Full-duplex capture is what makes interruption possible: the microphone is never
gated on playback, so the VAD sees caller speech *while* the agent is talking.

When speech is detected during `SPEAKING` or `THINKING`:

1. `output.stop_immediately()` — the speaker ring is cleared first, because
   every millisecond after this is audible to the caller.
2. The turn token is cancelled, and the in-flight turn is unwound.
3. The frame that triggered the barge-in seeds the new utterance, so the
   caller's first syllable is not clipped.

Measured playback-stop latency through the real output device: **0 ms**
(median, p95 and max), against a 200 ms target. Clearing the ring is a
synchronous buffer drop; the PortAudio callback finds it empty on its next tick.

### Turn tokens

Every stage carries the token of the turn it belongs to, and re-checks it
immediately before queuing audio. A turn that loses the race is discarded rather
than spoken over its successor — without this, an answer to a question the
caller has already abandoned arrives on top of the new one.

One subtlety, learned the hard way: **the engine call is shielded from
cancellation.** The engine touches the database, and aborting mid-statement
leaves the `AsyncSession` unusable, which deadlocked the following turn. A
superseded turn therefore lets the engine finish and discards the *result*. This
is safe because no booking is ever committed without a spoken confirmation.

## Endpointing

`app/voice/endpointing.py` decides when the caller has finished. Time is counted
in frames, not wall clock, so the logic is exact and reproducible in tests;
`monotonic` is used only to report latency.

| Setting | Default | Why |
|---|---|---|
| `end_silence_ms` | 500 | measured sweep below |
| `initial_silence_ms` | 6000 | how long to wait for a caller who says nothing |
| `min_speech_ms` | 200 | below this it is a cough, not a turn |
| `max_utterance_ms` | 20000 | bounds a stuck line |

Measured sweep (Silero VAD, synthetic clinic utterances):

| `end_silence_ms` | median lag | utterances split |
|---|---|---|
| 300 | 344 ms | 0 |
| 500 | 544 ms | 0 |
| 700 | 744 ms | 0 |

No threshold split an utterance, so the sweep alone would argue for 300 ms.
**500 ms is kept deliberately.** The test utterances are continuous synthetic
speech; real callers pause mid-sentence — "I'd like to see… um… Doctor Patel" —
and in a clinic, being cut off halfway through a symptom is a worse failure than
200 ms of extra latency. The setting is `VOICE_END_SILENCE_MS` for anyone whose
measurements say otherwise.

## Measured latency

Apple M2, 8 GB, macOS 15.5, Python 3.13.3. faster-whisper `base.en` (int8, CPU),
Piper `en_GB-alan-medium`, Silero VAD v5, Ollama `qwen2.5:1.5b-instruct`.
Reproduce with `uv run python scripts/benchmark_voice.py --sweep-endpointing`.

| Stage | Median | p95 | Target | |
|---|---|---|---|---|
| VAD speech-start detection | 100 ms | 120 ms | < 150 ms | ✅ |
| Endpoint after speech | 544 ms | 576 ms | ≈ `end_silence_ms` | ✅ |
| STT (warm, per utterance) | 449 ms | 1622 ms | < 500 ms median | ✅ |
| TTS time to first audio | 135 ms | 278 ms | < 300 ms | ✅ |
| Barge-in playback stop | 0 ms | 0 ms | < 200 ms | ✅ |
| **Speech end → first agent audio** | **1135 ms** | 1197 ms | < 700 ms | ❌ |

STT real-time factor is 0.329 — transcription costs roughly a third of the
audio's duration.

Measure on an otherwise idle machine. A run taken with a browser and an editor
competing for the same cores produced a real-time factor of 1.180 and a p95 of
4465 ms — a 3× spread that is contention, not code.

### Short utterances and the temperature-fallback trap

faster-whisper re-decodes at rising temperatures when a result trips its
compression or log-probability thresholds. Short utterances trip them
constantly, and a clinic call is *full* of short utterances — a surname, "yes",
an appointment type.

| Utterance | Default (fallback on) | `temperature=0.0` | Transcript |
|---|---|---|---|
| "Hughes" | 1602 ms | 386 ms | identical |
| "Nguyen" | 1671 ms | 363 ms | identical |
| "yes" | 363 ms | 385 ms | identical |
| "Thursday" | 378 ms | 392 ms | identical |

The retries cost up to 1.3 s and changed nothing. The provider pins
`temperature=0.0`.

It is the *tail* this fixes, not the median. Comparing two runs on a
comparably-loaded machine, the median moved little while the p95 collapsed,
because one retrying turn no longer dominated it:

| | Fallback on | `temperature=0.0` |
|---|---|---|
| STT median | 354 ms | 449 ms |
| STT p95 | 7437 ms | 1622 ms |
| End-to-end p95 | 2343 ms | 1197 ms |

The trade is that a degenerate decode is no longer retried, which is acceptable
here — our own VAD has already segmented the audio and `MAX_UTTERANCE_SECONDS`
bounds its length, so the rambling input the fallback protects against cannot
reach it. `tests/voice_live/test_live_speech.py` guards the regression.

### The 700 ms target is not met, and cannot be with this stack

This is arithmetic, not tuning. Before the agent can say anything at all:

    endpointing   561 ms   (500 ms silence + 60 ms VAD hangover)
    transcription 422 ms
    ────────────────────
                  983 ms   before the first word is even known

An acknowledgement cannot be brought forward past that, because until
transcription returns we do not know whether the caller said "yes" or coughed.
The remaining ~150 ms is the engine's floor before the acknowledgement fires.

The honest levers, none free:

* **`end_silence_ms` 500 → 300** saves ~200 ms and risks cutting callers off
  mid-sentence. Rejected for a clinic; available as configuration.
* **`tiny.en` instead of `base.en`** saves ~240 ms and is *unsafe* — see below.
* **A streaming STT decoder** (transcribing as the caller speaks rather than
  after) is the only change that removes the 444 ms rather than trading against
  it. faster-whisper has no streaming decoder; this is the right Milestone 4+
  investigation.
* **A phone line changes the arithmetic anyway** — 8 kHz codecs and network
  jitter will add to this, not subtract.

What the caller actually experiences is softened by the acknowledgement: on a
slow turn they hear "One moment while I check" (served from cache, ~0 ms
synthesis) rather than silence, and the LLM's ~1.3 s runs underneath it.

## Acknowledgement while thinking

`VOICE_ACKNOWLEDGEMENT_AFTER_MS` (900 ms) is a budget for **the caller's whole
silence**, measured from the moment they stop speaking — not a timeout handed to
the engine. Endpointing and transcription are spent from it first.

An earlier version started the clock when the engine was called, which billed
the caller twice for the same silence and measured 1337 ms to first audio.
Anchoring it to speech end brought that to 1135 ms without making filler any
more likely on a fast turn.

`MIN_ACKNOWLEDGEMENT_WAIT_MS` (150 ms) is the floor once the rest of the budget
is spent. It sits above the deterministic path (~30 ms), which therefore stays
silent — saying "one moment" before every "yes" would sound ridiculous — and far
below the local LLM (~1.3 s), which therefore gets covered.

The acknowledgement is in the TTS phrase cache, so it costs no synthesis time:
measured 130 ms cold, 0.01 ms warm.

## The TTS cache is an allowlist, not an LRU of everything

`app/speech/tts/cache.py` caches only phrases drawn from
`app.conversation.responses`, and excludes any string containing `{` — because a
template becomes patient-specific the moment it is filled. "Anything else?" is
cached; "Booked. Dr Sarah Patel, tomorrow at 12 pm." is not, and
`is_cacheable()` returns `False` for it.

This is a privacy control as much as a latency one: a general-purpose cache of
recent utterances would be a store of patient information sitting in memory
across callers.

## Engine selection, by measurement

### Speech to text

`scripts/benchmark_stt.py` — three phrase sets, scored on word error rate, on
whether safety keywords survive, and on whether the **production** extractors
still recover dates of birth and phone numbers from the transcript.

| Model | Safety WER | Safety keywords | Booking WER | Parsers | Median |
|---|---|---|---|---|---|
| `tiny.en` | 1.2% | **9/10** | 7.0% | **2/3** | 187 ms |
| `base.en` | 1.4% | 10/10 | 0.0% | 3/3 | 427 ms |
| `small.en` | 0.0% | 10/10 | 1.4% | 3/3 | 1322 ms |

**`base.en` is the recommendation.** The latency table alone would pick
`tiny.en`; the safety columns disqualify it:

* it heard *"my face has gone **down** on one side"* for "gone **numb**" —
  losing a stroke symptom before the SafetyEngine ever sees it;
* it read a date of birth as **1980** where the caller said **1982** — silently
  verifying the wrong patient.

`small.en` costs 3× the latency for no gain worth having (it is *worse* on the
booking set than `base.en`).

### Text to speech

`scripts/benchmark_tts.py` speaks each reply and transcribes it back. The round
trip is a proxy, not a listening test — but it catches mangled times, dates and
addresses, which is what a clinic reply is mostly made of.

| Voice | Round-trip WER | Exact | First audio (detail) | Genuine mispronunciations |
|---|---|---|---|---|
| `en_GB-alan-medium` | 26.0% | 1/8 | 150 ms | **none** |
| `en_US-lessac-medium` | 20.3% | 2/8 | 114 ms | none (US accent) |
| `en_GB-alba-medium` | 33.1% | 1/8 | 124 ms | 2 |
| `en_GB-jenny_dioco-medium` | 33.8% | 0/8 | 134 ms | — |

Most of that WER is formatting noise that penalises every voice equally
("11:45 am" transcribed back as "11.45am"). The differentiator is genuine
mispronunciation, found by inspection:

* `alba` is a **Scottish** voice. It rendered "Dr Patel is **away**" as "is **a
  wee**", and "We're at 42 Riverside Drive, Parramatta" as "**RAT-40,
  2-RIVICIDE DRIVE, PARAMATTER**" — an address a caller could not write down.
* `alan` said both correctly, and is British English, the closest available to
  Australian English. Piper ships no en_AU voice.

`en_US-lessac-medium` scores slightly better and is slightly faster, but a US
accent on an Australian clinic line is its own kind of wrong. **`alan` is the
recommendation**; `lessac` is the fallback if a listening test disagrees.

### Voice activity detection

Silero VAD v5 (ONNX, ~2 MB) over WebRTC VAD: it holds up under background noise
where an energy threshold does not, and costs ~1 ms per frame.

`EnergyVAD` (`app/speech/vad/energy.py`) is a dependency-free fallback with
hysteresis, used when the Silero model is absent and as a test double.

**Silero v5 requires a 64-sample context prepended to each 512-sample window.**
Without it the model returns ~0.001 on obvious speech and no turn ever ends.
Measured: mean speech probability **0.936** with the context, **0.008** without.

## Privacy

Consistent with `docs/privacy/data-inventory.md`, and stricter for audio:

* **Nothing is recorded.** Audio exists as in-memory PCM for the length of one
  utterance and is discarded. No `.wav` files, no temporary speech files, no STT
  debug dumps.
* **Transcripts are not stored** unless `RETAIN_TRANSCRIPTS=true`, unchanged
  from Milestone 1.
* **No audio or content in logs.** Voice logs carry timings, provider names,
  frame counts and character *counts* — never PCM, base64, transcripts, caller
  speech, or patient-specific reply text. `faster_whisper`'s own logger is
  turned down to `WARNING` at model load because it writes a per-utterance line
  through the stdlib root logger, bypassing our redaction-aware handlers.
* **The STT vocabulary bias contains clinic terms only** — practitioner and
  appointment-type names. Patient names are never put in the decoder prompt:
  it is sent on every utterance, so it would leak across callers.
* `scripts/voice_chat.py --show-transcript` prints to *your terminal only*, for
  development against the synthetic clinic. It writes nothing to disk.

## Security

Spoken input is untrusted input. The transcript goes through the same path as a
typed message, so a spoken prompt injection has the same reach as a typed one —
none. This is covered by parametrized tests that assert the appointment and
patient row counts are unchanged after the attempt.

`docs/security/threat-model.md` carries the audio-specific entries: malformed
and oversized audio, native decoder exposure, resource exhaustion under
concurrent sessions, and the residual misrecognition risk that no amount of STT
tuning removes.

## What is deliberately absent

Milestone 4 territory, designed for but not built: no SIP, RTP, Asterisk, ARI,
AudioSocket, DTMF, transfer, hold or queue. The seam is
`app/voice/transport.py` — `AudioInput` and `AudioOutput` Protocols. A telephony
session is built by passing a different pair to `build_voice_session()`; nothing
in `VoiceSession` knows a microphone from a phone line.

## Running it

```bash
uv run python scripts/voice_chat.py                 # talk to it
uv run python scripts/voice_chat.py --devices       # list audio devices
uv run python scripts/voice_chat.py --show-state    # watch the state machine
uv run python scripts/voice_chat.py --push-to-talk  # bypass VAD (debug)

uv run python scripts/benchmark_voice.py --sweep-endpointing
uv run python scripts/benchmark_stt.py --models tiny.en,base.en,small.en
uv run python scripts/benchmark_tts.py --show
```

**Use headphones.** The pipeline listens while it speaks — that is what makes
barge-in work — so on open speakers the agent can hear its own voice and
interrupt itself.

Push-to-talk exists to separate layers when something feels wrong: if it
transcribes correctly but normal mode does not, the problem is VAD or
endpointing; if it transcribes badly too, the problem is STT or the microphone.
