# Voice-Only Local Wake-Word Assistant - V1 Design Requirements

## 1. Document purpose

This document captures the complete current design requirements for a voice-only, Alexa-like assistant device. It is intended as a handoff document for a larger AI model or engineering agent that will implement the solution later.

This document intentionally focuses on product behavior, runtime requirements, configuration, interaction rules, and admin-portal expectations. It does not include concrete source-code implementation details, service connection secrets, deployment instructions, or home-network AI service connection values. Those details will be supplied separately at implementation time.

The design is centered around a thin client on the home network. The thin client has an attached speakerphone with an always-on microphone and speaker output. The device is primarily operated by voice. A separate browser-based admin portal is also in scope for configuration, debugging, testing, telemetry, and maintenance.

## 2. Design authority and implementation guidance

The requirements in this document come from the product owner/user. Implementation agents must not treat speculative assistant suggestions as confirmed requirements unless those suggestions are explicitly included here.

Implementation agents should follow these principles:

- Do not add new product features unless they are explicitly required here or later requested.
- Do not remove or weaken the voice-only operating model.
- Do not replace local wake-word detection with general speech-to-text based wake detection.
- Do not treat every captured utterance as an LLM prompt; local command recognition comes first.
- Do not introduce authentication for the v1 admin portal unless the requirement is later changed.
- Do not implement out-of-scope Alexa/Home Assistant-style features in v1.
- Prefer configurability where the design explicitly calls for it.
- Keep implementation choices separate from product behavior. The implementing model may choose libraries and internal state names, but must preserve the external behavior described here.

## 3. Product summary

The device behaves like a voice assistant with a local wake-word engine and a conversational LLM backend.

At a high level:

1. The thin client listens locally for a configured wake word.
2. When the wake word is detected, the device plays a configurable acknowledgement sound.
3. Prompt recording begins at the same time the acknowledgement sound begins playing.
4. Prompt capture ends after a configurable silence condition or maximum prompt duration.
5. The captured utterance is first checked by a local command recognizer.
6. If the utterance is a supported local command, the command is handled locally.
7. If no local command is recognized, the captured audio is sent to speech-to-text.
8. If speech-to-text returns no text or an error, the prompt is invalid and a configurable failure sound is played.
9. If speech-to-text returns text, the text is sent to the LLM as part of the current conversation context.
10. The LLM response is sent to text-to-speech.
11. The generated speech audio is played through the speakerphone.
12. The conversation context persists across later wake-word-initiated prompts until a configurable inactivity timeout expires or the user issues a local "new conversation" command.

The device can be interrupted during processing or response playback by saying the wake word again. This is called barge-in. Barge-in immediately cancels the active process and begins a new prompt-capture flow.

## 4. V1 scope

V1 includes:

- Local wake-word detection on the thin client.
- One initially configured wake phrase, with design support for multiple wake phrases later.
- Voice-only normal device interaction.
- Prompt capture after wake-word detection.
- Configurable prompt-capture timing.
- Local command recognition after prompt capture and before main STT.
- STT, LLM, and TTS processing pipeline.
- Conversational context across nearby prompts.
- Barge-in during processing and playback.
- Configurable sound effects for device feedback.
- Full post-wake telemetry and history.
- Optional storage of post-wake prompt audio and generated TTS audio.
- Local-network admin portal with no authentication in v1.
- Admin portal configuration, telemetry, testing, debugging, sound management, service health, and maintenance controls.

## 5. V1 non-goals and out-of-scope features

The following are out of scope for v1:

- Physical buttons, keyboard, mouse, touchscreen, monitor, LEDs, or any other normal on-device user input/output besides audio.
- Manual physical activation fallback for normal use.
- Wake-word disabling through normal user operation.
- Follow-up prompts without saying the wake word.
- Proactive speech, reminders, alerts, announcements, or unsolicited assistant output.
- Timers, alarms, reminders, and announcements.
- Smart-home control.
- Routines or multi-step automations.
- Local "help" command.
- Local "repeat that" command.
- Voice-based volume control.
- Voice-based system restart, reboot, service restart, or privileged system commands.
- Local summarization of old conversation context.
- Thin-client-enforced maximum conversation history size.
- Bulk telemetry export.
- Manual deletion of individual telemetry records or audio artifacts.
- Reset-to-defaults action in the admin portal.
- Admin portal authentication or authorization.

Future versions may add some of these capabilities, but v1 should not implement them unless the requirements are explicitly changed.

## 6. Hardware and runtime assumptions

### 6.1 Thin client

- The assistant runs on a thin client on the home network.
- The thin client has a dedicated IP address.
- The thin client is an Ubuntu/Linux machine.
- The thin client has a speakerphone attached.
- The speakerphone provides microphone input and speaker output.
- The microphone is always on from a hardware perspective.
- The speakerphone is expected to provide built-in noise cancellation or echo suppression so that it does not meaningfully hear its own playback.

### 6.2 Audio-only physical device

The physical assistant device itself is audio-only.

It does not have:

- monitor
- screen
- LEDs
- keyboard
- mouse
- button
- touchscreen
- other normal user-facing non-audio I/O

Normal interaction with the device is through sound only:

- Input: microphone audio.
- Output: local sound effects and TTS-generated spoken responses.

The admin portal is a separate browser-based network management interface. It does not change the physical device's audio-only user interaction model.

## 7. Core architectural principles

### 7.1 Local wake-word detection

Wake-word detection must run locally on the thin client. The design must not rely on the general speech-to-text model to detect the wake word.

The wake-word engine should be a pre-existing dedicated wake-word tool or engine. The solution should not attempt to build a wake-word detector from scratch as a core product requirement.

The selected wake-word engine should be suitable for always-on local listening on the chosen thin client. The final tool choice is an implementation decision, but the design expectation is that the implementation uses a dedicated local wake-word engine rather than a general STT model.

### 7.2 Voice-only normal operation

The wake word is required to begin normal interaction. The user should not need a screen, keyboard, mouse, button, or other physical interface to use the assistant.

### 7.3 Local sound effects for local state feedback

The device must avoid local spoken status phrases in v1. Local state feedback should use configurable sound effects.

The device should only speak natural-language responses when those responses are generated by the configured text-to-speech model. This avoids making the assistant sound like two different voices or personalities.

### 7.4 Configurable behavior

The design should avoid hard-coding values or one-off behavior where configurability has been requested. Many values should be exposed through configuration and admin portal controls.

### 7.5 Local command gate before normal STT/LLM prompting

After wake-word detection and prompt capture, the device must check for supported local commands before sending the captured audio to the main STT model.

The local command check must evaluate whether the entire captured utterance expresses a supported command. It must not treat the mere presence of a command word inside a longer prompt as a command.

Example:

- "cancel" may be a local cancel command.
- "How do I cancel a process in Linux?" must not be treated as a local cancel command merely because it contains the word "cancel".

### 7.6 Conversational context

The assistant is not a stateless command-only system. Because it communicates with a large language model, it should maintain conversation context across nearby prompts.

Conversation continuity is based on stored LLM context, not on leaving the microphone open for wake-word-free follow-up speech.

## 8. Wake-word requirements

### 8.1 Wake-word availability

Wake-word listening cannot be disabled during normal operation.

The device has no normal physical input method, so the wake word is required for interaction. If wake-word listening were disabled, the device would have no normal way to receive user input.

### 8.2 State-dependent wake-word behavior

"Always listening for the wake word" is state-dependent:

- During idle, the wake word begins an interaction.
- During active processing, the wake word is a barge-in signal and cancels the current process.
- During response playback, the wake word is a barge-in signal and cancels playback/current process.
- During prompt capture, the wake word phrase is treated as part of the captured prompt and does not trigger a new wake event.

### 8.3 Wake-phrase count

The design should support the concept of one or more configured wake phrases, but v1 should start with a single configured wake phrase.

The system should not permanently restrict itself to one wake phrase. Additional wake phrases may be added later depending on:

- thin-client processing capacity
- idle CPU usage
- power usage
- wake-word engine support
- recognition reliability

### 8.4 Wake acknowledgement sound

Every valid wake-word detection must play the configured wake acknowledgement sound.

This includes wake detections during:

- idle state
- backend processing
- response playback

### 8.5 Prompt recording start

After wake-word detection, prompt recording begins at the same time the wake acknowledgement sound begins playing.

This timing is intentional. The acknowledgement sound marks the start of the prompt-capture window. A user may begin speaking as soon as the sound begins. The design relies on the speakerphone's noise-cancellation/echo-suppression behavior, or equivalent filtering, so the acknowledgement sound is not treated as meaningful user speech.

### 8.6 Wake word during active process

If the wake word is detected during active processing or response playback, the event is treated as barge-in.

Barge-in means:

1. Stop the current active process immediately.
2. Play the wake acknowledgement sound.
3. Begin prompt capture for the new interaction.
4. Evaluate the new captured utterance through the normal local command gate.
5. If it is not a local command, process it as a new prompt.

## 9. Prompt capture requirements

### 9.1 Prompt-capture start

Prompt capture begins at the same time the wake acknowledgement sound begins playing.

### 9.2 Prompt-capture end

Prompt capture ends when one of these conditions occurs:

- the configured silence condition indicates that the user has stopped speaking
- the configurable maximum prompt duration is reached

### 9.3 Silence detection

The prompt period ends when the microphone detects a long enough period of silence.

Silence detection must be configurable. The design should include configuration for at least:

- silence duration needed to end capture
- any threshold or sensitivity required by the implementation

A specific default silence duration was not finalized in this requirements conversation. The implementing model may choose a reasonable default, but it must be configurable in the admin portal.

### 9.4 Minimum prompt capture duration

The device shall have a configurable minimum prompt-capture period before silence detection is allowed to end the prompt.

Reason: users may pause briefly while formalizing their thoughts, and the device should not immediately end capture just because the user hesitates after the wake sound.

Default:

- minimum prompt capture duration: 3 seconds

After that initial period, the configured silence detection rule may begin deciding whether the prompt has ended.

### 9.5 Maximum prompt duration

The device shall have a configurable maximum prompt duration to prevent indefinite recording if silence is never detected.

Default:

- maximum prompt duration: 2 minutes

If the maximum duration is reached, prompt capture stops and the captured audio continues to the local command gate.

### 9.6 Wake word during prompt capture

If the wake phrase is spoken during prompt capture, it is treated as part of the prompt audio. It does not trigger a new wake event while capture is already active.

## 10. Local command recognition requirements

### 10.1 Command recognition location

Supported assistant/session commands shall be recognized locally on the thin client.

The design must not rely on the main STT model to recognize local commands.

### 10.2 Command recognition timing

Command recognition happens after wake-word detection and prompt capture, but before sending the captured audio to the main STT model.

Flow:

1. Wake word detected.
2. Wake acknowledgement sound begins.
3. Prompt capture begins.
4. Prompt capture ends by silence or maximum duration.
5. Captured utterance goes to local command recognizer.
6. If a command is recognized, handle it locally.
7. If no command is recognized, send audio to main STT.

### 10.3 Whole-utterance command matching

Command recognition must evaluate the captured utterance as a whole.

The implementation must not use naive substring matching against command words.

A command word appearing inside a longer ordinary prompt is not enough to treat the prompt as a command.

### 10.4 Command registry

Commands shall be organized through a configurable command registry.

Each command should define:

- command intent
- one or more phrase aliases
- behavior to execute when recognized
- configurable acknowledgement sound
- enabled/disabled state if useful to the implementation

Every recognized command must produce local sound feedback so the user knows the device accepted, interpreted, or understood the command and is moving to the next step.

### 10.5 Multiple aliases per command intent

Command matching shall support multiple configurable phrase aliases per command intent.

The design should not limit a command to a single exact phrase.

For example, a cancel intent may include configured aliases such as:

- "stop"
- "cancel"
- "never mind"
- "forget it"
- other configured phrases

The exact alias list is configurable and may expand or shrink based on thin-client performance and recognition reliability.

### 10.6 Command-recognition performance constraint

The command recognizer should be constrained enough to run acceptably on the target thin client.

The design should allow the alias set to be expanded or reduced based on observed:

- latency
- CPU usage
- power usage
- false positives
- false negatives
- local recognition reliability

### 10.7 Initial v1 local commands

V1 local commands should include only the commands intentionally required by the current design:

1. Cancel/stop intent.
2. New conversation intent.

Future commands may be added later through the command registry, but should not be implemented in v1 unless the requirements change.

## 11. Cancel/stop command behavior

### 11.1 Intent

The cancel/stop command intent means:

- cancel the current active process if one exists
- return the device to idle listening
- preserve the active LLM conversation context until the configured conversation timeout expires or a new conversation command is issued

### 11.2 Aliases

The cancel/stop intent should support configurable aliases.

Possible aliases include:

- "stop"
- "cancel"
- "never mind"
- other configured phrases

These aliases are not continuously listened for like wake words. They are evaluated by the local command recognizer after wake-word detection and prompt capture.

### 11.3 Stop has consistent meaning

"Stop" should mean the same thing in all contexts.

It cancels the current active process and returns the device to idle listening while preserving the active conversation context until timeout or explicit reset.

### 11.4 Stop during playback

"Stop" must interrupt response playback.

Response playback is part of the active process. If the user says the wake word during playback and then gives a cancellation command, playback stops and the device returns to idle listening.

### 11.5 Cancel after barge-in

If the user barges in during processing or playback and says a cancel/stop command, the current process has already been cancelled by the barge-in. The cancel command should still be acknowledged and the device should return to idle.

## 12. New conversation command behavior

### 12.1 Intent

The "new conversation" command means that the next gathered prompt begins a new conversation with the LLM.

The locally stored LLM conversation context used to continue the previous conversation shall be discarded.

### 12.2 Difference from inactivity timeout

The inactivity timeout expires silently.

The explicit "new conversation" command does not happen silently. It must be acknowledged with a configurable sound.

### 12.3 New prompt capture after command

When the "new conversation" command is recognized:

1. Play the configured new-conversation acknowledgement sound.
2. Discard the locally stored LLM conversation context.
3. Immediately begin a new prompt-capture window without requiring the wake word again.
4. The next valid prompt gathered in that window begins a new LLM conversation.

### 12.4 Aliases

The new conversation command should use configurable aliases.

Possible aliases may include:

- "new conversation"
- "start a new conversation"
- "start over"
- "new chat"
- other configured phrases

The exact aliases are configuration, not hard-coded product behavior.

## 13. Prompt validity and STT requirements

### 13.1 Prompt validity is based on STT result

Prompt validity is determined after the main STT step, not through pre-STT local audio analysis.

If the captured utterance is not recognized as a local command, it is sent to the main STT model.

A prompt is valid if the STT model returns any text.

A prompt is invalid only if:

- STT returns no text
- STT returns an error
- STT otherwise fails to produce usable text

If STT returns text, the prompt is valid even if it is short, strange, accidental, caused by a cough/noise, or otherwise low quality. The LLM is allowed to interpret whatever text STT produced.

### 13.2 Invalid prompt behavior

If STT produces no text or errors:

1. Stop the thinking/processing sound if it is playing.
2. Play the configured invalid-prompt/failure sound.
3. Log the failure as telemetry.
4. Return to idle listening.
5. Preserve the active conversation context unless the failure handling is later explicitly changed.

### 13.3 Valid prompt behavior

If STT returns text:

1. Treat the prompt as valid.
2. Log the transcript and interaction telemetry.
3. Continue to the LLM step using the current conversation context.

The design includes a configurable prompt-accepted sound event. Because validity is known only after STT and the thinking sound may already be active during STT, the implementation must sequence local audio cues deterministically so sounds do not overlap confusingly. The product requirement is that valid prompt acceptance can have its own configurable sound event, but local audio playback must remain coherent.

## 14. Active processing pipeline

### 14.1 Process definition

The active process begins when the thin client sends the captured recording to the STT service and continues through:

1. STT processing
2. LLM response generation
3. TTS generation
4. response playback through the speakerphone

The process is complete only after the generated response audio has finished playing.

### 14.2 Processing sequence

For a normal non-command prompt:

1. Captured audio is sent to STT.
2. STT returns text or an error/no-text result.
3. If no text/error, the invalid-prompt/failure path runs.
4. If text is returned, the text is sent to the LLM with the current conversation context.
5. The LLM response text is sent to TTS.
6. The generated TTS audio is played through the speakerphone.
7. When playback finishes, the conversation inactivity timeout begins.
8. The device returns to idle wake-word listening.

### 14.3 Thinking/processing sound

The device should play a configurable thinking/processing sound while the active process is underway.

The preferred thinking sound is a local sound effect, such as a short set of tones, not a spoken phrase like "thinking".

Requirements:

- The thinking sound shall loop continuously while processing is underway.
- It shall stop if a processing failure occurs.
- It shall stop immediately when generated response playback begins.
- It shall not be a locally spoken phrase.
- It shall be individually configurable.

### 14.4 Failure during processing

If the active process fails at any stage, such as STT, LLM, TTS, network/service error, or internal error:

1. Stop the thinking/processing sound if it is playing.
2. Play the configured failure sound for that event.
3. Log the failure in telemetry.
4. Return to idle listening unless later requirements define a different recovery behavior.

Each failure event should have an independently configurable sound reference, even if multiple failures initially point to the same audio file.

## 15. Barge-in and interruption behavior

### 15.1 Wake word remains active during processing and playback

The wake-word engine must continue listening during:

- STT processing
- LLM response generation
- TTS generation
- response playback

### 15.2 Barge-in behavior

If the wake word is detected during any active process stage:

1. Stop the current active process immediately.
2. Stop any thinking sound or response playback currently occurring.
3. Play the wake acknowledgement sound.
4. Begin prompt capture for the new interaction.
5. Run the captured utterance through the local command gate.
6. If it is a command, handle it locally.
7. If it is not a command, process it as a new prompt.

### 15.3 Rephrasing use case

Barge-in is not only for cancellation.

A user may barge in because they changed their mind about how to phrase the prompt. In that case, the previous active process is cancelled and the new captured utterance is processed normally.

### 15.4 Barge-in during response playback

Response playback is part of the active process. The wake word must be able to interrupt spoken playback.

## 16. Conversation/session management

### 16.1 Conversation continuity

The assistant should maintain conversation context across nearby prompts.

If the user wakes the device, asks a prompt, receives a response, and then wakes the device again within the configured conversation inactivity timeout, the next prompt is part of the same LLM conversation.

### 16.2 Wake word required for every prompt

Every user prompt requires the wake word first.

The assistant shall not stay open for a wake-word-free follow-up window after speaking.

Conversation continuity is maintained by preserving LLM context, not by leaving the microphone open for follow-up speech.

### 16.3 Inactivity timeout

The conversation inactivity timeout shall be configurable.

Default:

- conversation inactivity timeout: 1 minute

If the device remains idle for longer than this timeout, the current LLM conversation context is considered concluded, and the next valid prompt begins a new conversation.

### 16.4 Timeout start point

The conversation inactivity timeout begins when assistant response playback finishes.

Response playback is part of the conversation. The timeout starts only after the device completes speaking its response.

### 16.5 Silent timeout expiration

When the conversation inactivity timeout expires, the transition to a new conversation happens silently.

No sound effect or spoken notice is required when prior conversation context expires due to inactivity.

### 16.6 Stop/cancel does not clear conversation context

Returning to idle after a stop/cancel command does not start a new conversation.

Idle means the device is no longer processing, capturing, or speaking, and is waiting for the wake word. The active conversation context remains available until the configured inactivity timeout expires or the user explicitly issues the new conversation command.

### 16.7 No thin-client history size limit

The thin client/assistant solution shall not impose its own maximum conversation history size.

Conversation context should remain available until the inactivity timeout expires or the user issues the new conversation command.

Any limits related to model context size, truncation, or history capacity are concerns for the LLM/backend layer, not this thin-client design.

### 16.8 No thin-client summarization

The thin client shall not summarize older conversation context.

Conversation summarization, truncation, or context-window management is out of scope for this design and should be handled by the LLM/backend layer if needed.

## 17. Local sound effect requirements

### 17.1 Sound-effect-only local status

In v1, the device shall not use local spoken status phrases.

Local state feedback shall be sound-effect-only.

The device should only speak human-language responses when audio is generated by the configured TTS model.

### 17.2 Per-event configurability

Every device-played sound shall be individually configurable by event.

Multiple events may initially reference the same audio file, but the design shall not require them to share one hard-coded sound.

Any current or future event that produces local sound feedback should have its own configurable audio reference.

### 17.3 Known sound events

The current design includes sound events for at least:

- wake word detected
- invalid prompt / STT returned nothing
- prompt accepted / STT returned text
- thinking / processing loop
- cancel/stop command accepted
- new conversation command accepted
- STT failure
- LLM failure
- TTS failure
- network/service failure
- internal processing failure
- admin/test playback where relevant

### 17.4 Default sharing allowed

Different events may initially point to the same audio file.

For example, several failure events may initially use the same generic failure sound, and several command events may initially use the same generic command-accepted sound.

The important requirement is that these references remain independently configurable.

### 17.5 Audio format guidance

For v1, local sound effects should be WAV files, preferably simple/uncompressed PCM WAV, to keep playback simple, fast, predictable, and compatible with the Ubuntu thin client.

The admin portal shall provide guidance about the expected audio format when uploading sound files.

However, v1 does not need deep programmatic validation of uploaded audio files. The admin portal should provide playback/test controls so the administrator can confirm whether an uploaded sound works.

## 18. Privacy and storage boundaries

### 18.1 Pre-wake audio

Pre-wake audio is audio heard by the microphone before the wake word has been detected.

Pre-wake audio shall be processed locally only for wake-word detection.

Pre-wake audio shall not be:

- sent to STT
- sent to the LLM
- sent to TTS
- uploaded
- stored
- logged
- persisted
- exposed in telemetry

Only audio captured after wake-word detection enters the prompt-capture and processing flow.

### 18.2 Post-wake audio

Post-wake audio may be logged and stored according to the telemetry and retention requirements.

This includes valid interactions, failures, and admin-initiated test recordings, subject to configuration and retention policy.

## 19. Telemetry and history requirements

### 19.1 Telemetry purpose

The system should provide strong telemetry because future administration may require visibility into configuration, conversation history, debugging, and device behavior.

### 19.2 Telemetry scope

The system shall support storing full telemetry for post-wake interactions.

Telemetry should include:

- timestamps
- state/phase transitions described in human-readable form
- wake detections
- prompt capture start/end
- command recognition attempts/results
- command intent recognized
- STT request/result metadata
- STT transcripts
- LLM request/result metadata
- LLM response text
- TTS request/result metadata
- response playback start/end
- success/failure details
- error details
- timing/duration data
- service health events
- barge-in events
- cancellation events
- conversation/session identifiers
- interaction identifiers
- admin test events
- maintenance/cleanup events

### 19.3 Transcript storage

The system shall support storing:

- user prompt transcripts
- assistant response transcripts

### 19.4 Audio artifact storage

The system shall support storing:

- captured prompt audio files
- generated TTS response audio files
- admin microphone test recordings

Audio-file storage shall be configurable with a true/false setting.

For v1, storing audio files is allowed, but the user must be able to disable saving actual audio files later to manage disk usage.

### 19.5 Telemetry retention

Telemetry and stored artifacts shall have a retention policy.

Default:

- retention period: 1 year

The retention period shall be configurable from the admin portal.

### 19.6 Cleanup/maintenance task

A cleanup/maintenance task shall remove telemetry and artifacts older than the configured retention period.

Default:

- cleanup interval: daily

The cleanup schedule shall be configurable from the admin portal, including the time of day the maintenance task runs.

The admin portal shall also include a manual action to run cleanup immediately.

### 19.7 Telemetry deletion limits

Manual deletion of individual telemetry records or audio artifacts is out of scope for v1.

Telemetry and artifact deletion happens through the configured retention cleanup process, including scheduled cleanup and admin-triggered manual cleanup.

### 19.8 Bulk telemetry export

Bulk telemetry-history export is out of scope for v1.

Telemetry may be viewed in the admin portal, and individual audio artifacts may be viewable/downloadable where applicable, but no full telemetry export feature is required.

## 20. Admin portal requirements

### 20.1 Purpose

The thin client shall expose an admin portal over the local network using its dedicated IP address.

The admin portal is intended for an administrator to:

- view current configuration
- change configuration
- apply configuration changes
- persist configuration changes
- manage sound files
- monitor live telemetry
- review historical telemetry
- access logs/history
- view/download audio artifacts when enabled
- inspect runtime status
- inspect service health
- run tests/debug workflows
- run cleanup/maintenance
- restart the assistant software/service
- reboot the thin-client machine

### 20.2 Network exposure and authentication

The v1 admin portal shall not require authentication or authorization.

Anyone on the trusted local home network who can reach the device IP may access the portal.

The portal is intended for local home-network access only. It should not be intentionally exposed to the public internet.

### 20.3 Admin portal vs physical device interaction

The admin portal is a browser-based management interface.

It does not change the physical assistant's normal audio-only interaction model.

The physical device remains sound-only. The admin portal is out-of-band administration.

### 20.4 Configuration display

The admin portal shall expose all configurable assistant settings.

It shall show current/default/currently saved values by default.

For settings requiring restart, the portal should distinguish between:

- currently active runtime value
- saved pending value that will become active after restart

### 20.5 Applying changes

Admin portal configuration changes shall be applied through a grouped "Apply changes" action rather than applying each field immediately one at a time.

The portal should allow edits, show unsaved changes, and apply the edited group only when the administrator explicitly chooses to apply changes.

### 20.6 Runtime and persistent behavior

Most configuration changes should be applied at runtime and also persisted across device restart.

After restart, the device shall load the most recently applied configuration rather than reverting to defaults.

### 20.7 Restart-required settings

Some settings may require restart before becoming active.

At minimum, changes to STT, LLM, and TTS connection settings shall require restart before becoming active.

The admin portal shall clearly mark restart-required settings.

Applying a restart-required setting persists the change, but the active runtime value remains unchanged until restart.

### 20.8 Configuration export/import

The admin portal shall support exporting the full current device configuration for backup.

The admin portal shall support importing a previously exported configuration to restore settings later.

Imported configuration should be applied through the same explicit apply flow as other admin portal changes and should persist across device restart once applied.

### 20.9 Reset to defaults

The admin portal shall not include a generic "reset to defaults" action in v1.

Restoring configuration should happen only by importing a previously exported configuration and applying it.

## 21. Admin portal configuration coverage

The admin portal shall expose configuration for all configurable behavior described in this document.

At minimum, this includes:

### 21.1 Wake-word settings

- wake-word engine selection/configuration where applicable
- configured wake phrase list
- active v1 wake phrase
- wake-word model/path/settings where applicable
- wake-word sensitivity/confidence threshold where supported by the engine

### 21.2 Prompt-capture settings

- minimum prompt capture duration, default 3 seconds
- maximum prompt duration, default 2 minutes
- silence duration threshold
- silence detection sensitivity/threshold settings where applicable

### 21.3 Conversation settings

- conversation inactivity timeout, default 1 minute

### 21.4 Sound-event settings

- wake acknowledgement sound
- invalid prompt/failure sound
- prompt accepted sound
- thinking/processing sound
- cancel/stop command acknowledgement sound
- new conversation command acknowledgement sound
- STT failure sound
- LLM failure sound
- TTS failure sound
- network/service failure sound
- internal processing failure sound
- any additional local sound events added later

### 21.5 Command registry

- command intents
- aliases per command intent
- behavior associated with each intent
- acknowledgement sound per command intent
- enabled/disabled state if implemented

### 21.6 Telemetry settings

- audio artifact storage enabled/disabled
- telemetry retention period, default 1 year
- cleanup schedule/time of day
- cleanup interval, default daily

### 21.7 Service connection settings

The admin portal shall allow configuring downstream service connection details for:

- STT service
- LLM service
- TTS service

This may include:

- endpoint URLs
- model names or service identifiers
- request timeouts
- other connection settings needed by the assistant runtime

Actual connection details will be supplied separately at implementation time and are not included in this document.

Changes to these service connection settings require restart before becoming active.

### 21.8 Audio device settings

If the implementation needs configurable audio device selection, the admin portal should expose input/output device settings for:

- microphone input path
- speaker/speakerphone output path

## 22. Admin portal sound management

### 22.1 Sound-file library

The admin portal shall include sound-file management.

The portal shall allow the administrator to:

- upload new sound files
- view currently uploaded/available sound files
- delete existing sound files
- play/test sound files
- assign available sounds to configurable sound-effect events

### 22.2 Sound selection controls

For each configurable sound event, the portal should provide a selection control, such as a dropdown, listing the available uploaded sound files that can be assigned to that event.

### 22.3 Sound testing controls

The portal shall provide playback/test controls in both:

- the sound-file management area
- any configuration area where a sound file is assigned to an event

This allows the administrator to test uploaded sounds directly and test which sound is currently configured for a specific event.

### 22.4 Audio format guidance

The portal shall guide the administrator toward WAV files, preferably simple/uncompressed PCM WAV.

The portal does not need to deeply validate audio files in v1. Playback/test controls are sufficient for the administrator to verify whether an uploaded sound works.

## 23. Admin portal telemetry views

### 23.1 Live telemetry

The admin portal shall include a live event/log view so the administrator can watch runtime activity as it happens.

Live telemetry should include relevant assistant events such as:

- wake detections
- prompt capture start/end
- local command recognition
- STT processing
- LLM processing
- TTS processing
- playback
- failures
- health changes
- barge-in events
- cancellation events
- new conversation events
- admin-triggered tests
- cleanup/maintenance events
- restart/reboot events

### 23.2 Historical telemetry

The admin portal shall expose historical telemetry/logs subject to the configured retention policy.

### 23.3 Filtering and searching

The admin portal shall support filtering and searching both live and historical telemetry views.

Filters may include:

- event type
- date/time range
- errors only
- conversation/session
- interaction ID
- component/service
- command intent
- processing stage
- other useful diagnostic dimensions

### 23.4 Audio artifact access

When audio retention is enabled, the admin portal shall allow viewing and downloading stored post-wake audio artifacts, including:

- captured prompt audio files
- generated TTS response audio files
- admin microphone test recordings

## 24. Admin portal runtime status and health

### 24.1 Runtime status

The admin portal shall show the device's current runtime status in a human-readable form.

Examples include whether the device is:

- waiting for the wake word
- capturing a prompt
- checking for a local command
- processing STT
- processing LLM
- processing TTS
- playing a response
- handling an error
- otherwise active

The design does not prescribe internal implementation state names, enums, or state-machine identifiers. Internal names are implementation concerns.

### 24.2 Service/component health

The admin portal shall show health/availability status for major runtime components and dependencies, including:

- wake-word engine
- local command recognizer
- STT service
- LLM service
- TTS service

## 25. Admin portal test and debug tools

### 25.1 General testing/debug stance

Debug and testing tools are welcome in the admin portal when they help verify:

- microphone input
- wake-word detection
- local command recognition
- sound playback
- service health
- end-to-end assistant behavior

### 25.2 LLM/TTS test interaction

The admin portal shall include a simple test interaction tool where the administrator can type text and send it directly through the LLM/TTS path.

Purpose:

- test downstream assistant response generation
- test TTS generation
- test physical speakerphone output
- avoid requiring wake-word and microphone capture for this specific diagnostic path

The generated TTS audio from this test shall play through the physical speakerphone attached to the thin client, not merely in the browser.

### 25.3 Microphone input test

The admin portal shall include a microphone input test tool.

The administrator should be able to trigger a short test recording from the thin client's microphone path and review what the device captured.

Admin microphone test recordings shall be included in telemetry/history and subject to the configured retention policy.

### 25.4 Wake-word engine testing

The admin portal shall include wake-word engine testing/debug functionality.

This may include:

- recent wake detections
- confidence scores if exposed by the selected wake-word engine
- test/tuning view for validating wake-word behavior

## 26. Admin portal maintenance and restart controls

### 26.1 Manual cleanup

The admin portal shall include a manual action to run the cleanup/maintenance task immediately.

This is in addition to the configured scheduled cleanup interval.

### 26.2 Restart assistant service

The admin portal shall provide an action to restart the assistant software/service without rebooting the thin-client machine.

This is needed for settings that require restart before becoming active, such as STT/LLM/TTS connection settings.

### 26.3 Reboot thin-client machine

The admin portal shall provide an action to reboot the Linux thin-client machine itself.

### 26.4 Confirmation required

Restart actions shall require a confirmation step before execution.

This applies to:

- restarting the assistant software/service
- rebooting the thin-client machine

### 26.5 Startup after reboot

After a machine reboot, all required assistant software services shall automatically start and become available without manual intervention.

The device should return to its normal wake-word listening state once startup is complete.

The admin portal should become available again after reboot.

## 27. Error handling expectations

The design requires clear handling and telemetry for errors, but does not require complex recovery behavior in v1.

At minimum:

- STT no-text/error: play configured invalid-prompt/failure sound and return to idle.
- LLM failure: stop thinking sound, play configured LLM failure/general failure sound, log telemetry, return to idle.
- TTS failure: stop thinking sound, play configured TTS failure/general failure sound, log telemetry, return to idle.
- Network/service failure: stop thinking sound, play configured network/service failure/general failure sound, log telemetry, return to idle.
- Internal processing failure: stop thinking sound, play configured internal/general failure sound, log telemetry, return to idle.

The active conversation context should not be cleared by generic failures unless the requirements are later changed.

## 28. Behavioral flow summaries

### 28.1 Normal prompt flow

```text
Idle wake listening
  -> wake word detected locally
  -> wake acknowledgement sound starts
  -> prompt capture starts at same time
  -> prompt capture ends by silence or max duration
  -> local command recognizer checks captured utterance
  -> no command recognized
  -> send audio to STT
  -> STT returns text
  -> send text to LLM with current conversation context
  -> send LLM response to TTS
  -> stop thinking sound when response audio is ready to play
  -> play TTS response through speakerphone
  -> response playback finishes
  -> conversation inactivity timer begins
  -> return to idle wake listening
```

### 28.2 Invalid prompt flow

```text
Idle wake listening
  -> wake word detected locally
  -> wake acknowledgement sound starts
  -> prompt capture starts at same time
  -> prompt capture ends by silence or max duration
  -> local command recognizer checks captured utterance
  -> no command recognized
  -> send audio to STT
  -> STT returns no text or error
  -> stop thinking sound if playing
  -> play configured invalid-prompt/failure sound
  -> log telemetry
  -> return to idle wake listening
```

### 28.3 Local cancel/stop command flow

```text
Wake word detected
  -> wake acknowledgement sound starts
  -> prompt capture starts
  -> prompt capture ends
  -> local command recognizer recognizes cancel/stop intent
  -> play configured cancel/stop acknowledgement sound
  -> cancel active process if one exists
  -> return to idle wake listening
  -> preserve LLM conversation context until timeout or new conversation command
```

### 28.4 New conversation command flow

```text
Wake word detected
  -> wake acknowledgement sound starts
  -> prompt capture starts
  -> prompt capture ends
  -> local command recognizer recognizes new conversation intent
  -> play configured new-conversation acknowledgement sound
  -> discard locally stored LLM conversation context
  -> immediately begin a new prompt-capture window without requiring wake word again
  -> next valid prompt starts a new LLM conversation
```

### 28.5 Barge-in flow during processing/playback

```text
Active process in progress
  -> wake word detected locally
  -> immediately stop current processing/playback
  -> play wake acknowledgement sound
  -> begin prompt capture
  -> local command recognizer checks captured utterance
  -> if command: handle locally
  -> if not command: process as a new prompt
```

### 28.6 Conversation timeout flow

```text
Assistant response playback finishes
  -> conversation inactivity timer starts
  -> no valid prompt occurs before timeout
  -> timeout expires silently
  -> locally stored LLM conversation context is discarded
  -> next valid prompt begins a new LLM conversation
```

## 29. Configuration inventory

This section consolidates known configurable values.

### 29.1 Wake/configuration

- wake phrase list
- active wake phrase
- wake-word engine/model settings where applicable
- wake-word sensitivity/confidence threshold where applicable

### 29.2 Prompt capture

- minimum prompt capture duration, default 3 seconds
- maximum prompt duration, default 2 minutes
- silence duration threshold
- silence detection threshold/sensitivity where applicable

### 29.3 Conversation

- conversation inactivity timeout, default 1 minute

### 29.4 Commands

- command intents
- command aliases per intent
- command behavior mapping
- command acknowledgement sound per intent
- command enabled/disabled state if implemented

### 29.5 Sound effects

- wake acknowledgement sound
- invalid prompt sound
- prompt accepted sound
- thinking/processing sound
- command accepted sounds
- new conversation sound
- per-failure sounds
- other future sound events

### 29.6 Telemetry/retention

- enable/disable audio file storage
- retention duration, default 1 year
- cleanup interval, default daily
- cleanup time of day

### 29.7 Service connections

- STT endpoint/settings/model identifier/timeouts
- LLM endpoint/settings/model identifier/timeouts
- TTS endpoint/settings/model identifier/timeouts

Connection values are supplied separately at implementation time.

### 29.8 Admin portal/admin operations

- configuration export/import
- manual cleanup action
- restart assistant service action
- reboot machine action

## 30. Out-of-scope command categories for v1

The command registry should be extensible, but v1 should not implement command categories beyond those explicitly required.

The following are out of scope as v1 local voice commands:

- volume control
- restart assistant service
- reboot machine
- system settings changes
- security-sensitive operations
- help
- repeat last response
- timers
- alarms
- reminders
- announcements
- smart-home control
- routines/multi-step automation
- status check
- network check
- change voice
- change conversation mode

Some of these may exist as admin portal capabilities. For example, restart/reboot is out of scope as a voice command but in scope as an admin portal action.

## 31. Decision log

This section records the key decisions from the requirements conversation.

### 31.1 Wake-word decisions

1. Wake-word listening cannot be disabled during normal operation.
2. Wake-word listening is state-dependent: active in idle, processing, and playback; not treated as a new wake event during prompt capture.
3. V1 starts with one wake phrase, but the design allows multiple wake phrases later.
4. Prompt recording starts at the same time as the wake acknowledgement sound.
5. The wake acknowledgement sound plays every time wake is detected in a wake-active state.
6. Wake during processing or playback is barge-in and cancels the active process.
7. Wake during prompt capture is captured as part of the prompt, not as a new wake trigger.

### 31.2 Prompt capture decisions

1. Prompt capture ends by silence or maximum duration.
2. Minimum prompt capture duration is configurable and defaults to 3 seconds.
3. Maximum prompt duration is configurable and defaults to 2 minutes.
4. Silence detection settings are configurable.
5. Prompt validity is determined by STT result after local command recognition.

### 31.3 Command decisions

1. Commands are recognized locally on the thin client.
2. Command recognition occurs after prompt capture and before main STT.
3. Command recognition evaluates the whole utterance, not substring presence.
4. Commands are organized in a configurable registry.
5. Commands have intents, aliases, behaviors, and acknowledgement sounds.
6. Multiple aliases per command intent are supported.
7. V1 command intents are cancel/stop and new conversation.
8. Future commands are not defined in detail for v1.
9. Privileged voice commands are out of scope for v1.

### 31.4 Processing decisions

1. Active process includes STT, LLM, TTS, and response playback.
2. Thinking sound loops continuously during processing.
3. Thinking sound stops immediately when response playback begins.
4. Barge-in applies during STT, LLM, TTS, and playback.
5. Barge-in immediately cancels the current process.
6. If barge-in follow-up speech is not a command, it becomes a new prompt.
7. Stop/cancel have the same meaning in all contexts.

### 31.5 Conversation decisions

1. Conversation context is preserved across nearby wake-word-initiated prompts.
2. Every prompt requires the wake word.
3. No wake-word-free follow-up window.
4. Conversation timeout is configurable and defaults to 1 minute.
5. Timeout starts after assistant response playback finishes.
6. Timeout expiration is silent.
7. New conversation command clears local LLM conversation context.
8. New conversation command plays acknowledgement and immediately starts another prompt-capture window without requiring wake again.
9. Thin client does not impose conversation history size limits.
10. Thin client does not summarize old context.

### 31.6 Feedback/sound decisions

1. Local device feedback is sound-effect-only in v1.
2. No local spoken status phrases.
3. Every sound event is independently configurable.
4. Different sound events may initially reference the same file.
5. WAV files are preferred/recommended for local sounds.
6. No deep programmatic audio validation is required in v1.
7. Admin portal provides sound playback/test controls.

### 31.7 Telemetry decisions

1. Pre-wake audio is never stored, logged, uploaded, or sent to STT/LLM/TTS.
2. Post-wake interactions may be logged.
3. Telemetry should be comprehensive.
4. Store metadata, transitions, success/failure, durations, transcripts, prompt audio, and TTS audio.
5. Audio storage is configurable true/false.
6. Retention defaults to 1 year and is configurable.
7. Cleanup runs daily by default and is configurable.
8. Manual cleanup action exists in admin portal.
9. Manual per-record telemetry deletion is out of scope.
10. Bulk telemetry export is out of scope.

### 31.8 Admin portal decisions

1. Admin portal is in scope.
2. Admin portal is available on the local network through the device IP.
3. No authentication or authorization in v1.
4. Intended for local home-network access only, not public internet exposure.
5. Portal exposes all configurable settings.
6. Portal shows current/default/current values by default.
7. Configuration changes use grouped apply.
8. Changes persist across restart.
9. Some settings, especially STT/LLM/TTS connection settings, require restart before becoming active.
10. Portal marks restart-required settings.
11. Portal should show active vs pending values where restart is required.
12. Portal supports config export/import.
13. Portal does not support reset-to-defaults in v1.
14. Portal manages sound upload/list/delete/select/test.
15. Portal shows live and historical telemetry with filtering/search.
16. Portal shows runtime status.
17. Portal shows component health.
18. Portal supports typed LLM/TTS test interaction, with output through the physical speakerphone.
19. Portal supports microphone test recording.
20. Portal supports wake-word testing/debugging.
21. Portal supports manual cleanup.
22. Portal supports assistant service restart and thin-client reboot.
23. Restart/reboot actions require confirmation.
24. After reboot, services should auto-start and return to normal wake listening.

## 32. Known implementation-sensitive areas

These are not unresolved product requirements, but implementation agents should treat them carefully.

### 32.1 Audio cue sequencing

The design contains multiple local sounds, including wake acknowledgement, prompt accepted, command accepted, thinking loop, and failure sounds. Implementation must avoid confusing overlap.

Especially important:

- Prompt capture starts while wake acknowledgement plays.
- Thinking sound may be active during STT.
- Prompt validity is known only after STT returns text.
- Response playback must stop the thinking sound immediately.
- Barge-in must stop whatever audio/process is active.

The implementation should define a clear local audio priority/serialization system that preserves these behaviors.

### 32.2 Speakerphone echo/noise cancellation

The attached speakerphone is expected to provide noise cancellation/echo suppression. The design depends on this for recording while acknowledgement sounds begin.

Implementation should still avoid treating local playback sounds as meaningful user speech where possible.

### 32.3 Local command recognition engine

The local command recognizer must not become a continuous general-purpose listener for all commands. It runs after wake and prompt capture.

It must be efficient enough for the thin client and should support configurable aliases per intent.

### 32.4 Backend service connection details

STT, LLM, and TTS connection details are intentionally absent from this document. They will be supplied separately during implementation.

The design only requires that such settings be configurable in the admin portal and that changes require restart to take effect.

### 32.5 Admin portal security posture

The v1 admin portal has no authentication or authorization by requirement.

Because it can expose logs, audio, transcripts, configuration, testing tools, restart, and reboot controls, it must be treated as local-network-only. If future deployment changes expose it beyond the trusted home network, security requirements must be revisited.

## 33. Final implementation handoff instruction

An implementation agent should use this document as the behavioral source of truth for v1.

The agent may choose implementation frameworks, libraries, service wrappers, storage formats, UI frameworks, and internal state-machine names, but those choices must preserve the requirements above.

The implementation must not silently add unrelated assistant features, smart-home behavior, proactive alerts, physical-input assumptions, authentication, or voice commands beyond the defined v1 scope.
