from __future__ import annotations

import base64
import json

import httpx
import pytest

from tts_book.audio import pcm_to_wav
from tts_book.engines import gemini
from tts_book.engines.base import EngineError
from tts_book.engines.gemini import GeminiEngine, build_request, extract_pcm
from tts_book.resolve import Job

PCM = b"\x01\x00\x02\x00\x03\x00\x04\x00"


def _job(style: str | None = "šeptem") -> Job:
    return Job(
        key="k",
        text="Ahoj <sigh>",
        voice="Kore",
        style=style,
        speaker="n",
        language="cs-CZ",
        engine="gemini",
        model="gemini-3.8-flash-tts",
        sample_rate=24000,
    )


def _response(data: bytes, mime: str = "audio/l16") -> dict:
    return {
        "steps": [
            {"type": "user_input", "content": [{"type": "text", "text": "Ahoj"}]},
            {
                "type": "model_output",
                "content": [{"type": "audio", "mime_type": mime, "data": base64.b64encode(data).decode()}],
            },
        ]
    }


def test_build_request_shape():
    body = build_request(_job(), model="gemini-3.8-flash-tts", sample_rate=24000)
    assert body == {
        "model": "gemini-3.8-flash-tts",
        "input": [
            {
                "type": "user_input",
                "content": [
                    {
                        "type": "text",
                        "text": "Ahoj <sigh>",
                        "annotations": [{"type": "speech_metadata", "style": "šeptem"}],
                    }
                ],
            }
        ],
        "response_format": {"type": "audio", "mime_type": "audio/l16", "sample_rate": 24000},
        "generation_config": {"speech_config": [{"voice": "Kore"}]},
    }


def test_build_request_without_style_has_no_annotations():
    body = build_request(_job(style=None), model="m", sample_rate=24000)
    assert "annotations" not in body["input"][0]["content"][0]


def test_extract_raw_pcm():
    assert extract_pcm(_response(PCM), sample_rate=24000) == PCM


def test_extract_wav_strips_header():
    assert extract_pcm(_response(pcm_to_wav(PCM, 24000), "audio/wav"), sample_rate=24000) == PCM


def test_extract_fallback_search():
    resp = {"output": {"audio": {"type": "audio", "data": base64.b64encode(PCM).decode()}}}
    assert extract_pcm(resp, sample_rate=24000) == PCM


def test_extract_without_audio_raises():
    with pytest.raises(EngineError, match="neobsahuje audio"):
        extract_pcm({"steps": []}, sample_rate=24000)


def _engine(handler) -> GeminiEngine:
    return GeminiEngine(api_key="test-key", model="gemini-3.8-flash-tts", transport=httpx.MockTransport(handler))


def test_synthesize_sends_key_and_body(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers["x-goog-api-key"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_response(PCM))

    assert _engine(handler).synthesize(_job()) == PCM
    assert seen["url"] == gemini.API_URL
    assert seen["key"] == "test-key"
    assert seen["body"]["generation_config"]["speech_config"] == [{"voice": "Kore"}]


def test_synthesize_retries_on_429(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(gemini.time, "sleep", sleeps.append)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"message": "quota"}})
        return httpx.Response(200, json=_response(PCM))

    assert _engine(handler).synthesize(_job()) == PCM
    assert calls["n"] == 3
    assert sleeps == [7.0, 7.0]


def test_synthesize_gives_up_after_retries(monkeypatch):
    monkeypatch.setattr(gemini.time, "sleep", lambda _s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "overloaded", "status": "UNAVAILABLE"}})

    with pytest.raises(EngineError, match="HTTP 503 UNAVAILABLE: overloaded") as exc:
        _engine(handler).synthesize(_job())
    assert exc.value.retryable


def test_synthesize_client_error_is_not_retried(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": {"message": "API key not valid", "status": "INVALID_ARGUMENT"}})

    with pytest.raises(EngineError, match="API key not valid"):
        _engine(handler).synthesize(_job())
    assert calls["n"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        [{"error": {"code": 403, "message": "IP address restriction", "status": "PERMISSION_DENIED"}}],
        {"error": {"code": 403, "message": "IP address restriction", "status": "PERMISSION_DENIED"}},
    ],
)
def test_error_payload_as_list_or_object(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json=payload)

    with pytest.raises(EngineError, match="HTTP 403 PERMISSION_DENIED: IP address restriction"):
        _engine(handler).synthesize(_job())


def test_error_payload_unexpected_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json=["něco jiného"])

    with pytest.raises(EngineError, match="HTTP 403"):
        _engine(handler).synthesize(_job())


def test_from_env_requires_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(EngineError, match="GEMINI_API_KEY"):
        GeminiEngine.from_env(model="m", sample_rate=24000)
