"""TTS enginy. Každý vrací 16bit mono PCM ve vzorkovací frekvenci knihy."""

from __future__ import annotations

from ..model import Book
from .base import EngineError, TTSEngine


def create_engine(name: str, book: Book, *, model: str | None = None) -> TTSEngine:
    if name == "gemini":
        from .gemini import GeminiEngine

        return GeminiEngine.from_env(
            model=model or book.engine.model,
            sample_rate=book.engine.sample_rate,
            api_key_env=book.engine.api_key_env,
        )
    if name == "mock":
        from .mock import MockEngine

        return MockEngine(sample_rate=book.engine.sample_rate)
    raise EngineError(f"neznámý engine '{name}'")


__all__ = ["EngineError", "TTSEngine", "create_engine"]
