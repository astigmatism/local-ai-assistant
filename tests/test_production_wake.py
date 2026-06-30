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
    DEFAULT_WAKE_PHRASE,
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


def _install_fake_wake_commands(tmp_path, monkeypatch, *, decoder_python: str, audio_bytes: int = 800):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    arecord = bin_dir / "arecord"
    decoder = bin_dir / "pocketsphinx_continuous"
    arecord.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.stdout.buffer.write(b'\\0' * {audio_bytes})\n"
        "sys.stdout.flush()\n"
    )
    decoder.write_text("#!/usr/bin/env python3\n" + decoder_python)
    arecord.chmod(0o755)
    decoder.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr(pocketsphinx_wake.shutil, "which", lambda name: None if name == "stdbuf" else str(bin_dir / name))
    return bin_dir


def _fast_args(tmp_path, *extra: str):
    hmm = tmp_path / "hmm"
    hmm.mkdir(exist_ok=True)
    dict_path = tmp_path / "dict.txt"
    if not dict_path.exists():
        dict_path.write_text("hello HH AH L OW\n", encoding="utf-8")
    return pocketsphinx_wake.build_parser().parse_args(
        [
            "--phrase",
            DEFAULT_WAKE_PHRASE,
            "--device",
            "fake",
            "--sample-rate",
            "1000",
            "--window-seconds",
            "0.1",
            "--hop-seconds",
            "0.05",
            "--hmm",
            str(hmm),
            "--dict",
            str(dict_path),
            "--custom-dict-path",
            str(tmp_path / "wake-custom.dict"),
            *extra,
        ]
    )


def test_default_config_uses_packaged_production_external_wake_engine_with_rosalina():
    cfg = AssistantConfig()
    assert cfg.wake.engine == "external_command"
    assert cfg.wake.active_wake_phrase == "Rosalina"
    assert cfg.wake.wake_phrases == ["Rosalina"]
    assert cfg.wake.external_command == DEFAULT_PRODUCTION_WAKE_COMMAND
    assert cfg.wake.external_health_command[-1] == "--self-test"


def test_config_store_migrates_old_computer_default_to_rosalina(tmp_path):
    config_path = tmp_path / "config.json"
    old = AssistantConfig().public_dict()
    old["wake"]["wake_phrases"] = ["computer"]
    old["wake"]["active_wake_phrase"] = "computer"
    config_path.write_text(json.dumps(old), encoding="utf-8")

    store = ConfigStore(config_path)

    assert store.get_saved().wake.active_wake_phrase == "Rosalina"
    assert "Rosalina" in store.get_saved().wake.wake_phrases
    assert "computer" not in store.get_saved().wake.wake_phrases


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


def test_config_store_migrates_simulated_wake_to_rosalina_without_overwriting_unrelated_settings(tmp_path):
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
    assert result.saved["wake"]["active_wake_phrase"] == "Rosalina"
    assert result.saved["wake"]["wake_phrases"][0] == "Rosalina"
    assert "computer" not in result.saved["wake"]["wake_phrases"]
    assert result.saved["wake"]["external_command"] == DEFAULT_PRODUCTION_WAKE_COMMAND
    assert result.saved["services"]["llm"]["url"] == "http://router.local:11434/api/chat"


def test_external_wake_parser_accepts_json_confidence_and_source():
    engine = ExternalCommandWakeWordEngine([sys.executable, "-c", "pass"], DEFAULT_WAKE_PHRASE)
    detection = engine.parse_detection_line(
        json.dumps({"event": "wake", "phrase": DEFAULT_WAKE_PHRASE, "confidence": 0.73, "engine": "test_detector"})
    )
    assert detection is not None
    assert detection.phrase == DEFAULT_WAKE_PHRASE
    assert detection.confidence == pytest.approx(0.73)
    assert detection.engine == "external_command:test_detector"


def test_pocketsphinx_wake_uses_overlapping_arecord_stream_not_pocketsphinx_live_mic(tmp_path):
    dict_path = tmp_path / "dict.txt"
    dict_path.write_text("rosalina R OW Z AH L IY N AH\n", encoding="utf-8")
    args = pocketsphinx_wake.build_parser().parse_args(
        [
            "--phrase",
            "Rosalina",
            "--device",
            "plughw:0,0",
            "--sample-rate",
            "16000",
            "--dict",
            str(dict_path),
            "--window-seconds",
            "4",
            "--hop-seconds",
            "1",
        ]
    )

    capture_command = pocketsphinx_wake.build_arecord_command(args)
    decoder_command = pocketsphinx_wake.build_pocketsphinx_command(args)

    assert capture_command[:4] == ["arecord", "-q", "-D", "plughw:0,0"]
    assert "-f" in capture_command
    assert "S16_LE" in capture_command
    assert "-r" in capture_command
    assert "16000" in capture_command
    assert "-c" in capture_command
    assert "1" in capture_command
    assert "-t" in capture_command
    assert "raw" in capture_command
    assert "-d" not in capture_command
    assert pocketsphinx_wake.window_bytes(args) == 128000
    assert pocketsphinx_wake.hop_bytes(args) == 32000
    assert pocketsphinx_wake.overlap_seconds(args) == pytest.approx(3.0)
    assert "pocketsphinx_continuous" in decoder_command
    assert "-infile" in decoder_command
    assert "/dev/stdin" in decoder_command
    assert "-keyphrase" in decoder_command
    assert "rosalina" in decoder_command
    assert "-inmic" not in decoder_command
    assert "-adcdev" not in decoder_command


def test_pocketsphinx_custom_dictionary_adds_rosalina_when_base_dict_lacks_it(tmp_path):
    dict_path = tmp_path / "dict.txt"
    dict_path.write_text("hello HH AH L OW\n", encoding="utf-8")
    custom_path = tmp_path / "custom.dict"
    args = pocketsphinx_wake.build_parser().parse_args(
        ["--phrase", "Rosalina", "--dict", str(dict_path), "--custom-dict-path", str(custom_path)]
    )

    resolution = pocketsphinx_wake.resolve_dictionary(args)
    command = pocketsphinx_wake.build_pocketsphinx_command(args)
    custom_text = custom_path.read_text(encoding="utf-8")

    assert resolution.path == str(custom_path)
    assert resolution.added_pronunciations["rosalina"][0] == "R OW Z AH L IY N AH"
    assert "rosalina R OW Z AH L IY N AH" in custom_text
    assert "rosalina(2) R OW S AH L IY N AH" in custom_text
    assert str(custom_path) in command
    assert "rosalina" in command


def test_pocketsphinx_wake_run_decodes_overlapping_window_and_emits_json(tmp_path, monkeypatch, capsys):
    _install_fake_wake_commands(
        tmp_path,
        monkeypatch,
        decoder_python=(
            "import sys\n"
            "sys.stdin.buffer.read()\n"
            "print('Rosalina', flush=True)\n"
        ),
    )
    args = _fast_args(tmp_path, "--max-chunks", "1", "--cooldown-seconds", "0")

    assert pocketsphinx_wake.run(args) == 0
    output_lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]

    assert len(output_lines) == 1
    payload = json.loads(output_lines[0])
    assert payload["event"] == "wake"
    assert payload["phrase"] == "Rosalina"
    assert payload["engine"] == "pocketsphinx_continuous_arecord_overlap"
    assert payload["capture_backend"] == "arecord_stream_overlap"
    assert payload["window_seconds"] == pytest.approx(0.1)
    assert payload["hop_seconds"] == pytest.approx(0.05)


def test_pocketsphinx_wake_loop_continues_across_empty_overlapping_windows(tmp_path, monkeypatch, capsys):
    counter = tmp_path / "count.txt"
    _install_fake_wake_commands(
        tmp_path,
        monkeypatch,
        decoder_python=(
            "from pathlib import Path\n"
            "import sys\n"
            f"counter = Path({str(counter)!r})\n"
            "try:\n"
            "    value = int(counter.read_text())\n"
            "except FileNotFoundError:\n"
            "    value = 0\n"
            "counter.write_text(str(value + 1))\n"
            "sys.stdin.buffer.read()\n"
            "if value == 1:\n"
            "    print('Rosalina', flush=True)\n"
        ),
    )
    args = _fast_args(tmp_path, "--max-chunks", "2", "--cooldown-seconds", "0")

    assert pocketsphinx_wake.run(args) == 0
    output_lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]

    assert len(output_lines) == 1
    payload = json.loads(output_lines[0])
    assert payload["engine"] == "pocketsphinx_continuous_arecord_overlap"
    assert payload["capture_backend"] == "arecord_stream_overlap"
    assert payload["phrase"] == "Rosalina"
    assert counter.read_text() == "2"


def test_pocketsphinx_self_test_reports_overlap_backend_and_custom_pronunciation(tmp_path, monkeypatch, capsys):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ["arecord", "pocketsphinx_continuous", "stdbuf"]:
        command = bin_dir / name
        command.write_text("#!/bin/sh\nexit 0\n")
        command.chmod(0o755)
    monkeypatch.setattr(pocketsphinx_wake.shutil, "which", lambda name: str(bin_dir / name))
    hmm = tmp_path / "hmm"
    hmm.mkdir()
    dict_path = tmp_path / "dict.txt"
    dict_path.write_text("hello HH AH L OW\n", encoding="utf-8")
    custom_path = tmp_path / "custom.dict"
    args = pocketsphinx_wake.build_parser().parse_args(
        ["--phrase", "Rosalina", "--hmm", str(hmm), "--dict", str(dict_path), "--custom-dict-path", str(custom_path), "--self-test"]
    )

    assert pocketsphinx_wake.self_test(args) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["engine"] == "pocketsphinx_continuous_arecord_overlap"
    assert payload["capture_backend"] == "arecord_stream_overlap"
    assert payload["window_seconds"] == pytest.approx(4.0)
    assert payload["hop_seconds"] == pytest.approx(1.0)
    assert payload["overlap_seconds"] == pytest.approx(3.0)
    assert payload["cooldown_seconds"] == pytest.approx(1.5)
    assert payload["custom_pronunciations_added"]["rosalina"][0] == "R OW Z AH L IY N AH"
    assert payload["dict"] == str(custom_path)
    assert "-d" not in payload["arecord_command"]


def test_pocketsphinx_diagnostic_summary_reports_detection_statistics(tmp_path, monkeypatch, capsys):
    counter = tmp_path / "count.txt"
    _install_fake_wake_commands(
        tmp_path,
        monkeypatch,
        decoder_python=(
            "from pathlib import Path\n"
            "import sys\n"
            f"counter = Path({str(counter)!r})\n"
            "try:\n"
            "    value = int(counter.read_text())\n"
            "except FileNotFoundError:\n"
            "    value = 0\n"
            "counter.write_text(str(value + 1))\n"
            "sys.stdin.buffer.read()\n"
            "print('Rosalina' if value == 1 else 'noise', flush=True)\n"
        ),
    )
    args = _fast_args(tmp_path, "--max-chunks", "2", "--cooldown-seconds", "0", "--diagnostic-summary")

    assert pocketsphinx_wake.run(args) == 0
    payloads = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]

    assert [payload["event"] for payload in payloads] == ["wake", "diagnostic_summary"]
    summary = payloads[-1]
    assert summary["chunks_processed"] == 2
    assert summary["detections"] == 1
    assert summary["last_detection_phrase"] == "Rosalina"
    assert summary["backend"] == "pocketsphinx_continuous_arecord_overlap"
    assert summary["threshold"] == args.threshold
    assert summary["window_seconds"] == pytest.approx(0.1)
    assert summary["hop_seconds"] == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_external_wake_engine_reads_stdout_detection_and_stops_process():
    script = "import json,time; print(json.dumps({'event':'wake','phrase':'Rosalina','confidence':0.91}), flush=True); time.sleep(60)"
    engine = ExternalCommandWakeWordEngine([sys.executable, "-c", script], DEFAULT_WAKE_PHRASE)
    stop_event = asyncio.Event()
    detections = []

    async def callback(detection):
        detections.append(detection)
        stop_event.set()

    await asyncio.wait_for(engine.run(callback, stop_event), timeout=3)

    assert len(detections) == 1
    assert detections[0].phrase == "Rosalina"
    assert detections[0].confidence == pytest.approx(0.91)
    status = engine.status()
    assert status["process_running"] is False
    assert status["detection_count"] == 1
    assert status["packaged_backend"] == "pocketsphinx_continuous_arecord_overlap"
    assert status["capture_backend"] == "arecord_stream_overlap"
    assert status["window_seconds"] == pytest.approx(4.0)
    assert status["hop_seconds"] == pytest.approx(1.0)
    assert status["threshold"] == pocketsphinx_wake.threshold_from_sensitivity(0.5)
    assert status["channels"] == 1


@pytest.mark.asyncio
async def test_external_wake_engine_surfaces_stderr_when_process_exits():
    script = "import sys; print('arecord exited with code 1', file=sys.stderr, flush=True); sys.exit(1)"
    engine = ExternalCommandWakeWordEngine([sys.executable, "-c", script], DEFAULT_WAKE_PHRASE)
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
    engine = ExternalCommandWakeWordEngine([sys.executable, "-c", script], DEFAULT_WAKE_PHRASE)
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
