"""Příkazová řádka: tts-book validate | plan | render | say | schema."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import __version__
from .audio import AudioError, write_wav
from .engines import EngineError, create_engine
from .loader import BookError, load_book
from .model import Book
from .render import (
    assemble_chapter,
    cache_for,
    find_paragraphs,
    job_owner_map,
    missing_jobs,
    output_dir,
    synthesize,
    unique_jobs,
    write_preview,
)
from .resolve import Job, Report, build_plan, check, job_key
from .text import convert_tags, normalize_whitespace, strip_tags

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Audioknihy z YAML scénáře s režií hlasů (Gemini TTS).",
)
console = Console()
err_console = Console(stderr=True)

CHARS_PER_SECOND = 14.0  # odhad tempa předčítání
AUDIO_TOKENS_PER_SECOND = 25
# USD za 1M výstupních (audio) tokenů, standardní tier – stav 11/2026.
# U 3.8 modelů se od 1. 1. 2027 cena zdvojnásobí. Přepiš přes --price.
PRICES_USD_PER_M = {
    "gemini-3.8-flash-tts": 9.0,
    "gemini-3.8-flash-lite-tts": 6.0,
    "gemini-3.1-flash-tts-preview": 20.0,
    "gemini-2.5-flash-preview-tts": 10.0,
    "gemini-2.5-pro-preview-tts": 20.0,
}

BookArg = Annotated[Path, typer.Argument(help="Soubor knihy (*.book.yaml).", exists=True, dir_okay=False)]
EngineOpt = Annotated[str | None, typer.Option("--engine", "-e", help="Přepíše engine ze souboru: gemini | mock.")]
ModelOpt = Annotated[str | None, typer.Option("--model", "-m", help="Přepíše model ze souboru.")]


def _fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _num(n: float) -> str:
    return f"{n:,.0f}".replace(",", "\u00a0")


def _print_report(report: Report) -> None:
    for e in report.errors:
        err_console.print(f"[red]chyba[/]  {e}")
    for w in report.warnings:
        err_console.print(f"[yellow]pozor[/]  {w}")


def _load_checked(path: Path) -> Book:
    try:
        book = load_book(path)
    except BookError as exc:
        for p in exc.problems:
            err_console.print(f"[red]chyba[/]  {p}")
        err_console.print(f"[red]Soubor {path} není platný.[/]")
        raise typer.Exit(1) from None
    report = check(book)
    _print_report(report)
    if not report.ok:
        err_console.print(f"[red]Soubor {path} obsahuje {len(report.errors)} chyb(y).[/]")
        raise typer.Exit(1)
    return book


@app.callback(invoke_without_command=True)
def _main(
    version: Annotated[bool, typer.Option("--version", help="Vypíše verzi.")] = False,
) -> None:
    if version:
        console.print(f"tts-book {__version__}")
        raise typer.Exit()


@app.command()
def validate(book_path: BookArg) -> None:
    """Zkontroluje soubor knihy (schéma, odkazy na postavy, ID, značky)."""
    book = _load_checked(book_path)
    chapters = build_plan(book)
    paragraphs = sum(len(c.paragraphs) for c in chapters)
    jobs = unique_jobs(j for c in chapters for j in c.jobs)
    chars = sum(len(strip_tags(j.text)) for j in jobs)
    console.print(
        f"[green]OK[/]  {book.meta.title}: {len(chapters)} kapitol, {paragraphs} odstavců, "
        f"{len(jobs)} požadavků, {_num(chars)} znaků"
    )


@app.command()
def plan(
    book_path: BookArg,
    engine: EngineOpt = None,
    model: ModelOpt = None,
    price: Annotated[
        float | None, typer.Option(help="Cena v USD za 1M výstupních audio tokenů (přepíše vestavěný ceník).")
    ] = None,
) -> None:
    """Zkušební běh: co se bude generovat, co je v cache, odhad délky a ceny."""
    book = _load_checked(book_path)
    engine_name = engine or book.engine.name
    model_name = model or book.engine.model
    chapters = build_plan(book, engine=engine_name, model=model_name)
    cache = cache_for(book, book_path)

    table = Table(title=f"{book.meta.title} – plán renderu ({engine_name}, {model_name})")
    for col in ("kapitola", "odstavců", "požadavků", "znaků", "v cache", "délka*"):
        table.add_column(col, justify="left" if col == "kapitola" else "right")

    total_new_chars = 0
    total_seconds = 0.0
    all_jobs: list[Job] = []
    for c in chapters:
        jobs = unique_jobs(c.jobs)
        all_jobs.extend(jobs)
        cached = [j for j in jobs if cache.has(j.key)]
        seconds = sum(cache.duration(j.key) for j in cached)
        new = [j for j in jobs if not cache.has(j.key)]
        new_chars = sum(len(strip_tags(j.text)) for j in new)
        seconds += new_chars / CHARS_PER_SECOND
        total_new_chars += new_chars
        total_seconds += seconds
        table.add_row(
            f"{c.index:02d} {c.title or c.id}",
            str(len(c.paragraphs)),
            str(len(jobs)),
            str(sum(len(strip_tags(j.text)) for j in jobs)),
            f"{len(cached)}/{len(jobs)}",
            _fmt_duration(seconds),
        )
    console.print(table)

    unique_all = unique_jobs(all_jobs)
    to_generate = missing_jobs(unique_all, cache)
    new_seconds = total_new_chars / CHARS_PER_SECOND
    console.print(f"Celkem: {len(unique_all)} požadavků, k vygenerování {len(to_generate)}.")
    console.print(f"Odhad délky audioknihy: {_fmt_duration(total_seconds)} (* u nevygenerovaných odhad z počtu znaků)")
    if engine_name == "gemini" and to_generate:
        rate = price if price is not None else PRICES_USD_PER_M.get(model_name)
        tokens = new_seconds * AUDIO_TOKENS_PER_SECOND
        if rate is None:
            console.print(f"Odhad: ~{_num(tokens)} audio tokenů (cenu modelu neznám, zadej --price).")
        else:
            cost = tokens / 1_000_000 * rate
            console.print(
                f"Odhad ceny: ~${cost:.2f} za ~{_fmt_duration(new_seconds)} nového audia "
                f"(${rate:g}/1M tokenů, bez přegenerování; Batch API by bylo za polovinu)."
            )


@app.command()
def render(
    book_path: BookArg,
    chapter: Annotated[
        list[str] | None, typer.Option("--chapter", "-c", help="Jen tyto kapitoly (ID, lze opakovat).")
    ] = None,
    only: Annotated[
        list[str] | None,
        typer.Option(
            "--only",
            "-o",
            help="Přegeneruje jen tyto odstavce (ID, lze opakovat) a uloží je do previews/. "
            "Nová verze nahradí tu v cache.",
        ),
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Ignorovat cache a vše vygenerovat znovu.")] = False,
    engine: EngineOpt = None,
    model: ModelOpt = None,
) -> None:
    """Vygeneruje audio a složí kapitoly do souborů."""
    book = _load_checked(book_path)
    engine_name = engine or book.engine.name
    model_name = model or book.engine.model
    chapters = build_plan(book, engine=engine_name, model=model_name)
    out_dir = output_dir(book, book_path)
    cache = cache_for(book, book_path)
    owners = job_owner_map(chapters)

    targets_paragraphs = None
    if only:
        targets_paragraphs, unknown = find_paragraphs(chapters, only)
        if unknown:
            err_console.print(f"[red]Neznámé ID odstavce:[/] {', '.join(unknown)}")
            raise typer.Exit(1)
        jobs = unique_jobs(j for p in targets_paragraphs for j in p.jobs)
        force = True
    else:
        if chapter:
            known = {c.id for c in chapters}
            unknown = [c for c in chapter if c not in known]
            if unknown:
                err_console.print(f"[red]Neznámé ID kapitoly:[/] {', '.join(unknown)}")
                raise typer.Exit(1)
            chapters_sel = [c for c in chapters if c.id in chapter]
        else:
            chapters_sel = chapters
        jobs = unique_jobs(j for c in chapters_sel for j in c.jobs)

    todo = jobs if force else missing_jobs(jobs, cache)
    try:
        tts = create_engine(engine_name, book, model=model_name) if todo else None
    except EngineError as exc:
        err_console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from None

    if tts is not None:
        console.print(f"Generuji úseky: {len(todo)} ({engine_name}, {model_name}) …")
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("syntéza", total=len(todo))

            def on_progress(job: Job, status: str) -> None:
                if status == "cached":
                    return
                where = owners.get(job.key, job.key[:8])
                if status == "error":
                    progress.console.print(f"[red]✗[/] {where}")
                progress.update(task, advance=1, description=f"syntéza {where}")

            result = synthesize(tts, cache, todo, force=force, progress=on_progress)
        tts.close()
        for key, msg in result.failures.items():
            err_console.print(f"[red]chyba[/]  {owners.get(key, key[:8])}: {msg}")
        if result.aborted:
            err_console.print("[red]Příliš mnoho chyb za sebou – končím.[/]")
        console.print(f"Vygenerováno {result.synthesized}, chyb {len(result.failures)}.")

    try:
        if targets_paragraphs is not None:
            for para in targets_paragraphs:
                if missing_jobs(para.jobs, cache):
                    err_console.print(f"[yellow]přeskočeno[/]  {para.id}: chybí audio")
                    continue
                path, duration = write_preview(book, para, out_dir, cache)
                console.print(f"[green]✓[/] {para.id} → {path} ({_fmt_duration(duration)})")
            return

        failed = 0
        for c in chapters_sel:
            if missing_jobs(c.jobs, cache):
                err_console.print(f"[yellow]přeskočeno[/]  {c.id}: některé úseky chybí (viz chyby výše)")
                failed += 1
                continue
            path, duration = assemble_chapter(book, c, out_dir, cache, total=chapters[-1].index)
            console.print(f"[green]✓[/] {path.name} ({_fmt_duration(duration)})")
        console.print(f"Výstup: {out_dir}")
        if failed:
            raise typer.Exit(2)
    except AudioError as exc:
        err_console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from None


@app.command()
def say(
    text: Annotated[str, typer.Argument(help="Text k přečtení (lze použít značky jako [sigh]).")],
    voice: Annotated[str, typer.Option("--voice", "-v", help="Hlas, např. Kore, Puck, Charon, nebo voice_…")] = "Kore",
    style: Annotated[str | None, typer.Option("--style", "-s", help="Režie přednesu.")] = None,
    out: Annotated[Path, typer.Option("--out", "-o", help="Výstupní WAV.")] = Path("say.wav"),
    model: Annotated[str, typer.Option("--model", "-m")] = "gemini-3.8-flash-tts",
    engine: Annotated[str, typer.Option("--engine", "-e")] = "gemini",
) -> None:
    """Rychlé vyzkoušení hlasu a režie na jedné větě."""
    from .model import Book as _Book

    book = _Book.model_validate(
        {
            "version": 1,
            "meta": {"title": "say", "language": "cs-CZ"},
            "engine": {"name": engine, "model": model},
            "cast": {"x": {"voice": voice}},
            "defaults": {"speaker": "x"},
            "chapters": [{"paragraphs": [text]}],
        }
    )
    prepared = convert_tags(normalize_whitespace(text))
    sr = book.engine.sample_rate
    job = Job(
        key=job_key(engine=engine, model=model, voice=voice, style=style, text=prepared, sample_rate=sr),
        text=prepared,
        voice=voice,
        style=style,
        speaker="x",
        language="cs-CZ",
        engine=engine,
        model=model,
        sample_rate=sr,
    )
    try:
        tts = create_engine(engine, book, model=model)
        pcm = tts.synthesize(job)
        tts.close()
    except EngineError as exc:
        err_console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from None
    write_wav(out, [pcm], sr)
    console.print(f"[green]✓[/] {out} ({_fmt_duration(len(pcm) / (2 * sr))})")


@app.command()
def schema(
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Kam uložit (jinak stdout).")] = None,
) -> None:
    """Vypíše JSON Schema formátu knihy (pro editory a validaci)."""
    text = json.dumps(book_json_schema(), ensure_ascii=False, indent=2) + "\n"
    if out:
        out.write_text(text, encoding="utf-8")
        console.print(f"[green]✓[/] {out}")
    else:
        typer.echo(text, nl=False)


def book_json_schema() -> dict:
    schema = Book.model_json_schema()
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}


if __name__ == "__main__":  # pragma: no cover
    app()
