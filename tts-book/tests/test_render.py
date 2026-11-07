from __future__ import annotations

import json
import shutil
import wave

import pytest
from typer.testing import CliRunner

from tts_book.cli import app, book_json_schema
from tts_book.engines.base import EngineError
from tts_book.engines.mock import MockEngine
from tts_book.loader import load_book
from tts_book.render import cache_for, synthesize
from tts_book.resolve import build_plan

from .conftest import EXAMPLE, ROOT

runner = CliRunner()


@pytest.fixture
def example(tmp_path):
    path = tmp_path / "majak.book.yaml"
    shutil.copy(EXAMPLE, path)
    return path


def _set_format(path, fmt):
    text = path.read_text(encoding="utf-8").replace("format: mp3", f"format: {fmt}")
    path.write_text(text, encoding="utf-8")


def test_render_mock_wav_and_cache(example):
    _set_format(example, "wav")
    result = runner.invoke(app, ["render", str(example), "--engine", "mock"])
    assert result.exit_code == 0, result.output
    out = example.parent / "build" / "majak"
    files = sorted(p.name for p in out.glob("*.wav"))
    assert files == ["01 - Kapitola první – Bouřka.wav", "02 - Kapitola druhá – Loď.wav"]
    with wave.open(str(out / files[0])) as w:
        assert w.getframerate() == 24000 and w.getnchannels() == 1
        assert w.getnframes() / 24000 > 10

    again = runner.invoke(app, ["render", str(example), "--engine", "mock"])
    assert again.exit_code == 0
    assert "Generuji" not in again.output  # vše z cache


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg není k dispozici")
def test_render_mock_mp3(example):
    result = runner.invoke(app, ["render", str(example), "--engine", "mock", "-c", "lod"])
    assert result.exit_code == 0, result.output
    out = example.parent / "build" / "majak"
    assert [p.name for p in out.glob("*.mp3")] == ["02 - Kapitola druhá – Loď.mp3"]


def test_render_only_writes_preview(example):
    _set_format(example, "wav")
    result = runner.invoke(app, ["render", str(example), "--engine", "mock", "--only", "lod.konec"])
    assert result.exit_code == 0, result.output
    out = example.parent / "build" / "majak"
    assert (out / "previews" / "lod.konec.wav").is_file()
    assert not list(out.glob("*.wav"))  # kapitoly se při --only neskládají


def test_render_unknown_ids(example):
    assert runner.invoke(app, ["render", str(example), "--engine", "mock", "-c", "nic"]).exit_code == 1
    assert runner.invoke(app, ["render", str(example), "--engine", "mock", "--only", "nic"]).exit_code == 1


def test_validate_and_plan(example):
    result = runner.invoke(app, ["validate", str(example)])
    assert result.exit_code == 0 and "OK" in result.output
    result = runner.invoke(app, ["plan", str(example)])
    assert result.exit_code == 0, result.output
    assert "k vygenerování 14" in result.output


class _FailingEngine(MockEngine):
    def synthesize(self, job):
        raise EngineError("HTTP 400: API key not valid")


def test_synthesize_aborts_after_consecutive_failures(example):
    book = load_book(example)
    jobs = [j for c in build_plan(book) for j in c.jobs]
    result = synthesize(_FailingEngine(), cache_for(book, example), jobs)
    assert result.aborted
    assert len(result.failures) == 3
    assert result.synthesized == 0


def test_committed_schema_is_up_to_date():
    committed = json.loads((ROOT / "schema" / "tts-book.schema.json").read_text(encoding="utf-8"))
    assert committed == book_json_schema(), "spusť: tts-book schema -o schema/tts-book.schema.json"


def test_example_matches_json_schema():
    jsonschema = pytest.importorskip("jsonschema")
    import yaml

    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    jsonschema.validate(data, book_json_schema())
