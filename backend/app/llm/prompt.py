"""Prompt construction for structured utterance analysis.

Design constraints, all of which are load-bearing:

* **Small.** A caller is on the phone. Every token costs latency, so the prompt
  carries the current state and the clinic's own vocabulary and nothing else.
  No few-shot examples: on the models tested they cost more latency than the
  accuracy they bought (see docs/architecture/llm.md).
* **No transcript, no patient data.** The model receives one utterance, the
  conversation state, and the clinic's practitioner and appointment-type names.
  It never sees a name, date of birth, contact detail or previous turn.
* **Classification only.** The prompt tells the model it is not writing the
  reply, not giving advice and not producing identifiers. That is a latency and
  quality measure, not a security control — the security control is that
  nothing it returns can become an identifier or an action.
"""

from __future__ import annotations

from app.conversation.states import Intent

#: Intents the model may return. Anything else fails enum validation and the
#: turn falls back to the deterministic analyser.
_VALID_INTENTS = ", ".join(i.value for i in Intent)

#: What each state means, so the model can read a fragment in context. Only the
#: states where a bare fragment is genuinely ambiguous need an entry.
_STATE_GUIDANCE: dict[str, str] = {
    "IDENTIFY_INTENT": "The caller has not said what they want yet.",
    "PATIENT_TYPE": "You asked whether they are a new or existing patient.",
    "VERIFY_PATIENT": "You asked for their surname and date of birth.",
    "NEW_PATIENT_DETAILS": "You are collecting a new patient's name, date of birth and mobile.",
    "APPOINTMENT_TYPE": "You asked what kind of appointment they need.",
    "PRACTITIONER": "You asked whether they have a preferred doctor.",
    "PREFERRED_DATE": "You asked what day suits them.",
    "PREFERRED_PERIOD": "You asked whether they want morning or afternoon.",
    "SELECT_SLOT": "You read out several appointment times; they are choosing one.",
    "CONFIRM_BOOKING": "You asked them to confirm a specific appointment.",
    "RESCHEDULE_LOOKUP": "You are finding which existing appointment they mean.",
    "RESCHEDULE_SELECTION": "You asked what day they would like to move to.",
    "RESCHEDULE_CONFIRMATION": "You asked them to confirm the new time.",
    "CANCEL_LOOKUP": "You are finding which appointment they want to cancel.",
    "CANCEL_CONFIRMATION": "You asked them to confirm the cancellation.",
    "CLINIC_INFORMATION": "You answered a question and asked if they need anything else.",
    "BOOKING_COMPLETE": "The task is done and you asked if they need anything else.",
}

SYSTEM_PROMPT = f"""\
You classify single utterances from callers to a medical clinic's \
administrative phone line. You are not the receptionist and you do not reply \
to the caller. Your only output is JSON.

Return exactly this shape, with no other keys:
{{"intent": <intent>, "confidence": <0.0-1.0>, "entities": {{"practitioner": \
<string|null>, "preferred_date_text": <string|null>, "preferred_period": \
<string|null>, "appointment_type": <string|null>, "patient_type": \
<string|null>, "selection": <string|null>}}, "requires_human": <true|false>}}

Valid intent values: {_VALID_INTENTS}

Rules:
- Copy entity values from the caller's own words. Do not normalise dates, do \
not invent values, and leave a field null when the caller did not mention it.
- preferred_period is one of: morning, afternoon, evening.
- patient_type is one of: new, existing.
- selection is how they picked from a list, e.g. "the first one", "2:30", \
"the later one".
- Never output an identifier, a UUID, SQL, a function name or a reply to the \
caller. You do not give medical advice and you do not state clinic policy.
- Use UNKNOWN when the utterance is unclear, and set requires_human true only \
when the caller wants a person.
- Output only the JSON object."""


def build_user_prompt(
    message: str,
    *,
    state: str,
    known_practitioners: tuple[str, ...] = (),
    known_appointment_types: tuple[str, ...] = (),
) -> str:
    """Assemble the per-turn prompt.

    The vocabulary is included so the model recognises "Patel" as a name rather
    than a common noun. It is *not* how a name becomes a booking: the returned
    hint is re-matched against the same vocabulary in application code, so a
    practitioner who does not work here resolves to nothing.
    """
    lines: list[str] = []

    guidance = _STATE_GUIDANCE.get(state)
    lines.append(f"Conversation state: {state}" + (f" — {guidance}" if guidance else ""))

    if known_practitioners:
        lines.append("Doctors at this clinic: " + ", ".join(known_practitioners))
    if known_appointment_types:
        lines.append("Appointment types: " + ", ".join(known_appointment_types))

    # The utterance is delimited and explicitly labelled as data. This is a
    # quality measure that helps the model keep instruction-shaped caller text
    # separate from its own instructions. It is NOT the injection defence —
    # that is structural: nothing the model returns can become an action.
    lines.append("\nClassify this caller utterance:")
    lines.append(f"<<<{message}>>>")
    return "\n".join(lines)
