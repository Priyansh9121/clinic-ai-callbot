"""The call must stay up after the greeting and hear the caller's first words.

From the second live call to extension 700: the greeting was audible, and then
the application hung up. `voice.session_ended turns=0`, `telephony.hungup
cause=APPLICATION`, seven seconds end to end. The caller never got to speak.

This drives the same shape over the real media transport with fake speech
models: greet, hold the line silent for a moment, then talk.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from app.telephony.destinations import DestinationRegistry, TransferDestination
from app.telephony.manager import CallSession
from app.voice.states import SessionEndReason
from tests.fixtures.fake_telephony import FakeAsterisk, FakeCallController, wire_silence, wire_tone
from tests.telephony.test_call_lifecycle import voice_factory


@pytest.fixture
def engine(db, settings, session_store, clinic_data):
    from app.clinic.knowledge_data import build_dev_knowledge
    from app.conversation.engine import ConversationEngine
    from app.conversation.nlu.baseline import BaselineAnalyser
    from app.safety.engine import SafetyEngine

    return ConversationEngine(
        db=db,
        settings=settings,
        store=session_store,
        safety=SafetyEngine(),
        analyser=BaselineAnalyser(),
        knowledge=build_dev_knowledge(),
    )


pytestmark = pytest.mark.integration


@pytest.fixture
def destinations() -> DestinationRegistry:
    return DestinationRegistry(
        {TransferDestination.RECEPTION: "100", TransferDestination.VOICEMAIL: "101"},
        context="clinic-transfer",
    )


async def test_the_call_survives_the_greeting_and_hears_the_caller(engine, destinations) -> None:
    """The live failure, end to end.

    Greeting plays, the caller is briefly silent — as a real caller is while
    listening to it — and then speaks. That first utterance must become a turn.
    """
    pbx = FakeAsterisk()
    await pbx.start()
    try:
        channel = await pbx.connect()
        controller = FakeCallController()
        build = voice_factory(engine, stt_text="I'd like to book an appointment", greet=True)
        session = CallSession(
            call_id="call-greeting",
            controller=controller,
            channel=channel,
            voice_session_factory=build,
            destinations=destinations,
            hangup_drain_ms=0,
        )
        task = asyncio.create_task(session.run())
        await pbx.hold_line()  # a real line delivers silence continuously

        # A real caller listens to the greeting before speaking.
        await asyncio.sleep(1.2)
        assert not task.done(), "the call ended during the greeting"

        await pbx.send_audio(wire_tone(400) + wire_silence(500))
        await asyncio.sleep(1.5)

        deadline = asyncio.get_running_loop().time() + 8.0
        while session.stats.turns < 1 and asyncio.get_running_loop().time() < deadline:
            if task.done():
                break
            await asyncio.sleep(0.2)

        assert session.stats.turns >= 1, (
            f"the caller spoke and no turn completed; call still running={not task.done()}"
        )
    finally:
        controller.caller_hangs_up()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5.0)
        await pbx.stop()


async def test_silence_after_the_greeting_does_not_end_the_call(engine, destinations) -> None:
    """A caller who says nothing yet is still a caller.

    Well inside SILENCE_TERMINATE_SECONDS, the session must stay up rather than
    hanging up on someone who is still deciding what to say.
    """
    pbx = FakeAsterisk()
    await pbx.start()
    try:
        channel = await pbx.connect()
        controller = FakeCallController()
        build = voice_factory(engine, greet=True)
        session = CallSession(
            call_id="call-silence",
            controller=controller,
            channel=channel,
            voice_session_factory=build,
            destinations=destinations,
            hangup_drain_ms=0,
        )
        task = asyncio.create_task(session.run())
        await pbx.hold_line()  # a real line delivers silence continuously

        await asyncio.sleep(3.0)

        assert not task.done(), "three seconds of silence ended the call"
    finally:
        controller.caller_hangs_up()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5.0)
        await pbx.stop()


async def test_a_caller_hangup_is_reported_as_such(engine, destinations) -> None:
    """The end reason must name the caller, not the media layer.

    Both end the frame source; only one of them is the caller leaving.
    """
    pbx = FakeAsterisk()
    await pbx.start()
    try:
        channel = await pbx.connect()
        controller = FakeCallController()
        build = voice_factory(engine, greet=True)
        session = CallSession(
            call_id="call-hangup-reason",
            controller=controller,
            channel=channel,
            voice_session_factory=build,
            destinations=destinations,
            hangup_drain_ms=0,
        )
        task = asyncio.create_task(session.run())
        await pbx.hold_line()  # a real line delivers silence continuously
        await asyncio.sleep(0.6)
        controller.caller_hangs_up()
        await asyncio.wait_for(task, timeout=8.0)
    finally:
        await pbx.stop()

    voice = build.made.get("session")
    assert voice is not None
    assert voice._end_reason is SessionEndReason.STOP_REQUESTED


async def test_media_eof_is_reported_as_a_caller_hangup(engine, destinations) -> None:
    """The socket closing IS the caller leaving, and is labelled that way.

    CallSession wires the channel's hangup callback to _on_caller_hangup, so an
    EOF stops the voice session through stop() rather than by starving the
    frame generator. That distinction matters for reading a live log: an EOF
    produces `telephony.caller_hangup` BEFORE `voice.session_ended`, whereas
    the frame source ending on its own produces it after.

    In the live 700 call the caller_hangup came *after*, which is how we know
    the socket had not closed when the session gave up.
    """
    pbx = FakeAsterisk()
    await pbx.start()
    try:
        channel = await pbx.connect()
        controller = FakeCallController()
        build = voice_factory(engine, greet=True)
        session = CallSession(
            call_id="call-media-eof",
            controller=controller,
            channel=channel,
            voice_session_factory=build,
            destinations=destinations,
            hangup_drain_ms=0,
        )
        task = asyncio.create_task(session.run())
        await pbx.hold_line()
        await asyncio.sleep(0.4)

        await pbx.hang_up()  # socket EOF, no controller callback
        await asyncio.wait_for(task, timeout=10.0)
    finally:
        await pbx.stop()

    voice = build.made.get("session")
    assert voice is not None
    assert voice._end_reason is SessionEndReason.STOP_REQUESTED


async def test_the_end_reason_records_the_first_cause_not_the_last(engine, destinations) -> None:
    """Teardown must not relabel why the call ended.

    A caller hangup stops the input, which then also looks like the frame
    source ending — and the second fact would otherwise overwrite the first.
    """
    pbx = FakeAsterisk()
    await pbx.start()
    try:
        channel = await pbx.connect()
        controller = FakeCallController()
        build = voice_factory(engine, greet=True)
        session = CallSession(
            call_id="call-first-reason",
            controller=controller,
            channel=channel,
            voice_session_factory=build,
            destinations=destinations,
            hangup_drain_ms=0,
        )
        task = asyncio.create_task(session.run())
        await pbx.hold_line()
        await asyncio.sleep(0.4)

        controller.caller_hangs_up()
        await asyncio.sleep(0.1)
        await pbx.hang_up()  # media EOF arrives second, as it does live
        await asyncio.wait_for(task, timeout=10.0)
    finally:
        await pbx.stop()

    voice = build.made.get("session")
    assert voice._end_reason is SessionEndReason.STOP_REQUESTED, (
        "the caller hangup was relabelled by the media EOF that followed it"
    )
