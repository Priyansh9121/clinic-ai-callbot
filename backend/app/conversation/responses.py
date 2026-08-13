"""Spoken response catalogue.

Every sentence the callbot says originates here or from an approved knowledge
entry. Centralising the wording means:

* Length is controllable. Voice turns are one or two sentences — a caller
  cannot skim, and a long turn blocks them from speaking.
* Tone is consistent: warm, calm, brief. Not enthusiastic, no exclamation
  marks, no "Absolutely! I'd be more than happy to...".
* Milestone 3 can pre-synthesise the fixed phrases below (``PREGENERATED``) so
  the common ones play with no TTS latency at all.
"""

from __future__ import annotations

from app.conversation.states import TransferReason

# --- Openings ---------------------------------------------------------------
GREETING = "Hello, you've reached {clinic_name}."
AI_DISCLOSURE = "I'm an automated assistant. How can I help?"
GREETING_WITH_DISCLOSURE = (
    "Hello, you've reached {clinic_name}. I'm an automated assistant. How can I help?"
)

# --- Intent ----------------------------------------------------------------
ASK_INTENT = "How can I help?"
CLARIFY_INTENT = "I can book, change or cancel an appointment. Which would you like?"

# --- Patient identification -------------------------------------------------
ASK_PATIENT_TYPE = "Are you a new or existing patient?"
ASK_IDENTITY = "Can I have your surname and date of birth?"
ASK_IDENTITY_SURNAME = "And your surname?"
ASK_IDENTITY_DOB = "Thanks. And your date of birth?"
# Deliberately identical whether or not a record exists — no enumeration.
IDENTITY_FAILED = (
    "I couldn't verify those details. Let me try once more — your surname and date of birth?"
)
IDENTITY_FAILED_FINAL = "I still couldn't verify those details. I'll put you through to reception."

ASK_NEW_PATIENT_NAME = "No problem. Can I have your full name?"
ASK_NEW_PATIENT_DOB = "Thanks. And your date of birth?"
ASK_NEW_PATIENT_MOBILE = "And the best mobile number for you?"

# --- Booking ---------------------------------------------------------------
ASK_APPOINTMENT_TYPE = "What kind of appointment do you need?"
ASK_APPOINTMENT_TYPE_OPTIONS = "Is that a standard appointment, a longer one, or telehealth?"
ASK_PRACTITIONER = "Do you have a preferred doctor?"
ASK_DATE = "What day suits you?"
ASK_PERIOD = "Morning or afternoon?"

CHECKING = "One moment while I check."
NO_AVAILABILITY = "I don't have anything then. Would another day work?"
NO_AVAILABILITY_AT_ALL = (
    "I can't find anything in the next couple of weeks. I'll put you through to reception."
)
OFFER_SINGLE = "I have {slot}. Does that suit?"
OFFER_MULTIPLE = "I have {slots}. Which suits?"
OFFER_ALTERNATIVE = "Nothing {requested}, but I have {slots}. Would any of those work?"

CONFIRM_BOOKING = "So that's {practitioner}, {when}. Shall I book it?"
BOOKING_CONFIRMED = "Booked. {practitioner}, {when}. Anything else?"
BOOKING_FAILED_TAKEN = "Sorry, that time was just taken. Would you like the next available?"
BOOKING_FAILED = "I couldn't complete that booking. Let me put you through to reception."

# --- Reschedule / cancel ----------------------------------------------------
NO_APPOINTMENTS_FOUND = (
    "I can't find a booking under those details. I'll put you through to reception."
)
ASK_WHICH_APPOINTMENT = "You have {count} appointments. {options}. Which one?"
CONFIRM_APPOINTMENT_FOUND = "I have you down for {when} with {practitioner}."
ASK_NEW_TIME = "What day would suit instead?"
CONFIRM_RESCHEDULE = "So I'll move it to {when}. Is that right?"
RESCHEDULE_CONFIRMED = "Done. You're now booked for {when}. Anything else?"
CONFIRM_CANCEL = "Just to confirm, you'd like to cancel {when} with {practitioner}?"
CANCEL_CONFIRMED = "That's cancelled. Anything else?"
CANCEL_ABORTED = "No problem, I've left it as it is. Anything else?"

# --- Information ------------------------------------------------------------
INFORMATION_UNAVAILABLE = "I don't have that information. I'll put you through to reception."
ANYTHING_ELSE = "Anything else?"

# --- Repair -----------------------------------------------------------------
DIDNT_CATCH = "Sorry, I didn't catch that."
DIDNT_UNDERSTAND = "Sorry, I didn't quite get that."
REPROMPT = "{apology} {question}"

# --- Transfer and close -----------------------------------------------------
TRANSFER_MESSAGES: dict[TransferReason, str] = {
    TransferReason.CALLER_REQUEST: "Of course. Putting you through to reception now.",
    TransferReason.SAFETY: "Let me put you through to reception straight away.",
    TransferReason.CLINICAL_QUESTION: (
        "I'm not able to help with that. I'll put you through to reception."
    ),
    TransferReason.REPEATED_MISUNDERSTANDING: (
        "Sorry, I'm having trouble understanding. I'll put you through to reception."
    ),
    TransferReason.VERIFICATION_FAILED: (
        "I couldn't verify those details. I'll put you through to reception."
    ),
    TransferReason.UNSUPPORTED_REQUEST: (
        "That's something reception handles. Putting you through now."
    ),
    TransferReason.NO_AVAILABILITY: (
        "I can't find a suitable time. I'll put you through to reception."
    ),
    TransferReason.TECHNICAL_FAILURE: (
        "Sorry, I'm having a technical problem. I'll put you through to reception."
    ),
    TransferReason.KNOWLEDGE_GAP: (
        "I don't have that information. I'll put you through to reception."
    ),
}

#: Spoken when the telephony layer could not complete a transfer to a human.
#: The caller is still on the line and must be told something — dropping them
#: after they have asked for a person is the worst available outcome.
TRANSFER_UNAVAILABLE = (
    "I'm sorry, I can't put you through just now. "
    "Please call the clinic again shortly and reception will help you."
)

GOODBYE = "Thanks for calling. Goodbye."
GOODBYE_AFTER_BOOKING = "You're all set. Thanks for calling."


def transfer_message(reason: TransferReason) -> str:
    return TRANSFER_MESSAGES.get(reason, TRANSFER_MESSAGES[TransferReason.CALLER_REQUEST])


#: Fixed phrases worth pre-synthesising in Milestone 3. Each plays while the
#: backend does work, so the caller never hears dead air.
PREGENERATED: tuple[str, ...] = (
    CHECKING,
    "Certainly.",
    "Thanks.",
    "Of course.",
    "One moment.",
    ANYTHING_ELSE,
    ASK_PATIENT_TYPE,
    ASK_DATE,
    ASK_PERIOD,
    ASK_PRACTITIONER,
    DIDNT_CATCH,
    GOODBYE,
    TRANSFER_MESSAGES[TransferReason.CALLER_REQUEST],
)


def join_options(options: list[str]) -> str:
    """ "a, b or c" — reads naturally aloud."""
    if not options:
        return ""
    if len(options) == 1:
        return options[0]
    return f"{', '.join(options[:-1])} or {options[-1]}"
