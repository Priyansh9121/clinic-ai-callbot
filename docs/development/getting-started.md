# Getting started

## First run

```bash
uv sync
cp .env.example .env
./scripts/dev_services.sh init          # or: docker compose up -d postgres redis
cd backend && uv run alembic upgrade head && cd ..
uv run python scripts/seed_dev_data.py
uv run uvicorn app.main:app --reload --app-dir backend
```

```bash
curl -s localhost:8000/health | python3 -m json.tool
uv run python scripts/clinic_chat.py
```

## Development services without Docker

`scripts/dev_services.sh` runs a PostgreSQL cluster and a Redis instance that
belong to this project only: data under `.devdata/` (gitignored), bound to
`127.0.0.1`, on ports 5433 and 6380. Any PostgreSQL or Redis already on the
machine is untouched.

```bash
./scripts/dev_services.sh init      # create cluster, roles, both databases
./scripts/dev_services.sh start
./scripts/dev_services.sh status
./scripts/dev_services.sh stop
./scripts/dev_services.sh destroy   # delete all local dev data
```

It auto-detects a usable PostgreSQL installation, verifying that the binary's
compiled-in share directory actually exists — Homebrew's keg-only
`postgresql@17` currently reports one it does not create, which makes `postgres`
fail at start-up even though `initdb -L` succeeds. Override with `DEV_PG_BIN`.

## Testing

```bash
cd backend
uv run pytest                        # all 309
uv run pytest -m "not integration"   # no database needed
uv run pytest tests/conversation -v
uv run pytest --cov=app --cov-report=term-missing
```

| Suite | What it covers |
|---|---|
| `tests/unit` | dates, intent baseline, redaction, config, metrics, session store, LLM provider/analyser/resolution |
| `tests/integration` | booking transactions, concurrency, HTTP API, LLM health |
| `tests/conversation` | full conversations through the real engine, with and without the LLM |
| `tests/safety` | emergency detection, escalation, false positives |
| `tests/security` | prompt injection (with and without the LLM), authorisation, input validation |
| `tests/llm_live` | **opt-in.** Needs a real Ollama; run with `-m llm_live` |

`uv run pytest` deselects `llm_live` automatically, so the suite passes on a
machine with no model installed.

Integration tests create and drop the schema per test against
`clinic_callbot_test`. Override with `TEST_DATABASE_URL`.

## Where to add things

| Task | Where |
|---|---|
| A new LLM provider | a module in `app/llm/`, one branch in `llm/factory.py`. Nothing else should change |
| A new entity hint | `EntityHints` in `llm/provider.py`, a resolver in `nlu/resolution.py`, the prompt in `llm/prompt.py` |
| A new intent | `states.py`, a pattern in `nlu/baseline.py`, a handler in `handlers/`, dispatch in `engine.py` |
| A new conversation state | `states.py`, then the flow that owns it |
| A new clinic FAQ | `clinic/knowledge_data.py` — approved entries only |
| A new safety rule | `safety/rules.py`, plus a positive **and** a negative test |
| A new spoken phrase | `conversation/responses.py`, never inline |
| A schema change | a model, then `alembic revision --autogenerate` |
| A new endpoint | a route in `api/routes/`, schemas in `schemas/`, logic in a service |

## Conventions worth knowing

- **Routes contain no business logic.** They resolve a service and call it.
- **Flows never get a database session** — only services. This is what makes
  prompt injection structurally impossible, so do not add one.
- **Nothing spoken to a caller is written inline.** It goes in `responses.py` or
  an approved knowledge entry, so length and tone stay controllable and
  Milestone 3 can pre-synthesise the fixed phrases.
- **Replies are one or two sentences.** A test asserts ≤220 characters and no
  exclamation marks.
- **Naive datetimes are rejected**, not guessed. `core/timeutils.py` is the only
  place timezone arithmetic happens.
- **Services own transactions.** Routes never commit.

## Common problems

**`FOR UPDATE cannot be applied to the nullable side of an outer join`**
A model relationship uses `lazy="joined"`, and joined eager loading emits a LEFT
OUTER JOIN that PostgreSQL will not lock. Use `selectinload` in the locking
query, as `_lock_slot` and `_lock_appointment` do.

**`MissingGreenlet` after a commit**
A relationship is being lazy-loaded outside the async context — usually on a
freshly created object whose relationships were never populated. Load them
explicitly (`_load_related`). This one is worth spotting early: it once caused a
booking that had actually committed to be reported to the caller as a failure.

**Objects unusable after a failed service call**
A rollback expires every object in the session. Capture the ids you need before
the call that might fail.

**Conversation behaving as though it already asked a question**
Routing must not pre-set a flow's internal state; it sets `IDENTIFY_INTENT` and
lets the flow decide. Setting the state directly makes the flow believe it has
already asked, and the caller gets a spurious "Sorry, I didn't catch that".

**The LLM appears to be doing nothing**
Check `LLM_MODE` (`baseline` ignores the model entirely) and `LLM_MODEL` (empty
means not configured, and the mode silently degrades to `baseline` — the
factory logs `llm.mode_downgraded`). Then check `GET /health`: `down` means the
runtime is unreachable or the model is not pulled. Hybrid routing also skips
the model whenever the deterministic answer is already confident, which is most
short utterances; set `LLM_HYBRID_ROUTING=false` to force every turn through it.

**A benchmark result looks too good**
Make sure the circuit breaker is disabled for the run. A model emitting a few
malformed responses will otherwise open the breaker, and the rest of the corpus
gets answered by the *baseline* — which reads as high accuracy for the model.
`scripts/benchmark_llm.py` sets `failure_threshold` to effectively infinity for
exactly this reason.
