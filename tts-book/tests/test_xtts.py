"""XTTS bez torch: falešný model místo coqui-tts."""

from __future__ import annotations

import shutil
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from tts_book import cli
from tts_book.engines import create_engine
from tts_book.engines.base import EngineError
from tts_book.engines.xtts import XttsEngine, missing_references, to_pcm16
from tts_book.loader import load_book
from tts_book.model import DEFAULT_MODELS, model_for
from tts_book.resolve import Job, build_plan, check
from tts_book.xtts import char_limit, matching_style, parse_voice, xtts_language

from .conftest import EXAMPLE, make_book

runner = CliRunner()

XTTS_CAST = {
    "narrator": {"voice": "Kore", "xtts": {"speaker": "Ana Florence"}},
    "anna": {
        "voice": "Puck",
        "xtts": {
            "reference": ["anna.wav", "anna2.wav"],
            "styles": {"šeptem": "anna-sepot.wav", "vesele": "anna-smich.wav", "velmi vesele": "anna-jasot.wav"},
        },
    },
}


def _xtts_book(**overrides):
    return make_book(engine={"name": "xtts"}, cast=XTTS_CAST, **overrides)


def _jobs(book, engine=None):
    return [j for c in build_plan(book, engine=engine) for j in c.jobs]


# --- Jazyky, limity, výběr nahrávky ------------------------------------------


@pytest.mark.parametrize(
    ("bcp47", "code"),
    [("cs-CZ", "cs"), ("cs", "cs"), ("en-US", "en"), ("pt-BR", "pt"), ("zh-Hans-CN", "zh-cn"), ("ZH", "zh-cn")],
)
def test_language_mapping(bcp47, code):
    assert xtts_language(bcp47) == code


@pytest.mark.parametrize("bcp47", ["sk-SK", "uk", "sv-SE"])
def test_unsupported_language(bcp47):
    assert xtts_language(bcp47) is None


def test_char_limits():
    assert char_limit("cs-CZ") == 186
    assert char_limit("en-GB") == 250
    assert char_limit("sk-SK") == 200  # nepodporovaný jazyk – check ho odmítne


def test_matching_style_rightmost_wins():
    styles = {"šeptem": "a.wav", "vesele": "b.wav", "velmi vesele": "c.wav"}
    assert matching_style(styles, "vesele; napjatě; šeptem") == "šeptem"
    assert matching_style(styles, "šeptem; Vesele") == "vesele"
    assert matching_style(styles, "klidně; velmi vesele") == "velmi vesele"
    assert matching_style(styles, "klidně") is None
    assert matching_style(styles, None) is None
    assert matching_style({}, "šeptem") is None


def test_parse_voice():
    assert parse_voice("wav:a.wav|b/c.wav") == ("wav", ["a.wav", "b/c.wav"])
    assert parse_voice("speaker:Ana Florence") == ("speaker", "Ana Florence")
    with pytest.raises(ValueError):
        parse_voice("Kore")


# --- Model a plán --------------------------------------------------------------


def test_xtts_voice_needs_reference_xor_speaker():
    with pytest.raises(ValueError, match="buď `reference`, nebo `speaker`"):
        make_book(cast={"narrator": {"xtts": {"speaker": "X", "reference": "a.wav"}}})
    with pytest.raises(ValueError, match="buď `reference`, nebo `speaker`"):
        make_book(cast={"narrator": {"xtts": {"styles": {"šeptem": "a.wav"}}}})


def test_cast_with_only_xtts_is_valid():
    book = make_book(engine={"name": "xtts"}, cast={"narrator": {"xtts": {"reference": "n.wav"}}})
    assert check(book).ok


def test_model_for():
    gemini = make_book(engine={"model": "gemini-3.8-flash-lite-tts"})
    assert model_for(gemini) == "gemini-3.8-flash-lite-tts"
    assert model_for(gemini, "xtts") == DEFAULT_MODELS["xtts"]
    assert model_for(gemini, "xtts", "C:/modely/xtts") == "C:/modely/xtts"
    assert model_for(_xtts_book()) == DEFAULT_MODELS["xtts"]
    assert model_for(_xtts_book(), "gemini") == DEFAULT_MODELS["gemini"]


def test_plan_selects_recording_by_direction():
    book = _xtts_book(
        defaults={"speaker": "narrator", "direction": "vesele"},
        chapters=[
            {
                "paragraphs": [
                    {"speaker": "anna", "text": "a"},
                    {"speaker": "anna", "direction": "šeptem", "text": "b"},
                    {"speaker": "anna", "direction": "klidně", "text": "c"},
                    "d",
                ]
            }
        ],
    )
    jobs = {j.text: j for j in _jobs(book)}
    assert jobs["a"].voice == "wav:anna-smich.wav"  # režie knihy 'vesele'
    assert jobs["b"].voice == "wav:anna-sepot.wav"  # odstavec je konkrétnější
    assert jobs["c"].voice == "wav:anna-smich.wav"
    assert jobs["d"].voice == "speaker:Ana Florence"
    assert all(j.style is None and j.engine == "xtts" for j in jobs.values())
    assert jobs["a"].model == DEFAULT_MODELS["xtts"]


def test_plan_default_reference_and_tags_stripped():
    book = _xtts_book(chapters=[{"paragraphs": [{"speaker": "anna", "text": "[sigh] Zase. [short pause] Ne."}]}])
    (job,) = _jobs(book)
    assert job.voice == "wav:anna.wav|anna2.wav"
    assert job.text == "Zase. Ne."


def test_plan_skips_tag_only_segment():
    book = _xtts_book(chapters=[{"paragraphs": [{"segments": [{"text": "[laugh]"}, {"text": "Haha."}]}]}])
    assert [j.text for j in _jobs(book)] == ["Haha."]


def test_plan_chunks_by_language_limit():
    text = " ".join(["Tohle je docela dlouhá česká věta, která se opakuje."] * 12)
    book = _xtts_book(
        chapters=[{"paragraphs": [text, {"language": "en-US", "text": text}]}],
    )
    plan = build_plan(book)[0]
    cs, en = plan.paragraphs
    assert len(cs.jobs) > 1 and all(len(j.text) <= 186 for j in cs.jobs)
    assert all(len(j.text) <= 250 for j in en.jobs)
    assert len(en.jobs) < len(cs.jobs)


def test_cache_key_includes_language_for_xtts():
    cs = _xtts_book(chapters=[{"paragraphs": ["Ahoj"]}])
    en = _xtts_book(chapters=[{"language": "en", "paragraphs": ["Ahoj"]}])
    assert _jobs(cs)[0].key != _jobs(en)[0].key


def test_engine_override_on_gemini_book():
    book = make_book(cast=XTTS_CAST)
    (job,) = _jobs(book, engine="xtts")[1:]  # [0] je název kapitoly
    assert job.engine == "xtts" and job.voice == "speaker:Ana Florence"
    assert job.model == DEFAULT_MODELS["xtts"]


# --- check ---------------------------------------------------------------------


def test_check_xtts_errors():
    book = make_book(
        engine={"sample_rate": 16000},
        cast={"narrator": {"voice": "Kore"}, "anna": {"voice": "Puck", "xtts": {"speaker": "Ana Florence"}}},
        chapters=[
            {
                "paragraphs": [
                    "a",
                    "b",
                    {"speaker": "anna", "language": "sk-SK", "text": "[sigh] c [laugh]"},
                    {"speaker": "anna", "text": "[sigh] d"},
                ]
            }
        ],
    )
    report = check(book, "xtts")
    errors = "\n".join(report.errors)
    assert "mluvčí 'narrator' nemá `xtts`" in errors
    assert errors.count("nemá `xtts`") == 1  # jednou za mluvčího
    assert "XTTS nepodporuje jazyk 'sk-SK'" in errors
    assert "sample_rate" in errors
    tag_warnings = [w for w in report.warnings if "neumí značky" in w]
    assert len(tag_warnings) == 1
    assert check(book).ok  # pro gemini je kniha v pořádku


def test_check_mock_does_not_need_voice():
    book = make_book(cast={"narrator": {"design": "hluboký hlas"}}, engine={"name": "mock"})
    assert check(book).ok
    assert check(book, "gemini").errors


def test_example_works_with_xtts():
    book = load_book(EXAMPLE)
    report = check(book, "xtts")
    assert report.ok, report.errors
    jobs = _jobs(book, engine="xtts")
    assert all(j.voice.startswith("speaker:") for j in jobs)
    assert {j.language for j in jobs} == {"cs-CZ", "en-US"}


# --- Engine s falešným modelem ------------------------------------------------


class FakeXtts:
    config = SimpleNamespace(
        max_ref_len=30,
        gpt_cond_len=30,
        gpt_cond_chunk_len=4,
        sound_norm_refs=False,
        temperature=0.75,
        length_penalty=1.0,
        repetition_penalty=5.0,
        top_k=50,
        top_p=0.85,
    )

    def __init__(self, wav=(0.0, 0.5, -0.5, 2.0, -2.0), fail: bool = False, speakers=("Ana Florence",)):
        self.speaker_manager = SimpleNamespace(
            speakers={name: {"gpt_cond_latent": f"G-{name}", "speaker_embedding": f"S-{name}"} for name in speakers}
        )
        self.wav = list(wav)
        self.fail = fail
        self.cond_calls: list[list[str]] = []
        self.inference_calls: list[tuple] = []

    def get_conditioning_latents(self, audio_path, **kwargs):
        assert kwargs["max_ref_length"] == 30 and kwargs["gpt_cond_chunk_len"] == 4
        self.cond_calls.append(audio_path)
        return f"G-{len(self.cond_calls)}", "S"

    def inference(self, text, language, gpt, spk, **kwargs):
        if self.fail:
            raise RuntimeError("CUDA out of memory")
        assert kwargs["temperature"] == 0.75 and kwargs["top_p"] == 0.85
        self.inference_calls.append((text, language, gpt, spk))
        return {"wav": self.wav}


def _job(voice: str, text: str = "Ahoj.", language: str = "cs-CZ") -> Job:
    return Job(
        key="k",
        text=text,
        voice=voice,
        style=None,
        speaker="x",
        language=language,
        engine="xtts",
        model=DEFAULT_MODELS["xtts"],
        sample_rate=24000,
    )


def _engine(tmp_path: Path, **kwargs) -> tuple[XttsEngine, FakeXtts]:
    fake = FakeXtts(**kwargs)
    return XttsEngine(model="fake", base_dir=tmp_path, tts_model=fake), fake


def test_to_pcm16_clips():
    pcm = to_pcm16([0.0, 0.5, -0.5, 2.0, -2.0])
    from array import array

    assert list(array("h", pcm)) == [0, 16383, -16383, 32767, -32767]


def test_engine_builtin_speaker(tmp_path):
    engine, fake = _engine(tmp_path)
    pcm = engine.synthesize(_job("speaker:Ana Florence", language="en-US"))
    assert len(pcm) == 10
    assert fake.inference_calls == [("Ahoj.", "en", "G-Ana Florence", "S-Ana Florence")]
    assert engine.speakers == ["Ana Florence"]


def test_engine_unknown_speaker_lists_available(tmp_path):
    engine, _ = _engine(tmp_path)
    with pytest.raises(EngineError, match="nezná vestavěný hlas 'Nikdo'. Dostupné: Ana Florence"):
        engine.synthesize(_job("speaker:Nikdo"))


def test_engine_caches_latents_per_voice(tmp_path):
    for name in ("a.wav", "b.wav"):
        (tmp_path / name).write_bytes(b"RIFF")
    engine, fake = _engine(tmp_path)
    engine.synthesize(_job("wav:a.wav|b.wav"))
    engine.synthesize(_job("wav:a.wav|b.wav", text="Druhá věta."))
    engine.synthesize(_job("wav:b.wav"))
    assert fake.cond_calls == [[str(tmp_path / "a.wav"), str(tmp_path / "b.wav")], [str(tmp_path / "b.wav")]]
    assert [c[2] for c in fake.inference_calls] == ["G-1", "G-1", "G-2"]


def test_engine_missing_reference(tmp_path):
    engine, fake = _engine(tmp_path)
    with pytest.raises(EngineError, match="chybí referenční nahrávka"):
        engine.synthesize(_job("wav:neni.wav"))
    assert fake.cond_calls == []


def test_engine_inference_error_becomes_engine_error(tmp_path):
    engine, _ = _engine(tmp_path, fail=True)
    with pytest.raises(EngineError, match="XTTS selhal: CUDA out of memory"):
        engine.synthesize(_job("speaker:Ana Florence"))


def test_engine_unsupported_language(tmp_path):
    engine, _ = _engine(tmp_path)
    with pytest.raises(EngineError, match="nepodporuje jazyk 'sk-SK'"):
        engine.synthesize(_job("speaker:Ana Florence", language="sk-SK"))


def test_missing_references(tmp_path):
    (tmp_path / "ok.wav").write_bytes(b"")
    jobs = [_job("wav:ok.wav|chybi.wav"), _job("speaker:Ana Florence"), _job("wav:chybi.wav")]
    assert missing_references(jobs, tmp_path) == [tmp_path / "chybi.wav"]


def test_create_engine_without_torch(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)  # import torch → ImportError
    with pytest.raises(EngineError, match="uv sync --extra xtts"):
        create_engine("xtts", _xtts_book())


# --- CLI -----------------------------------------------------------------------


@pytest.fixture
def fake_engine(monkeypatch):
    created: list[tuple[XttsEngine, FakeXtts]] = []
    speakers = ("Ana Florence", "Damien Black", "Gracie Wise", "Andrew Chipper", "Craig Gutsy")

    def factory(name, book, *, model=None, base_dir=None):
        assert name == "xtts"
        fake = FakeXtts(wav=[0.1] * 2400, speakers=speakers)
        engine = XttsEngine(model=model, base_dir=base_dir, tts_model=fake)
        created.append((engine, fake))
        return engine

    monkeypatch.setattr(cli, "create_engine", factory)
    return created


def test_cli_render_example_with_xtts(tmp_path, fake_engine):
    path = tmp_path / "majak.book.yaml"
    shutil.copy(EXAMPLE, path)
    path.write_text(path.read_text(encoding="utf-8").replace("format: mp3", "format: wav"), encoding="utf-8")
    result = runner.invoke(cli.app, ["render", str(path), "--engine", "xtts"])
    assert result.exit_code == 0, result.output
    assert "XTTS běží na CPU" in result.output  # falešný engine hlásí device cpu
    assert len(list((tmp_path / "build" / "majak").glob("*.wav"))) == 2
    engine, fake = fake_engine[0]
    assert engine.model_name == DEFAULT_MODELS["xtts"]
    assert engine.base_dir == tmp_path.resolve()
    assert {c[3] for c in fake.inference_calls} == {
        "S-Damien Black",
        "S-Gracie Wise",
        "S-Andrew Chipper",
        "S-Craig Gutsy",
    }


def test_cli_render_missing_reference_fails_before_model(tmp_path, fake_engine):
    path = tmp_path / "kniha.book.yaml"
    path.write_text(
        "version: 1\nmeta: {title: X, language: cs}\nengine: {name: xtts}\n"
        "cast: {narrator: {xtts: {reference: hlasy/vypravec.wav}}}\nchapters:\n  - paragraphs: [Ahoj.]\n",
        encoding="utf-8",
    )
    result = runner.invoke(cli.app, ["render", str(path)])
    assert result.exit_code == 1
    assert "chybí referenční nahrávka" in result.output
    assert fake_engine == []


def test_cli_validate_with_engine(tmp_path):
    result = runner.invoke(cli.app, ["validate", str(EXAMPLE), "--engine", "xtts"])
    assert result.exit_code == 0, result.output
    assert "neumí značky jako [sigh]" in result.output
    assert runner.invoke(cli.app, ["validate", str(EXAMPLE), "--engine", "nic"]).exit_code == 1


def test_cli_say_xtts(tmp_path, fake_engine, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "say.wav"
    text = "Tohle je první věta, která je docela dlouhá. " * 6
    result = runner.invoke(
        cli.app, ["say", "--engine", "xtts", "--voice", "Ana Florence", "-o", str(out), "[sigh] " + text]
    )
    assert result.exit_code == 0, result.output
    calls = fake_engine[0][1].inference_calls
    assert len(calls) > 1 and all(len(c[0]) <= 186 and "[sigh]" not in c[0] for c in calls)
    assert {c[1] for c in calls} == {"cs"}
    with wave.open(str(out)) as w:
        assert w.getframerate() == 24000


def test_cli_say_xtts_wav_voice_must_exist(tmp_path, fake_engine, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["say", "--engine", "xtts", "--voice", "ja.wav", "Ahoj."])
    assert result.exit_code == 1
    assert "chybí referenční nahrávka" in result.output
    result = runner.invoke(cli.app, ["say", "--engine", "xtts", "--voice", "X", "-l", "sk-SK", "Ahoj."])
    assert result.exit_code == 1 and "nepodporuje jazyk" in result.output
