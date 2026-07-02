from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from voice_assistant.clients import ServiceError
from voice_assistant.constants import EventType, RuntimeState, SoundEvent
from voice_assistant.telemetry import utc_now
from voice_assistant.wake import WakeDetection


def detection():
    return WakeDetection("Rosalina", 0.99, "simulated", 0.0)


@pytest.mark.asyncio
async def test_normal_prompt_flow_stt_llm_tts_playback_and_context(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    audio.command_texts = [None]
    stt.outputs = ["what is the weather"]
    llm.outputs = ["It is sunny."]

    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    assert runtime.state.state == RuntimeState.IDLE
    assert len(stt.calls) == 1
    assert llm.messages[0][-1] == {"role": "user", "content": "what is the weather"}
    assert tts.inputs == ["It is sunny."]
    assert any(call == ("play_sound_event", str(SoundEvent.WAKE_NEW_CONVERSATION)) for call in audio.calls)
    assert not any(call == ("play_sound_event", str(SoundEvent.PROMPT_ACCEPTED)) for call in audio.calls)
    assert telemetry.query_events(event_type=str(EventType.PROMPT_ACCEPTED))[0].data["sound_playback"] == "suppressed_during_processing_feedback"
    thinking_stop_index = [i for i, call in enumerate(audio.calls) if call[0] == "thinking_stop"][-1]
    playback_index = [i for i, call in enumerate(audio.calls) if call[0] == "play_file"][-1]
    assert thinking_stop_index < playback_index
    events = telemetry.query_events()
    event_types = {event.event_type for event in events}
    sequence = _event_sequence(telemetry)
    assert sequence.index(str(EventType.PROMPT_CAPTURE_ENDED)) < sequence.index(str(EventType.COMMAND_RECOGNITION_STARTED))
    assert sequence.index(str(EventType.COMMAND_RECOGNITION_STARTED)) < sequence.index(str(EventType.STT_STARTED))
    assert sequence.index(str(EventType.STT_RESULT)) < sequence.index(str(EventType.COMMAND_RECOGNITION_RESULT))
    assert str(EventType.WAKE_DETECTED) in event_types
    assert str(EventType.STT_RESULT) in event_types
    assert str(EventType.LLM_RESULT) in event_types
    assert str(EventType.TTS_RESULT) in event_types
    assert str(EventType.PLAYBACK_ENDED) in event_types


@pytest.mark.asyncio
async def test_first_wake_without_context_uses_new_conversation_wake_acknowledgement(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    audio.command_texts = ["stop"]

    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    assert ("play_sound_event", str(SoundEvent.WAKE_NEW_CONVERSATION)) in audio.calls
    assert ("play_sound_event", str(SoundEvent.WAKE_ACK)) not in audio.calls
    wake_event = telemetry.query_events(event_type=str(EventType.WAKE_DETECTED))[0]
    assert wake_event.data["sound_event"] == SoundEvent.WAKE_NEW_CONVERSATION.value
    assert wake_event.data["conversation_context_active"] is False


@pytest.mark.asyncio
async def test_wake_with_active_context_uses_continuing_wake_acknowledgement(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    audio.command_texts = [None]
    stt.outputs = ["remember this"]
    llm.outputs = ["I will remember it."]

    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    audio.calls.clear()
    audio.command_texts = ["stop"]
    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    assert ("play_sound_event", str(SoundEvent.WAKE_ACK)) in audio.calls
    assert ("play_sound_event", str(SoundEvent.WAKE_NEW_CONVERSATION)) not in audio.calls
    latest_wake = telemetry.query_events(event_type=str(EventType.WAKE_DETECTED))[0]
    assert latest_wake.data["sound_event"] == SoundEvent.WAKE_ACK.value
    assert latest_wake.data["conversation_context_active"] is True


@pytest.mark.asyncio
async def test_wake_after_conversation_timeout_uses_new_conversation_wake_acknowledgement(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    cfg = store.get_saved().public_dict()
    cfg["conversation"]["inactivity_timeout_seconds"] = 0.5
    store.apply_config(cfg)
    runtime.conversation.add_user("old prompt")
    runtime.conversation.add_assistant("old answer")
    runtime.conversation.mark_response_finished(utc_now() - timedelta(seconds=5))
    old_conversation_id = runtime.conversation.conversation_id
    audio.command_texts = ["stop"]

    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    assert runtime.conversation.conversation_id != old_conversation_id
    assert ("play_sound_event", str(SoundEvent.WAKE_NEW_CONVERSATION)) in audio.calls
    assert ("play_sound_event", str(SoundEvent.WAKE_ACK)) not in audio.calls


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

    command_event = str(SoundEvent.COMMAND_THINKING)
    thinking_event = str(SoundEvent.THINKING)
    command_starts = _call_indices(audio.calls, ("loop_requested", command_event))
    command_stops = _call_indices(audio.calls, ("loop_stop", command_event))
    invalid_prompt_start = _first_call_index(audio.calls, ("play_sound_event_start", str(SoundEvent.INVALID_PROMPT)))

    assert len(command_starts) == 1
    assert len(command_stops) == 1
    assert command_starts[0] < _first_call_named(audio.calls, "stt_start")
    assert _first_call_named(audio.calls, "stt_end") < command_stops[0] < invalid_prompt_start
    assert _call_indices(audio.calls, ("loop_requested", thinking_event)) == []
    assert _call_indices(audio.calls, ("loop_stop", thinking_event)) == []


@pytest.mark.parametrize("phrase", ["stop", "Stop.", "cancel", "Cancel.", "forget it", "Forget it.", "never mind", "Never mind!", " Stop. ", "CANCEL"])
@pytest.mark.asyncio
async def test_cancel_command_is_local_and_preserves_context(bundle_parts, phrase):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    runtime.conversation.add_user("previous")
    old_id = runtime.conversation.conversation_id
    old_messages = list(runtime.conversation.messages_for_llm())
    audio.command_texts = [phrase]

    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    assert runtime.state.state == RuntimeState.IDLE
    assert runtime.conversation.conversation_id == old_id
    assert runtime.conversation.messages_for_llm() == old_messages
    assert stt.calls
    assert llm.messages == []
    assert tts.inputs == []
    assert any(call == ("play_sound_event", str(SoundEvent.CANCEL_ACCEPTED)) for call in audio.calls)
    command_events = telemetry.query_events(event_type=str(EventType.COMMAND_ACCEPTED))
    assert command_events[0].command_intent == "cancel_stop"
    result_events = telemetry.query_events(event_type=str(EventType.COMMAND_RECOGNITION_RESULT))
    assert result_events[0].data["matched"] is True
    assert result_events[0].data["route"] == "local_command"


@pytest.mark.parametrize("phrase", ["How do I stop a Linux service?", "How do I cancel a process in Linux?"])
@pytest.mark.asyncio
async def test_longer_prompts_with_command_words_are_not_local_commands(bundle_parts, phrase):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    audio.command_texts = [phrase]
    stt.outputs = [phrase]
    llm.outputs = ["normal answer"]

    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    assert stt.calls
    assert llm.messages[0][-1] == {"role": "user", "content": phrase}
    assert tts.inputs == ["normal answer"]
    assert not telemetry.query_events(event_type=str(EventType.COMMAND_ACCEPTED))
    result_events = telemetry.query_events(event_type=str(EventType.COMMAND_RECOGNITION_RESULT))
    assert result_events[0].data["matched"] is False
    assert result_events[0].data["route"] == "normal_llm"


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

    new_id = runtime.conversation.conversation_id
    assert new_id != old_id
    assert stt.calls and tts.inputs == ["Fresh answer."]
    record_count = len([call for call in audio.calls if call[0] == "record_prompt_start"])
    continuing_wake_count = len([call for call in audio.calls if call == ("play_sound_event", str(SoundEvent.WAKE_ACK))])
    new_conversation_wake_count = len([call for call in audio.calls if call == ("play_sound_event", str(SoundEvent.WAKE_NEW_CONVERSATION))])

    assert record_count == 2
    assert continuing_wake_count == 1
    assert new_conversation_wake_count == 1
    assert ("play_sound_event", str(SoundEvent.NEW_CONVERSATION_ACCEPTED)) not in audio.calls
    assert len(llm.messages) == 1
    assert llm.messages[0] == [
        {"role": "system", "content": store.get_active().conversation.system_prompt},
        {"role": "user", "content": "fresh prompt"},
    ]
    assert "old context" not in str(llm.messages[0])
    assert "new conversation" not in str(llm.messages[0])

    reset_events = telemetry.query_events(event_type=str(EventType.NEW_CONVERSATION))
    assert reset_events[0].conversation_id == new_id
    assert reset_events[0].data["previous_conversation_id"] == old_id
    assert reset_events[0].data["wake_acknowledgement_sound_event"] == SoundEvent.WAKE_NEW_CONVERSATION.value
    assert reset_events[0].data["command_acknowledgement_sound_event_suppressed"] == SoundEvent.NEW_CONVERSATION_ACCEPTED.value


@pytest.mark.asyncio
async def test_new_conversation_wake_acknowledgement_and_capture_overlap_but_command_routing_waits(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    runtime.conversation.add_user("old context")
    audio.command_texts = ["start over", "stop"]
    audio.block_wake_ack_events = [str(SoundEvent.WAKE_NEW_CONVERSATION)]

    await runtime.on_wake_detected(detection())
    await _wait_until(lambda: len([call for call in audio.calls if call[0] == "record_prompt_start"]) >= 2)

    ack_start_index = _first_call_index(audio.calls, ("play_sound_event_start", str(SoundEvent.WAKE_NEW_CONVERSATION)))
    prompt_starts = [i for i, call in enumerate(audio.calls) if call[0] == "record_prompt_start"]
    second_prompt_start = prompt_starts[1]
    command_thinking_starts = _call_indices(audio.calls, ("loop_requested", str(SoundEvent.COMMAND_THINKING)))

    assert ack_start_index < second_prompt_start
    assert ("play_sound_event_end", str(SoundEvent.WAKE_NEW_CONVERSATION)) not in audio.calls
    assert len(command_thinking_starts) == 1
    assert ("play_sound_event", str(SoundEvent.NEW_CONVERSATION_ACCEPTED)) not in audio.calls

    audio.allow_wake_ack_finish.set()
    await runtime.wait_until_idle()

    ack_end_index = _first_call_index(audio.calls, ("play_sound_event_end", str(SoundEvent.WAKE_NEW_CONVERSATION)))
    command_thinking_starts = _call_indices(audio.calls, ("loop_requested", str(SoundEvent.COMMAND_THINKING)))
    assert second_prompt_start < ack_end_index < command_thinking_starts[1]
    assert llm.messages == []
    assert tts.inputs == []


@pytest.mark.asyncio
async def test_empty_new_conversation_wake_acknowledgement_still_captures_next_prompt(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    cfg = store.get_saved().public_dict()
    cfg["sounds"]["event_files"][SoundEvent.WAKE_NEW_CONVERSATION.value] = ""
    store.apply_config(cfg)
    runtime.conversation.add_user("old context")
    audio.command_texts = ["new chat", None]
    stt.outputs = ["fresh prompt"]
    llm.outputs = ["Fresh answer."]

    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    assert len([call for call in audio.calls if call[0] == "record_prompt_start"]) == 2
    assert ("play_sound_event", str(SoundEvent.WAKE_NEW_CONVERSATION)) in audio.calls
    assert ("play_sound_event", str(SoundEvent.NEW_CONVERSATION_ACCEPTED)) not in audio.calls
    assert llm.messages[0] == [
        {"role": "system", "content": store.get_active().conversation.system_prompt},
        {"role": "user", "content": "fresh prompt"},
    ]


@pytest.mark.asyncio
async def test_new_conversation_wake_acknowledgement_event_falls_back_to_wake_ack_when_missing(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    cfg = store.get_active()
    cfg.sounds.event_files.pop(SoundEvent.WAKE_NEW_CONVERSATION)

    assert runtime._new_conversation_wake_acknowledgement_event(cfg) == SoundEvent.WAKE_ACK


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

    thinking_event = str(SoundEvent.THINKING)
    thinking_starts = _call_indices(audio.calls, ("loop_requested", thinking_event))
    thinking_stops = _call_indices(audio.calls, ("loop_stop", thinking_event))
    failure_start = _first_call_index(audio.calls, ("play_sound_event_start", str(SoundEvent.LLM_FAILURE)))

    assert len(thinking_starts) == 1
    assert len(thinking_stops) == 1
    assert _first_call_named(audio.calls, "stt_end") < thinking_starts[0] < _first_call_named(audio.calls, "llm_start")
    assert _first_call_named(audio.calls, "llm_error") < thinking_stops[0] < failure_start


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
    assert len([call for call in audio.calls if call == ("play_sound_event", str(SoundEvent.WAKE_NEW_CONVERSATION))]) == 1
    assert len([call for call in audio.calls if call == ("play_sound_event", str(SoundEvent.WAKE_ACK))]) == 1
    events = telemetry.query_events(event_type=str(EventType.BARGE_IN))
    assert events and "cancelling" in events[0].human_message


@pytest.mark.asyncio
async def test_barge_in_followed_by_cancel_is_handled_locally_without_second_llm_request(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    audio.command_texts = [None, "cancel"]
    stt.outputs = ["first prompt"]
    llm.outputs = ["first answer"]
    audio.block_playback = True

    await runtime.on_wake_detected(detection())
    await asyncio.wait_for(audio.playback_started.wait(), timeout=3)
    assert runtime.state.state == RuntimeState.PLAYING_RESPONSE

    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    assert audio.stop_called is True
    assert len(stt.calls) == 2
    assert len(llm.messages) == 1
    assert tts.inputs == ["first answer"]
    assert any(call == ("play_sound_event", str(SoundEvent.CANCEL_ACCEPTED)) for call in audio.calls)
    assert telemetry.query_events(event_type=str(EventType.BARGE_IN))
    command_events = telemetry.query_events(event_type=str(EventType.COMMAND_ACCEPTED))
    assert command_events[0].command_intent == "cancel_stop"
    assert runtime.state.state == RuntimeState.IDLE


@pytest.mark.asyncio
async def test_wake_during_prompt_capture_is_not_valid_new_wake(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    runtime.state.set_state(RuntimeState.CAPTURING_PROMPT)

    await runtime.on_wake_detected(detection())

    assert runtime._current_task is None
    events = telemetry.query_events(event_type=str(EventType.WAKE_DETECTED))
    assert events[0].data["ignored_during_capture"] is True


@pytest.mark.asyncio
async def test_stt_first_command_routing_runs_only_after_wake_and_prompt_capture(bundle_parts):
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
    stt_start_index = _first_call_named(audio.calls, "stt_start")
    assert prompt_start_index < prompt_end_index < stt_start_index
    assert stt.calls


async def _wait_until(predicate, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not satisfied before timeout")
        await asyncio.sleep(0.01)


def _first_call_index(calls, expected):
    return next(i for i, call in enumerate(calls) if call == expected)


def _call_indices(calls, expected):
    return [i for i, call in enumerate(calls) if call == expected]


def _first_call_named(calls, name):
    return next(i for i, call in enumerate(calls) if call[0] == name)


def _event_sequence(telemetry):
    return [event.event_type for event in reversed(telemetry.query_events())]

class RaisingCommandRecognizer:
    async def recognize(self, audio_path, registry, hinted_text=None):
        raise RuntimeError("command recognizer unavailable")


class SlowOnceCommandRecognizer:
    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = False
        self.calls = 0

    async def recognize(self, audio_path, registry, hinted_text=None):
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return None


@pytest.mark.asyncio
async def test_command_interpretation_uses_command_thinking_before_normal_processing(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    audio.command_texts = [None]
    stt.outputs = ["ordinary prompt"]
    llm.outputs = ["ordinary response"]

    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    command_event = str(SoundEvent.COMMAND_THINKING)
    thinking_event = str(SoundEvent.THINKING)
    command_start = _first_call_index(audio.calls, ("loop_requested", command_event))
    command_stop = _first_call_index(audio.calls, ("loop_stop", command_event))
    normal_thinking_start = _first_call_index(audio.calls, ("loop_requested", thinking_event))

    assert command_start < command_stop < normal_thinking_start
    assert stt.calls
    command_started = telemetry.query_events(event_type=str(EventType.COMMAND_RECOGNITION_STARTED))[0]
    command_result = telemetry.query_events(event_type=str(EventType.COMMAND_RECOGNITION_RESULT))[0]
    assert command_started.data["sound_event"] == SoundEvent.COMMAND_THINKING.value
    assert command_result.data["sound_event"] == SoundEvent.COMMAND_THINKING.value


@pytest.mark.asyncio
async def test_normal_processing_thinking_is_single_continuous_loop_across_stt_llm_tts(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    audio.command_texts = [None]
    stt.outputs = ["ordinary prompt"]
    llm.outputs = ["ordinary response"]

    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    command_event = str(SoundEvent.COMMAND_THINKING)
    thinking_event = str(SoundEvent.THINKING)
    command_start = _first_call_index(audio.calls, ("loop_requested", command_event))
    command_stop = _first_call_index(audio.calls, ("loop_stop", command_event))
    thinking_start = _first_call_index(audio.calls, ("loop_requested", thinking_event))
    thinking_stop = _first_call_index(audio.calls, ("loop_stop", thinking_event))
    playback_start = _first_call_named(audio.calls, "play_file")

    assert _call_indices(audio.calls, ("loop_requested", thinking_event)) == [thinking_start]
    assert _call_indices(audio.calls, ("loop_stop", thinking_event)) == [thinking_stop]
    assert command_start < _first_call_named(audio.calls, "stt_start")
    assert _first_call_named(audio.calls, "stt_end") < command_stop < thinking_start
    assert thinking_start < _first_call_named(audio.calls, "llm_start")
    assert _first_call_named(audio.calls, "llm_end") < _first_call_named(audio.calls, "tts_start")
    assert _first_call_named(audio.calls, "tts_end") < thinking_stop < playback_start
    assert thinking_stop > _first_call_named(audio.calls, "llm_start")
    assert thinking_stop > _first_call_named(audio.calls, "tts_start")
    assert not any(call == ("play_sound_event", str(SoundEvent.PROMPT_ACCEPTED)) for call in audio.calls)

    prompt_accepted = telemetry.query_events(event_type=str(EventType.PROMPT_ACCEPTED))[0]
    assert prompt_accepted.data["sound_playback"] == "suppressed_during_processing_feedback"


@pytest.mark.asyncio
async def test_stt_error_stops_thinking_before_failure_sound(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    audio.command_texts = [None]
    stt.exc = ServiceError("STT failed")

    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    command_event = str(SoundEvent.COMMAND_THINKING)
    thinking_event = str(SoundEvent.THINKING)
    command_starts = _call_indices(audio.calls, ("loop_requested", command_event))
    command_stops = _call_indices(audio.calls, ("loop_stop", command_event))
    failure_start = _first_call_index(audio.calls, ("play_sound_event_start", str(SoundEvent.STT_FAILURE)))

    assert runtime.state.state == RuntimeState.IDLE
    assert llm.messages == []
    assert tts.inputs == []
    assert len(command_starts) == 1
    assert len(command_stops) == 1
    assert command_starts[0] < _first_call_named(audio.calls, "stt_start")
    assert _first_call_named(audio.calls, "stt_error") < command_stops[0] < failure_start
    assert _call_indices(audio.calls, ("loop_requested", thinking_event)) == []
    assert _call_indices(audio.calls, ("loop_stop", thinking_event)) == []


@pytest.mark.asyncio
async def test_tts_failure_keeps_thinking_until_failure_sound(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    audio.command_texts = [None]
    stt.outputs = ["hello"]
    llm.outputs = ["response"]
    tts.exc = ServiceError("TTS failed")

    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    thinking_event = str(SoundEvent.THINKING)
    thinking_starts = _call_indices(audio.calls, ("loop_requested", thinking_event))
    thinking_stops = _call_indices(audio.calls, ("loop_stop", thinking_event))
    failure_start = _first_call_index(audio.calls, ("play_sound_event_start", str(SoundEvent.TTS_FAILURE)))

    assert runtime.state.state == RuntimeState.IDLE
    assert len(thinking_starts) == 1
    assert len(thinking_stops) == 1
    assert _first_call_named(audio.calls, "stt_end") < thinking_starts[0] < _first_call_named(audio.calls, "llm_start")
    assert _first_call_named(audio.calls, "llm_end") < _first_call_named(audio.calls, "tts_start")
    assert _first_call_named(audio.calls, "tts_error") < thinking_stops[0] < failure_start
    assert not any(call[0] == "play_file" for call in audio.calls)


@pytest.mark.parametrize(
    ("phase", "state_name", "client_attr", "started_attr", "cancelled_attr"),
    [
        ("stt", RuntimeState.PROCESSING_STT, "stt", "started", "cancelled"),
        ("llm", RuntimeState.PROCESSING_LLM, "llm", "started", "cancelled"),
        ("tts", RuntimeState.PROCESSING_TTS, "tts", "started", "cancelled"),
    ],
)
@pytest.mark.asyncio
async def test_barge_in_during_processing_stops_normal_thinking(
    bundle_parts,
    phase,
    state_name,
    client_attr,
    started_attr,
    cancelled_attr,
):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    clients = {"stt": stt, "llm": llm, "tts": tts}
    blocked_client = clients[client_attr]
    blocked_client.block_calls = 1
    audio.command_texts = [None, None]
    stt.outputs = ["first prompt", "second prompt"]
    llm.outputs = ["first answer", "second answer"]

    await runtime.on_wake_detected(detection())
    await asyncio.wait_for(getattr(blocked_client, started_attr).wait(), timeout=3)
    assert runtime.state.state == state_name

    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    sound_event = str(SoundEvent.COMMAND_THINKING if phase == "stt" else SoundEvent.THINKING)
    loop_start = _first_call_index(audio.calls, ("loop_requested", sound_event))
    loop_stop = _first_call_index(audio.calls, ("loop_stop", sound_event))
    stop_all_index = _first_call_index(audio.calls, ("stop_all_playback", None))

    assert getattr(blocked_client, cancelled_attr) is True
    assert loop_start < stop_all_index < loop_stop
    if phase == "stt":
        assert ("loop_requested", str(SoundEvent.THINKING)) not in audio.calls[:stop_all_index]
    assert len([call for call in audio.calls if call == ("play_sound_event", str(SoundEvent.WAKE_NEW_CONVERSATION))]) == 1
    assert len([call for call in audio.calls if call == ("play_sound_event", str(SoundEvent.WAKE_ACK))]) == 1
    assert telemetry.query_events(event_type=str(EventType.BARGE_IN))
    assert runtime.state.state == RuntimeState.IDLE


@pytest.mark.asyncio
async def test_command_thinking_stops_before_command_acknowledgement(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    audio.command_texts = ["stop"]

    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    command_stop = _first_call_index(audio.calls, ("loop_stop", str(SoundEvent.COMMAND_THINKING)))
    acknowledgement_start = _first_call_index(audio.calls, ("play_sound_event_start", str(SoundEvent.CANCEL_ACCEPTED)))

    assert command_stop < acknowledgement_start
    assert ("loop_requested", str(SoundEvent.THINKING)) not in audio.calls
    assert stt.calls


@pytest.mark.asyncio
async def test_empty_command_thinking_sound_keeps_command_processing_working(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    cfg = store.get_saved().public_dict()
    cfg["sounds"]["event_files"][SoundEvent.COMMAND_THINKING.value] = ""
    store.apply_config(cfg)
    audio.command_texts = ["stop"]

    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    assert runtime.state.state == RuntimeState.IDLE
    assert stt.calls
    assert llm.messages == []
    assert any(call == ("play_sound_event", str(SoundEvent.CANCEL_ACCEPTED)) for call in audio.calls)
    result_events = telemetry.query_events(event_type=str(EventType.COMMAND_RECOGNITION_RESULT))
    assert result_events[0].data["route"] == "local_command"


@pytest.mark.asyncio
async def test_persisted_pocketsphinx_command_recognizer_config_does_not_block_stt_first_routing(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    cfg = store.get_saved().public_dict()
    cfg["command_registry"]["recognizer"]["engine"] = "pocketsphinx"
    cfg["command_registry"]["recognizer"]["pocketsphinx_command"] = ["definitely-missing-pocketsphinx"]
    store.apply_config(cfg)
    runtime.command_recognizer = RaisingCommandRecognizer()
    audio.command_texts = ["Cancel."]

    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    assert runtime.state.state == RuntimeState.IDLE
    assert len(stt.calls) == 1
    assert llm.messages == []
    assert tts.inputs == []
    result_events = telemetry.query_events(event_type=str(EventType.COMMAND_RECOGNITION_RESULT))
    assert result_events[0].data["route"] == "local_command"
    started_events = telemetry.query_events(event_type=str(EventType.COMMAND_RECOGNITION_STARTED))
    assert started_events[0].data["configured_legacy_recognizer_engine"] == "pocketsphinx"


@pytest.mark.asyncio
async def test_legacy_audio_command_recognizer_is_not_called_in_default_runtime_path(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    runtime.command_recognizer = RaisingCommandRecognizer()
    stt.outputs = ["fallback prompt"]
    llm.outputs = ["fallback response"]

    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    command_stop = _first_call_index(audio.calls, ("loop_stop", str(SoundEvent.COMMAND_THINKING)))
    normal_thinking_start = _first_call_index(audio.calls, ("loop_requested", str(SoundEvent.THINKING)))

    assert command_stop < normal_thinking_start
    assert stt.calls
    assert llm.messages[0][-1] == {"role": "user", "content": "fallback prompt"}
    assert tts.inputs == ["fallback response"]
    assert telemetry.query_events(errors_only=True) == []
    result_events = telemetry.query_events(event_type=str(EventType.COMMAND_RECOGNITION_RESULT))
    assert result_events[0].data["route"] == "normal_llm"


@pytest.mark.asyncio
async def test_barge_in_during_command_routing_stt_stops_command_thinking(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    stt.block_calls = 1
    stt.outputs = ["after barge in"]
    llm.outputs = ["after barge in response"]

    await runtime.on_wake_detected(detection())
    await asyncio.wait_for(stt.started.wait(), timeout=3)
    assert runtime.state.state == RuntimeState.PROCESSING_STT

    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    command_starts = [i for i, call in enumerate(audio.calls) if call == ("loop_requested", str(SoundEvent.COMMAND_THINKING))]
    command_stops = [i for i, call in enumerate(audio.calls) if call == ("loop_stop", str(SoundEvent.COMMAND_THINKING))]
    stop_all_index = _first_call_index(audio.calls, ("stop_all_playback", None))

    assert stt.cancelled is True
    assert command_starts and command_stops
    assert command_starts[0] < stop_all_index < command_stops[0]
    assert telemetry.query_events(event_type=str(EventType.BARGE_IN))


@pytest.mark.asyncio
async def test_wake_ack_playback_is_invoked_before_prompt_capture_starts(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    audio.command_texts = ["stop"]

    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    ack_start_index = _first_call_index(audio.calls, ("play_sound_event_start", str(SoundEvent.WAKE_NEW_CONVERSATION)))
    ack_end_index = _first_call_index(audio.calls, ("play_sound_event_end", str(SoundEvent.WAKE_NEW_CONVERSATION)))
    prompt_start_index = next(i for i, call in enumerate(audio.calls) if call[0] == "record_prompt_start")
    assert ack_start_index < ack_end_index < prompt_start_index

    events = _event_sequence(telemetry)
    assert events.index(str(EventType.WAKE_DETECTED)) < events.index(str(EventType.WAKE_ACK_PLAYBACK_STARTED))
    assert events.index(str(EventType.WAKE_ACK_PLAYBACK_STARTED)) < events.index(str(EventType.WAKE_ACK_PLAYBACK_ENDED))
    assert events.index(str(EventType.WAKE_ACK_PLAYBACK_ENDED)) < events.index(str(EventType.PROMPT_CAPTURE_STARTED))


@pytest.mark.asyncio
async def test_prompt_capture_waits_for_wake_ack_awaitable_to_complete(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    audio.command_texts = ["stop"]
    audio.block_wake_ack = True

    await runtime.on_wake_detected(detection())
    await asyncio.wait_for(audio.wake_ack_started.wait(), timeout=3)
    await asyncio.sleep(0.03)

    assert not any(call[0] == "record_prompt_start" for call in audio.calls)
    assert runtime.state.state == RuntimeState.WAKE_DETECTED

    audio.allow_wake_ack_finish.set()
    await runtime.wait_until_idle()

    ack_end_index = _first_call_index(audio.calls, ("play_sound_event_end", str(SoundEvent.WAKE_NEW_CONVERSATION)))
    prompt_start_index = next(i for i, call in enumerate(audio.calls) if call[0] == "record_prompt_start")
    assert ack_end_index < prompt_start_index


@pytest.mark.asyncio
async def test_wake_ack_playback_failure_is_logged_and_prompt_capture_still_starts(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    audio.command_texts = ["stop"]
    audio.fail_wake_ack = True

    await runtime.on_wake_detected(detection())
    await runtime.wait_until_idle()

    failure_events = telemetry.query_events(event_type=str(EventType.WAKE_ACK_PLAYBACK_FAILED))
    assert len(failure_events) == 1
    assert failure_events[0].success is False
    assert "wake ack playback failed" in failure_events[0].error
    failure_index = _first_call_index(audio.calls, ("play_sound_event_failed", str(SoundEvent.WAKE_NEW_CONVERSATION)))
    prompt_start_index = next(i for i, call in enumerate(audio.calls) if call[0] == "record_prompt_start")
    assert failure_index < prompt_start_index


@pytest.mark.asyncio
async def test_long_wake_ack_does_not_consume_prompt_capture_timer_window(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    cfg = store.get_saved().public_dict()
    cfg["prompt_capture"]["minimum_duration_seconds"] = 4.0
    cfg["prompt_capture"]["maximum_duration_seconds"] = 9.0
    cfg["prompt_capture"]["silence_duration_seconds"] = 1.25
    store.apply_config(cfg)
    audio.command_texts = ["stop"]
    audio.block_wake_ack = True

    await runtime.on_wake_detected(detection())
    await asyncio.wait_for(audio.wake_ack_started.wait(), timeout=3)
    await asyncio.sleep(0.05)

    assert telemetry.query_events(event_type=str(EventType.PROMPT_CAPTURE_STARTED)) == []

    audio.allow_wake_ack_finish.set()
    await runtime.wait_until_idle()

    events = _event_sequence(telemetry)
    assert events.index(str(EventType.WAKE_ACK_PLAYBACK_ENDED)) < events.index(str(EventType.PROMPT_CAPTURE_STARTED))
    prompt_started = telemetry.query_events(event_type=str(EventType.PROMPT_CAPTURE_STARTED))[0]
    assert prompt_started.data["minimum_duration_seconds"] == 4.0
    assert prompt_started.data["maximum_duration_seconds"] == 9.0
    assert prompt_started.data["silence_duration_seconds"] == 1.25


@pytest.mark.asyncio
async def test_admin_simulated_wake_uses_acknowledgement_before_capture(bundle_parts):
    store, telemetry, runtime, audio, stt, llm, tts = bundle_parts
    audio.command_texts = ["stop"]
    audio.block_wake_ack = True

    detection_result = await runtime.simulate_wake(confidence=0.42)
    assert detection_result.engine == "admin_simulated"
    await asyncio.wait_for(audio.wake_ack_started.wait(), timeout=3)
    await asyncio.sleep(0.03)

    assert not any(call[0] == "record_prompt_start" for call in audio.calls)

    audio.allow_wake_ack_finish.set()
    await runtime.wait_until_idle()

    ack_end_index = _first_call_index(audio.calls, ("play_sound_event_end", str(SoundEvent.WAKE_NEW_CONVERSATION)))
    prompt_start_index = next(i for i, call in enumerate(audio.calls) if call[0] == "record_prompt_start")
    assert ack_end_index < prompt_start_index
    wake_events = telemetry.query_events(event_type=str(EventType.WAKE_DETECTED))
    assert any(event.data.get("engine") == "admin_simulated" for event in wake_events)
