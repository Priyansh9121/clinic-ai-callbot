# Telephony

Milestone 4 connects the existing voice agent to real telephone calls. It adds
a transport and nothing else: the same `VoiceSession`, VAD, endpointing, STT,
`ConversationEngine` and TTS run unchanged whether the audio comes from a
microphone or a SIP call.

```
PSTN / SIP caller
      │
      ▼
SIP provider (Phase B — not yet connected)
      │
      ▼
Asterisk + PJSIP          ← owns all SIP: REGISTER, INVITE, SDP, RTP
      │
      ├── ARI (WebSocket + REST) ──────► call control: answer, hangup, transfer
      │
      └── AudioSocket (TCP) ───────────► media: 20 ms frames, both directions
                                              │
                                              ▼
                              AudioSocketInput / AudioSocketOutput
                                              │
                                              ▼
                                  existing VoiceSession
                                     VAD → endpointing → STT
                                              │
                                              ▼
                                  existing ConversationEngine
                                              │
                                              ▼
                                        existing TTS
                                              │
                                              ▼
                                     AudioSocketOutput → Asterisk → caller
```

**Python is not a SIP stack.** There is no REGISTER, INVITE, ACK, BYE, SDP or
digest authentication anywhere in this codebase. Asterisk does all of it, and
the application speaks to Asterisk's own control and media interfaces.

## Three state machines

Milestone 3 had two. There are now three, and conflating any two is how a
telephony bug becomes a clinic bug.

| Domain | Type | Knows about | Examples |
|---|---|---|---|
| Clinic workflow | `ConversationState` | appointments, patients | `VERIFY_PATIENT`, `CONFIRM_BOOKING` |
| Audio mechanics | `VoiceState` | frames, turns | `LISTENING`, `SPEAKING`, `INTERRUPTED` |
| The call itself | `CallState` | channels, media | `RINGING`, `ACTIVE`, `TRANSFERRING` |

They advance independently and legitimately disagree — a caller can be in
`CONFIRM_BOOKING` while `SPEAKING` on an `ACTIVE` call. Control flows one way:
telephony reacts to what the conversation decided (`requires_human` → transfer,
`ended` → hang up). The conversation layer never learns that SIP exists.

`CallState` transitions are **enforced**, not documented. The failure mode of an
unenforced call state machine is a call that is still "active" after the caller
has hung up, holding a database session and a Whisper worker.

## Choosing the integration mechanism

Four Asterisk mechanisms were evaluated against what this pipeline needs:
continuous bidirectional streaming, immediate playback cancellation, call
lifecycle events, and no WAV files.

| | AudioSocket | ARI + External Media (RTP) | Dialplan `AudioSocket()` only | AGI / EAGI |
|---|---|---|---|---|
| Bidirectional streaming | yes, TCP | yes, UDP/RTP | yes | inbound only, blocking |
| Transport | TCP — ordered, no jitter buffer needed | UDP — must handle reorder, loss, jitter | TCP | pipe |
| Implementation cost | ~200 lines of framing | RTP: sequence numbers, timestamps, SSRC, jitter buffer | trivial | low |
| Immediate cancellation | yes — stop writing | yes | yes | no |
| Call lifecycle | none | full (ARI) | none | limited |
| Transfer | no | yes | no | via dialplan only |
| Codec handling | Asterisk transcodes to slin | payload is the negotiated codec | slin | — |
| Production suitability | good | good | demo only | poor for realtime |

**Selected: ARI for control + AudioSocket for media.**

* AudioSocket over RTP because TCP removes an entire class of problem. RTP
  would mean implementing sequence numbers, reordering, loss concealment and a
  jitter buffer in Python for no benefit at these call volumes — and a jitter
  buffer adds exactly the latency this pipeline is short of.
* ARI alongside it because AudioSocket has no call control, and **transfer to a
  human is a requirement**. A dialplan-only AudioSocket setup can answer and
  converse but cannot escalate, which is the one thing a clinic bot must always
  be able to do.
* Asterisk transcodes G.711 to signed linear before AudioSocket sees it, so the
  media path carries `slin` and the G.711 codecs in `app/speech/audio.py` are
  used for the RTP path and the telephone-band benchmark rather than here.

The cost of this choice is setup complexity: a bridge, an originated media
channel, and a UUID to correlate them. That sequence is in
`app/telephony/service.py`.

## Call setup

```
1. Dialplan routes the AI extension to Stasis(clinic-ai)
2. ARI: StasisStart  →  mint an opaque call_id and a media UUID, answer
3. ARI: create a mixing bridge; add the caller's channel
4. ARI: originate AudioSocket/<host>:<port>/<media-uuid> into the same bridge
5. Asterisk connects to the media server; its first frame is the media UUID
6. The socket is matched to that call by UUID; CallSession runs VoiceSession
```

**The media UUID travels in the Stasis arguments** (`appArgs=media,<uuid>`),
not only in the endpoint string. Correlating instead on the channel id returned
by `POST /channels` is a race: that HTTP response and the ARI WebSocket are
independent connections, and Asterisk can deliver the channel's `StasisStart`
*first*. The correlation table would still be empty, the media channel would
never join the bridge, and the call would connect to silence — intermittently.
Carrying the UUID in the event makes the ordering irrelevant.
`tests/telephony/test_ari_call_setup.py` drives both orderings.

Step 5 is the security-relevant one. Without UUID correlation, a second
concurrent call could attach to the first call's conversation — the crossover
failure in the threat model. The media server refuses any connection it cannot
attribute.

Sending the caller back to the dialplan to run `AudioSocket()` would be shorter
and is a dead end: the channel leaves Stasis, and with it goes any ability to
transfer.

## Audio format

| | |
|---|---|
| SIP leg | G.711 μ-law or A-law, 8 kHz (`ulaw`/`alaw`) |
| AudioSocket wire | signed linear 16-bit, 8 kHz (`slin`) by default |
| Internal canonical | 16 kHz, mono, s16le, 20 ms frames — unchanged since Milestone 1 |
| Frame on the wire | 320 bytes (`slin`) / 640 (`slin16`) / 160 (G.711) |

Conversion happens at the transport edge and **only** in
`app/speech/audio.py`, which is where resampling already lived. G.711 μ-law and
A-law codecs were added there rather than in the telephony package for the same
reason: a second μ-law decoder growing inside an adapter is how two subtly
different decoders end up in one codebase.

Setting the channel format to `slin16` removes resampling from the call path
entirely. The information is still 8 kHz-band — Asterisk upsamples — but the
conversion cost and its filter delay disappear.

### Resampling is stateful, and must be flushed

Measured, because both facts caused bugs:

* **Per-frame resampling is wrong.** Resampling each 20 ms frame independently
  puts a filter discontinuity at every frame boundary — 50 a second. Spectral
  error against a whole-buffer reference: **0.327 per-frame, 0.000 streaming.**
  The streaming resampler costs 0.002 ms per frame.
* **The streaming resampler holds a tail.** At `HQ` it keeps ~100 ms in its
  filter. Without an explicit flush at the end of an utterance, that tail stays
  there until the *next* reply pushes it out — so every sentence loses its
  ending and the following one opens with a fragment of the last. Measured:
  **500 ms of audio arriving as 400 ms** until `flush()` was added.
* **Barge-in must reset it.** An interrupted utterance leaves up to 100 ms in
  the filter, which would otherwise prepend to the next reply — stale audio
  that passes every check above it, because by then the bytes look like the new
  turn's.

## Pacing — a barge-in requirement, not an audio nicety

A sound card consumes audio at exactly real time. A TCP socket accepts it as
fast as Python can write. Writing a 4-second reply into the socket in 3 ms
leaves four seconds of speech buffered inside Asterisk, and
`stop_immediately()` **cannot recall audio the kernel has already sent**.

So outbound audio is metered one 20 ms frame per 20 ms. That bounds the
un-recallable audio to roughly one frame.

Measured over a real loopback socket: 500 ms of audio transmits in 500 ms
(±35 ms), every frame exactly 320 bytes. After 100 ms of a 2-second reply,
fewer than 300 ms had been transmitted — so a caller interrupting hears the
agent stop, rather than listening to the rest of a sentence that was already
inside the PBX.

Conversion happens when audio is **queued**, not when it is sent, because
soxr's streaming resampler batches: at `HQ` it returns nothing for five frames
and then 104 ms in one blob. Converting inside the pacer would emit one
oversized frame every ~100 ms — bursts, the exact thing pacing prevents.

## Barge-in

Unchanged from Milestone 3 above the transport. The caller's audio is never
gated on playback, so VAD sees speech while the agent is talking; the transport
discards queued frames and resets the resampler.

**Measured latency has two parts, and only one of them is ours:**

| | Measured |
|---|---|
| Caller speech reaches the adapter → outbound transmission stops | < 1 ms (queue drop) |
| Audio already transmitted that the caller will still hear | ≤ ~40 ms (one to two frames) |
| Caller starts speaking → caller stops hearing the agent | **not measured** — needs a real PBX and a human ear |

The last row is the one a caller experiences, and it includes Asterisk's own
buffering, the RTP path and the handset. It cannot be measured without a live
call, and **is not claimed here**.

## Transfer

The clinic engine decides *when* to escalate; telephony decides *how*. The two
are connected by a closed table, not by text.

```
ConversationEngine: requires_human + TransferReason
        ↓  deterministic table (app/telephony/destinations.py)
TransferDestination.RECEPTION            ← an enum member, not a string
        ↓  DestinationRegistry.resolve()
extension "100" in context "clinic-transfer"   ← from configuration
        ↓  ARI continueInDialplan
Asterisk
```

Four independent barriers stop caller speech becoming a dial string:

1. `CallController.transfer()` takes a `TransferTarget`, which can only be
   produced by `DestinationRegistry.resolve()` from a `TransferDestination`
   enum member. There is no string-accepting signature to abuse.
2. `TransferDestination` has two members. Anything else raises
   `UnknownDestination`.
3. Extensions come from configuration and are validated at load against
   `^[A-Za-z0-9_-]{1,20}$`, so a misconfigured `.env` cannot smuggle in
   `9,0011447700900000` or a SIP URI either.
4. The dialplan context `[clinic-transfer]` contains only the approved
   extensions and no outbound trunk. Even a fully compromised application has
   nowhere to send a call.

`TransferReason.SAFETY` maps to **reception**, not to emergency services. The
clinic system advises the caller to ring 000 themselves, in wording owned by the
SafetyEngine. Automatic emergency dialling is out of scope and would need legal
review before it could be considered.

A successful transfer takes the channel out of Stasis, so Asterisk emits
`StasisEnd` immediately — which is indistinguishable from a caller hanging up
unless the call is marked finished *before* teardown is awaited. It is: the
caller did not hang up, they were handed to a human, and counting it as a
disconnect would misreport every transfer.

**Transfer failure** returns the caller to us with an approved apology from
`app/conversation/responses.py`, rather than dropping them. If reception does
not answer, the dialplan sends them to voicemail — never back into the AI, which
would trap someone who has already asked for a person.

## One call, one session

Every call gets its own `call_id`, `VoiceSession`, `ConversationSession`,
database session, `ConversationEngine`, media channel, VAD state, endpointer,
turn tracker and cancellation state. Shared between calls: the speech models
and the LLM provider, both stateless per call and internally
concurrency-bounded.

**Concurrent calls need separate `ConversationEngine` instances.** They hold an
`AsyncSession`, and sharing one serialises the calls — measured directly: with a
shared engine the second caller received no audio at all. Production builds one
per call from `session_scope()`; the test suite pins this.

Nothing is keyed by phone number. Two callers from the same office switchboard
are two calls.

## Capacity

`TELEPHONY_MAX_ACTIVE_CALLS` (default 2) bounds concurrent calls. The limit is
local inference, not the transport: the transport handles far more than the
machine can transcribe.

At capacity the behaviour is configuration, not a code default, because "busy"
and "send them to a human" are clinic decisions:

* `reject` (default) — do not answer. The dialplan's existing health-check
  failover routes the caller to reception, which is the arrangement documented
  since Milestone 1.
* `transfer_reception` — answer and transfer immediately.

## Latency

Measured for the media transport in isolation, on loopback:

| | Measured |
|---|---|
| Outbound pacing accuracy | 500 ms transmitted in 500 ms (±35 ms) |
| `play()` return time | < 1 ms — queues, never blocks the turn |
| `stop_immediately()` | < 1 ms |
| Inbound wire → canonical frame | one 20 ms frame, no added buffering |
| Resampler filter delay (`HQ`, 8↔16 kHz) | ~66 ms each way; `QQ` is ~0.25 ms |

**End-to-end call latency is not measured**, because it requires a live PBX. The
Milestone 3 figure of 1135 ms from speech end to first agent audio is the
floor; a phone call adds the SIP leg, Asterisk's buffering, and the resampler's
filter delay in both directions. Expect worse, and measure it on the real
system before quoting a number.

## Telephone-band speech recognition

The clean-audio accuracy from Milestone 3 does not automatically survive a
phone line. `scripts/benchmark_telephony_stt.py` puts the *same* phrases through
the real degradation: 16 kHz → 8 kHz → G.711 → 8 kHz → 16 kHz.

Clean and telephone results within a run are a **paired** comparison — the
telephone audio is derived from the same render. Absolute values move a few
points between runs because Piper's synthesis is stochastic.

### Codec path alone (no line noise)

| Set | Clean WER | Phone WER | Δ | Safety keywords |
|---|---|---|---|---|
| safety | 0.0% | 0.0% | +0.0 | **10/10** |
| booking | 1.4% | 1.4% | +0.0 | 4/4 |
| identity | 57.9% | 49.8% | −8.2 | 5/5 |
| australian | 16.5% | 15.8% | −0.6 | 6/7 |

Band-limiting and G.711 alone cost essentially nothing on synthetic speech.
**This flatters the codec** — it is a clean, well-enunciated, noise-free source.

### With line noise at −30 dBFS

| Set | Clean WER | Phone WER | Δ | Keywords | Parsers |
|---|---|---|---|---|---|
| safety | 0.0% | 2.0% | +2.0 | **10/10** | — |
| booking | 1.4% | 1.4% | +0.0 | 4/4 | — |
| identity | 52.8% | **69.8%** | **+17.0** | 4/5 | **1/3** |
| australian | 12.7% | **38.1%** | **+25.4** | 4/7 | — |

Actual failures on the noisy line:

```
"Siobhan O'Brien"           -> "you will know Brian."
"half past two"             -> "Hot, Hot, Hot too."
"can I see Doctor Nguyen"   -> "can I see Dr. New Year?"
"I'll come in on the weekend" -> "I'll come in only we can."
```

**Two conclusions, and they point in opposite directions.**

*Safety phrases hold up.* 10/10 keywords survived every condition tested. They
are long, redundant and lexically distinctive — "chest pain", "can't breathe" —
which is exactly the kind of phrase a degraded channel preserves. This is the
result that decides whether the milestone is releasable, and it passes.

*Structured data collapses.* Date-of-birth and phone-number recovery through
the **production** parsers fell from 3/3 to **1/3**, and times were mangled
("half past two" → "Hot, Hot, Hot too"). Names were already the weakest area in
Milestone 3 and get worse.

This is a real limitation, not a tuning problem, and it shapes the design:

* Identity confirmation must **not** rely on spelling a name aloud.
* The existing read-back-and-confirm step before any booking, reschedule or
  cancellation is doing more work over the phone than it was over a microphone.
  It is not optional.
* **DTMF is the right answer for numbers.** The transport already delivers
  keypad digits as structured input (never through STT), which is what a
  Milestone 5 OTP flow needs. Moving date-of-birth capture to the keypad is a
  product and security decision, not one to make here — but the architecture
  supports it.

## Privacy

* **No recording.** `app_mixmonitor` and `res_monitor` are explicitly
  `noload`ed in the Asterisk module template. Audio is in-memory PCM for one
  utterance and is discarded.
* **No transcripts**, unchanged from Milestone 1 (`RETAIN_TRANSCRIPTS=false`).
* **The caller's number is never a log key.** `call_id` — an opaque 16-hex
  correlation id — is. `CallerIdentity.__repr__` redacts the number so a
  traceback cannot print it, and `caller_number`, `caller_name`, `sip_uri` and
  friends were added to the log redactor as defence in depth.
* **The caller's number is never given to the model.** Not in a prompt, not in
  conversation memory. Only a salted fingerprint is derivable, and the salt is
  per-deployment — an unsalted hash of a 10-digit number is a lookup table, not
  an anonymisation.
* **Metric labels are closed sets** — a cause, a codec, an outcome. Never a
  number, patient id, appointment id or SIP URI.
* **Caller ID is not authentication.** It is absent on withheld calls, shared
  across households and switchboards, forwarded, and trivially spoofed. Surname
  and date of birth remain the only route to `IDENTIFIED`.

## Security

See `docs/security/threat-model.md` T13–T19 for SIP credential compromise, toll
fraud, caller-ID spoofing, flooding, media injection, unauthorised transfer and
call crossover.

The short version:

* Bind everything to loopback in development. **AudioSocket has no
  authentication of any kind** — anything that can reach the port can inject
  audio into a live call — so across hosts it belongs on a private media
  network.
* ARI's HTTP port is equivalent to the ability to answer, redirect and hang up
  every call on the PBX. Loopback, and TLS in production.
* An exposed SIP port is found by scanners within minutes. Do not put a
  development PBX on the internet to make a demo work.
* The AI extension has no outbound dial permission. `[clinic-transfer]`
  contains local extensions only.

## Deployment boundary

Local development runs everything on one machine. Production should not.

```
        PSTN
          │
   Australian SIP trunk
          │
   Firewall / SBC boundary          ← SIP + RTP terminate here
          │
      Asterisk host                 ← no AI workload; survives an AI crash
          │
   private media/control network    ← AudioSocket + ARI, never public
          │
    Clinic AI application
          │
   local STT / LLM / TTS            ← CPU/GPU-bound; the real capacity limit
          │
      PostgreSQL
```

Asterisk runs as its own process precisely so an AI crash cannot take telephony
down. If the application dies, Asterisk still answers and the dialplan's
health-check failover routes callers to reception.

This is a target architecture, not an instruction to build it now. No
Kubernetes, no distributed deployment, no infrastructure has been purchased.

## PSTN readiness (Phase B / Milestone 4B)

Local SIP working does **not** mean PSTN works. What remains before a real
Australian number can be connected:

| | |
|---|---|
| SIP trunk provider | Not selected. Must support inbound DID, G.711, and a fixed signalling IP for allowlisting |
| Australian DID | Not purchased |
| Emergency calls | Australian carriers have obligations around 000 that a VoIP trunk may not satisfy. The clinic's existing arrangements stay in place; this system advises callers to ring 000 and does not dial it |
| Caller ID | Presentation and privacy handling differ per carrier; withheld numbers must keep working |
| Concurrent channels | Priced per channel. Local inference caps us well below any sensible trunk minimum |
| Network | Static IP or SBC, firewalled RTP range, TLS/SRTP where the provider supports it |
| Number portability | Only relevant if the clinic's existing number moves — a business decision with an outage window |
| Fraud controls | Credential rotation, IP allowlisting, rate limiting, alerting on unusual call volume |

Nothing here has been purchased or configured, and no provider has been chosen.

## Running it

```bash
uv run python scripts/telephony_status.py          # configuration and PBX reachability
uv run python scripts/telephony_status.py --probe-media
uv run python scripts/benchmark_telephony_stt.py --noise -30

cd backend
uv run pytest tests/telephony -q                   # no Asterisk needed
uv run pytest -m telephony_live -q                 # needs a running PBX
```

Local SIP setup, softphone configuration and the manual acceptance script:
[`docs/development/local-sip.md`](../development/local-sip.md).
