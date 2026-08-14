"""Questions about who works here and what can be booked.

Both answers come from live database rows rather than the knowledge file: they
change when a clinic hires someone or retires an appointment type, and they
must agree with what the booking flow offers moments later.
"""

from __future__ import annotations

import pytest

from app.conversation.states import ConversationState

pytestmark = pytest.mark.integration


async def test_who_are_your_doctors_names_them(caller, clinic_data) -> None:
    """Previously a knowledge miss, which transferred the caller to reception."""
    await caller.say("who are your doctors")

    reply = caller.last.reply
    assert caller.last.state is not ConversationState.HUMAN_TRANSFER, (
        f"transferred instead of answering: {reply!r}"
    )
    assert any(p.display_name.split()[-1] in reply for p in clinic_data.practitioners.values()), (
        f"no practitioner named in {reply!r}"
    )


async def test_a_named_practitioner_is_confirmed(caller, clinic_data) -> None:
    surname = next(iter(clinic_data.practitioners.values())).last_name
    await caller.say(f"do you have Dr {surname}")

    assert surname in caller.last.reply, caller.last.reply


async def test_what_appointments_can_i_book_lists_real_types(caller, clinic_data) -> None:
    await caller.say("what kinds of appointments do you have")

    reply = caller.last.reply
    assert caller.last.state is not ConversationState.HUMAN_TRANSFER, reply
    assert any(t.name.lower() in reply.lower() for t in clinic_data.types.values()), (
        f"no configured appointment type named in {reply!r}"
    )


async def test_internal_identifiers_are_never_spoken(caller, clinic_data) -> None:
    """Slugs and UUIDs are keys, not words a receptionist says."""
    await caller.say("what kinds of appointments do you have")
    reply = caller.last.reply.lower()

    for appointment_type in clinic_data.types.values():
        assert str(appointment_type.id) not in reply
        # A slug only leaks if it differs from the spoken name.
        if appointment_type.slug.lower() != appointment_type.name.lower():
            assert appointment_type.slug.lower() not in reply, f"slug leaked: {reply!r}"


async def test_asking_about_doctors_then_booking_keeps_context(caller) -> None:
    """The caller must not have to start again after a question."""
    await caller.say("who are your doctors")
    await caller.say("I'd like to book an appointment")

    assert caller.last.state is not ConversationState.HUMAN_TRANSFER, caller.dump()


# --- Choosing an appointment type -------------------------------------------
async def test_the_caller_is_told_what_can_be_booked(caller, clinic_data) -> None:
    """Asking "what kind?" without saying the kinds is how callers guess wrong."""
    await caller.say("I'd like to book an appointment")
    await caller.say("existing patient")
    await caller.say("Hughes")
    await caller.say("14 March 1958")

    reply = caller.last.reply
    assert any(t.name.lower() in reply.lower() for t in clinic_data.types.values()), (
        f"the configured appointment types were not offered: {reply!r}"
    )


async def test_an_unchosen_type_is_not_silently_defaulted(caller) -> None:
    """The old behaviour booked standard_gp and said nothing.

    With no CLINIC_DEFAULT_APPOINTMENT_TYPE configured the caller must be asked
    again rather than committed to something they never selected.
    """
    await caller.say("I'd like to book an appointment")
    await caller.say("existing patient")
    await caller.say("Hughes")
    await caller.say("14 March 1958")
    assert caller.last.state is ConversationState.APPOINTMENT_TYPE, caller.dump()

    await caller.say("mmm I'm not sure")

    session = await caller.session()
    assert session.booking.appointment_type_id is None, (
        "an appointment type was chosen for a caller who never picked one"
    )


async def test_an_explicitly_configured_default_is_used_and_is_a_real_slug(
    caller, settings, clinic_data
) -> None:
    """A clinic may configure a fallback; it must name a type that exists."""
    slugs = {t.slug for t in clinic_data.types.values()}
    assert settings.clinic_default_appointment_type in ("", *slugs), (
        "CLINIC_DEFAULT_APPOINTMENT_TYPE names a slug this clinic does not have"
    )
