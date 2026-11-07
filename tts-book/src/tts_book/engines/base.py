from __future__ import annotations

from typing import Protocol

from ..resolve import Job


class EngineError(Exception):
    """Chyba při syntéze; `retryable` říká, jestli má smysl to zkusit znovu později."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class TTSEngine(Protocol):
    name: str
    sample_rate: int

    def synthesize(self, job: Job) -> bytes:
        """Vrátí 16bit mono PCM (little-endian) pro jeden požadavek."""
        ...

    def close(self) -> None: ...
