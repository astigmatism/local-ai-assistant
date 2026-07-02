from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from typing import Callable

from .audio import AudioController, CaptureResult, LoopingSoundHandle
from .clients import (
    LLMClient,
    MalformedServiceResponse,
    NetworkServiceError,
    STTClient,
    ServiceAuthError,
    ServiceError,
    TTSClient,
)
from .commands import CommandMatch, CommandRegistry, LocalCommandRecognizer
from .config import AssistantConfig, ConfigStore
from .constants import ArtifactKind, CommandIntent, EventType, RuntimeState, SoundEvent
from .conversation import ConversationManager
from .state import StateManager
from .telemetry import TelemetryStore, utc_now
from .wake import SimulatedWakeWordEngine, WakeDetection, WakeWordEngine, build_wake_engine


STTFactory = Callable[[AssistantConfig], STTClient]
LLMFactory = Callable[[AssistantConfig], LLMClient]
TTSFactory = Callable[[AssistantConfig], TTSClient]


class AssistantRuntime:
    def __init__(
        self,
        config_store: ConfigStore,
        telemetry: TelemetryStore,
        *,
        audio: AudioController | None = None,
        command_recognizer: LocalCommandRecognizer | None = None,
        wake_engine: WakeWordEngine | None = None,
        stt_factory: STTFactory | None = None,
        llm_factory: LLMFactory | None = None,
        tts_factory: TTSFactory | None = None,
    ):
        self.config_store = config_store
        self.telemetry = telemetry
        cfg = self.config_store.get_active()
        self.audio = audio or AudioController()
        # Retained for backwards-compatible injection/diagnostics only; the default runtime
        # command path now routes through configured STT and CommandRegistry.match_text.
        self.command_recognizer = command_recognizer
        self.wake_engine = wake_engine or build_wake_engine(cfg)
        self.stt_factory = stt_factory or (lambda c: STTClient(c.services.stt))
        self.llm_factory = llm_factory or (lambda c: LLMClient(c.services.llm))
        self.tts_factory = tts_factory or (lambda c: TTSClient(c.services.tts))
        self.state = StateManager(telemetry)
        self.conversation = ConversationManager(
            cfg.conversation.system_prompt,
            cfg.conversation.inactivity_timeout_seconds,
        )
        self._wake_stop_event = asyncio.Event()
        self._wake_task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._active_cancel_event: asyncio.Event | None = None
        self._current_task: asyncio.Task[None] | None = None
        self._interaction_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._wake_task and not self._wake_task.done():
            return
        self._wake_stop_event = asyncio.Event()
        self._wake_task = asyncio.create_task(self.wake_engine.run(self.on_wake_detected, self._wake_stop_event))
        self._wake_task.add_done_callback(self._wake_task_finished)
        self._cleanup_task = asyncio.create_task(self._cleanup_scheduler_loop())
        self.state.set_state(RuntimeState.IDLE)

    def _wake_task_finished(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        self.telemetry.log_event(
            EventType.FAILURE,
            "Wake-word listener stopped unexpectedly.",
            component="wake",
            success=False,
            error=str(exc),
        )

    async def stop(self) -> None:
        self._wake_stop_event.set()
        await self._cancel_current("service stopping")
        if self._wake_task:
            self._wake_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._wake_task
        if self._cleanup_task:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task


    async def _cleanup_scheduler_loop(self) -> None:
        while not self._wake_stop_event.is_set():
            cfg = self.config_store.get_active()
            try:
                hour, minute = [int(part) for part in cfg.telemetry.cleanup_time_of_day.split(":", 1)]
            except Exception:
                hour, minute = 3, 0
            now = utc_now()
            next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if next_run <= now:
                from datetime import timedelta

                next_run = next_run + timedelta(days=1)
            wait_seconds = max(1.0, (next_run - now).total_seconds())
            try:
                await asyncio.wait_for(self._wake_stop_event.wait(), timeout=wait_seconds)
                continue
            except asyncio.TimeoutError:
                pass
            active = self.config_store.get_active()
            result = self.telemetry.cleanup_older_than(active.telemetry.retention_days)
            self.telemetry.log_event(
                EventType.CLEANUP,
                "Scheduled telemetry/artifact cleanup completed.",
                component="maintenance",
                success=True,
                data=result,
            )

    async def simulate_wake(self, confidence: float = 1.0) -> WakeDetection:
        cfg = self.config_store.get_active()
        if isinstance(self.wake_engine, SimulatedWakeWordEngine):
            return await self.wake_engine.trigger(confidence=confidence, phrase=cfg.wake.active_wake_phrase)
        detection = WakeDetection(
            phrase=cfg.wake.active_wake_phrase,
            confidence=confidence,
            engine="admin_simulated",
            timestamp_monotonic=time.monotonic(),
        )
        await self.on_wake_detected(detection)
        return detection

    async def on_wake_detected(self, detection: WakeDetection) -> None:
        async with self._interaction_lock:
            current_state = self.state.state
            force_continuing_wake_ack = False
            if current_state == RuntimeState.CAPTURING_PROMPT:
                self.telemetry.log_event(
                    EventType.WAKE_DETECTED,
                    "Wake phrase heard during prompt capture and treated as prompt audio, not as a new wake event.",
                    state=current_state.value,
                    data={"phrase": detection.phrase, "confidence": detection.confidence, "ignored_during_capture": True},
                )
                return
            if current_state in {
                RuntimeState.PROCESSING_STT,
                RuntimeState.PROCESSING_LLM,
                RuntimeState.PROCESSING_TTS,
                RuntimeState.PLAYING_RESPONSE,
                RuntimeState.WAKE_DETECTED,
                RuntimeState.CHECKING_COMMAND,
            }:
                force_continuing_wake_ack = True
                self.telemetry.log_event(
                    EventType.BARGE_IN,
                    "Wake word detected during active processing/playback; cancelling current process and starting a new capture.",
                    state=current_state.value,
                    data={"phrase": detection.phrase, "confidence": detection.confidence, "engine": detection.engine},
                )
                await self._cancel_current("barge-in")
            await self._start_interaction_task(
                detection,
                play_wake_sound=True,
                force_continuing_wake_ack=force_continuing_wake_ack,
            )

    async def _start_interaction_task(
        self,
        detection: WakeDetection | None,
        *,
        play_wake_sound: bool,
        force_continuing_wake_ack: bool = False,
    ) -> None:
        interaction_id = str(uuid.uuid4())
        task = asyncio.create_task(
            self._interaction_flow(
                interaction_id,
                detection,
                play_wake_sound=play_wake_sound,
                force_continuing_wake_ack=force_continuing_wake_ack,
            )
        )
        self._current_task = task

    async def wait_until_idle(self, timeout: float = 30.0) -> None:
        task = self._current_task
        if task:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

    async def _cancel_current(self, reason: str) -> None:
        if self._active_cancel_event:
            self._active_cancel_event.set()
        await self.audio.stop_all_playback()
        task = self._current_task
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.telemetry.log_event(EventType.CANCELLATION, f"Active process cancelled: {reason}.", data={"reason": reason})
        self._current_task = None
        self._active_cancel_event = None

    async def _interaction_flow(
        self,
        interaction_id: str,
        detection: WakeDetection | None,
        *,
        play_wake_sound: bool,
        force_continuing_wake_ack: bool = False,
    ) -> None:
        cfg = self.config_store.get_active()
        self._refresh_conversation_config(cfg)
        self._active_cancel_event = asyncio.Event()
        conversation_id = self.conversation.conversation_id
        wake_detector_paused = False
        try:
            self.conversation.expire_if_needed()
            conversation_id = self.conversation.conversation_id
            if play_wake_sound:
                wake_sound_event = self._wake_acknowledgement_event(force_continuing=force_continuing_wake_ack)
                self.state.set_state(RuntimeState.WAKE_DETECTED, interaction_id=interaction_id, conversation_id=conversation_id)
                self.telemetry.log_event(
                    EventType.WAKE_DETECTED,
                    "Wake word detected locally.",
                    state=RuntimeState.WAKE_DETECTED.value,
                    conversation_id=conversation_id,
                    interaction_id=interaction_id,
                    data={
                        "phrase": detection.phrase if detection else cfg.wake.active_wake_phrase,
                        "confidence": detection.confidence if detection else None,
                        "engine": detection.engine if detection else cfg.wake.engine,
                        "sound_event": wake_sound_event.value,
                        "conversation_context_active": wake_sound_event == SoundEvent.WAKE_ACK,
                    },
                )
                await self.wake_engine.pause("wake_ack")
                wake_detector_paused = True
                await self._play_wake_acknowledgement(cfg, interaction_id, conversation_id, wake_sound_event)

            await self._capture_gate_and_process(
                cfg,
                interaction_id,
                conversation_id,
                wake_detector_already_paused=wake_detector_paused,
            )
            wake_detector_paused = False
        except asyncio.CancelledError:
            if wake_detector_paused:
                with contextlib.suppress(Exception):
                    await self.wake_engine.resume()
            raise
        except Exception as exc:
            if wake_detector_paused:
                with contextlib.suppress(Exception):
                    await self.wake_engine.resume()
                wake_detector_paused = False
            await self._handle_failure(
                cfg,
                interaction_id=interaction_id,
                conversation_id=conversation_id,
                event_type=EventType.FAILURE,
                sound_event=SoundEvent.INTERNAL_FAILURE,
                component="runtime",
                message="Internal processing failure.",
                error=exc,
            )
        finally:
            if self._current_task is asyncio.current_task():
                self._current_task = None
                self._active_cancel_event = None
            if self.state.state != RuntimeState.IDLE:
                self.state.set_state(RuntimeState.IDLE, interaction_id=interaction_id, conversation_id=conversation_id)

    async def _play_wake_acknowledgement(
        self,
        cfg: AssistantConfig,
        interaction_id: str,
        conversation_id: str,
        sound_event: SoundEvent,
    ) -> None:
        started = time.monotonic()
        self.telemetry.log_event(
            EventType.WAKE_ACK_PLAYBACK_STARTED,
            "Wake acknowledgement playback started.",
            state=RuntimeState.WAKE_DETECTED.value,
            conversation_id=conversation_id,
            interaction_id=interaction_id,
            component="audio",
            data={"sound_event": sound_event.value},
        )
        try:
            await self.audio.play_sound_event(
                cfg,
                sound_event,
                cancel_event=self._active_cancel_event,
                require_playback=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            duration_ms = (time.monotonic() - started) * 1000
            self.telemetry.log_event(
                EventType.WAKE_ACK_PLAYBACK_FAILED,
                "Wake acknowledgement playback failed; continuing to prompt capture.",
                state=RuntimeState.WAKE_DETECTED.value,
                conversation_id=conversation_id,
                interaction_id=interaction_id,
                component="audio",
                success=False,
                error=str(exc),
                duration_ms=duration_ms,
                data={"sound_event": sound_event.value},
            )
            return
        duration_ms = (time.monotonic() - started) * 1000
        self.telemetry.log_event(
            EventType.WAKE_ACK_PLAYBACK_ENDED,
            "Wake acknowledgement playback ended.",
            state=RuntimeState.WAKE_DETECTED.value,
            conversation_id=conversation_id,
            interaction_id=interaction_id,
            component="audio",
            success=True,
            duration_ms=duration_ms,
            data={"sound_event": sound_event.value},
        )

    def _wake_acknowledgement_event(self, *, force_continuing: bool = False) -> SoundEvent:
        if force_continuing or self.conversation.has_active_context():
            return SoundEvent.WAKE_ACK
        return SoundEvent.WAKE_NEW_CONVERSATION

    def _new_conversation_wake_acknowledgement_event(self, cfg: AssistantConfig) -> SoundEvent:
        # Current configs include wake_new_conversation and legacy configs are backfilled from wake_ack.
        # Keep a defensive fallback so command re-entry can still listen if an injected config omits it.
        if SoundEvent.WAKE_NEW_CONVERSATION in cfg.sounds.event_files:
            return SoundEvent.WAKE_NEW_CONVERSATION
        return SoundEvent.WAKE_ACK

    async def _capture_with_wake_acknowledgement_started(
        self,
        cfg: AssistantConfig,
        interaction_id: str,
        conversation_id: str,
        sound_event: SoundEvent,
    ) -> None:
        await self.wake_engine.pause("new_conversation_wake_ack")
        acknowledgement_task = asyncio.create_task(
            self._play_wake_acknowledgement(cfg, interaction_id, conversation_id, sound_event)
        )
        try:
            # Let acknowledgement telemetry/audio get requested before prompt capture opens. Capture then
            # runs while the short wake-style cue is playing, and command routing waits for it to finish
            # so command_thinking cannot overlap the cue if a custom sound is long.
            await asyncio.sleep(0)
            await self._capture_gate_and_process(
                cfg,
                interaction_id,
                conversation_id,
                wake_detector_already_paused=True,
                wait_before_command_routing=acknowledgement_task,
                defer_wake_resume_until_after_wait=True,
            )
        except asyncio.CancelledError:
            if not acknowledgement_task.done():
                acknowledgement_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await acknowledgement_task
            raise
        except Exception:
            if not acknowledgement_task.done():
                acknowledgement_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await acknowledgement_task
            raise

    def _refresh_conversation_config(self, cfg: AssistantConfig) -> None:
        self.conversation.inactivity_timeout_seconds = cfg.conversation.inactivity_timeout_seconds
        # Do not replace existing history when only the prompt text changes; a new conversation reset
        # will use the latest system prompt.
        self.conversation.system_prompt = cfg.conversation.system_prompt

    async def _capture_gate_and_process(
        self,
        cfg: AssistantConfig,
        interaction_id: str,
        conversation_id: str,
        *,
        wake_detector_already_paused: bool = False,
        wait_before_command_routing: asyncio.Task[None] | None = None,
        defer_wake_resume_until_after_wait: bool = False,
    ) -> None:
        cancel_event = self._active_cancel_event or asyncio.Event()
        prompt_path = self.audio.new_prompt_path(cfg, interaction_id)
        self.state.set_state(RuntimeState.CAPTURING_PROMPT, interaction_id=interaction_id, conversation_id=conversation_id)
        self.telemetry.log_event(
            EventType.PROMPT_CAPTURE_STARTED,
            "Prompt capture started.",
            state=RuntimeState.CAPTURING_PROMPT.value,
            conversation_id=conversation_id,
            interaction_id=interaction_id,
            data={
                "minimum_duration_seconds": cfg.prompt_capture.minimum_duration_seconds,
                "maximum_duration_seconds": cfg.prompt_capture.maximum_duration_seconds,
                "silence_duration_seconds": cfg.prompt_capture.silence_duration_seconds,
                "silence_rms_threshold": cfg.prompt_capture.silence_rms_threshold,
            },
        )
        resume_after_ack_wait = wait_before_command_routing is not None and defer_wake_resume_until_after_wait
        capture_completed = False
        if not wake_detector_already_paused:
            await self.wake_engine.pause("prompt_capture")
        try:
            capture_task = asyncio.create_task(self.audio.record_prompt(cfg, prompt_path, cancel_event=cancel_event))
            capture = await capture_task
            capture_completed = True
        finally:
            if not resume_after_ack_wait or not capture_completed:
                await self.wake_engine.resume()
        try:
            self.telemetry.log_event(
                EventType.PROMPT_CAPTURE_ENDED,
                "Prompt capture ended.",
                state=RuntimeState.CAPTURING_PROMPT.value,
                conversation_id=conversation_id,
                interaction_id=interaction_id,
                duration_ms=capture.duration_seconds * 1000,
                data={"ended_by": capture.ended_by, "bytes_written": capture.bytes_written, "rms_peak": capture.rms_peak},
            )
            if cfg.telemetry.audio_artifact_storage_enabled:
                artifact = self.telemetry.create_artifact(
                    capture.path,
                    ArtifactKind.PROMPT_AUDIO,
                    conversation_id=conversation_id,
                    interaction_id=interaction_id,
                    metadata={"ended_by": capture.ended_by},
                )
                self.telemetry.log_event(
                    EventType.PROMPT_CAPTURE_ENDED,
                    "Prompt audio artifact stored.",
                    state=RuntimeState.CAPTURING_PROMPT.value,
                    conversation_id=conversation_id,
                    interaction_id=interaction_id,
                    data={"artifact_id": artifact.id, "artifact_kind": artifact.kind},
                )
            if wait_before_command_routing is not None:
                await wait_before_command_routing
        finally:
            if resume_after_ack_wait and capture_completed:
                await self.wake_engine.resume()

        self.state.set_state(RuntimeState.CHECKING_COMMAND, interaction_id=interaction_id, conversation_id=conversation_id)
        self.telemetry.log_event(
            EventType.COMMAND_RECOGNITION_STARTED,
            "STT-first local command routing started.",
            state=RuntimeState.CHECKING_COMMAND.value,
            conversation_id=conversation_id,
            interaction_id=interaction_id,
            component="command_router",
            stage="command_routing",
            data={
                "routing_mode": "stt_first",
                "sound_event": SoundEvent.COMMAND_THINKING.value,
                "configured_legacy_recognizer_engine": cfg.command_registry.recognizer.engine,
            },
        )

        try:
            transcript = await self._transcribe_for_command_routing_with_feedback(
                cfg,
                capture,
                interaction_id,
                conversation_id,
            )
        except asyncio.TimeoutError as exc:
            await self._handle_command_routing_stt_failure(cfg, interaction_id, conversation_id, exc)
            return
        except ServiceAuthError as exc:
            await self._handle_command_routing_stt_failure(cfg, interaction_id, conversation_id, exc)
            return
        except NetworkServiceError as exc:
            await self._handle_command_routing_stt_failure(cfg, interaction_id, conversation_id, exc)
            return
        except MalformedServiceResponse as exc:
            await self._handle_command_routing_stt_failure(cfg, interaction_id, conversation_id, exc)
            return
        except ServiceError as exc:
            await self._handle_command_routing_stt_failure(cfg, interaction_id, conversation_id, exc)
            return

        self.state.set_state(RuntimeState.CHECKING_COMMAND, interaction_id=interaction_id, conversation_id=conversation_id)
        if not transcript.strip():
            self._log_command_match_result(
                transcript,
                None,
                interaction_id,
                conversation_id,
                route="invalid_prompt",
                success=False,
                human_message="STT-first command routing found no transcript to match.",
            )
            await self.audio.play_sound_event(cfg, SoundEvent.INVALID_PROMPT, cancel_event=cancel_event)
            self.state.set_state(RuntimeState.IDLE, interaction_id=interaction_id, conversation_id=conversation_id)
            return

        registry = CommandRegistry(cfg.command_registry)
        command = registry.match_text(transcript)
        self._log_command_match_result(transcript, command, interaction_id, conversation_id)
        if command:
            await self._handle_command(command, cfg, interaction_id, conversation_id)
            return
        await self._process_transcribed_prompt(transcript, cfg, interaction_id, conversation_id)

    async def _transcribe_for_command_routing_with_feedback(
        self,
        cfg: AssistantConfig,
        capture: CaptureResult,
        interaction_id: str,
        conversation_id: str,
    ) -> str:
        command_thinking: LoopingSoundHandle | None = self.audio.start_looping_sound(cfg, SoundEvent.COMMAND_THINKING)
        started = time.monotonic()
        try:
            self.state.set_state(RuntimeState.PROCESSING_STT, interaction_id=interaction_id, conversation_id=conversation_id)
            self.telemetry.log_event(
                EventType.STT_STARTED,
                "STT request started for command routing.",
                state=RuntimeState.PROCESSING_STT.value,
                conversation_id=conversation_id,
                interaction_id=interaction_id,
                component="stt",
                stage="command_routing",
                data={"routing_mode": "stt_first", "sound_event": SoundEvent.COMMAND_THINKING.value},
            )
            transcript = await self.stt_factory(cfg).transcribe(capture.path)
            stt_duration_ms = (time.monotonic() - started) * 1000
            if not transcript:
                self.telemetry.log_event(
                    EventType.STT_RESULT,
                    "STT returned no text during command routing; prompt is invalid.",
                    state=RuntimeState.PROCESSING_STT.value,
                    conversation_id=conversation_id,
                    interaction_id=interaction_id,
                    component="stt",
                    stage="command_routing",
                    success=False,
                    duration_ms=stt_duration_ms,
                    data={"transcript": "", "routing_mode": "stt_first", "route": "invalid_prompt"},
                )
                return ""
            self.telemetry.log_event(
                EventType.STT_RESULT,
                "STT returned transcript for command routing.",
                state=RuntimeState.PROCESSING_STT.value,
                conversation_id=conversation_id,
                interaction_id=interaction_id,
                component="stt",
                stage="command_routing",
                success=True,
                duration_ms=stt_duration_ms,
                data={"transcript": transcript, "routing_mode": "stt_first"},
            )
            return transcript
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            stt_duration_ms = (time.monotonic() - started) * 1000
            self.telemetry.log_event(
                EventType.STT_RESULT,
                "STT failed during command routing.",
                state=RuntimeState.PROCESSING_STT.value,
                conversation_id=conversation_id,
                interaction_id=interaction_id,
                component="stt",
                stage="command_routing",
                success=False,
                error=str(exc),
                duration_ms=stt_duration_ms,
                data={"transcript": "", "routing_mode": "stt_first", "route": "stt_failure"},
            )
            raise
        finally:
            if command_thinking:
                await command_thinking.stop()

    async def _handle_command_routing_stt_failure(
        self,
        cfg: AssistantConfig,
        interaction_id: str,
        conversation_id: str,
        error: BaseException,
    ) -> None:
        self.telemetry.log_event(
            EventType.COMMAND_RECOGNITION_RESULT,
            "STT-first command routing failed before command matching.",
            state=RuntimeState.PROCESSING_STT.value,
            conversation_id=conversation_id,
            interaction_id=interaction_id,
            component="command_router",
            stage="command_routing",
            success=False,
            error=str(error),
            data={
                "routing_mode": "stt_first",
                "matched": False,
                "alias": None,
                "route": "stt_failure",
                "sound_event": SoundEvent.COMMAND_THINKING.value,
            },
        )
        await self._handle_failure(
            cfg,
            interaction_id=interaction_id,
            conversation_id=conversation_id,
            event_type=EventType.FAILURE,
            sound_event=self._sound_for_service_exception(error, fallback=SoundEvent.STT_FAILURE),
            component=self._component_for_service_exception(error, fallback="stt"),
            message=self._message_for_service_exception(error, fallback="STT failure."),
            error=error,
        )

    def _log_command_match_result(
        self,
        transcript: str,
        command: CommandMatch | None,
        interaction_id: str,
        conversation_id: str,
        *,
        route: str | None = None,
        success: bool = True,
        human_message: str = "STT-first local command routing completed.",
    ) -> None:
        registry = CommandRegistry(self.config_store.get_active().command_registry)
        normalized_transcript = registry.normalize(transcript)
        resolved_route = route or ("local_command" if command else "normal_llm")
        self.telemetry.log_event(
            EventType.COMMAND_RECOGNITION_RESULT,
            human_message,
            state=RuntimeState.CHECKING_COMMAND.value,
            conversation_id=conversation_id,
            interaction_id=interaction_id,
            component="command_router",
            stage="command_routing",
            command_intent=command.intent if command else None,
            success=success,
            data={
                "routing_mode": "stt_first",
                "matched": bool(command),
                "intent": command.intent if command else None,
                "alias": command.alias if command else None,
                "transcript": transcript,
                "normalized_transcript": normalized_transcript,
                "sound_event": SoundEvent.COMMAND_THINKING.value,
                "route": resolved_route,
            },
        )

    async def _handle_command(self, command: CommandMatch, cfg: AssistantConfig, interaction_id: str, conversation_id: str) -> None:
        registry = CommandRegistry(cfg.command_registry)
        definition = registry.get(command.intent)
        sound_event = definition.acknowledgement_sound_event if definition else SoundEvent.CANCEL_ACCEPTED
        self.telemetry.log_event(
            EventType.COMMAND_ACCEPTED,
            f"Local command accepted: {command.intent}.",
            state=RuntimeState.CHECKING_COMMAND.value,
            conversation_id=conversation_id,
            interaction_id=interaction_id,
            component="command_router",
            stage="command_routing",
            command_intent=command.intent,
            success=True,
            data={"alias": command.alias, "transcript": command.transcript, "routing_mode": "stt_first"},
        )
        if command.intent == CommandIntent.CANCEL_STOP.value:
            await self.audio.play_sound_event(cfg, sound_event, cancel_event=self._active_cancel_event)
            self.telemetry.log_event(
                EventType.CANCELLATION,
                "Cancel/stop command handled; returning to idle and preserving conversation context.",
                conversation_id=conversation_id,
                interaction_id=interaction_id,
                command_intent=command.intent,
            )
            self.state.set_state(RuntimeState.IDLE, interaction_id=interaction_id, conversation_id=conversation_id)
            return
        if command.intent == CommandIntent.NEW_CONVERSATION.value:
            new_cfg = self.config_store.get_active()
            self._refresh_conversation_config(new_cfg)
            new_conversation_id = self.conversation.reset()
            new_interaction_id = str(uuid.uuid4())
            wake_sound_event = self._new_conversation_wake_acknowledgement_event(new_cfg)
            self.state.set_state(
                RuntimeState.WAKE_DETECTED,
                interaction_id=new_interaction_id,
                conversation_id=new_conversation_id,
            )
            self.telemetry.log_event(
                EventType.NEW_CONVERSATION,
                "New conversation command handled; local context discarded and wake-style prompt capture restarted without another wake word.",
                state=RuntimeState.WAKE_DETECTED.value,
                conversation_id=new_conversation_id,
                interaction_id=new_interaction_id,
                command_intent=command.intent,
                data={
                    "previous_conversation_id": conversation_id,
                    "command_acknowledgement_sound_event_suppressed": sound_event.value,
                    "wake_acknowledgement_sound_event": wake_sound_event.value,
                },
            )
            await self._capture_with_wake_acknowledgement_started(
                new_cfg,
                new_interaction_id,
                new_conversation_id,
                wake_sound_event,
            )
            return
        raise RuntimeError(f"Unsupported command intent in v1 registry: {command.intent}")

    async def _process_transcribed_prompt(self, transcript: str, cfg: AssistantConfig, interaction_id: str, conversation_id: str) -> None:
        cancel_event = self._active_cancel_event or asyncio.Event()
        thinking: LoopingSoundHandle | None = None

        async def stop_processing_feedback() -> None:
            nonlocal thinking
            if thinking:
                await thinking.stop()
                thinking = None

        try:
            self.telemetry.log_event(
                EventType.PROMPT_ACCEPTED,
                "Prompt accepted because STT returned non-command text.",
                state=RuntimeState.CHECKING_COMMAND.value,
                conversation_id=conversation_id,
                interaction_id=interaction_id,
                component="command_router",
                stage="command_routing",
                success=True,
                data={
                    "sound_event": SoundEvent.PROMPT_ACCEPTED.value,
                    "sound_playback": "suppressed_during_processing_feedback",
                    "routing_mode": "stt_first",
                    "route": "normal_llm",
                },
            )

            self.conversation.add_user(transcript)
            self.state.set_state(RuntimeState.PROCESSING_LLM, interaction_id=interaction_id, conversation_id=conversation_id)
            thinking = self.audio.start_looping_sound(cfg, SoundEvent.THINKING)
            self.telemetry.log_event(
                EventType.LLM_STARTED,
                "LLM request started.",
                state=RuntimeState.PROCESSING_LLM.value,
                conversation_id=conversation_id,
                interaction_id=interaction_id,
                component="llm",
            )
            started = time.monotonic()
            llm_text = await self.llm_factory(cfg).chat(self.conversation.messages_for_llm())
            llm_duration_ms = (time.monotonic() - started) * 1000
            self.conversation.add_assistant(llm_text)
            self.telemetry.log_event(
                EventType.LLM_RESULT,
                "LLM returned response text.",
                state=RuntimeState.PROCESSING_LLM.value,
                conversation_id=conversation_id,
                interaction_id=interaction_id,
                component="llm",
                success=True,
                duration_ms=llm_duration_ms,
                data={"assistant_response": llm_text},
            )

            self.state.set_state(RuntimeState.PROCESSING_TTS, interaction_id=interaction_id, conversation_id=conversation_id)
            self.telemetry.log_event(EventType.TTS_STARTED, "TTS request started.", state=RuntimeState.PROCESSING_TTS.value, conversation_id=conversation_id, interaction_id=interaction_id, component="tts")
            tts_path = self.audio.new_tts_path(cfg, interaction_id)
            started = time.monotonic()
            tts_output = await self.tts_factory(cfg).synthesize(llm_text, tts_path)
            tts_duration_ms = (time.monotonic() - started) * 1000
            self.telemetry.log_event(
                EventType.TTS_RESULT,
                "TTS audio generated.",
                state=RuntimeState.PROCESSING_TTS.value,
                conversation_id=conversation_id,
                interaction_id=interaction_id,
                component="tts",
                success=True,
                duration_ms=tts_duration_ms,
                data={"path": str(tts_output)},
            )
            if cfg.telemetry.audio_artifact_storage_enabled:
                artifact = self.telemetry.create_artifact(
                    tts_output,
                    ArtifactKind.TTS_AUDIO,
                    conversation_id=conversation_id,
                    interaction_id=interaction_id,
                    metadata={"assistant_response": llm_text},
                )
                self.telemetry.log_event(
                    EventType.TTS_RESULT,
                    "TTS audio artifact stored.",
                    state=RuntimeState.PROCESSING_TTS.value,
                    conversation_id=conversation_id,
                    interaction_id=interaction_id,
                    data={"artifact_id": artifact.id, "artifact_kind": artifact.kind},
                )

            await stop_processing_feedback()
            self.state.set_state(RuntimeState.PLAYING_RESPONSE, interaction_id=interaction_id, conversation_id=conversation_id)
            self.telemetry.log_event(EventType.PLAYBACK_STARTED, "Response playback started.", state=RuntimeState.PLAYING_RESPONSE.value, conversation_id=conversation_id, interaction_id=interaction_id, component="audio")
            started = time.monotonic()
            await self.audio.play_file(cfg, tts_output, cancel_event=cancel_event)
            playback_duration_ms = (time.monotonic() - started) * 1000
            self.conversation.mark_response_finished(utc_now())
            self.telemetry.log_event(
                EventType.PLAYBACK_ENDED,
                "Response playback ended; conversation inactivity timer started.",
                state=RuntimeState.PLAYING_RESPONSE.value,
                conversation_id=conversation_id,
                interaction_id=interaction_id,
                component="audio",
                success=True,
                duration_ms=playback_duration_ms,
            )
            self.state.set_state(RuntimeState.IDLE, interaction_id=interaction_id, conversation_id=conversation_id)
        except asyncio.CancelledError:
            await stop_processing_feedback()
            raise
        except ServiceAuthError as exc:
            await stop_processing_feedback()
            await self._handle_failure(cfg, interaction_id=interaction_id, conversation_id=conversation_id, event_type=EventType.FAILURE, sound_event=SoundEvent.NETWORK_FAILURE, component="auth", message="Service authentication failure.", error=exc)
        except NetworkServiceError as exc:
            await stop_processing_feedback()
            await self._handle_failure(cfg, interaction_id=interaction_id, conversation_id=conversation_id, event_type=EventType.FAILURE, sound_event=SoundEvent.NETWORK_FAILURE, component="network", message="Network/service failure.", error=exc)
        except MalformedServiceResponse as exc:
            await stop_processing_feedback()
            await self._handle_failure(cfg, interaction_id=interaction_id, conversation_id=conversation_id, event_type=EventType.FAILURE, sound_event=SoundEvent.INTERNAL_FAILURE, component="service", message="Malformed downstream service response.", error=exc)
        except ServiceError as exc:
            await stop_processing_feedback()
            component = "service"
            sound = SoundEvent.INTERNAL_FAILURE
            text = str(exc).lower()
            if "llm" in text:
                component, sound = "llm", SoundEvent.LLM_FAILURE
            elif "tts" in text:
                component, sound = "tts", SoundEvent.TTS_FAILURE
            await self._handle_failure(cfg, interaction_id=interaction_id, conversation_id=conversation_id, event_type=EventType.FAILURE, sound_event=sound, component=component, message=f"{component.upper()} failure.", error=exc)
        except Exception:
            await stop_processing_feedback()
            raise

    def _component_for_service_exception(self, error: BaseException, *, fallback: str) -> str:
        if isinstance(error, ServiceAuthError):
            return "auth"
        if isinstance(error, NetworkServiceError):
            return "network"
        if isinstance(error, MalformedServiceResponse):
            return "service"
        if isinstance(error, ServiceError):
            text = str(error).lower()
            if "stt" in text:
                return "stt"
            if "llm" in text:
                return "llm"
            if "tts" in text:
                return "tts"
            return "service"
        return fallback

    def _sound_for_service_exception(self, error: BaseException, *, fallback: SoundEvent) -> SoundEvent:
        if isinstance(error, (ServiceAuthError, NetworkServiceError)):
            return SoundEvent.NETWORK_FAILURE
        if isinstance(error, MalformedServiceResponse):
            return SoundEvent.INTERNAL_FAILURE
        if isinstance(error, ServiceError):
            text = str(error).lower()
            if "stt" in text:
                return SoundEvent.STT_FAILURE
            if "llm" in text:
                return SoundEvent.LLM_FAILURE
            if "tts" in text:
                return SoundEvent.TTS_FAILURE
            return SoundEvent.INTERNAL_FAILURE
        return fallback

    def _message_for_service_exception(self, error: BaseException, *, fallback: str) -> str:
        if isinstance(error, ServiceAuthError):
            return "Service authentication failure."
        if isinstance(error, NetworkServiceError):
            return "Network/service failure."
        if isinstance(error, MalformedServiceResponse):
            return "Malformed downstream service response."
        if isinstance(error, ServiceError):
            component = self._component_for_service_exception(error, fallback="service")
            return f"{component.upper()} failure."
        if isinstance(error, asyncio.TimeoutError):
            return "STT timeout."
        return fallback

    async def _handle_failure(
        self,
        cfg: AssistantConfig,
        *,
        interaction_id: str,
        conversation_id: str,
        event_type: EventType,
        sound_event: SoundEvent,
        component: str,
        message: str,
        error: BaseException,
    ) -> None:
        self.state.set_state(RuntimeState.HANDLING_ERROR, interaction_id=interaction_id, conversation_id=conversation_id)
        self.telemetry.log_event(
            event_type,
            message,
            state=RuntimeState.HANDLING_ERROR.value,
            conversation_id=conversation_id,
            interaction_id=interaction_id,
            component=component,
            success=False,
            error=str(error),
        )
        with contextlib.suppress(Exception):
            await self.audio.play_sound_event(cfg, sound_event, cancel_event=self._active_cancel_event)
        self.state.set_state(RuntimeState.IDLE, interaction_id=interaction_id, conversation_id=conversation_id)

    def status(self) -> dict[str, object]:
        conv = self.conversation.snapshot()
        wake = self.wake_status()
        return {
            "state": self.state.state.value,
            "conversation_id": conv.conversation_id,
            "conversation_message_count": len(conv.messages),
            "last_response_finished_at": conv.last_response_finished_at,
            "wake_engine": self.config_store.get_active().wake.engine,
            "wake": wake,
        }

    def wake_status(self) -> dict[str, object]:
        cfg = self.config_store.get_active()
        task_running = bool(self._wake_task and not self._wake_task.done())
        task_done = bool(self._wake_task and self._wake_task.done())
        task_error = None
        if self._wake_task and self._wake_task.done() and not self._wake_task.cancelled():
            exc = self._wake_task.exception()
            task_error = str(exc) if exc else None
        engine_status = self.wake_engine.status()
        production_ready = (
            cfg.wake.engine != "simulated"
            and task_running
            and task_error is None
            and bool(engine_status.get("production_ready", False))
        )
        return {
            **engine_status,
            "configured_engine": cfg.wake.engine,
            "active_wake_phrase": cfg.wake.active_wake_phrase,
            "wake_phrases": list(cfg.wake.wake_phrases),
            "sensitivity": cfg.wake.sensitivity,
            "task_running": task_running,
            "task_done": task_done,
            "task_error": task_error,
            "production_ready": production_ready,
            "simulated_admin_endpoint_available": True,
        }

    async def reload_runtime_components(self) -> None:
        cfg = self.config_store.get_active()
        self.command_recognizer = None
        self._refresh_conversation_config(cfg)
        if self._wake_task and not self._wake_task.done():
            self._wake_stop_event.set()
            self._wake_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._wake_task
            self.wake_engine = build_wake_engine(cfg)
            self._wake_stop_event = asyncio.Event()
            self._wake_task = asyncio.create_task(self.wake_engine.run(self.on_wake_detected, self._wake_stop_event))
            self._wake_task.add_done_callback(self._wake_task_finished)
        else:
            self.wake_engine = build_wake_engine(cfg)
