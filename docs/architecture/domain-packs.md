# Domain packs — analysis

> **This is analysis, not a plan.** Nothing here is agreed, scheduled or
> designed. It is a reading of what `app/conversation/` actually does, written
> to be checked against a real second domain rather than a hypothetical one. Where
> it guesses, it says so.

## Why this question is worth asking

The clinic is the demo; the architecture is the product. A language model that
cannot reach an action — hints only, every one re-resolved against a real record
in code — is worth more in insurance, utilities and financial services than in a
GP clinic, because the thing it prevents is more expensive there. It is also
something a tool-calling platform structurally cannot offer.

The finding that matters most for that pitch is in
[§4](#4-what-cannot-be-declarative): the guarantee is not produced by any check
that could be lifted into a schema. It is produced by **reachability in
straight-line Python** — the model is consulted at exactly one call site, its
output is six free-text fields, each field has a named resolver whose codomain is
`{real row id, deterministic parse, ∅}`, and every mutating call is reachable
only from one confirmation state on a deterministic affirmative. A declarative
flow layer can break that in three ways, each one line of YAML.

---

## 1. Domain-specific vs generic machinery

### The state machine

Of 23 `ConversationState` values: **11 port unchanged, 3 are dead, 4 are
genuinely calendar-bound, and 5 look generic while carrying clinic assumptions.**

| | states |
|---|---|
| **Generic machinery** | `IDENTIFY_INTENT`, `CONFIRM_BOOKING`, `BOOKING_COMPLETE`, `RESCHEDULE_LOOKUP`, `CANCEL_LOOKUP`, `RESCHEDULE_SELECTION`, `RESCHEDULE_CONFIRMATION`, `CANCEL_CONFIRMATION`, `CLINIC_INFORMATION`, `HUMAN_TRANSFER`, `END_CALL` |
| **Dead / unobservable** | `GREETING`, `AI_DISCLOSURE`, `SEARCHING_AVAILABILITY` |
| **Calendar-bound** | `NEW_PATIENT_DETAILS`, `PRACTITIONER`, `PREFERRED_DATE`, `PREFERRED_PERIOD` |
| **Superficially generic** | `PATIENT_TYPE`, `VERIFY_PATIENT`, `APPOINTMENT_TYPE`, `SELECT_SLOT`, `EMERGENCY` |

Two of the dead ones are worth naming. `GREETING` is never persisted — the
session is constructed and immediately reassigned to `IDENTIFY_INTENT`
(`engine.py:121-128`) before the first save. `AI_DISCLOSURE` is referenced
nowhere: the disclosure is discharged by a string prefix
(`responses.py:21-23`) and tracked by `session.ai_disclosed`, which is written
once and **read by nothing**. The comment says the disclosure is "checked, not
assumed"; nothing checks it. In a clinic a spoken prefix is adequate. In
regulated financial services, bot disclosure and recording consent must be
*acknowledged* — a real turn with a refusal path. That enum member is an empty
slot where that state would go, and a second domain has to build it, not
configure it.

**`SELECT_SLOT` is the most instructive of the five.** "Choose from offered
options" is generic *only when the option labels are inert*. Here a time is
simultaneously an option label and a search constraint, which is why saying
"12 pm" back had to be disambiguated between selecting an offer and changing a
preference (`booking.py:52-76`, the livelock fixed earlier in this project).
Roughly a third of the booking handler's complexity exists to resolve that
collision. In a claims flow the options are inert — "claim ABC-123 from March"
is not also a filter — so a third of that code has no counterpart.

`Intent` (7): 3 generic (`CLINIC_INFORMATION`, `HUMAN_RECEPTION`, `END_CALL`),
4 domain (the CRUD triple plus `UNKNOWN`).
`TransferReason` (9): 6 generic machinery, 3 domain (`SAFETY`,
`CLINICAL_QUESTION`, `NO_AVAILABILITY`).
`VerificationLevel` (3): fully generic — and `VERIFIED` is already reserved for
the OTP step that does not exist yet.

### The entity model

**"Practitioner" is about 80% "bookable resource".** The term/alias/longest-match
machinery (`vocabulary.py:65-89`) is entirely generic — and is already written
*twice, identically*, for two entity kinds, which is the cheapest available
evidence that it wants to be parameterised.

What does not port is the alias **generator** (person-name morphology with
medical honorifics — `{last}`, `"dr {last}"`, `"doctor {last}"`) and, more
importantly, three separate places that encode the resource as **substitutable**:

- dropped first when availability fails (`booking.py:289-295`)
- silently defaulted to "any" when the caller does not answer
- yet made *sticky* on reschedule (`reschedule.py`, "they are moving, not
  rebooking")

In a clinic, silently relaxing "Dr Patel" to "anyone" is a mild UX wart. In
insurance, *"I need the adjuster who handled my last claim"* is a constraint the
caller will not accept being relaxed, and relaxing it silently produces an
appointment they will not attend.

Structurally generic fields are exactly those shaped `(id resolved from a
vocabulary, display name for speech)`: practitioner and appointment-type ids,
the `practitioner_any` tri-state, `selection_ordinal`, `target_appointment_id`.

**The time-slot model is not generic — it is specifically calendar scheduling.**
`preferred_date` / `preferred_period` / `not_before` describe a search over a
pre-partitioned inventory. Appointment type is "service variant" plus two things
that do not port: it pre-partitions the slot inventory (slots are cut *per
type*), and it carries a duration.

---

## 2. Where clinic concepts are load-bearing beyond naming

These are the places another domain **breaks silently** rather than reads oddly.
Silent is the operative word: none of these raise.

### `nlu/dates.py` is a forward-only scheduling parser

416 lines with no clinic noun anywhere — and three functions hardwired to
"future appointment". `_roll_forward` (`dates.py:390-397`) pushes a year-less
past date into next year; `_next_month_day` (`400-408`) resolves "the 21st" to
next month if it has passed; `coming_weekday` and `weekday_next_week` only ever
move forward. **`resolve_date` has no branch that can produce a past date.**

A clinic caller always names a future date. Insurance and utilities callers
mostly name a past one — *"the accident was on the 3rd"*, *"the bill dated the
3rd"*, *"the transaction last Friday"*. Today that resolves to **next** month's
3rd and becomes an incident date in the future. Nothing errors.

Related and cheaper: `_to_24h(..., assume_pm_below=8)` with the comment *"a
caller wanting 3 am would not be booking a GP appointment"*. A utility outage
line takes calls at 3am about 3am. The parameter already exists and is already
threaded — under an hour to fix, and easy to never notice.

### Grounding would reject a correct policy number

`is_grounded` (`resolution.py:113-149`) filters to tokens of **≥3 characters**
and does substring matching. It assumes hints are word-shaped and that
transcription preserves substrings.

A hint `"AB-1234-X"` against an utterance transcribed as *"a b one two three four
x"* grounds on **nothing** — the correct answer is discarded as a hallucination.
This undercuts precisely the pitch: the entity whose non-invention is the whole
product is the entity grounding cannot check.

### The safety engine's default is clinical

`engine.py:226-230` defaults every unrecognised escalation to
`CLINICAL_QUESTION` and writes it to the audit record. In another domain that is
a mislabelled audit trail on every unclassified escalation.

The engine itself is clean: normalise, ordered first-match-wins, negation
guards, severity ratchet — no medical term anywhere, and it already accepts
injected rules (`safety/engine.py:68-72`). The rules, categories and wording are
entirely clinical, including `"Lifeline on 13 11 14"` hardcoded *past* the
`emergency_number` setting that is already parameterised.

### Identity is hardwired in five layers

Surname + DOB appears in session fields, handler, wording, extractor and
service. The **security shape** is fully generic and is the sellable part: two
independent factors, ambiguity treated identically to no-match, one
indistinguishable failure message, a hard attempt cap of 2, an audit row on
every attempt including failures.

What is clinic-specific is that the *identity of the two factors* is spelled out
in five places rather than declared once.

### Two more worth flagging

**The intent fallback creates records.** `engine.py:378` defaults an
unrecognised utterance carrying entities to `BOOK_APPOINTMENT` — *"a caller
volunteering a day or a doctor is almost always trying to book"*. Safe in a
clinic: starting a booking is reversible and costs a question. The equivalent
default in insurance is **lodging a claim**, which creates a record, can affect a
no-claims position, and is not reversible by hanging up. One line; a category of
harm the clinic version does not have.

**Domain-service exceptions speak to the caller.**
`appointments/service.py:207-222` raises `BookingRuleError` with inline
`public_message` strings — *"That time is too soon to book over the phone."*
Every other spoken sentence originates in `responses.py` or an approved
knowledge entry; that is the stated invariant. These quietly break it, and in a
regulated domain they are exactly the sentences needing legal sign-off.

---

## 3. What a domain pack would have to declare

Six blocks. Each is forced by an existing coupling rather than chosen for
tidiness.

```yaml
subject:            # who is on the phone
  name: patient | policyholder | account_holder
  identity_factors: [ {field, kind, matcher, prompt} ]   # ordered
  attempt_limit: 2

catalogues:         # entities resolved by matching live rows
  practitioner:
    alias_generator: person_name | reference_code | plain
    substitutable: true|false      # may the search silently relax this?
    relaxation_order: 1

scalars:            # parser-backed entities
  preferred_date:
    parser: date
    temporal_direction: future | past | either
    invalidates_offers: [booking]  # per-flow, not global

flows:              # per intent
  book:
    phases: [identify, locate, choose, confirm, execute]
    required: [appointment_type, practitioner, preferred_date]
    service_role: transaction
    states: [ {name, kind: open|answer|identity|choice|terminal} ]

escalation:
  categories: [ {name, announced: true|false, action, wording} ]
  rules: [ {id, category, pattern, guards} ]
  reason_to_destination: { SAFETY: reception, FRAUD: fraud_queue }

speech:
  strings: { ASK_IDENTITY: "...", IDENTITY_FAILED: "..." }
  knowledge_file: packs/clinic/knowledge.json
```

**A seventh block should be separate: locale, not domain.** The mobile regex
(`+61|0[45]`), day-first date parsing and `PERIOD_BOUNDS` are Australian, not
clinical. A UK insurance line and a UK GP clinic share all of them. Conflating
them makes every domain pack carry an implicit country and forces a pack fork on
the first non-Australian deployment.

**What stays fixed:** all of `engine._run_turn`'s ordering, all of
`nlu/resolution.py`'s algorithm, the `FlowContext` services-not-sessions
contract, and the state *graph* — because transitions are decided by result
cardinality and by which factor is still missing, so a declarative transition
table would have to encode predicates over service results, at which point it is
a programming language with worse tools.

---

## 4. What cannot be declarative

**The important section.** For each candidate config key, the test is: *which
invariant becomes reachable if this key is set to its most permissive value?*

The codebase already contains this rule, applied to an existing flag and with
the reasoning written down — the identity-state check sits **physically above**
the hybrid-routing switch, because routing is configurable and leaning on it
would make a correctness property depend on a performance setting. That is the
transferable design rule, and it is domain-independent.

### Must stay in Python

| invariant | how config would break it |
|---|---|
| **Safety runs first** | Not a policy value — it is the physical order of statements in one function, and the model is not even *constructed* when the emergency decision is made. A declared `pipeline: [analyse, safety, route]` looks reasonable — an author wanting distress rules only on complaints needs the intent first — and silently puts the model upstream of safety. |
| **Model consulted only in non-identity states** | *The single most important one.* Identification is where the target domains differ most and where the model is structurally absent — `EntityHints` has **no identity fields**. A pack declaring `state: verify_customer, extract: [policy_number]` routes identity through the model in two lines. |
| **Six hints, six resolvers** | A pack-declared entity gets **no resolver**, and the only extractor that can produce a novel field is the model. `policy_number` becomes a passthrough *by construction*. This is the precise mechanism by which the abstraction fails for the target domains. |
| **Hints fill gaps, never contradict** | As config this becomes `practitioner: {source: prefer_llm}` — exactly the knob an author turns when their deterministic parser is weak, which it will be, because nobody has written domain regexes yet. |
| **Dates re-parsed, never trusted** | The parser is not a validator, it is the semantics. A pack legitimately needs different conventions, and the natural way to offer that is to accept the model's ISO date. |
| **Confirm-before-mutate** | A reachability property, not a flag — there is no `confirmed=True` anywhere. The affirmative regex is `^`-anchored; an author dropping the anchor to catch *"yeah go ahead then"* makes *"no, don't go ahead"* affirmative. **One character converts a decline into a booking.** |
| **Correction beats confirmation** | Enforced by statement order at the top of the handler. As a declared listener priority, the correct order is not obvious from either listener's description — and the naive order is the livelock this project already fixed once. |
| **Transfer allowlist** | Split carefully: the reason→destination map *is* legitimately declarative. What cannot move is the closed codomain and its boot-time extension validator — that is the toll-fraud control. |
| **Terminal-state absorption** | Has a deliberate exception: safety is evaluated *before* the absorption check, so a post-transfer emergency is still handled. A declared `terminal: true` gets this wrong in one direction or the other. |
| **Knowledge returns approved text or nothing** | A docstring contract, not a type. A pack shipping an LLM-backed knowledge provider satisfies the interface exactly. No type system distinguishes a lookup from a generator. |

### The traps that look most safe

Two deserve naming because they are the ones a careful author gets wrong:

**Safety rule ordering.** A rule is `{id, category, action, pattern}` — obviously
a YAML row, and the data genuinely is declarative-shaped. But priority is
list-construction order with first-match-wins and no severity comparison. If a
pack appends an escalation rule matching `\b(help|emergency)\b` and the loader
puts pack rules first, **every emergency rule that also matches is masked**, and
nothing raises.

**Negation guards.** The only de-escalation primitive in the system — a guard
match skips the rule entirely. It is also the first thing an author reaches for
when tuning false positives, because that is what it is *for*. A guard like
`\b(not|no)\b` on `EMG_BREATHING` suppresses *"I'm not able to breathe"*.

### One latent risk today

`expected_patient_id` defaults to `None`, meaning **unchecked**. The
cross-patient authorisation on cancel and reschedule is preserved by two call
sites remembering to pass it, not by any type. A third mutating flow — or a
declared step `{action: cancel, appointment: $target}` — omits it, type-checks
perfectly, passes every test where callers cancel their own booking, and
silently accepts any appointment id in the session. This is worth fixing
independently of any of this.

---

## 5. Cost, honestly

**The code is cheaper than it looks. The tests are far more expensive than they
look.**

| | work | estimate |
|---|---|---|
| **Cheap** | response catalogue, vocabulary alias tables, handler dispatch registry, safety rule injection, knowledge topic genericisation, intent-fallback default | **1.5–2 weeks** |
| **Moderate** | resolver registry in `resolution.py`, declarative field/invalidation model replacing `_apply_entities`, dynamic session draft schema, temporal direction for `dates.py` | **5–6 weeks** |
| **Hard** | N-factor identity (5 files), declarative state graph, confidence ladder | **1 month+ each** |

The hard items are hard for **entangled semantics, not code volume**. The
confidence numbers in `_INTENT_PATTERNS` look like the most obviously
declarative thing in the codebase and are the one where a naive pack does most
damage: they encode *which utterances the hybrid router shows to the model*. An
author writing `0.9` for a loose "claim" pattern permanently locks in its false
positives, because the model is never consulted to correct them.

### The test suite is the omitted line item

~450 conversation and integration tests encode clinic semantics.

| | | |
|---|---|---|
| **~35%** | ports free | transport, audio framing, date arithmetic — the layers nobody doubts |
| **~45%** | template only | ~170 conversation/integration tests need re-authoring — **3–4 weeks** |
| **~20%** | cannot port | must be deleted and re-earned |

That last 20% is the important one. `MINIMUM_ACCURACY = 0.90` over a 140-case
clinic corpus is *evidence*, and evidence does not transfer between domains. A
second domain starts with **no baseline numbers** until someone labels ~140 new
utterances — a week of careful work by a domain expert, and the actual
prerequisite for claiming the architecture works in insurance. The injection
tests port conceptually but should be re-derived: the prize in financial
services is bank details, not a patient list, and the payloads differ.

Roughly 60% of `booking.py` exists only because two callers can want the same 15
minutes. A claim lodgement has no scarce resource — nothing is taken between
offering and confirming — so `offered_slot_ids`, `select_from_offers`,
`clear_offers` and the whole re-offer path have **no analogue**. The flows get
rewritten per domain at 1–2 weeks each, and that is the correct outcome rather
than a failure of design.

---

## What this analysis suggests

**Spend nothing on generalisation until the second domain exists** — but not
because it is cheaper. It is not.

### Correction: the second-domain estimate

An earlier draft of this document said a second vertical was 4–6 weeks "behind
existing Protocol seams, touching zero abstraction code". The second clause is
true and the number is wrong, in a specific way worth naming.

**The five Protocol seams are not where the clinic lives.** `MessageAnalyser`,
`SessionStore`, `ClinicKnowledgeProvider`, `LLMProvider` and `SafetyClassifier`
are real seams, and writing implementations behind them genuinely is a few
weeks. But the clinic lives in the handlers, the resolvers, `dates.py`, the
identity flow and `appointments/service.py` — **none of which sit behind a
Protocol.** The 4–6 weeks priced the seams and omitted a second implementation
of the parts that took longest the first time.

Costed properly:

| | |
|---|---|
| Domain flows (3–4 handlers at 1–2 weeks each) | 4–8 weeks |
| Domain transaction service (the `appointments/service.py` equivalent) | 2–3 weeks |
| Identity flow — N factors across 5 files | 2–3 weeks |
| Resolvers, including a resolver *kind* that does not exist yet for checksummed identifiers | 1.5–2 weeks |
| `dates.py` temporal direction | 1 week |
| Safety rule set — 1 week engineering, plus weeks of domain and compliance sign-off | 1 week + external |
| Knowledge set and a ~140-case labelled NLU corpus | 1–2 weeks, mostly domain-expert time |
| Test re-authoring (~170 conversation/integration tests) | 3–4 weeks |
| **Total** | **≈ 4–6 months** |

Which is the same order as the abstraction-first estimate, and that comparison
was the point the old number was making. **It does not survive.** A second
vertical is 4–6 months whichever way it is built, because most of that work is
the domain itself rather than the plumbing.

### So the real question is different

Not "second domain or abstraction" — you are paying for the domain either way.
The question is whether to **abstract while building it or after**, and the
argument for after is no longer cost. It is that an abstraction derived from one
example is a guess, and the specific guesses this document makes
([§4](#4-what-cannot-be-declarative)) are the expensive kind to get wrong: they
are about where the model can re-enter the causal path.

Two things follow. Building the second domain concretely and extracting the
abstraction from the diff costs roughly one extra month over abstracting as you
go — a month spent buying evidence instead of assumptions. And the sequencing is
load-bearing: the ~140-case labelled corpus and the domain's safety rule set are
on the critical path for *any* claim that the architecture works in that domain,
they are mostly not engineering time, and they can start immediately.

Three things are worth doing regardless, because they are bugs or latent risks
rather than generalisation:

1. ~~`expected_patient_id` defaulting to unchecked.~~ **Fixed** — `owner` is now
   required and typed `UUID | StaffAuthorised`, so omitting it fails to run and
   passing `None` fails to type-check. See below for what the fix uncovered.
2. `_apply_entities`' string-literal `changed` set, where adding a field
   silently stops corrections working with no failing test.
3. The `public_message` strings in `appointments/service.py` that reach the
   caller from outside the response catalogue.

### What the authorisation fix uncovered

Making the parameter required surfaced a second defect the permissive default
had been hiding. `session.patient_id` is `UUID | None`, and the old signature
accepted `UUID | None` — so a session that reached cancel or reschedule without
an identified patient would have passed `None` and **skipped the check
entirely**. Both handlers do gate identity earlier, so it was probably
unreachable; it was unreachable *by convention across a function boundary*, not
by type. Both now refuse explicitly rather than relying on that.

A second permissive default remains, and is a policy gate rather than an
authorisation one: `BookingRequest.is_new_patient` defaults to `False`, and
`_enforce_booking_rules` reads `if is_new_patient and not
appointment_type.new_patient_allowed` — so the default takes the *permissive*
branch. Both production callers pass it today. Making it required costs ~31 test
call sites and is worth doing, but it is a smaller consequence than the
authorisation one: a new patient booking a type not meant for them, rather than
a caller reaching someone else's record.

### The two things to test against a real second domain

If a claims or outage flow is written, these are the predictions to check —
they are where this analysis is most likely to be wrong:

- **That the state graph cannot be declarative.** The argument is that
  transitions depend on result cardinality (`PREFERRED_PERIOD` is entered only
  when a speculative search returns too many options to read aloud). If a second
  domain's flows turn out not to need runtime-count-dependent transitions, a
  declarative graph becomes viable and the estimate drops by a month.
- **That identity generalises to "N factors + a matcher".** The claim is that
  the security shape is generic and only the factor identity is specific. A
  checksummed identifier may break this: a checksum leaks validity through
  itself in a way a surname does not, so the anti-enumeration property may need
  re-deriving rather than re-parameterising.
