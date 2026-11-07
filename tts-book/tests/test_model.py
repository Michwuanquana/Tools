from __future__ import annotations

import pytest
from pydantic import ValidationError

from tts_book.loader import BookError, load_book
from tts_book.model import Book, parse_duration

from .conftest import EXAMPLE, make_book


@pytest.mark.parametrize(
    ("value", "seconds"),
    [(0.8, 0.8), (2, 2.0), ("800ms", 0.8), ("1.5s", 1.5), (" 3 ", 3.0), ("250 ms", 0.25)],
)
def test_parse_duration(value, seconds):
    assert parse_duration(value) == pytest.approx(seconds)


@pytest.mark.parametrize("value", ["2 minuty", "-1s", -1, True, "1.5m", None])
def test_parse_duration_invalid(value):
    with pytest.raises(ValueError):
        parse_duration(value)


def test_example_loads():
    book = load_book(EXAMPLE)
    assert book.meta.language == "cs-CZ"
    assert "narrator" in book.cast


def test_string_paragraph_shorthand():
    book = make_book()
    assert book.chapters[0].paragraphs[0] == "Ahoj světe."


def test_paragraph_needs_text_xor_segments():
    with pytest.raises(ValidationError, match="buď `text`, nebo `segments`"):
        make_book(chapters=[{"paragraphs": [{"text": "a", "segments": [{"text": "b"}]}]}])
    with pytest.raises(ValidationError, match="buď `text`, nebo `segments`"):
        make_book(chapters=[{"paragraphs": [{"speaker": "anna"}]}])


def test_cast_needs_voice_or_design():
    with pytest.raises(ValidationError, match="`voice`, `design` nebo `xtts`"):
        make_book(cast={"narrator": {"style": "x"}})


def test_unknown_key_is_error():
    with pytest.raises(ValidationError):
        make_book(chapters=[{"paragraphs": [{"txet": "překlep"}]}])


def test_version_must_be_1():
    with pytest.raises(ValidationError):
        make_book(version=2)


def test_loader_reports_readable_errors(tmp_path):
    p = tmp_path / "x.book.yaml"
    p.write_text(
        "version: 1\nmeta: {title: X, language: cs}\ncast: {narrator: {voice: Kore}}\n"
        "chapters:\n  - paragraphs:\n      - speakr: narrator\n        text: Ahoj\n",
        encoding="utf-8",
    )
    with pytest.raises(BookError) as exc:
        load_book(p)
    assert exc.value.problems == ["chapters[0].paragraphs[0].speakr: neznámý klíč (překlep?)"]


def test_loader_rejects_duplicate_keys(tmp_path):
    p = tmp_path / "x.book.yaml"
    p.write_text(
        "version: 1\nmeta: {title: X, language: cs}\ncast: {narrator: {voice: Kore}}\n"
        "chapters:\n  - paragraphs:\n      - text: a\n        text: b\n",
        encoding="utf-8",
    )
    with pytest.raises(BookError, match="duplicitní klíč 'text'"):
        load_book(p)


def test_defaults_are_filled():
    book = make_book()
    assert book.engine.model == "gemini-3.8-flash-tts"
    assert make_book(engine={"name": "xtts"}).engine.model == "tts_models/multilingual/multi-dataset/xtts_v2"
    assert book.output.pauses.paragraph == 0.8
    assert book.defaults.speaker == "narrator"
    assert isinstance(book, Book)
