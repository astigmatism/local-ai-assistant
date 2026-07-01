from __future__ import annotations

from enum import StrEnum


class RuntimeState(StrEnum):
    IDLE = "waiting for the wake word"
    WAKE_DETECTED = "wake word detected"
    CAPTURING_PROMPT = "capturing a prompt"
    CHECKING_COMMAND = "checking for a local command"
    PROCESSING_STT = "processing STT"
    PROCESSING_LLM = "processing LLM"
    PROCESSING_TTS = "processing TTS"
    PLAYING_RESPONSE = "playing a response"
    HANDLING_ERROR = "handling an error"
    STOPPING = "stopping"


class SoundEvent(StrEnum):
    WAKE_ACK = "wake_ack"
    WAKE_NEW_CONVERSATION = "wake_new_conversation"
    INVALID_PROMPT = "invalid_prompt"
    PROMPT_ACCEPTED = "prompt_accepted"
    THINKING = "thinking"
    COMMAND_THINKING = "command_thinking"
    CANCEL_ACCEPTED = "cancel_accepted"
    NEW_CONVERSATION_ACCEPTED = "new_conversation_accepted"
    STT_FAILURE = "stt_failure"
    LLM_FAILURE = "llm_failure"
    TTS_FAILURE = "tts_failure"
    NETWORK_FAILURE = "network_failure"
    INTERNAL_FAILURE = "internal_failure"
    ADMIN_TEST = "admin_test"


class CommandIntent(StrEnum):
    CANCEL_STOP = "cancel_stop"
    NEW_CONVERSATION = "new_conversation"


class ArtifactKind(StrEnum):
    PROMPT_AUDIO = "prompt_audio"
    TTS_AUDIO = "tts_audio"
    ADMIN_MIC_TEST = "admin_mic_test"


class EventType(StrEnum):
    WAKE_DETECTED = "wake_detected"
    WAKE_ACK_PLAYBACK_STARTED = "wake_ack_playback_started"
    WAKE_ACK_PLAYBACK_ENDED = "wake_ack_playback_ended"
    WAKE_ACK_PLAYBACK_FAILED = "wake_ack_playback_failed"
    BARGE_IN = "barge_in"
    PROMPT_CAPTURE_STARTED = "prompt_capture_started"
    PROMPT_CAPTURE_ENDED = "prompt_capture_ended"
    COMMAND_RECOGNITION_STARTED = "command_recognition_started"
    COMMAND_RECOGNITION_RESULT = "command_recognition_result"
    COMMAND_ACCEPTED = "command_accepted"
    STT_STARTED = "stt_started"
    STT_RESULT = "stt_result"
    STT_FAILURE = "stt_failure"
    PROMPT_ACCEPTED = "prompt_accepted"
    LLM_STARTED = "llm_started"
    LLM_RESULT = "llm_result"
    LLM_FAILURE = "llm_failure"
    TTS_STARTED = "tts_started"
    TTS_RESULT = "tts_result"
    TTS_FAILURE = "tts_failure"
    PLAYBACK_STARTED = "playback_started"
    PLAYBACK_ENDED = "playback_ended"
    FAILURE = "failure"
    STATE_CHANGED = "state_changed"
    CANCELLATION = "cancellation"
    NEW_CONVERSATION = "new_conversation"
    CONVERSATION_TIMEOUT = "conversation_timeout"
    HEALTH = "health"
    ADMIN_TEST = "admin_test"
    CLEANUP = "cleanup"
    RESTART = "restart"
    REBOOT = "reboot"
    SOUND = "sound"
    CONFIG = "config"
