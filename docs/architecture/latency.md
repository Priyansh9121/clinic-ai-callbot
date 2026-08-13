# Latency budget

A caller notices roughly 700–800 ms of dead air on a phone line. Beyond about a
second they start to repeat themselves, which costs a whole extra turn.

## Target budget (Milestone 3)

| Stage | Target | Metric |
|---|---|---|
| caller stops → VAD endpoint | 50 ms | — |
| endpoint → final transcript | 300 ms | `speech.stt_latency` |
| transcript → intent | 100 ms | `llm.analyse_latency` |
| intent → tool result | 150 ms | `appointments.availability_search_latency` |
| tool result → first TTS audio | 200 ms | `speech.tts_first_audio_latency` |
| **total to first audio** | **~800 ms** | `conversation.turn_latency` |

Measured with the deterministic analyser only, text-only, on the development
machine: **1–12 ms** per conversation turn end to end, including the database
round trips. The deterministic analyser itself costs microseconds.

## Measured with the local LLM (Milestone 2)

Apple M2, 8 GB, Ollama 0.32.9, `qwen2.5:1.5b-instruct`:

| | Latency |
|---|---|
| Deterministic turn (model skipped by hybrid routing) | 1–30 ms |
| Model-consulted turn, warm | ~1.2 s median, ~1.4 s max observed live |
| Model-consulted turn, benchmark p95 | 2.3 s |
| Cold first call (model not resident) | 1.2–7.2 s depending on model |

**This does not fit the ~800 ms budget above**, and that is the honest headline
finding of Milestone 2. Three things make it workable today and one problem
remains:

1. **Hybrid routing** skips the model whenever the deterministic analyser is
   already confident, so a typical booking conversation makes about five model
   calls rather than one per turn. The other turns stay in single-digit
   milliseconds.
2. **`keep_alive`** stops Ollama unloading the model between calls. Without it
   an idle line pays a multi-second reload on the next caller — which on this
   hardware exceeded the request timeout, failed three turns in a row, and
   opened the circuit breaker. That was a real observed failure, not a
   theoretical one.
3. **The timeout is a hard cap.** `LLM_TIMEOUT_SECONDS` cancels a slow
   generation and the turn falls back, so a slow model costs a little accuracy
   rather than dead air.

**Remaining problem for Milestone 3:** a model-consulted turn will be audible
as a pause once speech is attached. Options, in rough order of preference:
speak a pre-synthesised acknowledgement ("One moment while I check.") while the
model runs — the phrases are already in `responses.PREGENERATED` for exactly
this; run the model on a faster host; or accept the deterministic analyser for
voice and reserve the model for text channels.

## What not to build

```
record whole turn → transcribe → wait → generate whole reply
                 → wait → synthesise whole WAV → play
```

Each arrow is a full stop. The stages serialise and the caller hears silence
through all of it.

## What to build

```
stream audio ─┬─► VAD ──────────────► endpoint detection
              └─► partial transcripts ──► early intent
                                            │
                          "One moment while I check." ──► plays immediately
                                            │
                          availability search runs concurrently
                                            │
                                     streaming TTS ──► first chunk ASAP
```

Three techniques, in order of value:

1. **Acknowledge while working.** `responses.CHECKING` plays while the
   availability query runs. The caller hears a natural pause instead of dead
   air, and the query is effectively free.
2. **Pre-synthesise fixed phrases.** `responses.PREGENERATED` lists the phrases
   worth caching — greeting, "one moment", "anything else?", the standard
   questions, the transfer message. They play with zero synthesis latency.
   This is why every spoken string lives in the response catalogue rather than
   inline.
3. **Start before the caller finishes.** Partial transcripts allow intent
   classification to begin mid-utterance.

## Database

The availability search is the only query in the conversational hot path.
Supported by `ix_availability_slots_search` on
`(appointment_type_id, status, start_time, practitioner_id)`.

Booking is not in the hot path in the same way — the caller expects a brief
pause when confirming — but it holds a row lock, so it must stay short. Nothing
slow (no model call, no network) may ever run inside that transaction.

## Instrumentation

`app/core/metrics.py` records counters and latency histograms in process,
exposed at `GET /health/metrics`. Buckets are 25 ms to 6 s, chosen around the
conversational budget rather than a generic web-request one.

Names are constants on `M` so the dashboard and the tests cannot drift from the
call sites. The Milestone 3 and 4 names are already reserved there, so adding
instrumentation later does not mean renaming anything.

Label values are restricted to closed sets (intent, outcome, reason code).
Never a patient identifier, a phone number or free text.
