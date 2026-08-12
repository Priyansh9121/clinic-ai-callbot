# Conversation engine

## The turn pipeline

Every caller utterance passes through the same six stages, in this order. The
order is the design: it is what guarantees a model can never introduce a state
transition, a permission or a database write.

```
1. SAFETY        deterministic rules. No model, no network, no database.
                 Emergency → leave the workflow. Cannot be re-entered.
2. ANALYSIS      intent + entities. Replaceable by an LLM (Milestone 2).
3. MEMORY        entities folded into the session; changed fields recorded.
4. ROUTING       transfer requests, intent switches, call closing.
5. FLOW          the handler for the active intent takes one step.
6. PERSISTENCE   session to the store, structured events to PostgreSQL.
```

Stage 2 is the only one a language model will ever touch. Stages 1, 4 and 5 are
code.

## Why slot filling rather than a script

The booking flow does not walk a fixed sequence of questions. Each turn fills
whatever the caller volunteered, then asks for the highest-priority field still
missing.

"I need to see Dr Patel on Friday afternoon" fills practitioner, date and period
in one turn and skips three questions. "I need an appointment" fills nothing and
gets asked for all of them.

This is also what makes corrections cheap. The engine records which session
fields actually *changed* this turn (`FlowContext.changed`). A flow uses that to
tell a correction from an answer:

- Field changed while slots were on offer → discard the offers, search again.
- Nothing changed → the utterance is an answer to the question just asked.

Re-stating the same day is not a change. "Actually Thursday" is.

```python
# app/conversation/engine.py
if entities.preferred_date is not None:
    if draft.preferred_date != resolution.day:
        changed.add("preferred_date")     # only then are offers discarded
```

Identity and verification level deliberately survive both corrections and intent
switches. A caller who has already identified themselves and then says "actually
cancel it instead" is not asked for their date of birth again.

## State machine

```
                    GREETING / AI_DISCLOSURE
                              │
                       IDENTIFY_INTENT
        ┌────────────┬────────┴────────┬──────────────┐
        ▼            ▼                 ▼              ▼
    BOOK         RESCHEDULE         CANCEL      CLINIC_INFORMATION
        │            │                 │              │
  PATIENT_TYPE       │                 │              ▼
        │            │                 │      (approved answer)
  ┌─────┴─────┐      │                 │
  ▼           ▼      │                 │
VERIFY_    NEW_PATIENT_DETAILS         │
PATIENT       │      │                 │
  └─────┬─────┘      ▼                 ▼
        │      RESCHEDULE_LOOKUP  CANCEL_LOOKUP
  APPOINTMENT_TYPE   │                 │
        │      RESCHEDULE_SELECTION    │
   PRACTITIONER      │                 │
        │      RESCHEDULE_CONFIRMATION │
   PREFERRED_DATE    │           CANCEL_CONFIRMATION
        │            │                 │
   PREFERRED_PERIOD  │                 │
        │            │                 │
 SEARCHING_AVAILABILITY                │
        │            │                 │
    SELECT_SLOT      │                 │
        │            │                 │
  CONFIRM_BOOKING    │                 │
        └────────────┴────────┬────────┘
                              ▼
                      BOOKING_COMPLETE
                              │
                          END_CALL

    HUMAN_TRANSFER and EMERGENCY are reachable from any state and are terminal.
```

`PREFERRED_PERIOD` is only entered when the chosen day has more open slots than
three offers could fairly represent. Asking "morning or afternoon?" when there
are four slots left in the day wastes a turn.

## Confirmation

- **One option offered** → the offer *is* the confirmation ("I have Friday at
  2:30 with Dr Patel. Does that suit?"). Asking twice is irritating.
- **Several offered** → select, then confirm explicitly with the practitioner
  and time read back.

A confirmation state never books on anything other than an affirmative. Neither
yes nor no re-asks. Cancellation always confirms before executing, so a misheard
"cancel" cannot remove a booking.

## Escalation

The bot never argues with a request for a human. `Intent.HUMAN_RECEPTION` is
checked in routing, before any flow, and transfers immediately from any state.

Automatic escalation triggers:

| Trigger | Reason code |
|---|---|
| explicit request | `CALLER_REQUEST` |
| safety rule (clinical advice, medication, results, distress) | `CLINICAL_QUESTION` / `SAFETY` |
| `MAX_MISUNDERSTANDINGS` exceeded | `REPEATED_MISUNDERSTANDING` |
| identification failed twice | `VERIFICATION_FAILED` |
| no approved answer for the question | `KNOWLEDGE_GAP` |
| no availability at all | `NO_AVAILABILITY` |
| unhandled exception | `TECHNICAL_FAILURE` |

That last row matters: `handle_message` catches everything. A crash produces a
transfer, never silence and never a stack trace read aloud.

## Conversation memory

`ConversationSession` *is* the memory. The engine never replays a transcript.

Two reasons. Latency: prompts stay small because only the fields relevant to the
current decision are ever sent. Privacy: `memory_snapshot()` contains ids and
flags only — no names, no dates of birth, no contact details — so it is safe to
log, safe to show on a dashboard, and safe to hand to a model later.

State lives in Redis with a sliding TTL. It is never the system of record;
anything that must outlive the call is written to PostgreSQL by a service.

## Silence handling (Milestone 3)

Thresholds are configured, not hardcoded, because the right values depend on the
line and the caller:

```
SILENCE_SHORT_SECONDS      1.2   keep waiting — the caller is thinking
SILENCE_CHECKIN_SECONDS    4.0   "Are you still there?"
SILENCE_REPROMPT_SECONDS   8.0   repeat the question
SILENCE_TERMINATE_SECONDS 15.0   transfer or close, per configuration
```
