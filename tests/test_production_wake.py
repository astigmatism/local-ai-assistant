from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest
from pydantic import ValidationError

from voice_assistant import pocketsphinx_wake
from voice_assistant.config import (
    DEFAULT_PRODUCTION_WAKE_COMMAND,
    AssistantConfig,
    ConfigStore,
)
from voice_assistant.health import HealthChecker
from voice_assistant.wake import ExternalCommandWakeWordEngine, SimulatedWakeWordEngine, build_wake_engine


async def _wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached before timeout")


def test_default_config_uses_packaged_production_external_wake_engine():
    cfg = AssistantConfig()
    assert cfg.wake.engine == "external_command"
    assert cfg.wake.active_wake_phrase == "computer"
    assert cfg.wake.external_command == DEFAULT_PRODUCTION_WAKE_COMMAND
    assert cfg.wake.external_health_command[-1] == "--self-test"


def test_simulated_wake_remains_available_for_diagnostics():
    data = AssistantConfig().public_dict()
    data["wake"]["engine"] = "simulated"
    data["wake"]["external_command"] = []
    data["wake"]["external_health_command"] = []
    cfg = AssistantConfig.model_validate(data)
    assert isinstance(build_wake_engine(cfg), SimulatedWakeWordEngine)


def test_external_wake_config_requires_command():
    data = AssistantConfig().public_dict()
    data["wake"]["engine"] = "external_command"
    data["wake"]["external_command"] = []
    with pytest.raises(ValidationError):
        AssistantConfig.model_validate(data)


def test_config_store_migrates_simulated_wake_without_overwriting_unrelated_settings(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    data = store.get_saved().public_dict()
    data["wake"]["engine"] = "simulated"
    data["wake"]["external_command"] = []
    data["wake"]["external_health_command"] = []
    data["wake"]["wake_phrases"] = ["computer", "assistant"]
    data["wake"]["active_wake_phrase"] = "assistant"
    data["services"]["llm"]["url"] = "http://router.local:11434/api/chat"
    store.apply_config(data)

    result = store.migrate_to_production_wake()

    assert result.saved["wake"]["engine"] == "external_command"
    assert result.saved["wake"]["active_wake_phrase"] == "assistant"
    assert result.saved["wake"]["external_command"] == DEFAULT_PRODUCTION_WAKE_COMMAND
    assert result.saved["services"]["llm"]["url"] == "http://router.local:11434/api/chat"


def test_external_wake_parser_accepts_json_confidence_and_source():
    engine = ExternalCommandWakeWordEngine([sys.executable, "-c", "pass"], "computer")
    detection = engine.parse_detection_line(
        json.dumps({"event": "wake", "phrase": "computer", "confidence": 0.73, "engine": "test_detector"})
    )
    assert detection is not None
    assert detection.phrase == "computer"
    assert detection.confidence == pytest.approx(0.73)
    assert detection.engine == "external_command:test_detector"


def test_pocketsphinx_wake_uses_arecord_pipe_not_pocketsphinx_live_mic():
    args = pocketsphinx_wake.build_parser().parse_args(
        ["--phrase", "computer", "--device", "plughw:0,0", "--sample-rate", "16000"]
    )

    capture_command = pocketsphinx_wake.build_arecord_command(args)
    decoder_command = pocketsphinx_wake.build_pocketsphinx_command(args)

    assert capture_command[:4] == ["arecord", "-q", "-D", "plughw:0,0"]
    assert "-f" in capture_command
    assert "S16_LE" in capture_command
    assert "-r" in capture_command
    assert "16000" in capture_command
    assert "-t" in capture_command
    assert "raw" in capture_command
    assert "pocketsphinx_continuous" in decoder_command
    assert "-infile" in decoder_command
    assert "/dev/stdin" in decoder_command
    assert "-keyphrase" in decoder_command
    assert "computer" in decoder_command
    assert "-inmic" not in decoder_command
    assert "-adcdev" not in decoder_command


def test_pocketsphinx_wake_run_pipes_arecord_to_decoder_and_emits_json(tmp_path, monkeypatch, capsys):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    arecord = bin_dir / "arecord"
    decoder = bin_dir / "pocketsphinx_continuous"
    arecord.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdout.buffer.write(b'\\0' * 6400)\n"
        "sys.stdout.flush()\n"
    )
    decoder.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdin.buffer.read(1)\n"
        "print('computer', flush=True)\n"
        "sys.stdin.buffer.read()\n"
    )
    arecord.chmod(0o755)
    decoder.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr(pocketsphinx_wake.shutil, "which", lambda name: None if name == "stdbuf" else str(bin_dir / name))
    hmm = tmp_path / "hmm"
    hmm.mkdir()
    dict_path = tmp_path / "dict.txt"
    dict_path.write_text("COMPUTER K AH M P Y UW T ER\n")
    args = pocketsphinx_wake.build_parser().parse_args(
        ["--phrase", "computer", "--device", "fake", "--hmm", str(hmm), "--dict", str(dict_path)]
    )

    assert pocketsphinx_wake.run(args) == 0
    output_lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]

    assert len(output_lines) == 1
    payload = json.loads(output_lines[0])
    assert payload["event"] == "wake"
    assert payload["phrase"] == "computer"
    assert payload["engine"] == "pocketsphinx_continuous_arecord_pipe"


@pytest.mark.asyncio
async def test_external_wake_engine_reads_stdout_detection_and_stops_process():
    script = "import json,time; print(json.dumps({'event':'wake','phrase':'computer','confidence':0.91}), flush=True); time.sleep(60)"
    engine = ExternalCommandWakeWordEngine([sys.executable, "-c", script], "computer")
    stop_event = asyncio.Event()
    detections = []

    async def callback(detection):
        detections.append(detection)
        stop_event.set()

    await asyncio.wait_for(engine.run(callback, stop_event), timeout=3)

    assert len(detections) == 1
    assert detections[0].confidence == pytest.approx(0.91)
    assert engine.status()["process_running"] is False
    assert engine.status()["detection_count"] == 1


@pytest.mark.asyncio
async def test_external_wake_engine_surfaces_stderr_when_process_exits():
    script = "import sys; print('arecord exited with code 1', file=sys.stderr, flush=True); sys.exit(1)"
    engine = ExternalCommandWakeWordEngine([sys.executable, "-c", script], "computer")
    stop_event = asyncio.Event()

    async def callback(detection):  # pragma: no cover - script never emits detections
        pass

    task = asyncio.create_task(engine.run(callback, stop_event))
    await _wait_for(lambda: engine.status()["last_exit_code"] == 1)
    stop_event.set()
    await asyncio.wait_for(task, timeout=3)
    status = engine.status()
    assert status["process_running"] is False
    assert "arecord exited with code 1" in status["last_error"]
    assert status["last_stderr_line"] == "arecord exited with code 1"


@pytest.mark.asyncio
async def test_external_wake_engine_pause_releases_process_and_resume_restarts_it():
    script = "import time; time.sleep(60)"
    engine = ExternalCommandWakeWordEngine([sys.executable, "-c", script], "computer")
    stop_event = asyncio.Event()

    async def callback(detection):  # pragma: no cover - script never emits detections
        pass

    task = asyncio.create_task(engine.run(callback, stop_event))
    await _wait_for(lambda: engine.status()["process_running"] is True)
    await engine.pause("prompt_capture")
    await _wait_for(lambda: engine.status()["process_running"] is False and engine.status()["paused"] is True)
    await engine.resume()
    await _wait_for(lambda: engine.status()["process_running"] is True and engine.status()["paused"] is False)
    stop_event.set()
    await asyncio.wait_for(task, timeout=3)
    assert engine.status()["process_running"] is False


@pytest.mark.asyncio
async def test_health_reports_simulated_wake_as_diagnostic_only():
    data = AssistantConfig().public_dict()
    data["wake"]["engine"] = "simulated"
    data["wake"]["external_command"] = []
    cfg = AssistantConfig.model_validate(data)
    item = await HealthChecker(cfg).check_wake_engine()
    assert item.ok is False
    assert item.severity == "warning"
    assert "diagnostics" in item.detail


@pytest.mark.asyncio
async def test_health_reports_missing_external_command_and_failed_health_command():
    data = AssistantConfig().public_dict()
    data["wake"]["external_command"] = ["definitely-missing-wake-command"]
    data["wake"]["external_health_command"] = []
    missing_cfg = AssistantConfig.model_validate(data)
    missing_item = await HealthChecker(missing_cfg).check_wake_engine()
    assert missing_item.ok is False
    assert "not found" in missing_item.detail

    data = AssistantConfig().public_dict()
    data["wake"]["external_command"] = [sys.executable]
    data["wake"]["external_health_command"] = [sys.executable, "-c", "import sys; print('missing model'); sys.exit(1)"]
    failed_cfg = AssistantConfig.model_validate(data)
    failed_item = await HealthChecker(failed_cfg).check_wake_engine()
    assert failed_item.ok is False
    assert "missing model" in failed_item.detail


@pytest.mark.asyncio
async def test_health_reports_production_wake_task_not_actually_running():
    cfg = AssistantConfig()
    item = await HealthChecker(cfg, wake_runtime_status={"engine": "external_command", "task_running": False}).check_wake_runtime()
    assert item.ok is False
    assert "not running" in item.detail
