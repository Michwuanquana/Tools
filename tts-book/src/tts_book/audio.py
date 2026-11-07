"""Práce s audiem: PCM, WAV, cache a skládání kapitol přes ffmpeg.

Interně se všude používá 16bit mono PCM (little-endian) ve vzorkovací
frekvenci enginu – stejný formát, jaký vrací Gemini TTS.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import wave
from collections.abc import Iterable
from pathlib import Path

SAMPLE_WIDTH = 2  # bajty (16 bit)
CHANNELS = 1


class AudioError(Exception):
    pass


def silence(seconds: float, sample_rate: int) -> bytes:
    return b"\x00\x00" * int(round(seconds * sample_rate))


def pcm_duration(pcm_len: int, sample_rate: int) -> float:
    return pcm_len / (SAMPLE_WIDTH * CHANNELS * sample_rate)


def wav_to_pcm(data: bytes, sample_rate: int) -> bytes:
    """Vytáhne PCM z WAV a ověří, že sedí formát."""
    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getsampwidth() != SAMPLE_WIDTH or w.getnchannels() != CHANNELS:
            raise AudioError(f"nečekaný formát WAV: {w.getnchannels()} kanál(y), {8 * w.getsampwidth()} bit")
        if w.getframerate() != sample_rate:
            raise AudioError(f"nečekaná vzorkovací frekvence {w.getframerate()} Hz (čekal jsem {sample_rate})")
        return w.readframes(w.getnframes())


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Cache:
    """Vygenerované úseky audia uložené jako WAV podle hashe požadavku."""

    def __init__(self, root: Path, sample_rate: int):
        self.root = root
        self.sample_rate = sample_rate

    def path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.wav"

    def has(self, key: str) -> bool:
        return self.path(key).is_file()

    def get(self, key: str) -> bytes:
        return wav_to_pcm(self.path(key).read_bytes(), self.sample_rate)

    def put(self, key: str, pcm: bytes) -> None:
        _atomic_write(self.path(key), pcm_to_wav(pcm, self.sample_rate))

    def duration(self, key: str) -> float:
        with wave.open(str(self.path(key)), "rb") as w:
            return w.getnframes() / w.getframerate()


def write_wav(path: Path, chunks: Iterable[bytes], sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".tmp-{path.name}")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(sample_rate)
        for chunk in chunks:
            w.writeframes(chunk)
    os.replace(tmp, path)


def ffmpeg_path() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise AudioError(
            "ffmpeg nebyl nalezen v PATH – nainstaluj ho (např. `sudo apt install ffmpeg`, "
            "`brew install ffmpeg`), nebo nastav `output.format: wav`"
        )
    return exe


def write_mp3(
    path: Path,
    chunks: Iterable[bytes],
    sample_rate: int,
    *,
    bitrate: str = "128k",
    metadata: dict[str, str] | None = None,
) -> None:
    """Zakóduje PCM do MP3 přes ffmpeg (PCM se posílá po částech na stdin)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".tmp-{path.name}")
    cmd = [
        ffmpeg_path(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "s16le",
        "-ar",
        str(sample_rate),
        "-ac",
        str(CHANNELS),
        "-i",
        "pipe:0",
        "-codec:a",
        "libmp3lame",
        "-b:a",
        bitrate,
    ]
    for k, v in (metadata or {}).items():
        cmd += ["-metadata", f"{k}={v}"]
    cmd += ["-f", "mp3", str(tmp)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        for chunk in chunks:
            proc.stdin.write(chunk)
        proc.stdin.close()
    except BrokenPipeError:
        pass
    stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
    if proc.wait() != 0:
        tmp.unlink(missing_ok=True)
        raise AudioError(f"ffmpeg selhal: {stderr.strip()}")
    os.replace(tmp, path)
