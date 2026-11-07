from __future__ import annotations

from pathlib import Path

import pytest

from tts_book.model import Book

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "majak.book.yaml"


def make_book(**overrides) -> Book:
    data = {
        "version": 1,
        "meta": {"title": "Test", "language": "cs-CZ"},
        "cast": {
            "narrator": {"voice": "Kore", "style": "klidně"},
            "anna": {"voice": "Puck", "style": "vesele"},
        },
        "chapters": [{"title": "Jedna", "paragraphs": ["Ahoj světe."]}],
    }
    data.update(overrides)
    return Book.model_validate(data)


@pytest.fixture
def book_factory():
    return make_book
