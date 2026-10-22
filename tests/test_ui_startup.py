"""Startup, fresh build (2026-10-22): THE PAGE ALWAYS OPENS.

The old UI died on the owner's PC with ERR_CONNECTION_REFUSED
and the reason hidden in a console window. Now the server binds
FIRST, ChatApp builds in a background thread, /api/boot reports
starting/ready/error, a boot crash shows its full text INSIDE
the page (and in startup-error.txt), ports fall back +1...+9,
and background services still start guarded, after the app —
they can never veto the UI."""

import json
import threading
import time
import urllib.request

import jarvis.chat as chat_mod
import jarvis.ui as ui
from jarvis.chat import ChatApp as RealChatApp
from jarvis.providers import FallbackChain, StubProvider


def _serve(handle):
    """start_ui binds and boots but does not serve — main() owns
    serve_forever in production; tests run it on a thread."""
    thread = threading.Thread(target=handle.serve_forever,
                              daemon=True)
    thread.start()
    return handle


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, resp.read()


def _wait_boot(handle, want, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if handle.boot.state == want:
            return True
        time.sleep(0.05)
    return handle.boot.state == want


def test_boot_error_is_shown_in_the_page_not_a_dead_tab(tmp_path,
                                                       monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("his real data broke construction")

    monkeypatch.setattr(ui, "ChatApp", boom)
    monkeypatch.chdir(tmp_path)
    handle = _serve(ui.start_ui(tmp_path, port=0,
                                open_browser=False,
                                start_services=False))
    try:
        assert _wait_boot(handle, "error")
        # The page itself still opens — that is the whole point.
        status, body = _get(handle.url)
        assert status == 200 and b"JARVIS" in body
        # And the reason is readable in the page's own data.
        status, body = _get(handle.url + "api/boot")
        boot = json.loads(body.decode())
        assert boot["state"] == "error"
        assert "his real data broke construction" in boot["error"]
        assert "Traceback" in boot["error"]
        # Belt and braces: the same evidence in the file.
        saved = (tmp_path / "startup-error.txt").read_text(
            encoding="utf-8")
        assert "his real data broke construction" in saved
        # Chat refuses honestly while there is no app.
        req = urllib.request.Request(
            handle.url + "api/chat",
            data=json.dumps({"text": "hi"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("expected HTTPError")
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
    finally:
        handle.shutdown()


def test_server_opens_before_the_app_is_ready(tmp_path,
                                              monkeypatch):
    def slow_chatapp(data_dir):
        time.sleep(1.5)
        return RealChatApp(data_dir,
                           chain=FallbackChain([StubProvider()]))

    monkeypatch.setattr(ui, "ChatApp", slow_chatapp)
    handle = _serve(ui.start_ui(tmp_path, port=0,
                                open_browser=False,
                                start_services=False))
    try:
        # The app is still building, yet everything answers.
        assert handle.boot.state == "starting"
        status, body = _get(handle.url)
        assert status == 200 and b"jarvis-core.jpg" in body
        status, body = _get(handle.url + "api/boot")
        assert json.loads(body.decode())["state"] == "starting"
        # /api/state keeps its shape (the overlay polls it).
        status, body = _get(handle.url + "api/state")
        state = json.loads(body.decode())
        assert state["activity"] == "starting"
        assert state["confirmations"] == [] \
            and state["notifications"] == 0
        # Once ready, a real chat round-trip works.
        assert _wait_boot(handle, "ready")
        req = urllib.request.Request(
            handle.url + "api/chat",
            data=json.dumps(
                {"text": "what time is it right now?"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        assert ":" in data["reply"]
    finally:
        handle.shutdown()


def test_port_fallback_skips_a_taken_port(tmp_path, monkeypatch):
    class FakeApp:
        def __init__(self, data_dir):
            self.data_dir = data_dir

    monkeypatch.setattr(ui, "ChatApp", FakeApp)
    blocker = ui.ThreadingHTTPServer(("127.0.0.1", 0),
                                     ui.BaseHTTPRequestHandler)
    taken = blocker.server_address[1]
    thread = threading.Thread(target=blocker.serve_forever,
                              daemon=True)
    thread.start()
    try:
        handle = _serve(ui.start_ui(tmp_path, port=taken,
                                open_browser=False,
                                start_services=False))
        try:
            assert taken < handle.port <= taken + 9
            status, body = _get(handle.url)
            assert status == 200 and b"JARVIS" in body
        finally:
            handle.shutdown()
    finally:
        blocker.shutdown()


def test_main_returns_1_and_records_when_no_port_binds(tmp_path,
                                                       monkeypatch):
    def no_bind(*a, **k):
        raise OSError("all ports busy")

    monkeypatch.setattr(ui, "build_server", no_bind)
    monkeypatch.chdir(tmp_path)
    code = ui.main(["--data-dir", str(tmp_path), "--no-open",
                    "--port", "0"])
    assert code == 1
    saved = (tmp_path / "startup-error.txt").read_text(
        encoding="utf-8")
    assert "all ports busy" in saved


def test_background_services_cannot_veto_the_ui(tmp_path,
                                                monkeypatch):
    class FakeApp:
        data_dir = tmp_path

    def raiser(app):
        raise RuntimeError("telegram blocked startup")

    monkeypatch.setattr(ui, "_maybe_auto_watch", raiser)
    monkeypatch.setattr(chat_mod, "start_scheduler", raiser)
    monkeypatch.setattr(chat_mod, "start_telegram", raiser)
    monkeypatch.chdir(tmp_path)
    # Must not raise even when every starter raises.
    ui._start_background_services(FakeApp())
    saved = (tmp_path / "startup-error.txt").read_text(
        encoding="utf-8")
    # One timestamped record per failed starter (three).
    assert saved.count("--- 20") == 3
    assert "telegram blocked startup" in saved


def test_background_services_run_the_happy_path(tmp_path,
                                                monkeypatch):
    seen = []

    class FakeApp:
        data_dir = tmp_path

    monkeypatch.setattr(ui, "_maybe_auto_watch", seen.append)
    monkeypatch.setattr(chat_mod, "start_scheduler", seen.append)
    monkeypatch.setattr(chat_mod, "start_telegram", seen.append)
    ui._start_background_services(FakeApp())
    assert len(seen) == 3
