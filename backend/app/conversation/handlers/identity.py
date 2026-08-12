"""Patient identification steps, shared by the booking, reschedule and cancel
flows.

Identification here establishes ``VerificationLevel.IDENTIFIED`` — surname plus
date of birth matching exactly one record. That is enough to book, and it is
what the current milestone ships. Operations that expose *existing* patient
data (reschedule, cancel) additionally consult
``app.conversation.policy``, which is where the OTP step raises the bar to
``VERIFIED`` in Milestone 5.
"""

from __future__ import annotations

from app.conversation import responses
from app.conversation.handlers.base import FlowContext, Reply
from app.conversation.states import (
    ConversationState,
    PatientType,
    TransferReason,
    VerificationLevel,
)

#: Identification attempts before handing to reception. Kept low: repeatedly
#: probing surname/DOB combinations is exactly what an attacker would do.
MAX_IDENTITY_ATTEMPTS = 2


async def ask_patient_type(ctx: FlowContext) -> Reply:
    ctx.goto(ConversationState.PATIENT_TYPE)
    return Reply(text=responses.ASK_PATIENT_TYPE)


async def handle_patient_type(ctx: FlowContext) -> Reply | None:
    """Resolve new vs existing. Returns None once it is known."""
    session = ctx.session
    if session.patient_type is not None:
        return None

    declared = ctx.analysis.entities.patient_type
    if declared is None:
        # A caller answering "yes" to "are you a new or existing patient?" has
        # not answered; asking again is better than guessing.
        if session.state is ConversationState.PATIENT_TYPE:
            return ctx.reprompt(responses.ASK_PATIENT_TYPE, responses.DIDNT_CATCH)
        return await ask_patient_type(ctx)

    session.patient_type = declared
    session.clear_retries()
    return None


async def ensure_identified(ctx: FlowContext) -> Reply | None:
    """Drive identification to completion. Returns None when done.

    For an existing patient this matches surname + DOB against the database.
    For a new patient it collects the details needed to create a record; the
    record itself is only created at booking time, so an abandoned call leaves
    no data behind.
    """
    session = ctx.session

    patient_type_reply = await handle_patient_type(ctx)
    if patient_type_reply is not None:
        return patient_type_reply

    if session.patient_type is PatientType.NEW:
        return await _collect_new_patient(ctx)
    return await _identify_existing(ctx)


async def _identify_existing(ctx: FlowContext) -> Reply | None:
    session = ctx.session
    if session.verification_level is not VerificationLevel.NONE:
        return None

    entities = ctx.analysis.entities
    # Only fill a factor that is still missing. Once a surname is captured, a
    # later turn supplying the date of birth must not overwrite it with a
    # stray word from that same utterance.
    if entities.last_name and session.identity_last_name is None:
        session.identity_last_name = entities.last_name
    if entities.date_of_birth and session.identity_dob is None:
        session.identity_dob = entities.date_of_birth

    if session.state is not ConversationState.VERIFY_PATIENT:
        ctx.goto(ConversationState.VERIFY_PATIENT)
        if not (session.identity_last_name and session.identity_dob):
            return Reply(text=responses.ASK_IDENTITY)

    # Ask for whichever factor is still missing.
    if not session.identity_last_name:
        return Reply(text=responses.ASK_IDENTITY_SURNAME)
    if not session.identity_dob:
        return Reply(text=responses.ASK_IDENTITY_DOB)

    patient = await ctx.patient_service.identify(
        clinic_id=ctx.clinic.id,
        last_name=session.identity_last_name,
        date_of_birth=session.identity_dob,
        actor_ref=session.session_id,
    )
    if patient is None:
        session.verification_attempts += 1
        # Discard the failed attempt so the caller supplies both factors again.
        session.identity_last_name = None
        session.identity_dob = None
        if session.verification_attempts >= MAX_IDENTITY_ATTEMPTS:
            return ctx.transfer(TransferReason.VERIFICATION_FAILED)
        return Reply(
            text=responses.IDENTITY_FAILED,
            events=[("VERIFICATION_ATTEMPT", {"attempt": session.verification_attempts})],
        )

    session.patient_id = patient.id
    session.patient_first_name = patient.first_name
    session.verification_level = VerificationLevel.IDENTIFIED
    session.clear_retries()
    return None


async def _collect_new_patient(ctx: FlowContext) -> Reply | None:
    session = ctx.session
    draft = session.new_patient
    entities = ctx.analysis.entities

    if entities.first_name and not draft.first_name:
        draft.first_name = entities.first_name
    if entities.last_name and not draft.last_name:
        draft.last_name = entities.last_name
    if entities.date_of_birth:
        draft.date_of_birth = entities.date_of_birth
    if entities.mobile:
        draft.mobile = entities.mobile
    session.new_patient = draft  # re-assign so pydantic validation runs

    if draft.is_complete:
        session.verification_level = VerificationLevel.IDENTIFIED
        session.patient_first_name = draft.first_name
        session.clear_retries()
        return None

    entering = session.state is not ConversationState.NEW_PATIENT_DETAILS
    ctx.goto(ConversationState.NEW_PATIENT_DETAILS)

    if not (draft.first_name and draft.last_name):
        return Reply(
            text=responses.ASK_NEW_PATIENT_NAME
            if entering
            else _retry(ctx, responses.ASK_NEW_PATIENT_NAME)
        )
    if draft.date_of_birth is None:
        return Reply(text=responses.ASK_NEW_PATIENT_DOB)
    return Reply(text=responses.ASK_NEW_PATIENT_MOBILE)


def _retry(ctx: FlowContext, question: str) -> str:
    """Re-ask without losing the misunderstanding count."""
    ctx.session.note_misunderstanding()
    return f"{responses.DIDNT_CATCH} {question}"
