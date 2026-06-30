from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

from .config import LLMServiceConfig, STTServiceConfig, TTSServiceConfig


class ServiceError(RuntimeError):
    component = "service"


class ServiceAuthError(ServiceError):
    component = "auth"


class NetworkServiceError(ServiceError):
    component = "network"


class MalformedServiceResponse(ServiceError):
    component = "malformed_response"


class STTClient:
    def __init__(self, config: STTServiceConfig):
        self.config = config

    def _api_key(self) -> str:
        key = os.getenv(self.config.api_key_env, "")
        if not key:
            raise ServiceAuthError(f"Missing STT API key environment variable {self.config.api_key_env}")
        return key

    async def transcribe(self, wav_path: str | Path) -> str:
        headers = {"Authorization": f"Bearer {self._api_key()}"}
        data = {"model": self.config.model, "response_format": self.config.response_format}
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
                with Path(wav_path).open("rb") as handle:
                    files = {"file": (Path(wav_path).name, handle, "audio/wav")}
                    response = await client.post(self.config.url, headers=headers, data=data, files=files)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise ServiceAuthError(f"STT authentication failed with HTTP {exc.response.status_code}") from exc
            raise ServiceError(f"STT request failed with HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise NetworkServiceError(f"STT network error: {exc}") from exc
        if self.config.response_format == "text":
            return response.text.strip()
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise MalformedServiceResponse("STT response was not JSON") from exc
        text = body.get("text")
        if not isinstance(text, str):
            raise MalformedServiceResponse("STT response did not contain text")
        return text.strip()


class LLMClient:
    def __init__(self, config: LLMServiceConfig):
        self.config = config

    def build_payload(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        # The Ollama router owns active model selection. The assistant intentionally sends no
        # model field here.
        return {"stream": self.config.stream, "messages": messages}

    async def chat(self, messages: list[dict[str, str]]) -> str:
        payload = self.build_payload(messages)
        if "model" in payload:
            raise RuntimeError("LLM payload must not include a model field")
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
                response = await client.post(self.config.url, json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ServiceError(f"LLM request failed with HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise NetworkServiceError(f"LLM network error: {exc}") from exc
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise MalformedServiceResponse("LLM response was not JSON") from exc
        message = data.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str) and message["content"].strip():
            return message["content"].strip()
        # Defensive compatibility with OpenAI-style responses during local testing only.
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                content = first.get("message", {}).get("content") if isinstance(first.get("message"), dict) else None
                if isinstance(content, str) and content.strip():
                    return content.strip()
        raise MalformedServiceResponse("LLM returned no message.content text")

    async def health(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=min(self.config.timeout_seconds, 10.0)) as client:
                response = await client.get(self.config.health_url)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise NetworkServiceError(f"LLM health check failed: {exc}") from exc


class TTSClient:
    def __init__(self, config: TTSServiceConfig):
        self.config = config

    def _api_key(self) -> str:
        key = os.getenv(self.config.api_key_env, "")
        if not key:
            raise ServiceAuthError(f"Missing TTS API key environment variable {self.config.api_key_env}")
        return key

    def build_payload(self, text: str) -> dict[str, Any]:
        return {
            "model": self.config.model,
            "voice": self.config.voice,
            "input": text,
            "response_format": self.config.response_format,
            "speed": self.config.speed,
            "stream": False,
            "volume_multiplier": self.config.volume_multiplier,
        }

    async def synthesize(self, text: str, output_path: str | Path) -> Path:
        headers = {"Authorization": f"Bearer {self._api_key()}", "Content-Type": "application/json"}
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout_seconds, follow_redirects=True) as client:
                response = await client.post(self.config.url, headers=headers, json=self.build_payload(text))
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise ServiceAuthError(f"TTS authentication failed with HTTP {exc.response.status_code}") from exc
            raise ServiceError(f"TTS request failed with HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise NetworkServiceError(f"TTS network error: {exc}") from exc
        if not response.content:
            raise MalformedServiceResponse("TTS returned an empty audio response")
        path.write_bytes(response.content)
        return path
