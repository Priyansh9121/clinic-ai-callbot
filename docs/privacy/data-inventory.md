# Personal information inventory

> This document records what the system does today. It is **not** a privacy
> impact assessment and does not constitute legal advice. A real deployment
> handling Australian health information needs review against the Privacy Act
> 1988 (Cth), the Australian Privacy Principles, and the relevant state health
> records legislation, plus the clinic's own governance.

## What is collected, and why

| Data | Where | Why | Retention | Notes |
|---|---|---|---|---|
| Name, date of birth | `patients` | Identify the caller; label the booking | Clinical record lifetime (clinic policy) | Existing records come from the clinic's system |
| Mobile, email | `patients` | Contact about appointments; OTP delivery (M5) | As above | `mobile_normalised` holds an E.164 copy for exact-match lookup |
| Appointment details | `appointments` | The booking itself | Clinical record lifetime | `reason_summary` is short and non-clinical |
| Call metadata | `calls` | Service quality, safety review, abuse control | `CALL_DATA_RETENTION_DAYS` (default 90) | Outcome, intent, turn count, transfer flag |
| Structured call events | `call_events` | Diagnose failed conversations | As `calls` (cascade) | State changes and intents, **not** a transcript |
| Caller number | `calls.caller_hash` | Correlate repeat callers | As `calls` | Salted SHA-256. The number itself is never stored |
| Audit events | `audit_events` | Accountability for sensitive operations | ≥ 7 years (clinic policy) | Ids and reason codes only |
| Conversation session | Redis | Hold state during the call | `SESSION_TTL_SECONDS` (default 30 min) | Transient; never the system of record |

## What the local LLM receives (Milestone 2)

The model runs locally — nothing leaves the machine — but data minimisation
applies regardless, because a prompt is still a copy of the data.

| Sent to the model | Why |
|---|---|
| The current utterance (truncated to 400 chars) | the thing being classified |
| The conversation state name | "the second one" means different things in different states |
| Practitioner display names | so "Patel" is recognised as a name |
| Appointment type names | so "long appointment" is recognised as a type |

**Never sent:** patient names, dates of birth, mobile numbers, email addresses,
patient or appointment identifiers, verification answers, previous turns, any
session history, or audit records. Patient matching is entirely deterministic
and happens in application code after analysis.

`test_only_permitted_context_reaches_the_provider` asserts the provider call
carries exactly those four arguments and nothing else.

Nothing about the model interaction is retained: no prompt, no raw response, no
per-turn record. Shadow mode keeps two enum values and a confidence bucket —
enough to answer "is the model better?" without retaining anything about the
caller. Patient conversations are never used to train or fine-tune a model;
models are local and static.

## What is deliberately **not** collected

- **Call audio.** Nothing stores or references audio. `RETAIN_AUDIO` exists so
  the decision is explicit and refusable; a production instance will not start
  with it enabled. Milestone 3 added a live microphone and still writes no
  audio: PCM exists in memory for one utterance and is discarded. There is no
  recording path, no temporary speech file, no STT debug dump and no `.wav`
  capture anywhere in the voice pipeline.
- **Transcripts.** Off by default. With `RETAIN_TRANSCRIPTS=true`, turn text is
  written to `call_events` truncated to 500 characters — enable only with a
  documented justification and a retention decision.
- **Symptoms or clinical detail.** The bot does not ask and does not record it.
  Anything clinical routes to a human.
- **Anything used for model training.** Patient conversations are never used to
  train or fine-tune a model. Models are local and static.

## What the voice pipeline receives (Milestone 3)

| Stage | Sees | Retains |
|---|---|---|
| Microphone / VAD | raw PCM frames | nothing — a bounded ring, overwritten continuously |
| STT | one utterance of PCM, plus a clinic-vocabulary decoder prompt | nothing |
| ConversationEngine | the transcript, as ordinary text | per Milestone 1 rules |
| TTS | the reply text | fixed catalogue phrases only (see below) |
| Speaker | synthesised PCM | nothing |

Three constraints are worth stating explicitly, because each is a place audio
could have quietly become a data store:

- **The STT vocabulary bias holds clinic terms only** — practitioner display
  names, appointment-type names, the clinic name. Patient names are never put in
  the decoder prompt: it is sent on *every* utterance, so a patient name in it
  would leak across callers as well as being retained.
- **The TTS cache is an allowlist, not a general cache.** It admits only phrases
  drawn from the response catalogue, and excludes any template containing `{`,
  because a template becomes patient-specific the moment it is filled. "Anything
  else?" is cached; "Booked. Dr Sarah Patel, tomorrow at 12 pm." is not.
- **Voice logs carry no content.** Timings, provider names, frame counts and
  character *counts* only — never PCM, base64 audio, transcripts, caller speech
  or patient-specific reply text.

`scripts/voice_chat.py --show-transcript` prints what was heard to the
developer's terminal for work against the synthetic clinic. It writes nothing to
disk and nothing to the logs.

## Where it lives

| Store | Contents | Exposure |
|---|---|---|
| PostgreSQL | everything above except sessions | `127.0.0.1` only in the shipped configuration |
| Redis | conversation sessions, TTL-bounded | `127.0.0.1` only; no persistence (`--save "" --appendonly no`) |
| Logs | redacted structured events | stderr |
| Audio | never persisted — in-memory PCM, one utterance at a time | process memory only |
| Metrics | counters and latency only | `/health/metrics` |

## Who can access it

| Actor | Access | Enforced by |
|---|---|---|
| The callbot | Its own domain services only. Reads one patient after identity match; writes appointments | No SQL path from an utterance; services take ids, not text |
| Staff (`staff`) | Book, cancel, reschedule via API | `require_role(Role.STAFF)` |
| Admin (`admin`) | As staff, plus configuration | `require_role(Role.ADMIN)` |
| Unauthenticated | Clinic directory, availability, conversation | Public by design — no patient data on those endpoints |

The application's database role is not a superuser and cannot create databases
or roles.

## Minimisation in practice

- `PatientSummary` exposes `id` and `first_name`. No date of birth, mobile or
  email crosses an API boundary.
- Log redaction is a structlog processor, applied to every record regardless of
  the call site, plus pattern scrubbing of numbers, emails and dates that appear
  inside free-text values.
- `ConversationSession.memory_snapshot()` returns ids and flags only.
- Audit metadata is restricted to an allowlist of safe keys; anything else is
  dropped and counted.
- Failed identification returns the same response whether the patient exists,
  does not exist, or matched ambiguously.

## Deletion

Not yet implemented — this is a Milestone 5 gap, recorded here rather than
glossed over:

- No scheduled job enforces `CALL_DATA_RETENTION_DAYS`. `calls` and
  `call_events` accumulate.
- No subject-access-request export.
- No "delete my data" workflow. Patient deletion is a clinic-system operation;
  `appointments` uses `ON DELETE RESTRICT` on `patient_id`, so a patient cannot
  be removed while appointments reference them. That is deliberate — it forces
  the decision rather than silently orphaning records — but the workflow to
  resolve it does not exist yet.

## Australian context

Development is configured for `Australia/Melbourne` and Triple Zero (000).
Emergency wording directs callers to 000 and, for self-harm, to Lifeline
(13 11 14). Synthetic mobile numbers use the `0491 570 xxx` range that ACMA
reserves for fictitious use. Example email domains use `.invalid`, which is
guaranteed never to resolve.
