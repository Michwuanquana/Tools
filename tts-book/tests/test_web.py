"""Webové rozhraní: API nad skutečným HTTP serverem, engine mock."""

from __future__ import annotations

import http.client
import io
import json
import shutil
import threading
import time
import wave

import pytest

from tts_book.web import Studio, make_server

from .conftest import EXAMPLE


@pytest.fixture
def book_path(tmp_path):
    path = tmp_path / "majak.book.yaml"
    shutil.copy(EXAMPLE, path)
    text = path.read_text(encoding="utf-8").replace("format: mp3", "format: wav")
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def server(book_path):
    studio = Studio(book_path, engine="mock")
    srv = make_server(studio, "127.0.0.1", 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield studio, srv.server_address[1]
    srv.shutdown()
    srv.server_close()
    studio.close()


def _req(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {"Host": f"127.0.0.1:{port}", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=data, headers=hdrs)
    resp = conn.getresponse()
    payload = resp.read()
    conn.close()
    return resp, payload


def _json(port, method, path, body=None):
    resp, payload = _req(port, method, path, body)
    return resp.status, json.loads(payload)


def _wait(port, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        _, st = _json(port, "GET", "/api/status")
        if not st["busy"]:
            return st
        time.sleep(0.05)
    raise AssertionError("úloha nedoběhla")


def test_index_and_book(server):
    _, port = server
    resp, html = _req(port, "GET", "/")
    assert resp.status == 200 and b"tts-book studio" in html
    status, book = _json(port, "GET", "/api/book")
    assert status == 200
    assert book["engine"] == "mock" and book["title"] == "Maják"
    assert [c["id"] for c in book["chapters"]] == ["bourka", "lod"]
    assert book["jobs"] == 14 and book["cached"] == 0
    first = book["chapters"][0]["paragraphs"][1]["items"][0]
    assert first["kind"] == "speech" and first["speaker"] == "marta" and first["cached"] is False


def test_book_for_xtts_engine(server):
    _, port = server
    _, book = _json(port, "GET", "/api/book?engine=xtts")
    assert book["engine"] == "xtts"
    assert book["model"] == "tts_models/multilingual/multi-dataset/xtts_v2"
    items = [i for c in book["chapters"] for p in c["paragraphs"] for i in p["items"]]
    voices = {i["voice"] for i in items if i["kind"] == "speech"}
    assert "speaker:Damien Black" in voices
    assert any("neumí značky" in w for w in book["warnings"])


def test_synthesize_play_and_assemble(server, book_path):
    _, port = server
    status, task = _json(port, "POST", "/api/synthesize", {"engine": "mock", "paragraphs": ["lod.konec"]})
    assert status == 200 and task["kind"] == "synthesize"
    st = _wait(port)
    assert st["task"]["state"] == "done" and st["task"]["done"] == 1

    resp, wav = _req(port, "GET", "/api/paragraph.wav?id=lod.konec&engine=mock")
    assert resp.status == 200 and resp.getheader("Content-Type") == "audio/wav"
    with wave.open(io.BytesIO(wav)) as w:
        assert w.getframerate() == 24000 and w.getnframes() > 24000

    resp, part = _req(port, "GET", "/api/paragraph.wav?id=lod.konec&engine=mock", headers={"Range": "bytes=0-99"})
    assert resp.status == 206 and len(part) == 100
    assert resp.getheader("Content-Range") == f"bytes 0-99/{len(wav)}"

    _, book = _json(port, "GET", "/api/book")
    key = book["chapters"][1]["paragraphs"][-1]["items"][0]["key"]
    resp, clip = _req(port, "GET", f"/api/clip/{key}.wav")
    assert resp.status == 200 and clip[:4] == b"RIFF"

    # kapitola ještě není celá → přehrát ani složit nejde
    resp, _ = _req(port, "GET", "/api/chapter.wav?id=lod&engine=mock")
    assert resp.status == 404
    _json(port, "POST", "/api/assemble", {"engine": "mock"})
    st = _wait(port)
    assert st["task"]["result"]["files"] == []
    assert len(st["task"]["errors"]) == 2

    _json(port, "POST", "/api/synthesize", {"engine": "mock"})
    st = _wait(port)
    assert st["task"]["done"] == 13  # zbytek knihy
    _json(port, "POST", "/api/assemble", {"engine": "mock"})
    st = _wait(port)
    files = st["task"]["result"]["files"]
    assert files == ["01 - Kapitola první – Bouřka.wav", "02 - Kapitola druhá – Loď.wav"]
    _, book = _json(port, "GET", "/api/book")
    assert book["outputs"] == files and book["cached"] == book["jobs"]
    resp, data = _req(port, "GET", "/api/output/" + "02%20-%20Kapitola%20druh%C3%A1%20%E2%80%93%20Lo%C4%8F.wav")
    assert resp.status == 200 and data[:4] == b"RIFF"
    resp, _ = _req(port, "GET", "/api/chapter.wav?id=lod&engine=mock")
    assert resp.status == 200


def test_regenerate_replaces_cache(server):
    _, port = server
    _json(port, "POST", "/api/synthesize", {"engine": "mock", "paragraphs": ["lod.konec"]})
    _wait(port)
    _json(port, "POST", "/api/synthesize", {"engine": "mock", "paragraphs": ["lod.konec"]})
    assert _wait(port)["task"]["message"] == "vše už je v cache"
    _json(port, "POST", "/api/synthesize", {"engine": "mock", "paragraphs": ["lod.konec"], "force": True})
    assert _wait(port)["task"]["done"] == 1


def test_say(server):
    _, port = server
    status, task = _json(port, "POST", "/api/say", {"engine": "mock", "voice": "Kore", "text": "Ahoj [sigh]."})
    assert status == 200
    st = _wait(port)
    sid = st["task"]["result"]["say"]
    resp, wav = _req(port, "GET", f"/api/say/{sid}.wav")
    assert resp.status == 200 and wav[:4] == b"RIFF"
    status, err = _json(port, "POST", "/api/say", {"engine": "xtts", "text": "Ahoj", "language": "sk-SK", "voice": "X"})
    assert status == 400 and "nepodporuje jazyk" in err["error"]


def test_book_reload_after_edit(server, book_path):
    _, port = server
    text = book_path.read_text(encoding="utf-8").replace("title: Maják", "title: Maják 2")
    book_path.write_text(text, encoding="utf-8")
    _, book = _json(port, "GET", "/api/book")
    assert book["title"] == "Maják 2"
    book_path.write_text("version: 1\nmeta: {title: X}\n", encoding="utf-8")
    time.sleep(0.01)
    status, book = _json(port, "GET", "/api/book")
    assert status == 200 and book["problems"] and book["chapters"] == []
    status, err = _json(port, "POST", "/api/synthesize", {"engine": "mock"})
    assert status == 409


def test_engine_failure_is_reported(book_path):
    from tts_book.engines.base import EngineError

    def factory(name, book, *, model=None, base_dir=None):
        raise EngineError("chybí torch – nainstaluj ho")

    studio = Studio(book_path, engine="xtts", engine_factory=factory)
    try:
        studio.submit_synthesize(engine=None, model=None, paragraphs=["lod.konec"])
        st = studio.wait()
        assert st["task"]["state"] == "error" and "chybí torch" in st["task"]["message"]
    finally:
        studio.close()


def test_one_task_at_a_time_and_cancel(book_path):
    gate = threading.Event()

    class SlowEngine:
        name = "mock"
        sample_rate = 24000

        def synthesize(self, job):
            gate.wait(5)
            return b"\x00\x00" * 100

        def close(self):
            pass

    studio = Studio(book_path, engine="mock", engine_factory=lambda *a, **k: SlowEngine())
    try:
        studio.submit_synthesize(engine=None, model=None)
        with pytest.raises(Exception, match="už běží jiná úloha"):
            studio.submit_synthesize(engine=None, model=None)
        studio.cancel()
        gate.set()
        st = studio.wait()
        assert st["task"]["state"] == "cancelled"
        assert st["task"]["done"] == 1
    finally:
        studio.close()


def test_security_checks(server):
    _, port = server
    resp, _ = _req(port, "GET", "/api/book", headers={"Host": "evil.example:80"})
    assert resp.status == 403
    resp, _ = _req(port, "POST", "/api/cancel", body={}, headers={"Content-Type": "text/plain"})
    assert resp.status == 415
    resp, _ = _req(port, "GET", "/api/output/..%2Fmajak.book.yaml")
    assert resp.status == 404
    resp, _ = _req(port, "GET", "/api/clip/abc.wav")
    assert resp.status == 404
