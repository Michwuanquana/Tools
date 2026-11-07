from __future__ import annotations

from tts_book.text import (
    apply_lexicon,
    chunk_text,
    compile_lexicon,
    convert_tags,
    find_tags,
    normalize_whitespace,
    split_sentences,
)


def test_normalize_whitespace():
    assert normalize_whitespace("  Ahoj\n  světe\t!  ") == "Ahoj světe !"


def test_lexicon_whole_words_only():
    rules = compile_lexicon({"km": "kilometrů", "sv.": "svatý"})
    assert apply_lexicon("12 km daleko, kmotr sv. Václav", rules) == "12 kilometrů daleko, kmotr svatý Václav"


def test_lexicon_longest_key_first():
    rules = compile_lexicon({"sv.": "svatý", "sv. Vít": "svatý Vít (katedrála)"})
    assert apply_lexicon("U sv. Víta", rules) == "U svatý Víta"  # "Vít" není celé slovo ve "Víta"
    assert apply_lexicon("Chrám sv. Vít.", rules) == "Chrám svatý Vít (katedrála)."


def test_tags_are_converted_only_when_known():
    text = "[sigh] Tak jo. [short pause] Viz [pozn. red.] a [Brno]."
    assert convert_tags(text) == "<sigh> Tak jo. <short pause> Viz [pozn. red.] a [Brno]."
    known, unknown = find_tags(text)
    assert known == ["sigh", "short pause"]
    assert unknown == ["Brno"]


def test_split_sentences_keeps_abbreviation_before_lowercase():
    text = "Byl tam např. pes. Pak přišla kočka! „Kdo je tam?“ Ticho."
    assert split_sentences(text) == ["Byl tam např. pes.", "Pak přišla kočka!", "„Kdo je tam?“", "Ticho."]


def test_chunk_text_short_text_unchanged():
    assert chunk_text("Krátký text.", 200) == ["Krátký text."]


def test_chunk_text_respects_limit_and_sentences():
    sentence = "Tohle je jedna docela obyčejná věta na zkoušku."
    text = " ".join([sentence] * 30)
    chunks = chunk_text(text, 200)
    assert all(len(c) <= 200 for c in chunks)
    assert all(c.endswith(".") for c in chunks)
    assert " ".join(chunks) == text


def test_chunk_text_splits_very_long_sentence():
    text = ", ".join(["slovo"] * 200) + "."
    chunks = chunk_text(text, 200)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)
    assert " ".join(chunks) == text
