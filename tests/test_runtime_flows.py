from __future__ import annotations

import asyncio

import pytest

from voice_assistant.clients import ServiceError
from voice_assistant.constants import EventType, RuntimeState, SoundEvent
from voice_assistant.wake import WakeDetection


def detection():
    return WakeDetection("computer", 0.99, "simulated", 0.0)


@pytest.mark.asyncio
async def test_normal_prompt_flow_stt_llm_tts_playback_and_context(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    audio.command_texts = [None]
    stt.outputs = ["what is the weather"]
    llm.outputs = ["It is sunny."]

    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    assert runtime.state.state == RuntimeState.IDLE
    assert stt.calls
    assert llm.messages[0][-1] == {"role": "user", "content": "what is the weather"}
    assert tts.inputs == ["It is sunny."]
    assert any(call == ("play_sound_event", str(SoundEvent.WAKE_ACK)) for call in audio.calls)
    assert any(call == ("play_sound_event", str(SoundEvent.PROMPT_ACCEPTED)) for call in audio.calls)
    thinking_stop_index = [i for i, call in enumerate(audio.calls) if call[0] == "thinking_stop"][-1]
    playback_index = [i for i, call in enumerate(audio.calls) if call[0] == "play_file"][-1]
    assert thinking_stop_index < playback_index
    events = telemetry.query_events()
    event_types = {event.event_type for event in events}
    assert str(EventType.WAKE_DETECTED) in event_types
    assert str(EventType.STT_RESULT) in event_types
    assert str(EventType.LLM_RESULT) in event_types
    assert str(EventType.TTS_RESULT) in event_types
    assert str(EventType.PLAYBACK_ENDED) in event_types


@pytest.mark.asyncio
async def test_invalid_prompt_when_stt_returns_no_text(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    audio.command_texts = [None]
    stt.outputs = [""]

    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    assert runtime.state.state == RuntimeState.IDLE
    assert llm.messages == []
    assert any(call == ("play_sound_event", str(SoundEvent.INVALID_PROMPT)) for call in audio.calls)
    assert len(runtime.conversation.messages_for_llm()) == 1


@pytest.mark.asyncio
async def test_cancel_command_is_local_and_preserves_context(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    runtime.conversation.add_user("previous")
    old_id = runtime.conversation.conversation_id
    audio.command_texts = ["stop"]

    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    assert runtime.conversation.conversation_id == old_id
    assert stt.calls == []
    assert any(call == ("play_sound_event", str(SoundEvent.CANCEL_ACCEPTED)) for call in audio.calls)
    command_events = telemetry.query_events(event_type=str(EventType.COMMAND_ACCEPTED))
    assert command_events[0].command_intent == "cancel_stop"


@pytest.mark.asyncio
async def test_new_conversation_command_clears_context_and_immediately_captures_next_prompt(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    runtime.conversation.add_user("old context")
    old_id = runtime.conversation.conversation_id
    audio.command_texts = ["new conversation", None]
    stt.outputs = ["fresh prompt"]
    llm.outputs = ["Fresh answer."]

    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    assert runtime.conversation.conversation_id != old_id
    assert stt.calls and tts.inputs == ["Fresh answer."]
    record_count = len([call for call in audio.calls if call[0] == "record_prompt_start"])
    wake_count = len([call for call in audio.calls if call == ("play_sound_event", str(SoundEvent.WAKE_ACK))])
    assert record_count == 2
    assert wake_count == 1
    assert any(call == ("play_sound_event", str(SoundEvent.NEW_CONVERSATION_ACCEPTED)) for call in audio.calls)


@pytest.mark.asyncio
async def test_llm_failure_stops_thinking_plays_failure_and_preserves_context(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    audio.command_texts = [None]
    llm.exc = ServiceError("LLM failed")
    stt.outputs = ["hello"]
    old_id = runtime.conversation.conversation_id

    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    assert runtime.state.state == RuntimeState.IDLE
    assert runtime.conversation.conversation_id == old_id
    assert any(call == ("play_sound_event", str(SoundEvent.LLM_FAILURE)) for call in audio.calls)
    errors = telemetry.query_events(errors_only=True)
    assert errors and "LLM" in errors[0].human_message


@pytest.mark.asyncio
async def test_barge_in_during_playback_cancels_and_starts_new_capture(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    audio.command_texts = [None, None]
    stt.outputs = ["first", "second"]
    llm.outputs = ["first answer", "second answer"]
    audio.block_playback = True

    await runtime.on_wake_detected(detection())
    await asyncio.wait_for(audio.playback_started.wait(), timeout=3)
    assert runtime.state.state == RuntimeState.PLAYING_RESPONSE

    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    assert audio.stop_called is True
    assert len(stt.calls) == 2
    events = telemetry.query_events(event_type=str(EventType.BARGE_IN))
    assert events and "cancelling" in events[0].human_message


@pytest.mark.asyncio
async def test_wake_during_prompt_capture_is_not_valid_new_wake(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    runtime.state.set_state(RuntimeState.CAPTURING_PROMPT)

    await runtime.on_wake_detected(detection())

    assert runtime._current_task is None
    events = telemetry.query_events(event_type=str(EventType.WAKE_DETECTED))
    assert events[0].data["ignored_during_capture"] is True


@pytest.mark.asyncio
async def test_local_command_recognizer_runs_only_after_wake_and_prompt_capture(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    assert telemetry.query_events(event_type=str(EventType.COMMAND_RECOGNITION_STARTED)) == []
    assert audio.calls == []

    audio.command_texts = ["stop"]
    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    events = telemetry.query_events(event_type=str(EventType.COMMAND_RECOGNITION_STARTED))
    assert len(events) == 1
    prompt_start_index = [i for i, call in enumerate(audio.calls) if call[0] == "record_prompt_start"][0]
    prompt_end_index = [i for i, call in enumerate(audio.calls) if call[0] == "record_prompt_end"][0]
    assert prompt_start_index < prompt_end_index
    assert stt.calls == []
