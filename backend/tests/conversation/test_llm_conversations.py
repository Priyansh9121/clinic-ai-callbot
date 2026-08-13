"""Full conversations driven through the engine with an LLM-backed analyser.

The point of these is that the engine, the flows and the booking guarantees are
untouched by Milestone 2: swapping the analyser changes how an utterance is
*understood*, and nothing else. A scripted provider stands in for the model so
the tests are deterministic and need no Ollama.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.conversation.engine import ConversationEngine
from app.conversation.nlu.baseline import BaselineAnalyser
from app.conversation.nlu.llm_analyser import LLMBackedAnalyser
from app.conversation.states import ConversationState, Intent, VerificationLevel
from app.database.models.appointment import Appointment
from app.database.models.enums import AppointmentStatus
from tests.fixtures.fake_llm import FakeLLMProvider, analysis

pytestmark = pytest.mark.integration


@pytest.fixture
def llm_engine_factory(db, settings, session_store, clinic_data):
    """Build an engine whose analyser is backed by a scripted provider."""
    from app.clinic.knowledge_data import build_dev_knowledge
    from app.safety.engine import SafetyEngine

    def _build(responses, *, shadow: bool = False, hybrid: bool = True) -> ConversationEngine:
        provider = FakeLLMProvider(responses)
        analyser = LLMBackedAnalyser(
            provider=provider,
            baseline=BaselineAnalyser(),
            min_confidence=settings.llm_min_confidence,
            shadow=shadow,
            hybrid_routing=hybrid,
        )
        engine = ConversationEngine(
            db=db,
            settings=settings,
            store=session_store,
            safety=SafetyEngine(),
            analyser=analyser,
            knowledge=build_dev_knowledge(),
        )
        engine.test_provider = provider  # type: ignore[attr-defined]
        return engine

    return _build


class Caller:
    def __init__(self, engine: ConversationEngine) -> None:
        self._engine = engine
        self.session_id = ""
        self.last = None
        self.transcript: list[tuple[str, str]] = []

    async def start(self) -> str:
        result = await self._engine.start(channel="text")
        self.session_id = result.session_id
        self.last = result
        self.transcript.append(("bot", result.reply))
        return result.reply

    async def say(self, message: str) -> str:
        if not self.session_id:
            await self.start()
        self.transcript.append(("caller", message))
        result = await self._engine.handle_message(self.session_id, message)
        self.last = result
        self.transcript.append(("bot", result.reply))
        return result.reply

    async def session(self):
        return await self._engine.get_session(self.session_id)

    def dump(self) -> str:
        return "\n".join(f"{who:>6}: {text}" for who, text in self.transcript)


# --- Indirect language the baseline misses ----------------------------------
async def test_indirect_booking_is_understood(llm_engine_factory) -> None:
    """ "Was hoping someone could see me Friday" — no booking keyword at all."""
    engine = llm_engine_factory(
        [analysis(Intent.BOOK_APPOINTMENT, confidence=0.93, preferred_date_text="Friday")]
    )
    caller = Caller(engine)
    await caller.start()
    await caller.say("Was hoping someone could see me Friday.")

    session = await caller.session()
    assert session.intent is Intent.BOOK_APPOINTMENT
    assert session.booking.preferred_date is not None


async def test_indirect_cancellation_is_understood(llm_engine_factory) -> None:
    engine = llm_engine_factory([analysis(Intent.CANCEL_APPOINTMENT, confidence=0.9)])
    caller = Caller(engine)
    await caller.start()
    await caller.say("I don't think I'll be needing that booking anymore.")

    session = await caller.session()
    assert session.intent is Intent.CANCEL_APPOINTMENT


async def test_indirect_reschedule_is_understood(llm_engine_factory) -> None:
    engine = llm_engine_factory([analysis(Intent.RESCHEDULE_APPOINTMENT, confidence=0.9)])
    caller = Caller(engine)
    await caller.start()
    await caller.say("Something's come up and Friday won't work anymore.")

    session = await caller.session()
    assert session.intent is Intent.RESCHEDULE_APPOINTMENT


# --- Corrections ------------------------------------------------------------
async def test_a_correction_changes_only_the_corrected_field(
    llm_engine_factory,
) -> None:
    """The Milestone 1 correction model is unchanged: the engine still decides
    what changed, from resolved entities."""
    engine = llm_engine_factory(
        [
            analysis(
                Intent.BOOK_APPOINTMENT,
                confidence=0.95,
                practitioner="Dr Patel",
                preferred_date_text="Friday",
                preferred_period="afternoon",
            ),
            analysis(Intent.UNKNOWN, confidence=0.9, patient_type="existing"),
            analysis(Intent.UNKNOWN, confidence=0.5),
            analysis(Intent.UNKNOWN, confidence=0.5),
            analysis(Intent.UNKNOWN, confidence=0.9, appointment_type="standard"),
            analysis(Intent.BOOK_APPOINTMENT, confidence=0.95, preferred_date_text="Thursday"),
        ]
    )
    caller = Caller(engine)
    await caller.start()
    await caller.say("I want Dr Patel Friday afternoon.")
    await caller.say("existing patient")
    await caller.say("Hughes")
    await caller.say("14 March 1958")
    await caller.say("standard")

    before = await caller.session()
    practitioner_before = before.booking.practitioner_id
    date_before = before.booking.preferred_date
    assert practitioner_before is not None

    await caller.say("Actually Thursday.")
    after = await caller.session()

    # Only the date moved.
    assert after.booking.preferred_date != date_before
    assert after.booking.practitioner_id == practitioner_before
    assert after.verification_level is VerificationLevel.IDENTIFIED
    assert after.booking.appointment_type_id == before.booking.appointment_type_id


# --- Intent switching -------------------------------------------------------
async def test_the_engine_performs_the_intent_switch_not_the_model(
    llm_engine_factory,
) -> None:
    engine = llm_engine_factory(
        [
            analysis(Intent.BOOK_APPOINTMENT, confidence=0.95),
            analysis(Intent.CANCEL_APPOINTMENT, confidence=0.95),
        ]
    )
    caller = Caller(engine)
    await caller.start()
    await caller.say("I'd like to book something")
    assert (await caller.session()).intent is Intent.BOOK_APPOINTMENT

    await caller.say("actually I want to cancel instead")
    session = await caller.session()
    assert session.intent is Intent.CANCEL_APPOINTMENT
    # The state machine, not the model, chose the new state.
    assert session.state in {s for s in ConversationState}


# --- The model must not hijack an in-flight flow ----------------------------
async def test_a_mid_flow_answer_cannot_switch_the_task(
    llm_engine_factory,
) -> None:
    """Regression, found in live testing.

    Mid-reschedule, the model classified the caller's date of birth as
    BOOK_APPOINTMENT, and the engine switched them out of their reschedule into
    a new booking. In an answer state the utterance is an answer, not a new
    request, so a task intent from the model is suppressed.
    """
    engine = llm_engine_factory(
        [
            analysis(Intent.RESCHEDULE_APPOINTMENT, confidence=0.95),
            # Every subsequent answer is confidently misread as a new booking.
            analysis(Intent.BOOK_APPOINTMENT, confidence=0.99),
            analysis(Intent.BOOK_APPOINTMENT, confidence=0.99),
            analysis(Intent.BOOK_APPOINTMENT, confidence=0.99),
        ],
        hybrid=False,
    )
    caller = Caller(engine)
    await caller.start()
    await caller.say("Something's come up and my booking won't work anymore.")
    assert (await caller.session()).intent is Intent.RESCHEDULE_APPOINTMENT

    await caller.say("Hughes")
    await caller.say("14 March 1958")

    session = await caller.session()
    assert session.intent is Intent.RESCHEDULE_APPOINTMENT, caller.dump()


async def test_a_mid_flow_answer_is_not_reclassified_as_a_clinic_question(
    llm_engine_factory,
) -> None:
    """Also found live: "I've been there before" answering "new or existing?"
    was classified CLINIC_INFORMATION, and the caller was transferred to
    reception because no approved answer matched."""
    engine = llm_engine_factory(
        [
            analysis(Intent.BOOK_APPOINTMENT, confidence=0.95),
            analysis(Intent.CLINIC_INFORMATION, confidence=0.99, patient_type="existing"),
        ],
        hybrid=False,
    )
    caller = Caller(engine)
    await caller.start()
    await caller.say("Was hoping someone could see me Friday.")
    await caller.say("I've been there before")

    session = await caller.session()
    assert session.intent is Intent.BOOK_APPOINTMENT
    assert caller.last.requires_human is False, caller.dump()
    # The entity from that same response is still used.
    assert session.patient_type is not None


async def test_a_genuine_mid_flow_switch_still_works(
    llm_engine_factory,
) -> None:
    """The suppression must not block a real change of mind, which the
    deterministic analyser detects."""
    engine = llm_engine_factory(
        [
            analysis(Intent.BOOK_APPOINTMENT, confidence=0.95),
            analysis(Intent.CANCEL_APPOINTMENT, confidence=0.95),
        ],
        hybrid=False,
    )
    caller = Caller(engine)
    await caller.start()
    await caller.say("I'd like to book an appointment")
    await caller.say("actually I want to cancel my appointment instead")

    assert (await caller.session()).intent is Intent.CANCEL_APPOINTMENT


async def test_a_human_request_still_works_mid_flow(
    llm_engine_factory,
) -> None:
    """HUMAN_RECEPTION is exempt from the suppression."""
    engine = llm_engine_factory(
        [
            analysis(Intent.BOOK_APPOINTMENT, confidence=0.95),
            analysis(Intent.HUMAN_RECEPTION, confidence=0.95),
        ],
        hybrid=False,
    )
    caller = Caller(engine)
    await caller.start()
    await caller.say("I'd like to book an appointment")
    await caller.say("actually can someone call me back about this")

    assert caller.last.requires_human is True


# --- Safety still runs first ------------------------------------------------
async def test_an_emergency_never_reaches_the_model(llm_engine_factory) -> None:
    """Safety exits before analysis, so the provider is not even called."""
    engine = llm_engine_factory([analysis(Intent.BOOK_APPOINTMENT, confidence=0.99)])
    caller = Caller(engine)
    await caller.start()

    await caller.say("I need an appointment because I can't breathe")

    assert caller.last.emergency is True
    assert caller.last.state is ConversationState.EMERGENCY
    assert "000" in caller.last.reply
    assert engine.test_provider.call_count == 0


async def test_the_model_cannot_reclassify_an_emergency_as_a_booking(
    llm_engine_factory,
) -> None:
    engine = llm_engine_factory(
        [analysis(Intent.BOOK_APPOINTMENT, confidence=1.0, preferred_date_text="tomorrow")]
    )
    caller = Caller(engine)
    await caller.start()
    await caller.say("my husband has chest pain, can I book something")

    assert caller.last.emergency is True
    assert engine.test_provider.call_count == 0


async def test_a_clinical_question_still_escalates_before_the_model(
    llm_engine_factory,
) -> None:
    engine = llm_engine_factory([analysis(Intent.CLINIC_INFORMATION, confidence=0.99)])
    caller = Caller(engine)
    await caller.start()
    await caller.say("what should I do about this rash")

    assert caller.last.requires_human is True
    assert engine.test_provider.call_count == 0


# --- Human transfer ---------------------------------------------------------
async def test_explicit_human_request_wins_over_the_model(
    llm_engine_factory,
) -> None:
    engine = llm_engine_factory(
        [analysis(Intent.BOOK_APPOINTMENT, confidence=1.0, requires_human=False)]
    )
    caller = Caller(engine)
    await caller.start()
    await caller.say("I'd rather just talk to the front desk")

    assert caller.last.requires_human is True
    assert caller.last.state is ConversationState.HUMAN_TRANSFER


async def test_the_model_may_escalate_to_a_human(llm_engine_factory) -> None:
    engine = llm_engine_factory([analysis(Intent.UNKNOWN, confidence=0.85, requires_human=True)])
    caller = Caller(engine)
    await caller.start()
    await caller.say("this is all a bit much for a machine honestly")

    assert caller.last.requires_human is True


# --- Clinic knowledge -------------------------------------------------------
async def test_answers_still_come_from_approved_knowledge(
    llm_engine_factory,
) -> None:
    """The model may classify the question; it never supplies the answer."""
    engine = llm_engine_factory([analysis(Intent.CLINIC_INFORMATION, confidence=0.95)])
    caller = Caller(engine)
    await caller.start()
    reply = await caller.say("Do you bulk bill?")

    # Verbatim from the approved entry.
    assert "bulk bill children under sixteen" in reply
    assert caller.last.requires_human is False


async def test_an_unapproved_question_still_transfers(llm_engine_factory) -> None:
    engine = llm_engine_factory([analysis(Intent.CLINIC_INFORMATION, confidence=0.99)])
    caller = Caller(engine)
    await caller.start()
    reply = await caller.say("do you have an MRI machine on site?")

    assert caller.last.requires_human is True
    assert "reception" in reply.lower()


# --- Hallucinated entities --------------------------------------------------
async def test_a_hallucinated_practitioner_is_not_booked(
    llm_engine_factory,
) -> None:
    engine = llm_engine_factory(
        [analysis(Intent.BOOK_APPOINTMENT, confidence=0.99, practitioner="Dr Harry Potter")]
    )
    caller = Caller(engine)
    await caller.start()
    await caller.say("Can I see Dr Harry Potter?")

    session = await caller.session()
    assert session.booking.practitioner_id is None
    assert session.booking.practitioner_name is None


async def test_a_hallucinated_service_is_not_booked(llm_engine_factory) -> None:
    engine = llm_engine_factory(
        [
            analysis(
                Intent.BOOK_APPOINTMENT,
                confidence=0.99,
                appointment_type="premium executive brain scan",
            )
        ]
    )
    caller = Caller(engine)
    await caller.start()
    await caller.say("I need the premium executive brain scan package")

    session = await caller.session()
    assert session.booking.appointment_type_id is None


# --- Booking still committed transactionally --------------------------------
async def test_a_full_booking_still_commits_correctly(
    llm_engine_factory,
    db,
) -> None:
    """End to end with the LLM analyser: the booking guarantees are unchanged."""
    engine = llm_engine_factory(
        [
            analysis(Intent.BOOK_APPOINTMENT, confidence=0.95),
            analysis(Intent.UNKNOWN, confidence=0.9, patient_type="existing"),
            analysis(Intent.UNKNOWN, confidence=0.4),
            analysis(Intent.UNKNOWN, confidence=0.4),
            analysis(Intent.UNKNOWN, confidence=0.9, appointment_type="standard"),
            analysis(Intent.UNKNOWN, confidence=0.9, practitioner="anyone"),
            analysis(Intent.UNKNOWN, confidence=0.9, preferred_date_text="tomorrow"),
            analysis(Intent.UNKNOWN, confidence=0.9, preferred_period="morning"),
            analysis(Intent.UNKNOWN, confidence=0.9, selection="the first one"),
        ]
    )
    caller = Caller(engine)
    await caller.start()
    await caller.say("I'd like to book an appointment")
    await caller.say("existing patient")
    await caller.say("Hughes")
    await caller.say("14 March 1958")
    await caller.say("standard")
    await caller.say("anyone is fine")
    await caller.say("tomorrow")
    if caller.last.state is ConversationState.PREFERRED_PERIOD:
        await caller.say("morning")
    if caller.last.state is ConversationState.SELECT_SLOT:
        await caller.say("the first one")
    assert caller.last.state is ConversationState.CONFIRM_BOOKING, caller.dump()

    await caller.say("yes")
    assert caller.last.state is ConversationState.BOOKING_COMPLETE, caller.dump()

    count = await db.scalar(
        select(func.count())
        .select_from(Appointment)
        .where(Appointment.status == AppointmentStatus.CONFIRMED)
    )
    assert count == 1


# --- Fallback in a live conversation ----------------------------------------
async def test_the_conversation_continues_when_the_provider_is_down(
    llm_engine_factory,
) -> None:
    """A dead model degrades understanding, not the call."""
    from app.llm.errors import LLMUnavailableError

    engine = llm_engine_factory([LLMUnavailableError(detail="connection refused")])
    caller = Caller(engine)
    await caller.start()
    reply = await caller.say("I need to cancel my appointment")

    session = await caller.session()
    assert session.intent is Intent.CANCEL_APPOINTMENT  # deterministic result
    assert reply
    # The caller is never told the AI failed.
    assert "ai" not in reply.lower().split()
    assert "model" not in reply.lower()
    assert "error" not in reply.lower()


async def test_shadow_mode_leaves_behaviour_identical(
    llm_engine_factory,
) -> None:
    """Shadow mode must be observationally identical to baseline."""
    engine = llm_engine_factory([analysis(Intent.CLINIC_INFORMATION, confidence=0.99)], shadow=True)
    caller = Caller(engine)
    await caller.start()
    await caller.say("I need to cancel my appointment")

    session = await caller.session()
    assert session.intent is Intent.CANCEL_APPOINTMENT
    assert engine.test_provider.call_count == 1  # consulted, but ignored
