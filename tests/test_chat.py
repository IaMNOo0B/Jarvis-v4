"""Chat core (S5): fast-path routing with a counting chain, the
remember/recall round-trip through real turns, one brain-path turn on
the stub chain, the reminder skill, and the CLI.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.chat import ChatApp
from jarvis.providers import FallbackChain, StubProvider

REPO_ROOT = Path(__file__).resolve().parent.parent


class CountingChain:
    """Wraps the real stub chain and counts model-seat calls — the
    fast path must NEVER reach it."""

    name = "counting"

    def __init__(self):
        self.inner = FallbackChain([StubProvider()])
        self.calls = 0

    def complete(self, messages, tools=None):
        self.calls += 1
        return self.inner.complete(messages, tools)


@pytest.fixture()
def app(tmp_path):
    return ChatApp(tmp_path / "data", chain=CountingChain())


def test_time_question_needs_no_chain_call(app):
    reply = app.turn("what time is it right now?")
    assert app.chain.inner.calls == 0
    assert re.search(r"\d{1,2}:\d{2} (AM|PM)", reply)
    assert app.last_provider == "fast-path"


def test_remember_and_recall_round_trip_without_chain(app):
    app.turn("mera naam Owais hai, yaad rakhna")
    reply = app.turn("mera naam kya hai?")
    assert app.chain.inner.calls == 0
    assert "Owais" in reply


def test_brain_path_turn_uses_chain_once(app):
    reply = app.turn("how are u?")
    assert app.chain.inner.calls >= 1
    assert "offline stub provider" in reply  # the honest stub answer


def test_memory_command_round_trip(app):
    assert "Yaad rakh liya" in app.handle_command("/memory naam = Owais")
    assert "naam = Owais" in app.handle_command("/memory")


def test_reminder_skill_is_live_through_dispatch(app):
    result = app.execute_tool(
        "reminder_set", {"in_minutes": 10, "text": "paani peena hai"})
    assert "error" not in result
    listing = app.execute_tool("reminder_list", {})
    texts = [r["text"] for r in listing["reminders"]]
    assert "paani peena hai" in texts


def test_pc_control_is_on_for_the_owner_profile(app):
    # Owner doctrine (v2, kept in v4): JARVIS acts on the owner's own
    # PC — pc.control IS in the default allowances, so open_app is
    # advertised (LAW 1) and a dispatch REACHES the op. On this
    # non-Windows test machine the op itself answers honestly that no
    # PC driver is present here; what must NOT happen is a gate denial
    # (that was the v2 live failure: advertised but never permitted).
    advertised = {t["name"]
                  for t in app.registry.advertised_tools(app.allowances)}
    assert "open_app" in advertised
    assert "time_now" in advertised and "web_search" in advertised
    result = app.execute_tool("open_app", {"name": "chrome"})
    assert "denied" not in str(result.get("error", ""))


def test_turns_are_journaled(app):
    app.turn("what time is it right now?")
    kinds = [r["kind"] for r in app.journal.replay()]
    assert kinds.count("user.message") == 1
    assert kinds.count("assistant.reply") == 1
    calls = [r["payload"] for r in app.journal.replay()
             if r["kind"] == "tool.call"]
    assert calls and calls[0]["tool"] == "time_now"
    assert calls[0]["gate_verdict"] == "allow"


def test_cli_once(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "jarvis.chat",
         "--data-dir", str(tmp_path / "data"),
         "--once", "what time is it right now?"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0
    assert "sir" in proc.stdout
