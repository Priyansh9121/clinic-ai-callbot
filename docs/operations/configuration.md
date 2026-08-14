# Where each setting lives

Four stores hold configuration, and each owns different things. Two stores
disagreeing about the same fact is the failure this page exists to prevent —
the clinic's opening hours are exactly that trap, and are called out below.

| Store | Owns | Changed by |
|---|---|---|
| **Environment** (`.env`) | how this process runs and what it talks to | redeploy |
| **PostgreSQL** | the clinic's operational data | staff, seed script, SQL |
| **Clinic knowledge file** | public wording spoken aloud | clinic sign-off, then redeploy |
| **Asterisk config** (`/etc/asterisk`) | telephony itself | PBX admin, then reload |

## Environment — `.env`

Runtime wiring: ports and bind addresses, the ARI URL, user and password, the
AudioSocket bind and advertised hosts, model paths and provider selection,
timeouts, concurrency and capacity limits, retention flags, feature switches.

Also `CLINIC_TIMEZONE` and `CLINIC_KNOWLEDGE_FILE`, which are *pointers* rather
than clinic data: which zone to interpret times in, and which approved-answer
file to read.

Every variable is documented in `.env.example`. `.env` is gitignored and holds
real secrets.

## PostgreSQL — the operational truth

Clinics, locations, practitioners, appointment types, availability slots,
appointments, patients, call records and the audit log.

Everything the booking flow reasons about lives here, and only here. This is
what makes "the LLM may not invent availability" enforceable: the model never
sees a query, and a slot that is not a row does not exist.

**Practitioners and appointment types are database-owned and nothing else may
describe them.** `app/clinic/directory.py` composes "who are your doctors?" and
"what can I book?" from live rows for exactly this reason — a second copy in the
knowledge file would drift, and the drift would show up as the receptionist
naming a doctor the booking flow cannot find.

## Clinic knowledge file — `CLINIC_KNOWLEDGE_FILE`

A JSON list of approved answers, read to callers **verbatim**. Opening hours,
address, parking, fees, bulk billing, cancellation policy, telehealth,
referrals, after-hours arrangements — the things a clinic signs off as wording.

Production refuses to start without it (`app/clinic/factory.py`); development
falls back to the fictional "Riverside Medical Clinic" set and logs
`clinic.knowledge_is_placeholder` every time.

Topics with `requires_human: true` (test results, prescriptions) are read out
and then transferred, which is a clinical-safety boundary rather than a
convenience.

### The one real overlap: opening hours

`OPENING_HOURS` is spoken from the knowledge file. What can actually be booked
comes from `availability_slots`. **Nothing checks that these agree**, so a
clinic that changes its hours must change both — and if it changes only the
slots, the bot will keep reading the old hours aloud while offering times
outside them.

Slots are the operational truth; the knowledge entry is what a caller hears.
Treat the entry as a description of the roster, and re-read it whenever the
roster changes. Deriving the sentence from the slot grid is the obvious
improvement and is not implemented.

## Asterisk config — `/etc/asterisk`

SIP endpoints and credentials, transports, dialplan contexts and extensions,
RTP ranges, the ARI user, trunks. Templates are tracked in
`infrastructure/asterisk/*.conf.example`; the populated `*.conf` files hold real
credentials, are gitignored, and a test pins that they cannot be committed.

Two values must agree with `.env`, and both have caused outages:

| Asterisk | `.env` | Symptom when they disagree |
|---|---|---|
| `ari.conf` section name | `ASTERISK_ARI_USER` | 401, indistinguishable from a wrong password |
| `Stasis(...)` argument | `ASTERISK_APP_NAME` | call reaches Stasis, nothing listening, caller falls to reception |

`test_dialplan_templates.py` pins the second across the templates.

## Quick answers

| Question | Answer |
|---|---|
| Add a doctor | PostgreSQL. Appears in booking *and* in "who are your doctors?" immediately |
| Retire an appointment type | PostgreSQL — set `active = false` |
| Change the fees answer | knowledge file, then redeploy |
| Change opening hours | knowledge file **and** the availability slots |
| Change the greeting | `app/conversation/responses.py` (code — see below) |
| Change the transfer extension | `.env` (`RECEPTION_EXTENSION`) and the dialplan's `[clinic-transfer]` |
| Change the safety wording | `app/safety/` (code — deliberately) |

Greeting and safety wording are code, not configuration, and that is a
deliberate choice: both are legally and clinically load-bearing, and the AI
disclosure in the greeting is a regulatory requirement rather than a
preference. Changing them should go through review, not an editor.
