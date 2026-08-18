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


## Measured over a real telephone call

Milestone 4, six completed turns, inbound SIP to extension 700 against Asterisk
20.6.0. Read from `telephony.turn_latency`.

| Stage | Median |
|---|---|
| Endpointing | 466 ms |
| STT | 466 ms |
| Engine — deterministic | 19 ms |
| Engine — LLM | 2145–3826 ms (behind the acknowledgement) |
| TTS to first audio | 176 ms |
| Transport to the wire | 1.5 ms |
| **Speech end → first byte on the wire** | **1128 ms** (1065–1155) |

**The telephone did not move the number.** 1135 ms measured locally over a
microphone was a floor and the phone came in at 1128 ms. The prediction that
soxr batching would add ~100–150 ms was wrong by two orders of magnitude: the
whole transport — resampler, outbound queue and pacer — costs 1.5 ms.

So the budget gap is where it always was, and no telephony work will close it:
endpointing (466 ms) plus transcription (466 ms) is 932 ms before the first
word is known. The engine is bimodal and the acknowledgement already covers the
LLM's 2–4 s, which is why the median does not move with it.

What this rules out as the next optimisation: the transport, the pacer, the
resampler, and the SIP leg. What it leaves: endpointing thresholds, and a
streaming STT decoder. Measure before changing either.

`to_wire_ms` is the honest end of this measurement. Asterisk's own buffering,
the network and the handset's jitter buffer are past the socket and were not
measured; the caller's perceived latency is this plus that.


## Step 2 (streaming STT): closed as a negative result

Four candidates measured, four distinct reasons, none adopted. **Speech-end to
wire stays at ~1330 ms.**

| candidate | licence | why not |
|---|---|---|
| Parakeet TDT 0.6b v3 | CC-BY-4.0 ✓ | encoder is full-context — no cache inputs, so "streaming" is a growing buffer. ~70 ms gain for 876 MB RSS |
| Whisper large-v3-turbo | MIT ✓ | best on names by far (Nguyen, Siobhan, Ranpura all correct) but 13x slower — 13.3 s per utterance — and +999 MB |
| NeMo FastConformer 80 ms | **NGC Terms** ✗ | cache-aware and fast, but NGC does not state a permissive licence. The sibling `_multi` declares CC-BY-4.0; this one does not, and that difference is the point |
| Streaming Zipformer | Apache-2.0 ✓ | cache-aware, 69 MB, **21.6 ms** post-speech-end — and deaf on G.711. "can't breathe" → "THREE", two utterances → empty |

Current breakdown, unchanged:

| stage | median |
|---|---|
| Endpointing | 750 ms |
| STT (base.en) | 466 ms |
| TTS to first audio | 176 ms |
| Transport to wire | 1.5 ms |
| **Speech end → first byte on wire** | **~1330 ms** |

The 750 ms endpointing figure is a **deliberate stopgap**, not a tuned value.
It trades ~200 ms of latency for not interrupting callers mid-identity: at
500 ms the hard-case harness split a spelled surname five times and cut a date
of birth in half, on live calls. See the endpointing section in
voice-pipeline.md.

**What would actually close the gap**, in order of likelihood:

1. **A streaming model trained with telephony augmentation.** Every rejected
   candidate lacked exactly this property. The Zipformer proves the shape works
   — 21.6 ms post-speech-end, 69 MB, Apache-2.0 — it simply cannot hear 8 kHz
   G.711. A model with the same architecture and narrowband training data would
   close most of the 1330 ms on its own.
2. **Hardware with headroom for large-v3-turbo.** It is already the best
   measured option for the name-recognition problem, and it is multilingual.
   8 GB shared with Ollama, Piper and Whisper is the constraint, not the model.

Both are procurement decisions rather than engineering ones, which is why this
step closes here rather than continuing.
