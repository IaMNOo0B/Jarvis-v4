"""Notifications: one channel for everything that must reach the
owner — brain notify tool, reminders coming due (exactly once),
upgrade events — surfaced in the UI badge/panel and the CLI.
Hermetic: tmp dirs, ephemeral ports, stub chain."""
import json
import threading
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from jarvis.chat import (ChatApp, _mark_notifications_read, _notify,
                         _read_notifications, _unread_count)
from jarvis.providers import FallbackChain, StubProvider
from jarvis.ui import build_server


class CountingChain:
    name = "counting"

    def __init__(self):
        self.inner = FallbackChain([StubProvider()])
        self.calls = 0

    def complete(self, messages, tools=None):
        self.calls += 1
        return self.inner.complete(messages, tools)


def test_notify_store_round_trip(tmp_path):
    data = tmp_path / "data"
    _notify(data, "Pehli", "body one", kind="test")
    _notify(data, "Doosri", "body two", kind="test")
    rows = _read_notifications(data)
    assert [r["title"] for r in rows] == ["Pehli", "Doosri"]
    assert _unread_count(data) == 2
    _mark_notifications_read(data)
    assert _unread_count(data) == 0


def test_notify_tool_reaches_the_store(tmp_path):
    data = tmp_path / "data"
    app = ChatApp(data, chain=CountingChain())
    out = app.parts.dispatch("notify", {"title": "Dhyaan do",
                                        "body": "kuch mila hai"})
    assert "error" not in out
    rows = _read_notifications(data)
    assert rows and rows[-1]["title"] == "Dhyaan do"
    assert rows[-1]["kind"] == "brain"


def test_due_reminder_notifies_exactly_once(tmp_path):
    data = tmp_path / "data"
    data.mkdir(parents=True)
    past = (datetime.now(timezone.utc) - timedelta(minutes=5))
    (data / "reminders.json").write_text(json.dumps([
        {"id": "r1", "text": "client ko call karna hai",
         "due": past.isoformat(), "done": False}]))
    app = ChatApp(data, chain=CountingChain())
    app.turn("what time is it right now?")
    rows = [r for r in _read_notifications(data)
            if r["kind"] == "reminder"]
    assert len(rows) == 1 and "client ko call" in rows[0]["body"]
    app.turn("what time is it right now?")
    rows = [r for r in _read_notifications(data)
            if r["kind"] == "reminder"]
    assert len(rows) == 1                      # no repeat spam


def test_future_reminder_stays_quiet(tmp_path):
    data = tmp_path / "data"
    data.mkdir(parents=True)
    future = (datetime.now(timezone.utc) + timedelta(hours=2))
    (data / "reminders.json").write_text(json.dumps([
        {"id": "r2", "text": "baad wala kaam",
         "due": future.isoformat(), "done": False}]))
    app = ChatApp(data, chain=CountingChain())
    app.turn("what time is it right now?")
    assert _read_notifications(data) == []


@pytest.fixture()
def ui(tmp_path):
    app = ChatApp(tmp_path / "data", chain=CountingChain())
    _notify(app.data_dir, "UI wala", "panel me dikhna chahiye")
    server = build_server(app, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", app
    server.shutdown()


def test_ui_notification_endpoints(ui):
    base, app = ui
    with urllib.request.urlopen(base + "/api/state", timeout=5) as r:
        state = json.loads(r.read().decode())
    assert state["notifications"] == 1
    with urllib.request.urlopen(base + "/api/notifications",
                                timeout=5) as r:
        payload = json.loads(r.read().decode())
    assert payload["unread"] == 1
    assert payload["items"][0]["title"] == "UI wala"
    req = urllib.request.Request(base + "/api/notifications/read",
                                 data=b"{}", method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        assert json.loads(r.read().decode())["ok"] is True
    assert _unread_count(app.data_dir) == 0
