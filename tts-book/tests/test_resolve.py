from __future__ import annotations

from tts_book.resolve import Silence, Speech, build_plan, chapter_timeline, check

from .conftest import make_book


def _jobs(book):
    return [j for c in build_plan(book) for j in c.jobs]


def test_generated_ids():
    book = make_book(
        chapters=[
            {"paragraphs": ["a", {"id": "vlastni", "text": "b"}, "c"]},
            {"id": "dva", "paragraphs": ["d"]},
        ]
    )
    plan = build_plan(book)
    assert [c.id for c in plan] == ["ch01", "dva"]
    assert [p.id for p in plan[0].paragraphs] == ["ch01.p001", "vlastni", "ch01.p003"]
    assert [p.id for p in plan[1].paragraphs] == ["dva.p001"]


def test_speaker_inheritance():
    book = make_book(
        cast={"narrator": {"voice": "Kore"}, "anna": {"voice": "Puck"}, "petr": {"voice": "Orus"}},
        chapters=[
            {
                "speaker": "anna",
                "paragraphs": [
                    "chapter speaker",
                    {"speaker": "petr", "text": "paragraph speaker"},
                    {"speaker": "petr", "segments": [{"text": "seg"}, {"speaker": "narrator", "text": "seg2"}]},
                ],
            }
        ],
    )
    voices = [(j.text, j.voice) for j in _jobs(book)]
    assert voices == [
        ("chapter speaker", "Puck"),
        ("paragraph speaker", "Orus"),
        ("seg", "Orus"),
        ("seg2", "Kore"),
    ]


def test_style_composition_order():
    book = make_book(
        defaults={"speaker": "narrator", "direction": "audiokniha"},
        chapters=[
            {
                "direction": "napjatě",
                "paragraphs": [
                    {
                        "direction": "šeptem",
                        "segments": [{"text": "x", "direction": "vyděšeně"}, {"text": "y"}],
                    }
                ],
            }
        ],
    )
    styles = [j.style for j in _jobs(book)]
    assert styles == [
        "audiokniha; klidně; napjatě; šeptem; vyděšeně",
        "audiokniha; klidně; napjatě; šeptem",
    ]


def test_language_inheritance():
    book = make_book(
        chapters=[
            {
                "language": "de-DE",
                "paragraphs": [
                    "Hallo",
                    {"language": "en-US", "segments": [{"text": "Hi"}, {"text": "Ahoj", "language": "cs-CZ"}]},
                ],
            }
        ]
    )
    assert [j.language for j in _jobs(book)] == ["de-DE", "en-US", "cs-CZ"]


def test_title_job_and_read_title_override():
    book = make_book(
        chapters=[
            {"title": "První", "paragraphs": ["a"]},
            {"title": "Druhá", "read_title": False, "paragraphs": ["b"]},
        ]
    )
    plan = build_plan(book)
    assert plan[0].title_job is not None and plan[0].title_job.text == "První"
    assert plan[0].title_job.voice == "Kore"
    assert plan[1].title_job is None


def test_skip_chapters_and_paragraphs():
    book = make_book(
        chapters=[
            {"paragraphs": ["a", {"text": "b", "skip": True}]},
            {"skip": True, "paragraphs": ["c"]},
            {"paragraphs": ["d"]},
        ]
    )
    plan = build_plan(book)
    assert [c.id for c in plan] == ["ch01", "ch03"]
    assert [c.index for c in plan] == [1, 3]
    assert [p.id for p in plan[0].paragraphs] == ["ch01.p001"]


def test_lexicon_and_tags_applied():
    book = make_book(lexicon={"km": "kilometrů"}, chapters=[{"paragraphs": ["[sigh] Ještě 5 km."]}])
    assert _jobs(book)[0].text == "<sigh> Ještě 5 kilometrů."


def test_long_paragraph_is_chunked_with_pauses():
    text = " ".join(["Tohle je věta, která se opakuje pořád dokola."] * 20)
    book = make_book(engine={"max_chars": 200}, chapters=[{"paragraphs": [text]}])
    para = build_plan(book)[0].paragraphs[0]
    assert len(para.jobs) > 1
    assert all(len(j.text) <= 200 for j in para.jobs)
    silences = [i for i in para.items if isinstance(i, Silence)]
    assert len(silences) == len(para.jobs) - 1
    assert all(s.seconds == book.output.pauses.chunk for s in silences)


def test_timeline_pauses():
    book = make_book(chapters=[{"title": "T", "paragraphs": ["a", {"text": "b", "pause_after": "2s"}, "c"]}])
    (chapter,) = build_plan(book)
    timeline = chapter_timeline(book, chapter)
    kinds = [("S", round(i.seconds, 2)) if isinstance(i, Silence) else i.job.text for i in timeline]
    p = book.output.pauses
    assert kinds == [
        ("S", p.chapter_start),
        "T",
        ("S", p.title),
        "a",
        ("S", p.paragraph),
        "b",
        ("S", 2.0),
        "c",
        ("S", p.chapter_end),
    ]
    assert all(isinstance(i, (Speech, Silence)) for i in timeline)


def test_cache_key_depends_on_voice_style_text_not_language():
    base = make_book(chapters=[{"paragraphs": ["Ahoj"]}])
    other_lang = make_book(chapters=[{"language": "sk-SK", "paragraphs": ["Ahoj"]}])
    other_style = make_book(chapters=[{"direction": "smutně", "paragraphs": ["Ahoj"]}])
    k = lambda b: _jobs(b)[0].key  # noqa: E731
    assert k(base) == k(other_lang)
    assert k(base) != k(other_style)
    assert _jobs(base)[0].key != build_plan(base, engine="mock")[0].jobs[0].key


def test_check_reports_semantic_errors():
    book = make_book(
        cast={"narrator": {"voice": "Kore"}, "anna": {"design": "mladá žena"}, "nikdo": {"voice": "Puck"}},
        chapters=[
            {
                "paragraphs": [
                    {"speaker": "karel", "text": "a"},
                    {"speaker": "anna", "text": "b"},
                    {"id": "ch01.p001", "text": "c [Neznama] [sigh]"},
                ]
            }
        ],
    )
    report = check(book)
    errors = "\n".join(report.errors)
    warnings = "\n".join(report.warnings)
    assert "mluvčí 'karel' není v `cast`" in errors
    assert "mluvčí 'anna' má jen `design`" in errors
    assert "ID 'ch01.p001' už má" in errors
    assert "neznámá značka [Neznama]" in warnings
    assert "cast.nikdo: postava se v knize nepoužívá" in warnings


def test_check_warns_about_long_style():
    book = make_book(chapters=[{"paragraphs": [{"text": "a", "direction": "x" * 400}]}])
    assert any("zhoršují stálost hlasu" in w for w in check(book).warnings)


def test_check_ok_for_example():
    from tts_book.loader import load_book

    from .conftest import EXAMPLE

    report = check(load_book(EXAMPLE))
    assert report.ok, report.errors
    assert report.warnings == []
