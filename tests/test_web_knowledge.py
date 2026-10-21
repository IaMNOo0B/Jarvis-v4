"""web_fetch + knowledge_search — the read half of internet access
and the shipped-library search. Hermetic: a local HTTP server and a
planted knowledge folder, no live network."""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from jarvis import builtin_skills as bs


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (b"<html><head><style>.x{color:red}</style>"
                b"<script>var secret = 1;</script></head><body>"
                b"<h1>Wake Word Notes</h1>"
                b"<p>The butler uses a tiny model &amp; listens "
                b"locally.</p></body></html>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture()
def local_page():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/page"
    server.shutdown()


def test_web_fetch_returns_readable_text(local_page):
    out = bs.op_web_fetch(local_page)
    assert "error" not in out
    assert "Wake Word Notes" in out["text"]
    assert "tiny model & listens locally" in out["text"]
    assert "secret" not in out["text"]      # script body stripped
    assert "<p>" not in out["text"]          # tags stripped
    assert out["truncated"] is False


def test_web_fetch_rejects_non_http():
    out = bs.op_web_fetch("ftp://example.com/file")
    assert "error" in out


def test_web_fetch_dead_address_is_an_honest_error():
    out = bs.op_web_fetch("http://127.0.0.1:9/nothing")
    assert "error" in out and "web_fetch failed" in out["error"]


@pytest.fixture()
def planted_library(tmp_path, monkeypatch):
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "voice.md").write_text(
        "# Voice pipeline\nThe wake word engine listens locally "
        "with a tiny model and a ring buffer.\n")
    (root / "money.md").write_text(
        "# Budget\nThe spend trip watches the daily burn rate.\n")
    monkeypatch.setattr(bs, "_knowledge_dir", lambda: root)
    bs._KNOWLEDGE_CACHE.update({"stamp": None, "index": None,
                                "docs": []})
    return root


def test_knowledge_search_finds_the_right_document(planted_library):
    out = bs.op_knowledge_search("wake word ring buffer")
    assert out["results"]
    top = out["results"][0]
    assert top["source"] == "voice.md"
    assert "ring buffer" in top["excerpt"]


def test_knowledge_search_no_match_is_a_note_not_an_error(
        planted_library):
    out = bs.op_knowledge_search("underwater basket weaving")
    assert out["results"] == []


def test_knowledge_search_empty_library_is_honest(tmp_path, monkeypatch):
    root = tmp_path / "knowledge"
    root.mkdir()
    monkeypatch.setattr(bs, "_knowledge_dir", lambda: root)
    bs._KNOWLEDGE_CACHE.update({"stamp": None, "index": None,
                                "docs": []})
    out = bs.op_knowledge_search("anything")
    assert "error" in out and "empty" in out["error"]
