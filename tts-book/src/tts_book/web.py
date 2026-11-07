"""Webové rozhraní: `tts-book serve kniha.book.yaml` → http://127.0.0.1:8765.

Jen standardní knihovna (http.server). Syntéza běží v jednom pracovním vlákně,
které drží načtené enginy – model XTTS se tak načítá jen jednou za běh serveru.
Kniha se znovu načte, kdykoli se změní soubor (edituje se dál v editoru).
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .audio import AudioError, Cache, pcm_to_wav, silence
from .engines import EngineError, TTSEngine, create_engine
from .loader import BookError, load_book
from .model import DEFAULT_MODELS, Book, model_for
from .render import (
    assemble_chapter,
    cache_for,
    find_paragraphs,
    job_owner_map,
    missing_jobs,
    output_dir,
    synthesize,
    unique_jobs,
)
from .resolve import (
    PREBUILT_VOICES,
    ChapterPlan,
    Item,
    Job,
    Silence,
    Speech,
    build_plan,
    chapter_timeline,
    check,
    say_jobs,
    say_voice,
)
from .xtts import XTTS_SPEAKERS, parse_voice

ENGINES = ("gemini", "xtts", "mock")
SAY_KEEP = 20  # kolik posledních zkoušek hlasu držet v paměti
_KEY_RE = re.compile(r"^[0-9a-f]{64}$")

EngineFactory = Callable[..., TTSEngine]


class ApiError(Exception):
    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


@dataclass
class Task:
    id: str
    kind: str  # synthesize | assemble | say
    label: str
    state: str = "queued"  # queued | loading | running | done | error | cancelled
    total: int = 0
    done: int = 0
    current: str = ""
    errors: list[str] = field(default_factory=list)
    message: str = ""
    started: float = field(default_factory=time.time)
    finished: float | None = None
    result: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "state": self.state,
            "total": self.total,
            "done": self.done,
            "current": self.current,
            "errors": self.errors[-50:],
            "message": self.message,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
            "result": self.result,
        }


class Studio:
    """Stav serveru: kniha, enginy, fronta úloh. Nezávislé na HTTP (testuje se přímo)."""

    def __init__(
        self,
        book_path: Path,
        *,
        engine: str | None = None,
        model: str | None = None,
        engine_factory: EngineFactory = create_engine,
    ):
        self.book_path = book_path.resolve()
        self.base_dir = self.book_path.parent
        self.default_engine = engine
        self.default_model = model
        self._factory = engine_factory
        self._lock = threading.RLock()
        self._book: Book | None = None
        self._mtime: float | None = None
        self._problems: list[str] = []
        self._engines: dict[tuple[str, str], TTSEngine] = {}
        self._tasks: dict[str, Task] = {}
        self._current: Task | None = None
        self._queue: queue.Queue[tuple[Task, Callable[[Task], None]] | None] = queue.Queue()
        self._stop = threading.Event()
        self._says: dict[str, bytes] = {}
        self._worker = threading.Thread(target=self._work, name="tts-book-worker", daemon=True)
        self._worker.start()

    # --- Kniha ----------------------------------------------------------------

    def _file_mtime(self) -> float | None:
        try:
            return self.book_path.stat().st_mtime
        except OSError:
            return None

    def book(self) -> Book:
        """Aktuální kniha; po změně souboru se načte znovu. Neplatný soubor → ApiError."""
        with self._lock:
            mtime = self._file_mtime()
            if mtime != self._mtime:
                self._mtime = mtime
                try:
                    self._book = load_book(self.book_path)
                    self._problems = []
                except BookError as exc:
                    self._book = None
                    self._problems = exc.problems
            if self._book is None:
                raise ApiError("Soubor knihy není platný: " + "; ".join(self._problems), HTTPStatus.CONFLICT)
            return self._book

    def settings(self, engine: str | None, model: str | None) -> tuple[Book, str, str]:
        """(kniha, engine, model). `--model` ze `serve` platí jen pro engine zadaný při spuštění."""
        book = self.book()
        start_engine = self.default_engine or book.engine.name
        name = engine or start_engine
        if name not in ENGINES:
            raise ApiError(f"neznámý engine '{name}'")
        if not model and name == start_engine:
            model = self.default_model
        return book, name, model_for(book, name, model)

    def _plan(self, engine: str | None, model: str | None) -> tuple[Book, str, str, list[ChapterPlan]]:
        book, name, model_name = self.settings(engine, model)
        report = check(book, name)
        if not report.ok:
            raise ApiError("Kniha má chyby: " + "; ".join(report.errors[:5]), HTTPStatus.CONFLICT)
        return book, name, model_name, build_plan(book, engine=name, model=model_name)

    def cache(self, book: Book) -> Cache:
        return cache_for(book, self.book_path)

    def overview(self, engine: str | None, model: str | None) -> dict[str, Any]:
        """Celý stav knihy pro GUI: kapitoly, odstavce, úseky a co z nich je v cache."""
        try:
            book, name, model_name = self.settings(engine, model)
        except ApiError as exc:
            if exc.status != HTTPStatus.CONFLICT:
                raise
            return {"path": str(self.book_path), "problems": self._problems, "chapters": [], "engines": ENGINES}
        report = check(book, name)
        out = output_dir(book, self.book_path)
        data: dict[str, Any] = {
            "path": str(self.book_path),
            "mtime": self._mtime,
            "title": book.meta.title,
            "author": book.meta.author,
            "language": book.meta.language,
            "engine": name,
            "model": model_name,
            "default_model": DEFAULT_MODELS[name],
            "book_engine": book.engine.name,
            "engines": ENGINES,
            "problems": [],
            "errors": report.errors,
            "warnings": report.warnings,
            "cast": [
                {
                    "id": sid,
                    "name": m.name,
                    "voice": m.voice,
                    "xtts": m.xtts.model_dump(exclude_defaults=True) if m.xtts else None,
                }
                for sid, m in book.cast.items()
            ],
            "output_dir": str(out),
            "outputs": sorted(p.name for p in out.glob("*") if p.suffix in (".mp3", ".wav")) if out.is_dir() else [],
            "chapters": [],
        }
        if not report.ok:
            return data
        cache = self.cache(book)
        total = cached = 0
        for c in build_plan(book, engine=name, model=model_name):
            chapter_jobs = unique_jobs(c.jobs)
            n_cached = sum(cache.has(j.key) for j in chapter_jobs)
            total += len(chapter_jobs)
            cached += n_cached
            data["chapters"].append(
                {
                    "id": c.id,
                    "index": c.index,
                    "title": c.title,
                    "title_item": _item(Speech(c.title_job), cache) if c.title_job else None,
                    "jobs": len(chapter_jobs),
                    "cached": n_cached,
                    "paragraphs": [
                        {
                            "id": p.id,
                            "chars": p.chars,
                            "pause_after": p.pause_after,
                            "items": [_item(i, cache) for i in p.items],
                        }
                        for p in c.paragraphs
                    ],
                }
            )
        data["jobs"] = total
        data["cached"] = cached
        return data

    def voices(self, engine: str) -> list[str]:
        if engine == "gemini":
            return sorted(PREBUILT_VOICES)
        if engine == "xtts":
            for (name, _), tts in self._engines.items():
                if name == "xtts" and getattr(tts, "speakers", None):
                    return list(tts.speakers)
            return sorted(XTTS_SPEAKERS)
        return []

    # --- Audio ----------------------------------------------------------------

    def clip(self, key: str) -> bytes:
        if not _KEY_RE.match(key):
            raise ApiError("neplatný klíč", HTTPStatus.NOT_FOUND)
        book = self.book()
        cache = self.cache(book)
        if not cache.has(key):
            raise ApiError("úsek není vygenerovaný", HTTPStatus.NOT_FOUND)
        return cache.path(key).read_bytes()

    def _timeline_wav(self, book: Book, items: list[Item]) -> bytes:
        cache = self.cache(book)
        sr = book.engine.sample_rate
        missing = [i for i in items if isinstance(i, Speech) and not cache.has(i.job.key)]
        if missing:
            raise ApiError(f"chybí {len(missing)} úsek(ů) – nejdřív je vygeneruj", HTTPStatus.NOT_FOUND)
        parts = [silence(i.seconds, sr) if isinstance(i, Silence) else cache.get(i.job.key) for i in items]
        return pcm_to_wav(b"".join(parts), sr)

    def paragraph_wav(self, pid: str, engine: str | None, model: str | None) -> bytes:
        book, _, _, chapters = self._plan(engine, model)
        found, _ = find_paragraphs(chapters, [pid])
        if not found:
            raise ApiError(f"neznámý odstavec '{pid}'", HTTPStatus.NOT_FOUND)
        return self._timeline_wav(book, [Silence(0.2), *found[0].items, Silence(0.2)])

    def chapter_wav(self, cid: str, engine: str | None, model: str | None) -> bytes:
        book, _, _, chapters = self._plan(engine, model)
        chapter = next((c for c in chapters if c.id == cid), None)
        if chapter is None:
            raise ApiError(f"neznámá kapitola '{cid}'", HTTPStatus.NOT_FOUND)
        return self._timeline_wav(book, chapter_timeline(book, chapter))

    def output_file(self, name: str) -> Path:
        book = self.book()
        out = output_dir(book, self.book_path)
        path = (out / name).resolve()
        if path.parent != out.resolve() or not path.is_file() or path.suffix not in (".mp3", ".wav"):
            raise ApiError("soubor neexistuje", HTTPStatus.NOT_FOUND)
        return path

    def say_wav(self, sid: str) -> bytes:
        with self._lock:
            if sid not in self._says:
                raise ApiError("zkouška už není v paměti", HTTPStatus.NOT_FOUND)
            return self._says[sid]

    # --- Úlohy ----------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            task = self._current
            last = task or (max(self._tasks.values(), key=lambda t: t.started) if self._tasks else None)
            return {
                "mtime": self._file_mtime(),
                "busy": task is not None,
                "queued": self._queue.qsize(),
                "task": last.public() if last else None,
                "engines": [
                    {"engine": n, "model": m, "device": getattr(e, "device", None)}
                    for (n, m), e in self._engines.items()
                ],
            }

    def _submit(self, kind: str, label: str, run: Callable[[Task], None]) -> dict[str, Any]:
        with self._lock:
            if self._current is not None or not self._queue.empty():
                raise ApiError("už běží jiná úloha – počkej nebo ji zruš", HTTPStatus.CONFLICT)
            task = Task(id=uuid.uuid4().hex[:12], kind=kind, label=label)
            self._tasks[task.id] = task
            self._current = task
            self._stop.clear()
        self._queue.put((task, run))
        return task.public()

    def cancel(self) -> dict[str, Any]:
        self._stop.set()
        return self.status()

    def unload(self) -> dict[str, Any]:
        """Uvolní načtené enginy (VRAM). Jen když nic neběží."""

        def run(task: Task) -> None:
            for engine in self._engines.values():
                engine.close()
            task.message = f"uvolněno {len(self._engines)} engine(ů)"
            self._engines.clear()

        return self._submit("unload", "uvolnění modelu", run)

    def _engine(self, task: Task, book: Book, name: str, model: str) -> TTSEngine:
        key = (name, model)
        if key not in self._engines:
            task.state = "loading"
            task.current = f"načítám {name} ({model}) …"
            self._engines[key] = self._factory(name, book, model=model, base_dir=self.base_dir)
            if getattr(self._engines[key], "device", None) == "cpu":
                task.message = "XTTS běží na CPU – bude to řádově pomalejší."
        task.state = "running"
        return self._engines[key]

    def submit_synthesize(
        self,
        *,
        engine: str | None,
        model: str | None,
        paragraphs: list[str] | None = None,
        chapters: list[str] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        book, name, model_name, plan = self._plan(engine, model)
        owners = job_owner_map(plan)
        if paragraphs:
            found, unknown = find_paragraphs(plan, paragraphs)
            if unknown:
                raise ApiError(f"neznámé odstavce: {', '.join(unknown)}")
            jobs = unique_jobs(j for p in found for j in p.jobs)
            label = f"odstavec {paragraphs[0]}" if len(paragraphs) == 1 else f"{len(paragraphs)} odstavců"
        else:
            selected = [c for c in plan if not chapters or c.id in chapters]
            jobs = unique_jobs(j for c in selected for j in c.jobs)
            label = f"kapitola {chapters[0]}" if chapters and len(chapters) == 1 else "celá kniha"
        cache = self.cache(book)
        todo = jobs if force else missing_jobs(jobs, cache)
        if name == "xtts":
            from .engines.xtts import missing_references

            missing = missing_references(todo, self.base_dir)
            if missing:
                raise ApiError("chybí referenční nahrávky: " + ", ".join(map(str, missing)))

        def run(task: Task) -> None:
            task.total = len(todo)
            if not todo:
                task.message = "vše už je v cache"
                return
            tts = self._engine(task, book, name, model_name)

            def progress(job: Job, status: str) -> None:
                if status == "cached":
                    return
                task.done += 1
                task.current = owners.get(job.key, job.key[:8])

            result = synthesize(tts, cache, todo, force=force, progress=progress, should_stop=self._stop.is_set)
            task.errors = [f"{owners.get(k, k[:8])}: {msg}" for k, msg in result.failures.items()]
            task.result = {"synthesized": result.synthesized, "failures": len(result.failures)}
            if self._stop.is_set():
                task.state = "cancelled"
            elif result.aborted:
                task.message = "příliš mnoho chyb za sebou – končím"
            elif task.errors:
                task.message = f"{len(task.errors)} chyb(y)"

        return self._submit("synthesize", f"syntéza: {label} ({name})", run)

    def submit_assemble(self, *, engine: str | None, model: str | None, chapters: list[str] | None = None):
        book, name, _, plan = self._plan(engine, model)
        selected = [c for c in plan if not chapters or c.id in chapters]
        out = output_dir(book, self.book_path)
        cache = self.cache(book)

        def run(task: Task) -> None:
            task.state = "running"
            task.total = len(selected)
            written: list[str] = []
            for c in selected:
                if self._stop.is_set():
                    task.state = "cancelled"
                    return
                task.current = c.id
                if missing_jobs(c.jobs, cache):
                    task.errors.append(f"{c.id}: některé úseky chybí – nejdřív je vygeneruj")
                else:
                    path, _ = assemble_chapter(book, c, out, cache, total=plan[-1].index)
                    written.append(path.name)
                task.done += 1
            task.result = {"files": written}
            task.message = f"složeno {len(written)} z {len(selected)} kapitol"

        return self._submit("assemble", f"skládání kapitol ({name})", run)

    def submit_say(
        self,
        *,
        engine: str | None,
        model: str | None,
        voice: str | None,
        text: str,
        language: str | None,
        style: str | None,
    ) -> dict[str, Any]:
        book, name, model_name = self.settings(engine, model)
        language = language or book.meta.language
        try:
            spec = say_voice(name, voice)
            jobs = say_jobs(
                engine=name,
                model=model_name,
                voice=spec,
                text=text,
                language=language,
                style=style or None,
                sample_rate=book.engine.sample_rate,
            )
        except ValueError as exc:
            raise ApiError(str(exc)) from None
        if name == "xtts":
            kind, value = parse_voice(spec)
            if kind == "wav":
                from .engines.xtts import missing_references

                missing = missing_references(jobs, self.base_dir)
                if missing:
                    raise ApiError("chybí nahrávka: " + ", ".join(map(str, missing)))
        sr = book.engine.sample_rate

        def run(task: Task) -> None:
            tts = self._engine(task, book, name, model_name)
            task.total = len(jobs)
            pcm: list[bytes] = []
            for i, job in enumerate(jobs):
                if self._stop.is_set():
                    task.state = "cancelled"
                    return
                if i:
                    pcm.append(silence(book.output.pauses.chunk, sr))
                pcm.append(tts.synthesize(job))
                task.done += 1
            wav = pcm_to_wav(b"".join(pcm), sr)
            with self._lock:
                self._says[task.id] = wav
                while len(self._says) > SAY_KEEP:
                    self._says.pop(next(iter(self._says)))
            task.result = {"say": task.id, "seconds": round(sum(map(len, pcm)) / (2 * sr), 2)}

        return self._submit("say", f"zkouška hlasu ({name})", run)

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            task, run = item
            task.state = "running"
            try:
                run(task)
                if task.state in ("running", "loading", "queued"):
                    task.state = "error" if task.errors and not task.result.get("synthesized") else "done"
            except (EngineError, AudioError, ApiError) as exc:
                task.state = "error"
                task.message = str(exc)
            except Exception as exc:  # chyba v kódu nesmí shodit vlákno
                task.state = "error"
                task.message = f"{type(exc).__name__}: {exc}"
            finally:
                task.finished = time.time()
                task.current = ""
                with self._lock:
                    self._current = None

    def wait(self, timeout: float = 30.0) -> dict[str, Any]:
        """Počká, až doběhne aktuální úloha (pro testy)."""
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                if self._current is None and self._queue.empty():
                    break
            time.sleep(0.02)
        return self.status()

    def close(self) -> None:
        self._stop.set()
        self._queue.put(None)
        self._worker.join(timeout=5)
        for engine in self._engines.values():
            engine.close()
        self._engines.clear()


def _item(item: Item, cache: Cache) -> dict[str, Any]:
    if isinstance(item, Silence):
        return {"kind": "silence", "seconds": item.seconds}
    job = item.job
    has = cache.has(job.key)
    return {
        "kind": "speech",
        "key": job.key,
        "text": job.text,
        "speaker": job.speaker,
        "voice": job.voice,
        "style": job.style,
        "language": job.language,
        "cached": has,
        "duration": round(cache.duration(job.key), 2) if has else None,
    }


# --- HTTP ---------------------------------------------------------------------


def _index_html() -> bytes:
    return resources.files("tts_book").joinpath("static/index.html").read_bytes()


class Handler(BaseHTTPRequestHandler):
    studio: Studio
    allowed_hosts: frozenset[str]
    server_version = "tts-book"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - podpis z BaseHTTPRequestHandler
        pass  # ticho; chyby vrací API

    # --- odpovědi ---

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data: Any, status: int = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _audio(self, data: bytes, content_type: str = "audio/wav", filename: str | None = None) -> None:
        """Audio s podporou Range – bez ní prohlížeč neumí přetáčet."""
        extra = {"Accept-Ranges": "bytes"}
        if filename:
            extra["Content-Disposition"] = f"inline; filename*=UTF-8''{_quote(filename)}"
        rng = self.headers.get("Range", "")
        m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
        if m and (m.group(1) or m.group(2)):
            size = len(data)
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
            else:
                start, end = max(0, size - int(m.group(2))), size - 1
            if start >= size or start > end:
                bad = {"Content-Range": f"bytes */{size}"}
                self._send(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, b"", content_type, bad)
                return
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
            self._send(HTTPStatus.PARTIAL_CONTENT, data[start : end + 1], content_type, extra)
            return
        self._send(HTTPStatus.OK, data, content_type, extra)

    # --- směrování ---

    def _host_ok(self) -> bool:
        # Ochrana proti DNS rebindingu: přijímat jen požadavky na naši adresu.
        host = (self.headers.get("Host") or "").lower()
        return host in self.allowed_hosts

    def _guard(self, fn: Callable[[], None]) -> None:
        if not self._host_ok():
            self._json({"error": "nepovolený Host"}, HTTPStatus.FORBIDDEN)
            return
        try:
            fn()
        except ApiError as exc:
            self._json({"error": str(exc)}, exc.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # pragma: no cover - neočekávaná chyba
            self._json({"error": f"{type(exc).__name__}: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        self._guard(self._get)

    def do_POST(self) -> None:  # noqa: N802
        self._guard(self._post)

    def _get(self) -> None:
        url = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        path = url.path
        studio = self.studio
        eng, mod = q.get("engine") or None, q.get("model") or None
        if path in ("/", "/index.html"):
            self._send(HTTPStatus.OK, _index_html(), "text/html; charset=utf-8")
        elif path == "/api/book":
            self._json(studio.overview(eng, mod))
        elif path == "/api/status":
            self._json(studio.status())
        elif path == "/api/voices":
            self._json({"voices": studio.voices(q.get("engine") or "gemini")})
        elif m := re.fullmatch(r"/api/clip/([0-9a-f]{64})\.wav", path):
            self._audio(studio.clip(m.group(1)))
        elif path == "/api/paragraph.wav":
            self._audio(studio.paragraph_wav(q.get("id", ""), eng, mod))
        elif path == "/api/chapter.wav":
            self._audio(studio.chapter_wav(q.get("id", ""), eng, mod))
        elif m := re.fullmatch(r"/api/say/([0-9a-f]{12})\.wav", path):
            self._audio(studio.say_wav(m.group(1)))
        elif path.startswith("/api/output/"):
            name = unquote(path[len("/api/output/") :])
            file = studio.output_file(name)
            ctype = "audio/mpeg" if file.suffix == ".mp3" else "audio/wav"
            self._audio(file.read_bytes(), ctype, filename=file.name)
        else:
            raise ApiError("nenalezeno", HTTPStatus.NOT_FOUND)

    def _body(self) -> dict[str, Any]:
        # Jen JSON: formulář z cizí stránky ho bez CORS preflightu poslat neumí.
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            raise ApiError("čekám application/json", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            raise ApiError("požadavek je příliš velký", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise ApiError("neplatný JSON") from None
        if not isinstance(data, dict):
            raise ApiError("čekám JSON objekt")
        return data

    def _post(self) -> None:
        path = urlparse(self.path).path
        body = self._body()
        studio = self.studio
        eng, mod = body.get("engine") or None, body.get("model") or None
        if path == "/api/synthesize":
            self._json(
                studio.submit_synthesize(
                    engine=eng,
                    model=mod,
                    paragraphs=_str_list(body.get("paragraphs")),
                    chapters=_str_list(body.get("chapters")),
                    force=bool(body.get("force")),
                )
            )
        elif path == "/api/assemble":
            self._json(studio.submit_assemble(engine=eng, model=mod, chapters=_str_list(body.get("chapters"))))
        elif path == "/api/say":
            self._json(
                studio.submit_say(
                    engine=eng,
                    model=mod,
                    voice=_opt_str(body.get("voice")),
                    text=str(body.get("text") or ""),
                    language=_opt_str(body.get("language")),
                    style=_opt_str(body.get("style")),
                )
            )
        elif path == "/api/cancel":
            self._json(studio.cancel())
        elif path == "/api/unload":
            self._json(studio.unload())
        else:
            raise ApiError("nenalezeno", HTTPStatus.NOT_FOUND)


def _str_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ApiError("čekám seznam řetězců")
    return value


def _opt_str(value: Any) -> str | None:
    return str(value).strip() or None if value is not None else None


def _quote(name: str) -> str:
    from urllib.parse import quote

    return quote(name, safe="")


def make_server(studio: Studio, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    real_port = server.server_address[1]
    names = {host, "127.0.0.1", "localhost", "[::1]"}
    hosts = frozenset(f"{n}:{real_port}" for n in names) | frozenset(names if real_port == 80 else ())
    server.RequestHandlerClass = type("BoundHandler", (Handler,), {"studio": studio, "allowed_hosts": hosts})
    server.daemon_threads = True
    return server
