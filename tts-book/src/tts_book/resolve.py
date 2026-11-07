"""Rozbalení knihy na plochý plán: kapitoly → odstavce → požadavky na TTS.

Tady se řeší dědičnost (mluvčí, jazyk), skládání režie do `style`,
lexikon, převod značek a dělení dlouhých textů.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field

from . import xtts
from .model import Book, Chapter, Paragraph, Segment, model_for
from .text import (
    apply_lexicon,
    chunk_text,
    compile_lexicon,
    convert_tags,
    find_tags,
    normalize_whitespace,
    strip_tags,
)

CACHE_KEY_VERSION = 1
STYLE_WARN_CHARS = 300

PREBUILT_VOICES: frozenset[str] = frozenset(
    {
        "Zephyr",
        "Puck",
        "Charon",
        "Kore",
        "Fenrir",
        "Leda",
        "Orus",
        "Aoede",
        "Callirrhoe",
        "Autonoe",
        "Enceladus",
        "Iapetus",
        "Umbriel",
        "Algieba",
        "Despina",
        "Erinome",
        "Algenib",
        "Rasalgethi",
        "Laomedeia",
        "Achernar",
        "Alnilam",
        "Schedar",
        "Gacrux",
        "Pulcherrima",
        "Achird",
        "Zubenelgenubi",
        "Vindemiatrix",
        "Sadachbia",
        "Sadaltager",
        "Sulafat",
    }
)


# --- Datové struktury plánu ---------------------------------------------------


@dataclass(frozen=True)
class Job:
    """Jeden požadavek na TTS. `key` je hash všeho, co ovlivňuje výsledné audio."""

    key: str
    text: str
    voice: str
    style: str | None
    speaker: str
    language: str
    engine: str
    model: str
    sample_rate: int


@dataclass(frozen=True)
class Speech:
    job: Job


@dataclass(frozen=True)
class Silence:
    seconds: float


Item = Speech | Silence


@dataclass
class ParagraphPlan:
    id: str
    chapter_id: str
    items: list[Item]
    pause_after: float
    chars: int

    @property
    def jobs(self) -> list[Job]:
        return [i.job for i in self.items if isinstance(i, Speech)]


@dataclass
class ChapterPlan:
    index: int  # pořadí v knize (od 1), stabilní i při vynechání kapitol
    id: str
    title: str | None
    title_job: Job | None
    paragraphs: list[ParagraphPlan] = field(default_factory=list)

    @property
    def jobs(self) -> list[Job]:
        jobs = [self.title_job] if self.title_job else []
        for p in self.paragraphs:
            jobs.extend(p.jobs)
        return jobs


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# --- Procházení knihy ---------------------------------------------------------


@dataclass(frozen=True)
class _Para:
    chapter_index: int
    chapter_id: str
    chapter: Chapter
    index: int
    id: str
    para: Paragraph


def chapter_id(chapter: Chapter, index: int) -> str:
    return chapter.id or f"ch{index:02d}"


def _as_paragraph(item: str | Paragraph) -> Paragraph:
    return Paragraph(text=item) if isinstance(item, str) else item


def iter_paragraphs(book: Book) -> Iterator[_Para]:
    for ci, chapter in enumerate(book.chapters, start=1):
        cid = chapter_id(chapter, ci)
        for pi, item in enumerate(chapter.paragraphs, start=1):
            para = _as_paragraph(item)
            yield _Para(ci, cid, chapter, pi, para.id or f"{cid}.p{pi:03d}", para)


def _segments(para: Paragraph) -> list[Segment]:
    if para.segments is not None:
        return para.segments
    return [Segment(text=para.text or "")]


def _join_style(*parts: str | None) -> str | None:
    cleaned = [normalize_whitespace(p) for p in parts if p and p.strip()]
    return "; ".join(cleaned) or None


def _speaker_for(book: Book, p: _Para, seg: Segment) -> str:
    return seg.speaker or p.para.speaker or p.chapter.speaker or book.defaults.speaker


def _language_for(book: Book, p: _Para, seg: Segment) -> str:
    return seg.language or p.para.language or p.chapter.language or book.meta.language


def _style_for(book: Book, p: _Para, seg: Segment, speaker: str) -> str | None:
    member = book.cast.get(speaker)
    return _join_style(
        book.defaults.direction,
        member.style if member else None,
        p.chapter.direction,
        p.para.direction,
        seg.direction,
    )


# --- Kontrola -----------------------------------------------------------------


def check(book: Book, engine: str | None = None) -> Report:
    """Sémantická kontrola nad rámec schématu: odkazy, ID, značky, délka režie.

    `engine` (výchozí z knihy) určuje, co musí mít postavy: gemini `voice`, xtts `xtts`,
    mock nic navíc.
    """
    engine = engine or book.engine.name
    report = Report()
    cast = book.cast
    reported: set[str] = set()

    def need_speaker(speaker: str | None, where: str) -> None:
        if speaker is None:
            return
        if speaker not in cast:
            report.errors.append(f"{where}: mluvčí '{speaker}' není v `cast`")
            return
        member = cast[speaker]
        if engine == "gemini" and not member.voice:
            report.errors.append(
                f"{where}: mluvčí '{speaker}' má jen `design` – automatické vytvoření hlasu "
                "zatím není hotové; vytvoř hlas v AI Studiu a ID vlož do `voice`"
            )
        elif engine == "xtts" and not member.xtts and speaker not in reported:
            report.errors.append(
                f"{where}: mluvčí '{speaker}' nemá `xtts` – doplň `cast.{speaker}.xtts.reference` "
                f"(nahrávka hlasu) nebo `cast.{speaker}.xtts.speaker` (vestavěný hlas)"
            )
            reported.add(speaker)

    need_speaker(book.defaults.speaker, "defaults.speaker")

    for sid, member in cast.items():
        if (
            engine == "gemini"
            and member.voice
            and not (member.voice in PREBUILT_VOICES or member.voice.startswith(("voice_", "voicekey_")))
        ):
            report.warnings.append(
                f"cast.{sid}: hlas '{member.voice}' není mezi předpřipravenými hlasy "
                "(v pořádku, pokud je z knihovny hlasů)"
            )

    if engine == "xtts" and book.engine.sample_rate != xtts.SAMPLE_RATE:
        report.errors.append(
            f"engine.sample_rate: XTTS generuje {xtts.SAMPLE_RATE} Hz, nastav `sample_rate: {xtts.SAMPLE_RATE}`"
        )

    bad_languages: set[str] = set()

    def need_language(language: str, where: str) -> None:
        if engine == "xtts" and xtts.xtts_language(language) is None and language not in bad_languages:
            report.errors.append(
                f"{where}: XTTS nepodporuje jazyk '{language}' (umí: {', '.join(sorted(xtts.XTTS_LANGUAGES))})"
            )
            bad_languages.add(language)

    chapter_ids: dict[str, int] = {}
    for ci, chapter in enumerate(book.chapters, start=1):
        cid = chapter_id(chapter, ci)
        if cid in chapter_ids:
            report.errors.append(f"chapters[{ci - 1}]: ID kapitoly '{cid}' už má kapitola č. {chapter_ids[cid]}")
        chapter_ids.setdefault(cid, ci)
        need_speaker(chapter.speaker, f"{cid}.speaker")
        if chapter.title:
            need_language(chapter.language or book.meta.language, f"{cid}.title")

    used: set[str] = {book.defaults.speaker}
    para_ids: dict[str, str] = {}
    xtts_tags_reported = False
    lexicon = compile_lexicon(book.lexicon)
    for p in iter_paragraphs(book):
        where = p.id
        if p.id in para_ids:
            report.errors.append(f"{p.chapter_id}, odstavec {p.index}: ID '{p.id}' už má {para_ids[p.id]}")
        para_ids.setdefault(p.id, f"{p.chapter_id}, odstavec {p.index}")
        need_speaker(p.para.speaker, f"{where}.speaker")
        long_style_reported = False
        for si, seg in enumerate(_segments(p.para), start=1):
            seg_where = f"{where}.s{si}" if p.para.segments else where
            need_speaker(seg.speaker, f"{seg_where}.speaker")
            speaker = _speaker_for(book, p, seg)
            used.add(speaker)
            known, unknown = find_tags(apply_lexicon(seg.text, lexicon))
            for tag in unknown:
                report.warnings.append(f"{seg_where}: neznámá značka [{tag}] – přečte se jako text")
            if engine == "xtts" and known and not xtts_tags_reported:
                report.warnings.append(f"{seg_where}: XTTS neumí značky jako [{known[0]}] – v celé knize se vystřihnou")
                xtts_tags_reported = True
            need_language(_language_for(book, p, seg), seg_where)
            style = _style_for(book, p, seg, speaker)
            if style and len(style) > STYLE_WARN_CHARS and not long_style_reported:
                report.warnings.append(
                    f"{seg_where}: složená režie má {len(style)} znaků – dlouhé popisy "
                    "zhoršují stálost hlasu, zkus ji zkrátit"
                )
                long_style_reported = True
        if p.chapter.speaker:
            used.add(p.chapter.speaker)

    for sid in cast:
        if sid not in used:
            report.warnings.append(f"cast.{sid}: postava se v knize nepoužívá")

    if all(c.skip for c in book.chapters):
        report.warnings.append("všechny kapitoly mají skip: true – není co renderovat")
    return report


# --- Plán ---------------------------------------------------------------------


def job_key(
    *,
    engine: str,
    model: str,
    voice: str,
    style: str | None,
    text: str,
    sample_rate: int,
    language: str | None = None,
) -> str:
    """Hash všeho, co ovlivňuje audio. `language` jen u enginů, kterým se jazyk posílá (xtts)."""
    data: dict[str, object] = {
        "v": CACHE_KEY_VERSION,
        "engine": engine,
        "model": model,
        "voice": voice,
        "style": style,
        "text": text,
        "sample_rate": sample_rate,
    }
    if language is not None:
        data["language"] = language
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class _JobFactory:
    def __init__(self, book: Book, engine: str, model: str):
        self.book = book
        self.engine = engine
        self.model = model
        self.lexicon = compile_lexicon(book.lexicon)

    def prepare_text(self, text: str) -> str:
        text = apply_lexicon(normalize_whitespace(text), self.lexicon)
        if self.engine == "xtts":
            return strip_tags(text)
        return convert_tags(text)

    def max_chars(self, language: str) -> int:
        limit = self.book.engine.max_chars
        if self.engine == "xtts":
            return min(limit, xtts.char_limit(language))
        return limit

    def _voice(self, speaker: str, style: str | None) -> tuple[str, str | None]:
        """(voice, style) pro Job. U xtts je režie zahrnutá ve výběru nahrávky."""
        member = self.book.cast[speaker]
        if self.engine == "xtts":
            assert member.xtts, f"mluvčí {speaker} nemá `xtts` (měl to odhalit check())"
            return xtts.voice_for(member.xtts, style), None
        if self.engine == "mock":
            return member.voice or speaker, style
        assert member.voice, f"mluvčí {speaker} nemá hlas (měl to odhalit check())"
        return member.voice, style

    def make(self, *, text: str, speaker: str, style: str | None, language: str) -> Job:
        voice, style = self._voice(speaker, style)
        sr = self.book.engine.sample_rate
        key = job_key(
            engine=self.engine,
            model=self.model,
            voice=voice,
            style=style,
            text=text,
            sample_rate=sr,
            language=language if self.engine == "xtts" else None,
        )
        return Job(
            key=key,
            text=text,
            voice=voice,
            style=style,
            speaker=speaker,
            language=language,
            engine=self.engine,
            model=self.model,
            sample_rate=sr,
        )


def build_plan(book: Book, *, engine: str | None = None, model: str | None = None) -> list[ChapterPlan]:
    """Vytvoří plán renderu. Kniha musí projít `check()` bez chyb."""
    engine = engine or book.engine.name
    model = model_for(book, engine, model)
    factory = _JobFactory(book, engine, model)
    pauses = book.output.pauses

    chapters: dict[str, ChapterPlan] = {}
    order: list[ChapterPlan] = []
    for ci, chapter in enumerate(book.chapters, start=1):
        if chapter.skip:
            continue
        cid = chapter_id(chapter, ci)
        read_title = book.output.read_titles if chapter.read_title is None else chapter.read_title
        title_job = None
        title_text = factory.prepare_text(chapter.title) if chapter.title and read_title else ""
        if title_text:
            narrator = book.defaults.speaker
            member = book.cast[narrator]
            title_job = factory.make(
                text=title_text,
                speaker=narrator,
                style=_join_style(book.defaults.direction, member.style),
                language=chapter.language or book.meta.language,
            )
        plan = ChapterPlan(index=ci, id=cid, title=chapter.title, title_job=title_job)
        chapters[cid] = plan
        order.append(plan)

    for p in iter_paragraphs(book):
        if p.para.skip or p.chapter_id not in chapters or p.chapter.skip:
            continue
        items: list[Item] = []
        chars = 0
        for si, seg in enumerate(_segments(p.para)):
            if si:
                items.append(Silence(pauses.segment))
            speaker = _speaker_for(book, p, seg)
            style = _style_for(book, p, seg, speaker)
            language = _language_for(book, p, seg)
            text = factory.prepare_text(seg.text)
            chars += len(text)
            if not text:
                continue  # segment jen ze značek, které engine vystřihl
            for ki, chunk in enumerate(chunk_text(text, factory.max_chars(language))):
                if ki:
                    items.append(Silence(pauses.chunk))
                items.append(Speech(factory.make(text=chunk, speaker=speaker, style=style, language=language)))
        pause = p.para.pause_after if p.para.pause_after is not None else pauses.paragraph
        chapters[p.chapter_id].paragraphs.append(
            ParagraphPlan(id=p.id, chapter_id=p.chapter_id, items=items, pause_after=pause, chars=chars)
        )

    return [c for c in order if c.paragraphs or c.title_job]


def chapter_timeline(book: Book, chapter: ChapterPlan) -> list[Item]:
    """Celá kapitola jako posloupnost řeči a ticha."""
    pauses = book.output.pauses
    items: list[Item] = [Silence(pauses.chapter_start)]
    if chapter.title_job:
        items.append(Speech(chapter.title_job))
        items.append(Silence(pauses.title))
    for i, para in enumerate(chapter.paragraphs):
        items.extend(para.items)
        if i < len(chapter.paragraphs) - 1:
            items.append(Silence(para.pause_after))
    items.append(Silence(pauses.chapter_end))
    return items


# --- Rychlá zkouška hlasu (say) ----------------------------------------------


def say_voice(engine: str, voice: str | None) -> str:
    """Hlas pro `say`: u xtts cesta k WAV → 'wav:…', jinak jméno vestavěného hlasu → 'speaker:…'."""
    if engine != "xtts":
        return voice or "Kore"
    if not voice:
        raise ValueError("U xtts zadej hlas: cestu k WAV nahrávce nebo jméno vestavěného hlasu.")
    if voice.lower().endswith(".wav"):
        return xtts.wav_spec(voice)
    return xtts.speaker_spec(voice)


def say_jobs(
    *,
    engine: str,
    model: str,
    voice: str,
    text: str,
    language: str,
    style: str | None,
    sample_rate: int,
) -> list[Job]:
    """Požadavky pro zkoušku hlasu na jedné replice; u xtts rozdělené podle limitu jazyka."""
    if engine == "xtts":
        if xtts.xtts_language(language) is None:
            raise ValueError(f"XTTS nepodporuje jazyk '{language}'.")
        style = None
        prepared = strip_tags(text)
        chunks = chunk_text(prepared, xtts.char_limit(language)) if prepared else []
    else:
        prepared = convert_tags(normalize_whitespace(text))
        chunks = [prepared] if prepared else []
    if not chunks:
        raise ValueError("Prázdný text.")
    return [
        Job(
            key=job_key(
                engine=engine,
                model=model,
                voice=voice,
                style=style,
                text=chunk,
                sample_rate=sample_rate,
                language=language if engine == "xtts" else None,
            ),
            text=chunk,
            voice=voice,
            style=style,
            speaker="say",
            language=language,
            engine=engine,
            model=model,
            sample_rate=sample_rate,
        )
        for chunk in chunks
    ]
