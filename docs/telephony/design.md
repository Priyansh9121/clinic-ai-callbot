# Telephony design (Milestone 4 — not implemented)

Nothing in this document exists in code yet. It is recorded now because two of
its requirements — the health check contract and the failure fallback — already
constrain decisions made in Milestone 1.

**No SIP credentials appear anywhere in this repository.** Configuration uses
`CHANGE_ME` placeholders in `.env.example`. Real trunk credentials come from the
provider and belong in the deployment's secret store.

## Intended call path

```
PSTN caller
    │
    ▼
SIP provider (Australian trunk — commercial, but only for connectivity;
    │          the AI stack stays local)
    ▼
Asterisk (PJSIP)
    │  ARI / external media over WebSocket
    ▼
audio gateway  ──► VAD ──► streaming STT ──► conversation engine
    ▲                                              │
    └──────────── streaming TTS ◄──────────────────┘
```

The conversation engine is unchanged: it already takes text and returns text.
The gateway is the only new component that touches audio.

## Process isolation

Asterisk runs as its own process, separate from the AI stack, and holds no
heavy AI workload. This is not tidiness — it is the requirement that **an AI
crash must not take telephony down.** If the gateway dies, Asterisk is still
answering calls and can route them to reception.

## Failure fallback — the critical requirement

**AI failure must never make the clinic unreachable.**

```
incoming call
    │
    ├─ GET /health  (short timeout, ~500 ms)
    │       │
    │       ├─ 200 and status == "ok"  ─────► AI answers
    │       │
    │       └─ anything else, or timeout ───► reception
    │
    └─ reception unavailable ────────────────► approved clinic voicemail
```

`GET /health` already reports per-component status and returns 503 when the
database or session store is down, precisely so the dialplan can make this
decision. `GET /health/live` exists for a cheaper process-liveness probe.

The dialplan must treat *any* uncertainty as failure: a slow health check is a
failure, a malformed response is a failure. Defaulting to reception is always
safe; defaulting to the AI is not.

## Human transfer

The engine already produces `requires_human` with a `transfer_reason` on every
turn. The gateway maps that to a blind or attended transfer to the reception
queue. If the queue is unavailable, the caller goes to voicemail — never back
into the AI, which would trap a caller who has already asked for a person.

## Audio format

`SAMPLE_RATE_HZ = 16000`, 16-bit mono, 20 ms frames — declared in
`app/speech/providers.py`. Chosen to match what the STT and TTS providers want
so that no resampling is needed in the hot path.

## Barge-in

The caller must be able to interrupt.

```
TTS playing
    │
    ▼ VAD detects caller speech (raw audio, tens of ms — no STT involved)
    │
    ▼ cancel playback immediately, mid-phrase
    │
    ▼ capture, transcribe, respond
```

Two design consequences already baked into the interfaces: VAD is independent of
STT, so interruption is detected long before any transcript exists; and
`synthesize_stream` must stop promptly when the consumer stops iterating, which
is what makes cancellation feel immediate rather than "after this sentence".

## Latency budget

See `docs/architecture/latency.md`. Target is roughly 800 ms from the caller
finishing speaking to the first audio back. The metric names are already
reserved in `app/core/metrics.py` (`STT_LATENCY`, `LLM_LATENCY`,
`TTS_FIRST_AUDIO`, `BARGE_IN`) so instrumentation lands without renaming
anything.

## Open questions for Milestone 4

- Which Australian SIP provider, and what the failover arrangement is.
- Whether reception transfer is blind or attended.
- Call-recording policy — currently nothing is recorded, and enabling it needs a
  privacy decision and, in most Australian jurisdictions, caller notification.
- Concurrent call capacity, which determines whether STT and TTS need to run on
  separate hosts.
