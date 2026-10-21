"""Truthful UI state — the word under the ring must be what JARVIS
is actually doing (owner caught the timer-rotated fake words,
2026-10-21: 'jo dikhe, wahi ho raha ho')."""
import json
import threading
import urllib.request

import pytest

from jarvis.chat import (Activity, ChatApp, _activity_dispatch,
                         state_for_tool)
from jarvis.providers import FallbackChain, StubProvider
from jarvis.ui import PAGE, build_server


class CountingChain:
    name = "counting"

    def __init__(self):
        self.inner = FallbackChain([StubProvider()])

    def complete(self, messages, tools=None):
        return self.inner.complete(messages, tools)


def test_tool_state_mapping():
    assert state_for_tool("web_search") == "researching"
    assert state_for_tool("deep_research") == "researching"
    assert state_for_tool("knowledge_search") == "researching"
    assert state_for_tool("create_project") == "creating"
    assert state_for_tool("run_code") == "creating"
    assert state_for_tool("write_file") == "creating"
    assert state_for_tool("open_app") == "working"
    assert state_for_tool("time_now") == "working"


def test_dispatch_reports_truth_during_execution():
    activity = Activity()
    seen = {}

    def fake_dispatch(name, args=None, origin="owner"):
        seen["during"] = activity.state
        seen["tool"] = activity.tool
        return {"ok": True}

    traced = _activity_dispatch(fake_dispatch, activity)
    out = traced("create_project", {"path": "x"})
    assert out == {"ok": True}
    assert seen["during"] == "creating"
    assert seen["tool"] == "create_project"
    assert activity.state == "thinking"  # brain generating again


def test_app_activity_starts_on_standby(tmp_path):
    app = ChatApp(tmp_path / "data", chain=CountingChain())
    assert app.activity.state == "standby"
    app.execute_tool("time_now", {})
    assert app.activity.state == "thinking"  # a tool ran; brain's move


@pytest.fixture()
def ui(tmp_path):
    app = ChatApp(tmp_path / "data", chain=CountingChain())
    server = build_server(app, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_state_endpoint_reports_real_activity(ui):
    with urllib.request.urlopen(ui + "/api/state", timeout=5) as resp:
        body = json.loads(resp.read().decode())
    assert body["activity"] == "standby"


def test_page_has_no_fake_rotating_states():
    assert "busyWords" not in PAGE
    assert "Standby" in PAGE
    # Listening/Speaking exist as labels now because the voice loop
    # makes them REAL (polled from the app's activity) — what must
    # never return is a timer rotating words nobody earned.
    assert "listening: 'Listening…'" in PAGE
    assert "speaking: 'Speaking…'" in PAGE
