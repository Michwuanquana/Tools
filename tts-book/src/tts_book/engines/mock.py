"""Offline engine: místo řeči tiché tóny (výška podle hlasu, délka podle textu).

Hodí se na vyzkoušení celé pipeline, pauz a struktury knihy bez API klíče.
"""

from __future__ import annotations

import hashlib
import math
from array import array

from ..resolve import Job
from ..text import strip_tags

CHARS_PER_SECOND = 14.0  # zhruba tempo předčítání


class MockEngine:
    name = "mock"

    def __init__(self, sample_rate: int = 24000):
        self.sample_rate = sample_rate

    def synthesize(self, job: Job) -> bytes:
        seconds = max(0.4, len(strip_tags(job.text)) / CHARS_PER_SECOND)
        h = int(hashlib.sha256(job.voice.encode()).hexdigest()[:4], 16)
        freq = 180 + (h % 240)
        n = int(seconds * self.sample_rate)
        fade = min(int(0.02 * self.sample_rate), n // 2)
        amp = 0.12 * 32767
        samples = array("h", bytes(2 * n))
        step = 2 * math.pi * freq / self.sample_rate
        for i in range(n):
            env = 1.0
            if i < fade:
                env = i / fade
            elif i > n - fade:
                env = (n - i) / fade
            samples[i] = int(amp * env * math.sin(step * i))
        return samples.tobytes()

    def close(self) -> None:
        pass
