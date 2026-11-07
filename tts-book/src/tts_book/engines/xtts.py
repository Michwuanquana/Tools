"""Lokální XTTS v2 přes coqui-tts (`uv sync --extra xtts`).

Torch a TTS se importují až při vytvoření enginu, takže zbytek programu
(i testy) funguje bez nich. Model má licenci CPML (jen nekomerční použití).
"""

from __future__ import annotations

from array import array
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..resolve import Job
from ..xtts import SAMPLE_RATE, parse_voice, xtts_language
from .base import EngineError

INSTALL_HINT = "nainstaluj ho příkazem `uv sync --extra xtts` (na Windows s CUDA buildem torch)"
TOS_HINT = (
    "model XTTS v2 má licenci CPML (jen nekomerční použití, https://coqui.ai/cpml). "
    "Pokud s ní souhlasíš, nastav proměnnou prostředí COQUI_TOS_AGREED=1"
)


def resolve_paths(paths: Iterable[str], base_dir: Path) -> list[Path]:
    return [p if p.is_absolute() else base_dir / p for p in (Path(x) for x in paths)]


def missing_references(jobs: Iterable[Job], base_dir: Path) -> list[Path]:
    """Referenční nahrávky z plánu, které neexistují (kontrola před načtením modelu)."""
    missing: dict[Path, None] = {}
    for job in jobs:
        kind, value = parse_voice(job.voice)
        if kind == "wav":
            for path in resolve_paths(value, base_dir):
                if not path.is_file():
                    missing.setdefault(path)
    return list(missing)


def to_pcm16(wav: Any) -> bytes:
    """Float audio (-1…1) → 16bit PCM little-endian, hodnoty mimo rozsah se ořežou."""
    if hasattr(wav, "detach"):  # torch.Tensor
        wav = wav.detach().cpu().numpy()
    try:
        import numpy as np
    except ImportError:  # pragma: no cover - numpy je vždy s coqui-tts
        samples = array("h", (int(max(-1.0, min(1.0, float(x))) * 32767) for x in wav))
        return samples.tobytes()
    data = np.clip(np.asarray(wav, dtype=np.float32).reshape(-1), -1.0, 1.0)
    return (data * 32767).astype("<i2").tobytes()


def _pick_device(device: str | None) -> str:
    try:
        import torch
    except ImportError as exc:
        raise EngineError(f"chybí torch – {INSTALL_HINT}") from exc
    if device:
        return device
    return "cuda" if torch.cuda.is_available() else "cpu"


def load_model(model: str, device: str) -> Any:
    """Načte model XTTS: název z katalogu coqui (stáhne se) nebo složku se staženým modelem."""
    try:
        from TTS.api import TTS
    except ImportError as exc:
        raise EngineError(f"chybí balíček coqui-tts ({exc.name}) – {INSTALL_HINT}") from exc
    folder = Path(model)
    try:
        if folder.is_dir():
            from TTS.tts.configs.xtts_config import XttsConfig
            from TTS.tts.models.xtts import Xtts

            config = XttsConfig()
            config.load_json(str(folder / "config.json"))
            xtts = Xtts(config)
            # load_checkpoint přepne model do režimu inference sám (Xtts.eval() vrací None).
            xtts.load_checkpoint(config, checkpoint_dir=str(folder), use_deepspeed=False)
            xtts.to(device)
            return xtts
        tts = TTS(model, progress_bar=False).to(device)
    except EOFError as exc:  # input() s dotazem na licenci bez terminálu
        raise EngineError(TOS_HINT) from exc
    except EngineError:
        raise
    except Exception as exc:
        raise EngineError(f"nepodařilo se načíst model XTTS '{model}': {exc}") from exc
    return tts.synthesizer.tts_model


class XttsEngine:
    name = "xtts"
    sample_rate = SAMPLE_RATE

    def __init__(
        self,
        *,
        model: str,
        base_dir: Path,
        device: str | None = None,
        tts_model: Any = None,
    ):
        """`tts_model` = už načtený objekt Xtts (testy); jinak se model načte tady."""
        self.model_name = model
        self.base_dir = base_dir
        if tts_model is None:
            self.device = _pick_device(device)
            tts_model = load_model(model, self.device)
        else:
            self.device = device or "cpu"
        self._model = tts_model
        self._latents: dict[str, tuple[Any, Any]] = {}

    @property
    def speakers(self) -> list[str]:
        manager = getattr(self._model, "speaker_manager", None)
        return sorted(getattr(manager, "speakers", None) or {})

    def _conditioning(self, voice: str) -> tuple[Any, Any]:
        """(gpt_cond_latent, speaker_embedding) pro hlas; počítá se jednou na hlas."""
        if voice in self._latents:
            return self._latents[voice]
        try:
            kind, value = parse_voice(voice)
        except ValueError as exc:
            raise EngineError(str(exc)) from exc
        m = self._model
        if kind == "speaker":
            speakers = getattr(getattr(m, "speaker_manager", None), "speakers", None) or {}
            if value not in speakers:
                raise EngineError(f"XTTS nezná vestavěný hlas '{value}'. Dostupné: {', '.join(sorted(speakers))}")
            gpt, spk = speakers[value].values()
        else:
            paths = resolve_paths(value, self.base_dir)
            missing = [str(p) for p in paths if not p.is_file()]
            if missing:
                raise EngineError(f"chybí referenční nahrávka: {', '.join(missing)}")
            cfg = m.config
            try:
                gpt, spk = m.get_conditioning_latents(
                    audio_path=[str(p) for p in paths],
                    max_ref_length=cfg.max_ref_len,
                    gpt_cond_len=cfg.gpt_cond_len,
                    gpt_cond_chunk_len=cfg.gpt_cond_chunk_len,
                    sound_norm_refs=cfg.sound_norm_refs,
                )
            except Exception as exc:
                raise EngineError(f"nepodařilo se zpracovat nahrávku {', '.join(map(str, paths))}: {exc}") from exc
        self._latents[voice] = (gpt, spk)
        return gpt, spk

    def synthesize(self, job: Job) -> bytes:
        language = xtts_language(job.language)
        if language is None:
            raise EngineError(f"XTTS nepodporuje jazyk '{job.language}'")
        gpt, spk = self._conditioning(job.voice)
        cfg = self._model.config
        try:
            out = self._model.inference(
                job.text,
                language,
                gpt,
                spk,
                temperature=cfg.temperature,
                length_penalty=cfg.length_penalty,
                repetition_penalty=cfg.repetition_penalty,
                top_k=cfg.top_k,
                top_p=cfg.top_p,
            )
        except Exception as exc:
            raise EngineError(f"XTTS selhal: {exc}") from exc
        return to_pcm16(out["wav"])

    def close(self) -> None:
        self._latents.clear()
        self._model = None
        if self.device.startswith("cuda"):
            try:
                import torch

                torch.cuda.empty_cache()
            except ImportError:  # pragma: no cover
                pass
