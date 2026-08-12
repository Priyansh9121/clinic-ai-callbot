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

## What is deliberately **not** collected

- **Call audio.** Nothing stores or references audio. `RETAIN_AUDIO` exists so
  the decision is explicit and refusable; a production instance will not start
  with it enabled.
- **Transcripts.** Off by default. With `RETAIN_TRANSCRIPTS=true`, turn text is
  written to `call_events` truncated to 500 characters — enable only with a
  documented justification and a retention decision.
- **Symptoms or clinical detail.** The bot does not ask and does not record it.
  Anything clinical routes to a human.
- **Anything used for model training.** Patient conversations are never used to
  train or fine-tune a model. Models are local and static.

## Where it lives

| Store | Contents | Exposure |
|---|---|---|
| PostgreSQL | everything above except sessions | `127.0.0.1` only in the shipped configuration |
| Redis | conversation sessions, TTL-bounded | `127.0.0.1` only; no persistence (`--save "" --appendonly no`) |
| Logs | redacted structured events | stderr |
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
