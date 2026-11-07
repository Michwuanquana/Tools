"""Render: syntéza chybějících úseků (s cache) a skládání kapitol."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .audio import Cache, silence, write_mp3, write_wav
from .engines.base import EngineError, TTSEngine
from .model import Book
from .resolve import ChapterPlan, Item, Job, ParagraphPlan, Silence, Speech, chapter_timeline

MAX_CONSECUTIVE_FAILURES = 3

ProgressFn = Callable[[Job, str], None]  # (job, "ok" | "cached" | "error")


def book_stem(book_path: Path) -> str:
    name = book_path.name
    for suffix in (".book.yaml", ".book.yml", ".yaml", ".yml"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return book_path.stem


def output_dir(book: Book, book_path: Path) -> Path:
    base = book_path.resolve().parent
    if book.output.dir:
        return (base / book.output.dir).resolve()
    return base / "build" / book_stem(book_path)


def cache_for(book: Book, book_path: Path) -> Cache:
    return Cache(output_dir(book, book_path) / "cache", book.engine.sample_rate)


_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def safe_filename(name: str) -> str:
    cleaned = _UNSAFE.sub("-", name).strip(" .-")
    return cleaned[:80] or "kapitola"


def chapter_filename(chapter: ChapterPlan, ext: str) -> str:
    return f"{chapter.index:02d} - {safe_filename(chapter.title or chapter.id)}.{ext}"


def unique_jobs(jobs: Iterable[Job]) -> list[Job]:
    seen: dict[str, Job] = {}
    for job in jobs:
        seen.setdefault(job.key, job)
    return list(seen.values())


@dataclass
class SynthesisResult:
    synthesized: int = 0
    cached: int = 0
    failures: dict[str, str] = field(default_factory=dict)  # job.key → chyba
    aborted: bool = False


def synthesize(
    engine: TTSEngine,
    cache: Cache,
    jobs: list[Job],
    *,
    force: bool = False,
    progress: ProgressFn | None = None,
) -> SynthesisResult:
    """Vygeneruje úseky, které nejsou v cache (nebo všechny při `force`).

    Po několika chybách za sebou skončí (typicky špatný klíč nebo vyčerpaná kvóta),
    ať se zbytečně nevolá API pro stovky dalších odstavců.
    """
    result = SynthesisResult()
    consecutive = 0
    for job in unique_jobs(jobs):
        if not force and cache.has(job.key):
            result.cached += 1
            if progress:
                progress(job, "cached")
            continue
        try:
            pcm = engine.synthesize(job)
        except EngineError as exc:
            result.failures[job.key] = str(exc)
            consecutive += 1
            if progress:
                progress(job, "error")
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                result.aborted = True
                break
            continue
        consecutive = 0
        cache.put(job.key, pcm)
        result.synthesized += 1
        if progress:
            progress(job, "ok")
    return result


def _pcm_stream(items: list[Item], cache: Cache, sample_rate: int, counter: list[int]) -> Iterator[bytes]:
    for item in items:
        if isinstance(item, Silence):
            data = silence(item.seconds, sample_rate)
        else:
            data = cache.get(item.job.key)
        counter[0] += len(data)
        yield data


def missing_jobs(jobs: Iterable[Job], cache: Cache) -> list[Job]:
    return [j for j in unique_jobs(jobs) if not cache.has(j.key)]


def write_audio(
    book: Book,
    path: Path,
    items: list[Item],
    cache: Cache,
    *,
    metadata: dict[str, str] | None = None,
) -> float:
    """Zapíše posloupnost řeči a ticha do souboru; vrací délku v sekundách."""
    sr = book.engine.sample_rate
    counter = [0]
    stream = _pcm_stream(items, cache, sr, counter)
    if path.suffix == ".wav":
        write_wav(path, stream, sr)
    else:
        write_mp3(path, stream, sr, bitrate=book.output.bitrate, metadata=metadata)
    return counter[0] / (2 * sr)


def assemble_chapter(book: Book, chapter: ChapterPlan, out_dir: Path, cache: Cache, total: int) -> tuple[Path, float]:
    ext = book.output.format
    path = out_dir / chapter_filename(chapter, ext)
    metadata = {
        "title": chapter.title or chapter.id,
        "album": book.meta.title,
        "track": f"{chapter.index}/{total}",
    }
    if book.meta.author:
        metadata["artist"] = book.meta.author
    duration = write_audio(book, path, chapter_timeline(book, chapter), cache, metadata=metadata)
    return path, duration


def write_preview(book: Book, paragraph: ParagraphPlan, out_dir: Path, cache: Cache) -> tuple[Path, float]:
    path = out_dir / "previews" / f"{safe_filename(paragraph.id)}.{book.output.format}"
    items: list[Item] = [Silence(0.3), *paragraph.items, Silence(0.3)]
    return path, write_audio(book, path, items, cache, metadata={"title": paragraph.id})


def find_paragraphs(chapters: list[ChapterPlan], ids: list[str]) -> tuple[list[ParagraphPlan], list[str]]:
    index = {p.id: p for c in chapters for p in c.paragraphs}
    found = [index[i] for i in ids if i in index]
    missing = [i for i in ids if i not in index]
    return found, missing


def job_owner_map(chapters: list[ChapterPlan]) -> dict[str, str]:
    """job.key → čitelné místo v knize (pro chybová hlášení)."""
    owners: dict[str, str] = {}
    for c in chapters:
        if c.title_job:
            owners.setdefault(c.title_job.key, f"{c.id} (název)")
        for p in c.paragraphs:
            for item in p.items:
                if isinstance(item, Speech):
                    owners.setdefault(item.job.key, p.id)
    return owners
