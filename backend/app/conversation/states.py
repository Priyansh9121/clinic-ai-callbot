"""Conversation states and intents.

The engine is a deterministic state machine. A language model may later
*classify* an utterance, but the set of reachable states and the transitions
between them are fixed here, in code. That is what makes it impossible for a
model to talk the system into an action the state machine does not allow.
"""

from __future__ import annotations

from enum import StrEnum


class ConversationState(StrEnum):
    GREETING = "GREETING"
    AI_DISCLOSURE = "AI_DISCLOSURE"
    IDENTIFY_INTENT = "IDENTIFY_INTENT"

    PATIENT_TYPE = "PATIENT_TYPE"
    VERIFY_PATIENT = "VERIFY_PATIENT"
    # Deviation from the original state list: collecting details for a caller
    # who is not yet a patient is a distinct task from verifying an existing
    # one, and folding both into VERIFY_PATIENT made that state ambiguous.
    NEW_PATIENT_DETAILS = "NEW_PATIENT_DETAILS"

    APPOINTMENT_TYPE = "APPOINTMENT_TYPE"
    PRACTITIONER = "PRACTITIONER"
    PREFERRED_DATE = "PREFERRED_DATE"
    PREFERRED_PERIOD = "PREFERRED_PERIOD"

    SEARCHING_AVAILABILITY = "SEARCHING_AVAILABILITY"
    SELECT_SLOT = "SELECT_SLOT"
    CONFIRM_BOOKING = "CONFIRM_BOOKING"
    BOOKING_COMPLETE = "BOOKING_COMPLETE"

    RESCHEDULE_LOOKUP = "RESCHEDULE_LOOKUP"
    RESCHEDULE_SELECTION = "RESCHEDULE_SELECTION"
    RESCHEDULE_CONFIRMATION = "RESCHEDULE_CONFIRMATION"

    CANCEL_LOOKUP = "CANCEL_LOOKUP"
    CANCEL_CONFIRMATION = "CANCEL_CONFIRMATION"

    CLINIC_INFORMATION = "CLINIC_INFORMATION"

    HUMAN_TRANSFER = "HUMAN_TRANSFER"
    EMERGENCY = "EMERGENCY"
    END_CALL = "END_CALL"


#: States after which no further caller input is processed.
TERMINAL_STATES: frozenset[ConversationState] = frozenset(
    {
        ConversationState.HUMAN_TRANSFER,
        ConversationState.EMERGENCY,
        ConversationState.END_CALL,
    }
)

#: States where the caller is being asked to identify themselves. Entity
#: extraction is restricted here so that a surname is not mistaken for a
#: practitioner name.
IDENTIFICATION_STATES: frozenset[ConversationState] = frozenset(
    {ConversationState.VERIFY_PATIENT, ConversationState.NEW_PATIENT_DETAILS}
)


class Intent(StrEnum):
    BOOK_APPOINTMENT = "BOOK_APPOINTMENT"
    RESCHEDULE_APPOINTMENT = "RESCHEDULE_APPOINTMENT"
    CANCEL_APPOINTMENT = "CANCEL_APPOINTMENT"
    CLINIC_INFORMATION = "CLINIC_INFORMATION"
    HUMAN_RECEPTION = "HUMAN_RECEPTION"
    END_CALL = "END_CALL"
    UNKNOWN = "UNKNOWN"


class PatientType(StrEnum):
    NEW = "NEW"
    EXISTING = "EXISTING"


class VerificationLevel(StrEnum):
    """How confident we are about who is on the line.

    NONE        nothing established
    IDENTIFIED  caller supplied details matching exactly one patient record
    VERIFIED    identity confirmed out-of-band (OTP) — Milestone 5

    ``app.conversation.policy`` maps operations to the level they require, so
    tightening the requirement later is a one-line configuration change rather
    than a rewrite of the flows.
    """

    NONE = "NONE"
    IDENTIFIED = "IDENTIFIED"
    VERIFIED = "VERIFIED"


class TransferReason(StrEnum):
    CALLER_REQUEST = "CALLER_REQUEST"
    SAFETY = "SAFETY"
    CLINICAL_QUESTION = "CLINICAL_QUESTION"
    REPEATED_MISUNDERSTANDING = "REPEATED_MISUNDERSTANDING"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    UNSUPPORTED_REQUEST = "UNSUPPORTED_REQUEST"
    NO_AVAILABILITY = "NO_AVAILABILITY"
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"
    KNOWLEDGE_GAP = "KNOWLEDGE_GAP"
