"""Gemini API (Interactions API, `v1beta/interactions`) – TTS přes API klíč.

Celé napojení na API je jen v tomto souboru: API je zatím v betě, takže
případné změny se opraví tady.
"""

from __future__ import annotations

import base64
import os
import random
import time
from typing import Any

import httpx

from ..audio import AudioError, wav_to_pcm
from ..resolve import Job
from .base import EngineError

API_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
RETRY_STATUS = {408, 429, 500, 502, 503, 504}


def build_request(job: Job, *, model: str, sample_rate: int) -> dict[str, Any]:
    part: dict[str, Any] = {"type": "text", "text": job.text}
    if job.style:
        part["annotations"] = [{"type": "speech_metadata", "style": job.style}]
    return {
        "model": model,
        "input": [{"type": "user_input", "content": [part]}],
        "response_format": {"type": "audio", "mime_type": "audio/l16", "sample_rate": sample_rate},
        "generation_config": {"speech_config": [{"voice": job.voice}]},
    }


def _find_audio(node: Any) -> list[dict[str, Any]]:
    """Najde všechny bloky {"type": "audio", "data": …} kdekoli v odpovědi."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("type") == "audio" and isinstance(node.get("data"), str):
            found.append(node)
        for value in node.values():
            found.extend(_find_audio(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_find_audio(value))
    return found


def extract_pcm(response: dict[str, Any], *, sample_rate: int) -> bytes:
    """Audio je v `steps[type=model_output].content[type=audio].data` (base64)."""
    blocks: list[dict[str, Any]] = []
    for step in response.get("steps") or []:
        if isinstance(step, dict) and step.get("type") == "model_output":
            for item in step.get("content") or []:
                if isinstance(item, dict) and item.get("type") == "audio" and item.get("data"):
                    blocks.append(item)
    if not blocks:
        blocks = _find_audio(response)
    if not blocks:
        snippet = str(response)[:500]
        raise EngineError(f"odpověď neobsahuje audio (blokovaný obsah?): {snippet}")
    data = base64.b64decode(blocks[-1]["data"])
    if data[:4] == b"RIFF":
        try:
            return wav_to_pcm(data, sample_rate)
        except AudioError as exc:
            raise EngineError(str(exc)) from exc
    mime = str(blocks[-1].get("mime_type") or "audio/l16")
    if "mulaw" in mime or "alaw" in mime:
        raise EngineError(f"nepodporovaný formát audia {mime}")
    if len(data) % 2:
        data = data[:-1]
    return data


def _error_message(resp: httpx.Response) -> str:
    """Google vrací chybu jako {"error": …} i jako [{"error": …}]."""
    try:
        payload = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}: {resp.text[:300]}"
    if isinstance(payload, list) and payload:
        payload = payload[0]
    err = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(err, dict):
        return f"HTTP {resp.status_code}: {resp.text[:300]}"
    msg = err.get("message") or resp.text[:300]
    status = err.get("status")
    return f"HTTP {resp.status_code}{' ' + status if status else ''}: {msg}"


class GeminiEngine:
    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        sample_rate: int = 24000,
        timeout: float = 300.0,
        max_retries: int = 5,
        transport: httpx.BaseTransport | None = None,
    ):
        self.model = model
        self.sample_rate = sample_rate
        self.max_retries = max_retries
        self._client = httpx.Client(
            timeout=timeout,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            transport=transport,
        )

    @classmethod
    def from_env(cls, *, model: str, sample_rate: int, api_key_env: str = "GEMINI_API_KEY") -> GeminiEngine:
        key = os.environ.get(api_key_env) or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise EngineError(
                f"chybí API klíč – nastav proměnnou prostředí {api_key_env} "
                "(klíč získáš na https://aistudio.google.com/apikey)"
            )
        return cls(api_key=key, model=model, sample_rate=sample_rate)

    def synthesize(self, job: Job) -> bytes:
        body = build_request(job, model=self.model, sample_rate=self.sample_rate)
        last_error = "neznámá chyba"
        retry_after: float | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                time.sleep(self._backoff(attempt, retry_after))
                retry_after = None
            try:
                resp = self._client.post(API_URL, json=body)
            except httpx.TransportError as exc:
                last_error = f"síťová chyba: {exc}"
                continue
            if resp.status_code == 200:
                try:
                    payload = resp.json()
                except ValueError as exc:
                    raise EngineError(f"odpověď není JSON: {resp.text[:300]}") from exc
                return extract_pcm(payload, sample_rate=self.sample_rate)
            last_error = _error_message(resp)
            if resp.status_code not in RETRY_STATUS:
                raise EngineError(last_error)
            header = resp.headers.get("retry-after", "")
            if header.isdigit():
                retry_after = float(header)
        raise EngineError(f"{last_error} (po {self.max_retries} opakováních)", retryable=True)

    @staticmethod
    def _backoff(attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, 120.0)
        return min(2.0**attempt, 60.0) + random.uniform(0, 1)

    def close(self) -> None:
        self._client.close()
