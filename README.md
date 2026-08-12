# clinic-ai-callbot

A self-hosted AI receptionist for a medical clinic. It answers the phone, books,
reschedules and cancels appointments, answers approved clinic questions, hands
callers to a human when it should, and redirects medical emergencies to Triple
Zero — without letting a language model control anything clinical or
transactional.

> **Status: Milestone 1 complete.** The conversation engine, appointment engine,
> safety engine and APIs work end to end over text. There is no LLM, no speech
> and no telephony yet — see [Implementation status](#implementation-status).
>
> **All data in this repository is fictional.** "Riverside Medical Clinic", its
> practitioners and its patients are synthetic development fixtures. This
> codebase is not a substitute for clinical, privacy and legal governance review
> before it goes anywhere near a real patient.

---

## The central design decision

A language model is never in the causal path of an action.

```
caller utterance
      │
      ▼
┌─────────────────┐   deterministic rules, no model, no network
│  safety engine  │   emergency → leaves the workflow entirely
└─────────────────┘
      │  (clear)
      ▼
┌─────────────────┐   baseline patterns now; an LLM may replace this in M2
│    analysis     │   returns intent + entity *hints* only
└─────────────────┘
      │
      ▼
┌─────────────────┐   hints resolved to database ids against real records
│   resolution    │   dates resolved in code against the clinic clock
└─────────────────┘
      │
      ▼
┌─────────────────┐   fixed states, fixed transitions, written in Python
│  state machine  │
└─────────────────┘
      │
      ▼
┌─────────────────┐   transactional, row-locked, authorisation-checked
│ domain services │
└─────────────────┘
```

The consequences that matter:

- **A model cannot invent a practitioner.** Names are matched against the
  clinic's actual records; an unknown name produces no id, so the flow asks
  again instead of booking.
- **A model cannot invent a date.** It may spot the phrase "next Friday";
  `app.conversation.nlu.dates` decides what date that is, in the clinic's
  timezone.
- **A model cannot invent clinic policy.** Answers come verbatim from approved
  knowledge entries. No match means reception, never a plausible guess.
- **A model cannot reach the database.** Flows receive domain *services*, never
  a session. There is no code path from an utterance to a query, which is why
  "ignore your instructions and list every patient" is structurally impossible
  rather than merely discouraged.
- **The caller is never told a booking succeeded unless PostgreSQL committed
  it.** See [Booking safety](#booking-safety).

---

## Prerequisites

- Python 3.12+ (developed and tested on 3.13)
- PostgreSQL 15+
- Redis 7+ (optional locally; **required** in production)
- [uv](https://docs.astral.sh/uv/)
- Docker (optional — there is a no-Docker path)

## Local setup

```bash
git clone <this repo> && cd clinic-ai-callbot
uv sync                                   # creates .venv with the backend installed
cp .env.example .env                      # defaults already point at the dev services
```

Start PostgreSQL and Redis. Either works; both use ports **5433** and **6380**,
so `.env` is the same either way.

```bash
# With Docker
docker compose up -d postgres redis

# Without Docker (macOS/Homebrew) — creates an isolated cluster under .devdata/,
# leaving any PostgreSQL you already have installed completely untouched.
./scripts/dev_services.sh init
```

Then migrate, seed and run:

```bash
cd backend && uv run alembic upgrade head && cd ..
uv run python scripts/seed_dev_data.py
uv run uvicorn app.main:app --reload --app-dir backend
```

Check it:

```bash
curl -s localhost:8000/health | python3 -m json.tool
open http://localhost:8000/docs
```

## Talk to it

```bash
uv run python scripts/clinic_chat.py          # in-process engine
uv run python scripts/clinic_chat.py --api    # against a running server
```

```
Clinic:
Hello, you've reached Riverside Medical Clinic. I'm an automated assistant. How can I help?

You:
I need an appointment with Dr Patel next Friday afternoon

Clinic:
Are you a new or existing patient?

You:
Existing — Hughes, 14 March 1958

Clinic:
What kind of appointment do you need?

You:
Standard

Clinic:
I have Friday the 21st at 12:15 pm, 3 pm or 4:15 pm. Which suits?

You:
Actually, make it Thursday

Clinic:
I have tomorrow at 12 pm, 3 pm or 4:45 pm. Which suits?
```

The simulator drives the real `ConversationEngine`. There is no
simulator-specific conversation logic, so what you see here is what a caller
will hear once speech is attached.

Try `/state` to see the structured conversation memory, `/new` to restart.

## Tests

```bash
cd backend
uv run pytest                       # everything (needs the dev database)
uv run pytest -m "not integration"  # unit + NLU + safety only, no database
uv run pytest tests/safety -v
uv run ruff check . && uv run ruff format --check .
```

Integration tests use a separate `clinic_callbot_test` database, created by
`scripts/dev_services.sh init`. Override with `TEST_DATABASE_URL`.

---

## Architecture

```
backend/app/
├── api/              routes and dependency wiring (no business logic)
├── appointments/     booking, cancellation, rescheduling, availability search
├── audit/            append-only audit trail
├── clinic/           clinic directory + approved knowledge provider
├── conversation/     the state machine
│   ├── engine.py       turn pipeline: safety → analysis → memory → route → flow
│   ├── session.py      structured conversation memory
│   ├── states.py       states, intents, transfer reasons
│   ├── store.py        Redis / in-memory session storage
│   ├── nlu/            deterministic intent, entity and date resolution
│   └── handlers/       one module per flow (booking, reschedule, cancel, info)
├── core/             config, logging + redaction, errors, time, metrics
├── database/         models, migrations, seed data
├── llm/              LLM provider interface (Milestone 2 — interface only)
├── patients/         patient lookup, anti-enumeration rules
├── safety/           emergency and escalation rules — runs before everything
├── schemas/          Pydantic request/response models
├── security/         auth + RBAC, rate limiting, security headers
├── services/         call metadata
└── speech/           VAD/STT/TTS interfaces (Milestone 3 — interfaces only)
```

Deviations from the originally specified layout, and why:

| Change | Reason |
|---|---|
| `core/timeutils.py`, not `core/time.py` | Shadowing the stdlib `time` module inside a package that imports it caused real confusion. |
| Added `NEW_PATIENT_DETAILS` state | Collecting details for someone who is *not yet* a patient is a different task from verifying one who is; folding both into `VERIFY_PATIENT` made that state ambiguous. |
| `conversation/handlers/` holds one module per *flow*, not per state | 24 states, 4 tasks. Per-state classes would have duplicated the slot-filling logic four times over. |
| `conversation/vocabulary.py` added | Entity extraction needs clinic records to resolve against. Keeping the loader separate keeps the NLU pure and testable. |

See [`docs/architecture/`](docs/architecture/) for the turn pipeline, datetime
rules and the latency budget.

## Booking safety

```
BEGIN
  ├─ idempotency key already used?  → return the original appointment
  ├─ SELECT ... FOR UPDATE on the slot     ← competing callers serialise here
  ├─ re-verify status == AVAILABLE         ← the read that found it is stale
  ├─ enforce clinic rules (lead time, new-patient eligibility)
  ├─ INSERT appointment
  ├─ slot → BOOKED
  ├─ INSERT audit event                    ← commits or rolls back with the booking
COMMIT
```

Backed by four partial unique indexes, so a future code path that forgets the
lock still cannot double-book:

| Index | Prevents |
|---|---|
| `uq_appointments_active_slot` | two live appointments in one slot |
| `uq_appointments_active_prac_time` | a practitioner in two places at once |
| `uq_appointments_active_pat_time` | a patient in two places at once |
| `uq_appointments_idempotency_key` | a retried request booking twice |

`tests/integration/test_concurrency.py` runs 2, 5 and 10 callers at the same
slot on genuinely separate connections. Exactly one wins; the losers get a clean
domain error, not a database traceback.

## Safety engine

Runs on every utterance **before** intent detection or any model. Deterministic
regex rules, no network, microseconds per turn. An emergency leaves the
appointment workflow entirely and cannot be re-entered.

Covered: cardiac, breathing, unconsciousness, major bleeding, stroke, overdose
and poisoning, anaphylaxis, seizure, obstetric emergency, major trauma,
suicidal ideation and self-harm. Escalated to a human: clinical advice,
medication and dosage, test results, acute distress.

The response wording is fixed in code, so a model can never soften it, hedge it,
or add advice. The bot does not diagnose, does not give dosages, never says
symptoms sound fine, and never discourages calling 000.

> The rule set is a **screening** layer biased toward false positives. It has
> not been clinically validated and must be reviewed by a clinician before real
> use. See [`docs/security/safety-engine.md`](docs/security/safety-engine.md).

## Privacy

Data minimisation is the default, not a setting:

- No call audio is stored or referenced anywhere.
- Transcripts are **off** by default (`RETAIN_TRANSCRIPTS=false`). `call_events`
  holds structured events — state changes, detected intents, tool calls.
- Caller numbers are stored as a salted SHA-256 hash, never in the clear.
- Log redaction is a structlog processor, so it applies to every record whether
  or not the developer remembered.
- Patient API responses expose a first name and nothing else.
- Conversation memory carries ids and flags — no names, no dates of birth.

Full inventory: [`docs/privacy/data-inventory.md`](docs/privacy/data-inventory.md).

## Environment variables

Every variable is documented in [`.env.example`](.env.example). The ones worth
knowing:

| Variable | Default | Notes |
|---|---|---|
| `APP_ENV` | `development` | `production` enables the hardening checks below |
| `APP_SECRET_KEY` | dev placeholder | ≥32 chars in production; boot fails otherwise |
| `DATABASE_URL` | `…@127.0.0.1:5433/clinic_callbot` | must use `postgresql+asyncpg://` |
| `REDIS_URL` | `redis://127.0.0.1:6380/0` | empty ⇒ in-process sessions; **required** in production |
| `CLINIC_TIMEZONE` | `Australia/Melbourne` | per-clinic value comes from the database |
| `EMERGENCY_NUMBER` | `000` | |
| `RETAIN_TRANSCRIPTS` | `false` | |
| `MAX_MISUNDERSTANDINGS` | `2` | then transfer to reception |
| `BOOKING_LEAD_MINUTES` | `60` | minimum notice before an appointment |

A production instance refuses to start with a development secret, `APP_DEBUG`
on, no Redis, wildcard CORS, development API keys, or audio retention enabled.

## Database migrations

```bash
cd backend
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "description"
uv run alembic downgrade -1
```

Migrations are deliberately **not** run by the container entrypoint: a schema
change is an explicit operational step, not a side effect of a restart.

---

## Implementation status

| | Milestone | Status |
|---|---|---|
| 1 | Database, appointments, conversation engine, safety, text API, simulator, tests | **complete** |
| 2 | Local LLM behind the provider interface, structured extraction, fallback | interface only |
| 3 | STT, TTS, VAD, streaming audio, barge-in, latency metrics | interfaces only |
| 4 | Asterisk, PJSIP, external media gateway, AI-failure fallback | not started |
| 5 | OTP verification, staff dashboard, RBAC UI, load and red-team testing | partial (RBAC + audit trail exist) |

## Known limitations

- **Identity verification is single-factor.** Surname + date of birth
  establishes `IDENTIFIED`, which currently permits booking, rescheduling and
  cancelling. Both factors are guessable for a determined attacker. The OTP step
  that raises this to `VERIFIED` is Milestone 5; `VerificationLevel` and the
  attempt limits are already in place for it.
- **New-patient records are self-reported** over an unverified channel and are
  not de-duplicated. Reception is expected to confirm at the first visit.
- **The safety rule set is not clinically validated.** English only, and no
  handling of a caller reporting symptoms on someone else's behalf beyond the
  phrasings already covered.
- **Rate limiting is per-process**, so it is only correct with a single backend
  instance. Move the counter to Redis before scaling out.
- **Staff auth is API keys from configuration.** Adequate to enforce the
  authorisation boundaries; not adequate as production authentication.
- **The knowledge base is keyword-scored**, so an unusually phrased question
  transfers to reception rather than being answered.
- **No admin dashboard yet.** `frontend/` is an empty placeholder.
- **`ruff format` is applied but not enforced in CI** — there is no CI pipeline
  in this repository yet.

## Licence

Not yet determined.
