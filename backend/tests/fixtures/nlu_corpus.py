"""Labelled evaluation corpus for NLU.

This *extends* the Milestone 1 corpus in ``tests/unit/test_intent_baseline.py``
rather than replacing it: that one stays as the regression guard for the
deterministic analyser, this one is the harder set both analysers are measured
against.

The phrasing is deliberately awkward — indirect requests, Australian
colloquialisms, fragments, and answers that only mean something in context.
Keyword matching struggles with exactly this, so it is where an LLM has to earn
its latency. Cases are labelled with the state they would realistically be
heard in, because "the second one" means different things in different states.

Every utterance is synthetic. No real caller text appears here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.conversation.states import Intent, PatientType


@dataclass(frozen=True, slots=True)
class Case:
    text: str
    intent: Intent
    state: str = "IDENTIFY_INTENT"
    #: Expected resolved entities. Only assert what the utterance really
    #: implies; None means "not checked".
    practitioner: str | None = None
    appointment_type: str | None = None
    #: Offset in days from "today" for the resolved date, when unambiguous.
    date_offset_days: int | None = None
    period: str | None = None
    patient_type: PatientType | None = None
    selection_ordinal: int | None = None
    #: Free-form grouping for the benchmark report.
    tags: tuple[str, ...] = field(default_factory=tuple)


# --- Booking ----------------------------------------------------------------
BOOKING: list[Case] = [
    Case("I need an appointment", Intent.BOOK_APPOINTMENT),
    Case("I'd like to book an appointment please", Intent.BOOK_APPOINTMENT),
    Case("Was hoping to get in sometime Friday.", Intent.BOOK_APPOINTMENT, tags=("indirect",)),
    Case(
        "Is Patel around tomorrow?",
        Intent.BOOK_APPOINTMENT,
        practitioner="Dr Sarah Patel",
        date_offset_days=1,
        tags=("indirect",),
    ),
    Case("I probably need to see someone this week.", Intent.BOOK_APPOINTMENT, tags=("indirect",)),
    Case(
        "Anything free after work?", Intent.BOOK_APPOINTMENT, period="EVENING", tags=("indirect",)
    ),
    Case("Can you squeeze me in?", Intent.BOOK_APPOINTMENT, tags=("indirect",)),
    Case(
        "Could I grab an appointment sometime after lunch?",
        Intent.BOOK_APPOINTMENT,
        period="AFTERNOON",
        tags=("indirect",),
    ),
    Case(
        "Any chance Patel's around Friday arvo?",
        Intent.BOOK_APPOINTMENT,
        practitioner="Dr Sarah Patel",
        period="AFTERNOON",
        tags=("casual", "australian"),
    ),
    Case(
        "Yeah look I just need to get in sometime next week.",
        Intent.BOOK_APPOINTMENT,
        tags=("casual",),
    ),
    Case("Was hoping someone could see me Friday.", Intent.BOOK_APPOINTMENT, tags=("indirect",)),
    Case("I want to see a doctor", Intent.BOOK_APPOINTMENT),
    Case("can I see Dr Patel on Friday", Intent.BOOK_APPOINTMENT, practitioner="Dr Sarah Patel"),
    Case(
        "anything available tomorrow afternoon",
        Intent.BOOK_APPOINTMENT,
        date_offset_days=1,
        period="AFTERNOON",
    ),
    Case("do you have any appointments free next week", Intent.BOOK_APPOINTMENT),
    Case("I need to come in and see the GP", Intent.BOOK_APPOINTMENT),
    Case("book me in for a check up", Intent.BOOK_APPOINTMENT),
    Case("I'd like to arrange a consultation", Intent.BOOK_APPOINTMENT),
    Case("Need to get the kids in for their jabs.", Intent.BOOK_APPOINTMENT, tags=("indirect",)),
    Case(
        "Could I see Dr Wilson for a longer appointment?",
        Intent.BOOK_APPOINTMENT,
        practitioner="Dr James Wilson",
        appointment_type="Long GP",
    ),
    Case(
        "I'd like a telehealth appointment please",
        Intent.BOOK_APPOINTMENT,
        appointment_type="Telehealth",
    ),
    Case("Do you have anything first thing Monday?", Intent.BOOK_APPOINTMENT, period="MORNING"),
    Case(
        "Can I get in to see Emily Chen next Thursday?",
        Intent.BOOK_APPOINTMENT,
        practitioner="Dr Emily Chen",
    ),
    Case("Looking to organise a skin check", Intent.BOOK_APPOINTMENT, tags=("indirect",)),
    Case(
        "Maybe after three if you've got anything.",
        Intent.BOOK_APPOINTMENT,
        period="AFTERNOON",
        tags=("casual", "fragment"),
    ),
]

# --- Rescheduling -----------------------------------------------------------
RESCHEDULING: list[Case] = [
    Case("I need to reschedule my appointment", Intent.RESCHEDULE_APPOINTMENT),
    Case("Something's come up Friday.", Intent.RESCHEDULE_APPOINTMENT, tags=("indirect",)),
    Case("I won't make the one I've got.", Intent.RESCHEDULE_APPOINTMENT, tags=("indirect",)),
    Case(
        "Can we push it back a couple of days?", Intent.RESCHEDULE_APPOINTMENT, tags=("indirect",)
    ),
    Case("Can I shift my booking to next week?", Intent.RESCHEDULE_APPOINTMENT),
    Case("I'd rather come in later.", Intent.RESCHEDULE_APPOINTMENT, tags=("indirect",)),
    Case("can I move my appointment", Intent.RESCHEDULE_APPOINTMENT),
    Case("I can't make Friday anymore", Intent.RESCHEDULE_APPOINTMENT),
    Case("can I change the time of my appointment", Intent.RESCHEDULE_APPOINTMENT),
    Case("I need a different day", Intent.RESCHEDULE_APPOINTMENT),
    Case("could we push my appointment back", Intent.RESCHEDULE_APPOINTMENT),
    Case(
        "Something's come up and Friday won't work anymore.",
        Intent.RESCHEDULE_APPOINTMENT,
        tags=("indirect",),
    ),
    Case(
        "I can't do the booking I've got anymore.", Intent.RESCHEDULE_APPOINTMENT, tags=("casual",)
    ),
    Case(
        "Is there any way to swap my appointment to the morning?",
        Intent.RESCHEDULE_APPOINTMENT,
        period="MORNING",
    ),
    Case("Need to move what I've got booked.", Intent.RESCHEDULE_APPOINTMENT, tags=("casual",)),
]

# --- Cancellation -----------------------------------------------------------
CANCELLATION: list[Case] = [
    Case("I need to cancel my appointment", Intent.CANCEL_APPOINTMENT),
    Case("cancel my appointment tomorrow", Intent.CANCEL_APPOINTMENT, date_offset_days=1),
    Case("please cancel it", Intent.CANCEL_APPOINTMENT),
    Case(
        "I don't think I'll need that booking anymore.",
        Intent.CANCEL_APPOINTMENT,
        tags=("indirect",),
    ),
    Case(
        "Can you take me off the list for tomorrow?",
        Intent.CANCEL_APPOINTMENT,
        date_offset_days=1,
        tags=("indirect",),
    ),
    Case("I won't be coming in.", Intent.CANCEL_APPOINTMENT, tags=("indirect",)),
    Case("Please get rid of my appointment.", Intent.CANCEL_APPOINTMENT),
    Case("I don't need the appointment anymore", Intent.CANCEL_APPOINTMENT),
    Case(
        "I don't think I'll be needing the one tomorrow.",
        Intent.CANCEL_APPOINTMENT,
        date_offset_days=1,
        tags=("indirect",),
    ),
    Case("Scrap the booking please.", Intent.CANCEL_APPOINTMENT, tags=("casual",)),
]

# --- Clinic information -----------------------------------------------------
INFORMATION: list[Case] = [
    Case("what time do you open", Intent.CLINIC_INFORMATION),
    Case("what are your opening hours", Intent.CLINIC_INFORMATION),
    Case("do you bulk bill", Intent.CLINIC_INFORMATION),
    Case("how much is a consultation", Intent.CLINIC_INFORMATION),
    Case("where are you located", Intent.CLINIC_INFORMATION),
    Case("is there parking", Intent.CLINIC_INFORMATION),
    Case("do you offer telehealth", Intent.CLINIC_INFORMATION),
    Case("what's your cancellation policy", Intent.CLINIC_INFORMATION),
    Case("Whereabouts are you?", Intent.CLINIC_INFORMATION, tags=("casual", "australian")),
    Case("Do I have to pay?", Intent.CLINIC_INFORMATION, tags=("indirect",)),
    Case("Are you guys open late?", Intent.CLINIC_INFORMATION, tags=("casual",)),
    Case("Where can I park?", Intent.CLINIC_INFORMATION),
    Case("Do you see patients Saturday?", Intent.CLINIC_INFORMATION),
    Case("Does the clinic do phone consultations?", Intent.CLINIC_INFORMATION),
    Case("Whereabouts are you guys?", Intent.CLINIC_INFORMATION, tags=("casual",)),
    Case("Am I able to get a referral there?", Intent.CLINIC_INFORMATION, tags=("indirect",)),
]

# --- Human transfer ---------------------------------------------------------
HUMAN: list[Case] = [
    Case("let me speak to someone", Intent.HUMAN_RECEPTION),
    Case("I want reception", Intent.HUMAN_RECEPTION),
    Case("can I talk to a person", Intent.HUMAN_RECEPTION),
    Case("put me through to a human", Intent.HUMAN_RECEPTION),
    Case("I'd like to speak to a receptionist", Intent.HUMAN_RECEPTION),
    Case("Can I just talk to the front desk?", Intent.HUMAN_RECEPTION, tags=("indirect",)),
    Case("I'd rather speak with somebody.", Intent.HUMAN_RECEPTION, tags=("indirect",)),
    Case("Can you put reception on?", Intent.HUMAN_RECEPTION, tags=("casual",)),
    Case("I don't want to use the automated system.", Intent.HUMAN_RECEPTION, tags=("indirect",)),
    Case("I'd rather deal with the front desk.", Intent.HUMAN_RECEPTION, tags=("indirect",)),
    Case("I don't want to talk to a robot", Intent.HUMAN_RECEPTION, tags=("indirect",)),
]

# --- End call ---------------------------------------------------------------
END_CALL: list[Case] = [
    Case("no thanks that's all", Intent.END_CALL, state="BOOKING_COMPLETE"),
    Case("nothing else", Intent.END_CALL, state="BOOKING_COMPLETE"),
    Case("goodbye", Intent.END_CALL, state="BOOKING_COMPLETE"),
    Case("That's it cheers.", Intent.END_CALL, state="BOOKING_COMPLETE", tags=("casual",)),
    Case("All good thanks.", Intent.END_CALL, state="BOOKING_COMPLETE", tags=("casual",)),
    Case("Nope that's everything.", Intent.END_CALL, state="BOOKING_COMPLETE", tags=("casual",)),
    Case("I'm done.", Intent.END_CALL, state="BOOKING_COMPLETE", tags=("casual",)),
    Case(
        "That'll do me, thanks.",
        Intent.END_CALL,
        state="BOOKING_COMPLETE",
        tags=("casual", "australian"),
    ),
    Case(
        "That's all mate, cheers.",
        Intent.END_CALL,
        state="BOOKING_COMPLETE",
        tags=("casual", "australian"),
    ),
]

# --- Corrections ------------------------------------------------------------
# Heard mid-flow, so the state matters. Intent normally stays the task intent;
# what these must get right is the *entity*.
CORRECTIONS: list[Case] = [
    Case("Actually Thursday.", Intent.BOOK_APPOINTMENT, state="SELECT_SLOT", tags=("correction",)),
    Case("No sorry, Friday.", Intent.BOOK_APPOINTMENT, state="SELECT_SLOT", tags=("correction",)),
    Case(
        "I meant Patel.",
        Intent.BOOK_APPOINTMENT,
        state="SELECT_SLOT",
        practitioner="Dr Sarah Patel",
        tags=("correction",),
    ),
    Case(
        "Sorry, I meant Dr Wilson.",
        Intent.BOOK_APPOINTMENT,
        state="SELECT_SLOT",
        practitioner="Dr James Wilson",
        tags=("correction",),
    ),
    Case(
        "Not morning, afternoon.",
        Intent.BOOK_APPOINTMENT,
        state="SELECT_SLOT",
        period="AFTERNOON",
        tags=("correction",),
    ),
    Case(
        "Can we do afternoon instead?",
        Intent.BOOK_APPOINTMENT,
        state="SELECT_SLOT",
        period="AFTERNOON",
        tags=("correction",),
    ),
    Case(
        "Nah actually make it Thursday.",
        Intent.BOOK_APPOINTMENT,
        state="SELECT_SLOT",
        tags=("correction", "casual"),
    ),
    Case(
        "No wait, tomorrow would be better.",
        Intent.BOOK_APPOINTMENT,
        state="SELECT_SLOT",
        date_offset_days=1,
        tags=("correction",),
    ),
]

# --- Selection --------------------------------------------------------------
SELECTION: list[Case] = [
    Case(
        "The second one.",
        Intent.BOOK_APPOINTMENT,
        state="SELECT_SLOT",
        selection_ordinal=2,
        tags=("selection",),
    ),
    Case(
        "the first one",
        Intent.BOOK_APPOINTMENT,
        state="SELECT_SLOT",
        selection_ordinal=1,
        tags=("selection",),
    ),
    Case(
        "I'll take the later one.",
        Intent.BOOK_APPOINTMENT,
        state="SELECT_SLOT",
        selection_ordinal=-1,
        tags=("selection",),
    ),
    Case(
        "The second time you said.",
        Intent.BOOK_APPOINTMENT,
        state="SELECT_SLOT",
        selection_ordinal=2,
        tags=("selection",),
    ),
    Case(
        "Whichever is earliest.",
        Intent.BOOK_APPOINTMENT,
        state="SELECT_SLOT",
        selection_ordinal=1,
        tags=("selection",),
    ),
    Case(
        "Earliest you've got.",
        Intent.BOOK_APPOINTMENT,
        state="SELECT_SLOT",
        selection_ordinal=1,
        tags=("selection",),
    ),
]

# --- Patient type -----------------------------------------------------------
PATIENT_TYPE: list[Case] = [
    Case(
        "I've been there before.",
        Intent.UNKNOWN,
        state="PATIENT_TYPE",
        patient_type=PatientType.EXISTING,
        tags=("state_dependent",),
    ),
    Case(
        "I've been before",
        Intent.UNKNOWN,
        state="PATIENT_TYPE",
        patient_type=PatientType.EXISTING,
        tags=("state_dependent",),
    ),
    Case(
        "Never been to your clinic.",
        Intent.UNKNOWN,
        state="PATIENT_TYPE",
        patient_type=PatientType.NEW,
        tags=("state_dependent",),
    ),
    Case(
        "I'm a new patient",
        Intent.UNKNOWN,
        state="PATIENT_TYPE",
        patient_type=PatientType.NEW,
        tags=("state_dependent",),
    ),
    Case(
        "Existing.",
        Intent.UNKNOWN,
        state="PATIENT_TYPE",
        patient_type=PatientType.EXISTING,
        tags=("fragment",),
    ),
    Case(
        "First time actually.",
        Intent.UNKNOWN,
        state="PATIENT_TYPE",
        patient_type=PatientType.NEW,
        tags=("fragment",),
    ),
]

# --- Practitioner preference ------------------------------------------------
PRACTITIONER_PREFERENCE: list[Case] = [
    Case("Anyone is fine.", Intent.UNKNOWN, state="PRACTITIONER", tags=("fragment",)),
    Case("Whoever's free first is fine.", Intent.UNKNOWN, state="PRACTITIONER", tags=("casual",)),
    Case(
        "Dr Patel please.",
        Intent.UNKNOWN,
        state="PRACTITIONER",
        practitioner="Dr Sarah Patel",
        tags=("fragment",),
    ),
    Case(
        "Wilson if he's about.",
        Intent.UNKNOWN,
        state="PRACTITIONER",
        practitioner="Dr James Wilson",
        tags=("casual",),
    ),
    Case("No preference really.", Intent.UNKNOWN, state="PRACTITIONER", tags=("fragment",)),
]

# --- Ambiguous fragments ----------------------------------------------------
# Short answers to a question the agent just asked. Intent is genuinely
# UNKNOWN; the value is entirely in the entity.
AMBIGUOUS: list[Case] = [
    Case("Friday.", Intent.UNKNOWN, state="PREFERRED_DATE", tags=("fragment",)),
    Case(
        "Afternoon.",
        Intent.UNKNOWN,
        state="PREFERRED_PERIOD",
        period="AFTERNOON",
        tags=("fragment",),
    ),
    Case("Maybe next week.", Intent.UNKNOWN, state="PREFERRED_DATE", tags=("fragment",)),
    Case(
        "Anything after lunch.",
        Intent.UNKNOWN,
        state="PREFERRED_PERIOD",
        period="AFTERNOON",
        tags=("fragment",),
    ),
]

# --- Register / tone --------------------------------------------------------
# Same intents, different register: formal, blunt, hesitant, frustrated but not
# abusive. A classifier that only handles neutral phrasing fails these.
REGISTER: list[Case] = [
    Case(
        "Good morning, I would like to arrange an appointment please.",
        Intent.BOOK_APPOINTMENT,
        tags=("formal",),
    ),
    Case("I wish to cancel a scheduled appointment.", Intent.CANCEL_APPOINTMENT, tags=("formal",)),
    Case(
        "Would it be possible to move my appointment to another day?",
        Intent.RESCHEDULE_APPOINTMENT,
        tags=("formal",),
    ),
    Case("Could you advise your consultation fees?", Intent.CLINIC_INFORMATION, tags=("formal",)),
    Case(
        "Appointment. Tomorrow.",
        Intent.BOOK_APPOINTMENT,
        date_offset_days=1,
        tags=("blunt", "fragment"),
    ),
    Case("Cancel. Tomorrow's one.", Intent.CANCEL_APPOINTMENT, date_offset_days=1, tags=("blunt",)),
    Case("Reception. Now please.", Intent.HUMAN_RECEPTION, tags=("blunt",)),
    Case(
        "Um, I was just, yeah, wondering about booking something.",
        Intent.BOOK_APPOINTMENT,
        tags=("hesitant",),
    ),
    Case(
        "Sorry, um, I need to change my booking I think.",
        Intent.RESCHEDULE_APPOINTMENT,
        tags=("hesitant",),
    ),
    Case(
        "This is the third time I've called. I just want to book something.",
        Intent.BOOK_APPOINTMENT,
        tags=("frustrated",),
    ),
    Case(
        "Look, I've been on hold forever, can I just book an appointment.",
        Intent.BOOK_APPOINTMENT,
        tags=("frustrated",),
    ),
    Case(
        "Honestly I just want to talk to an actual human being.",
        Intent.HUMAN_RECEPTION,
        tags=("frustrated",),
    ),
    Case(
        "G'day, after an appointment with Patel if that's alright.",
        Intent.BOOK_APPOINTMENT,
        practitioner="Dr Sarah Patel",
        tags=("casual", "australian"),
    ),
    Case(
        "Righto, that's me sorted. Thanks.",
        Intent.END_CALL,
        state="BOOKING_COMPLETE",
        tags=("casual", "australian"),
    ),
    Case("Cheers, appreciate it.", Intent.END_CALL, state="BOOKING_COMPLETE", tags=("casual",)),
]

# --- Multi-entity in one sentence -------------------------------------------
# The payoff case for slot filling: one sentence should fill several fields.
MULTI_ENTITY: list[Case] = [
    Case(
        "Can I get a standard appointment with Dr Patel next Friday afternoon?",
        Intent.BOOK_APPOINTMENT,
        practitioner="Dr Sarah Patel",
        appointment_type="Standard GP",
        period="AFTERNOON",
        tags=("multi",),
    ),
    Case(
        "I'm a new patient and I'd like to see Dr Wilson tomorrow morning.",
        Intent.BOOK_APPOINTMENT,
        practitioner="Dr James Wilson",
        date_offset_days=1,
        period="MORNING",
        patient_type=PatientType.NEW,
        tags=("multi",),
    ),
    Case(
        "Existing patient, long appointment with Emily Chen please.",
        Intent.BOOK_APPOINTMENT,
        practitioner="Dr Emily Chen",
        appointment_type="Long GP",
        patient_type=PatientType.EXISTING,
        tags=("multi",),
    ),
    Case(
        "Telehealth with Patel tomorrow if possible.",
        Intent.BOOK_APPOINTMENT,
        practitioner="Dr Sarah Patel",
        appointment_type="Telehealth",
        date_offset_days=1,
        tags=("multi",),
    ),
    Case(
        "Need to move my Friday booking to Wilson instead.",
        Intent.RESCHEDULE_APPOINTMENT,
        practitioner="Dr James Wilson",
        tags=("multi",),
    ),
    Case(
        "Book me with Dr Chen, afternoon, any day next week.",
        Intent.BOOK_APPOINTMENT,
        practitioner="Dr Emily Chen",
        period="AFTERNOON",
        tags=("multi",),
    ),
]

# --- Unknown entities -------------------------------------------------------
# The intent is fine; the entity must resolve to nothing. Asserting no
# practitioner here is the point — see the hallucination tests.
UNKNOWN_ENTITIES: list[Case] = [
    Case("Can I see Dr Harry Potter?", Intent.BOOK_APPOINTMENT, tags=("hallucination",)),
    Case(
        "I need the premium executive brain scan package.",
        Intent.BOOK_APPOINTMENT,
        tags=("hallucination",),
    ),
    Case("Is Dr Nonexistent available Friday?", Intent.BOOK_APPOINTMENT, tags=("hallucination",)),
    Case("Do you do full body MRI screening?", Intent.CLINIC_INFORMATION, tags=("hallucination",)),
]

ALL_CASES: list[Case] = [
    *BOOKING,
    *RESCHEDULING,
    *CANCELLATION,
    *INFORMATION,
    *HUMAN,
    *END_CALL,
    *CORRECTIONS,
    *SELECTION,
    *PATIENT_TYPE,
    *PRACTITIONER_PREFERENCE,
    *AMBIGUOUS,
    *REGISTER,
    *MULTI_ENTITY,
    *UNKNOWN_ENTITIES,
]

#: Cases where the intent label is meaningful. Fragments answering a question
#: ("Friday.", "Existing.") carry no intent of their own, so scoring an
#: analyser on them would measure noise rather than understanding.
INTENT_CASES: list[Case] = [c for c in ALL_CASES if c.intent is not Intent.UNKNOWN]

#: Cases asserting at least one resolved entity.
ENTITY_CASES: list[Case] = [
    c
    for c in ALL_CASES
    if any(
        (
            c.practitioner,
            c.appointment_type,
            c.date_offset_days is not None,
            c.period,
            c.patient_type,
            c.selection_ordinal is not None,
        )
    )
]
