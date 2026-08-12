# Safety engine

## Status and limitations — read first

This rule set is a **screening layer, not clinical triage.** It has not been
clinically validated. It must be reviewed by a qualified clinician, and the
approved response wording signed off, before any real patient interaction.

It is deliberately biased toward false positives. Wrongly directing a caller to
emergency services is a recoverable inconvenience; missing a genuine emergency
is not.

Known gaps:

- English only. No handling of other languages or heavy accents (which will also
  interact with STT accuracy in Milestone 3).
- Paediatric and obstetric phrasing is thin.
- Symptom *combinations* are not modelled — each rule fires independently.
- A caller who understates ("bit of chest tightness, probably nothing") may not
  trigger. The negation guards mitigate the opposite error but not this one.

## Why it runs first

Safety evaluation happens before intent detection, entity extraction, knowledge
lookup and any language model. It is deterministic: regex over a normalised
string, no network, no database, no model.

That matters for three reasons:

1. **Availability.** A safety decision must not depend on an LLM being up. If
   every model in the stack is down, emergency detection still works.
2. **Reproducibility.** A rule that fired can be explained and tested. A model
   that fired cannot be, not reliably.
3. **Priority.** "I want to book an appointment, I'm having chest pain" is an
   emergency. Because safety runs first and returns immediately, the booking
   intent in the same sentence is never reached.

## Categories

| Category | Action | Examples |
|---|---|---|
| `MEDICAL_EMERGENCY` | `EMERGENCY` | chest pain, can't breathe, unconscious, severe bleeding, stroke, overdose, anaphylaxis, seizure, obstetric, major trauma |
| `SELF_HARM` | `EMERGENCY` | suicidal ideation, self-harm |
| `CLINICAL_ADVICE` | `ESCALATE_HUMAN` | "is this serious?", "what's wrong with me?" |
| `MEDICATION` | `ESCALATE_HUMAN` | dosage, interactions, stopping medication |
| `TEST_RESULTS` | `ESCALATE_HUMAN` | "are my results back?" |
| `DISTRESS` | `ESCALATE_HUMAN` | "I'm in agony", "I can't cope" |

Emergency rules are evaluated first, so the first match is always the most
severe one present.

## Negation guards

Some trigger terms appear in ordinary administrative speech. `NEGATION_GUARDS`
suppresses a specific rule when a phrase indicates a past or administrative
reference:

- "a follow-up about my chest pain last month" → not an emergency
- "I have a stroke clinic review booked" → not an emergency

Guards are per-rule, so suppressing one never weakens another.

## Approved responses

Wording is fixed in `app/safety/engine.py`, not generated. A model can never
soften it, hedge it, or append advice.

**Medical emergency**

> This sounds like an emergency. Please hang up and call triple zero, 000, now.
> They can help you immediately.

**Self-harm**

> I'm really sorry you're going through this. Please call triple zero, 000, or
> Lifeline on 13 11 14 right now. They can help you straight away.

The bot must never: diagnose, give treatment or dosage advice, state that
symptoms sound safe, or discourage calling emergency services.
`test_emergency_response_never_gives_medical_advice` asserts the absence of
advice-shaped language in these strings.

## Extensibility

`SafetyClassifier` is a protocol for adding a trained classifier later as an
*additional* signal. Two invariants, enforced in `evaluate_async`:

- It may only **raise** severity. A classifier returning "all clear" can never
  override an emergency the rules found.
- Failure is non-fatal. An exception or timeout is logged and the deterministic
  verdict stands.

Both are covered by tests (`test_classifier_may_not_downgrade_a_rule_hit`,
`test_classifier_failure_falls_back_to_rules`).

## What is recorded

On an emergency: `EMERGENCY_TRIGGERED` audit event, a `SAFETY_TRIGGERED` call
event, a metric keyed by category, and a warning log — all carrying the
`rule_id` and category only. **The utterance itself is never logged.**

The call is marked `emergency_triggered`, the outcome becomes
`EMERGENCY_REDIRECTED`, and the state machine cannot re-enter the appointment
workflow for the remainder of the call.
