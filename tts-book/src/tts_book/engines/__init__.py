"""TTS enginy. Každý vrací 16bit mono PCM ve vzorkovací frekvenci knihy."""

from __future__ import annotations

from pathlib import Path

from ..model import Book, model_for
from .base import EngineError, TTSEngine


def create_engine(
    name: str,
    book: Book,
    *,
    model: str | None = None,
    base_dir: Path | None = None,
) -> TTSEngine:
    """`base_dir` = složka, vůči které se berou relativní cesty k nahrávkám (xtts)."""
    model = model_for(book, name, model)
    if name == "gemini":
        from .gemini import GeminiEngine

        return GeminiEngine.from_env(
            model=model,
            sample_rate=book.engine.sample_rate,
            api_key_env=book.engine.api_key_env,
        )
    if name == "xtts":
        from .xtts import XttsEngine

        return XttsEngine(model=model, base_dir=base_dir or Path.cwd())
    if name == "mock":
        from .mock import MockEngine

        return MockEngine(sample_rate=book.engine.sample_rate)
    raise EngineError(f"neznámý engine '{name}'")


__all__ = ["EngineError", "TTSEngine", "create_engine"]
