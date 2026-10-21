"""Learned documents: the owner drops .txt/.md files into data/inbox
(or pastes text and says 'learn this') and JARVIS ingests them
AUTOMATICALLY — no command — then actually uses them when answering.
Hermetic: tmp data dirs and the counting stub chain."""
from pathlib import Path

import pytest

from jarvis import builtin_skills as bs
from jarvis.chat import ChatApp
from jarvis.providers import FallbackChain, StubProvider


class CountingChain:
    name = "counting"

    def __init__(self):
        self.inner = FallbackChain([StubProvider()])
        self.calls = 0

    def complete(self, messages, tools=None):
        self.calls += 1
        return self.inner.complete(messages, tools)


STUDY_TEXT = (
    "Project Falcon is the owner's video editing venture. "
    "The falcon mascot is a peregrine named Zara. "
    "Invoices are raised on the seventh of every month."
)


def _drop(data: Path, name: str, text: str) -> Path:
    inbox = data / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / name
    path.write_text(text, encoding="utf-8")
    return path


def test_file_present_at_startup_is_learned(tmp_path):
    data = tmp_path / "data"
    _drop(data, "falcon.txt", STUDY_TEXT)
    app = ChatApp(data, chain=CountingChain())
    hits = app.memory.recall("falcon mascot peregrine")
    assert any("Zara" in hit["text"] for hit in hits)
    assert any(hit["source"] == "learned:falcon.txt" for hit in hits)


def test_file_dropped_mid_session_is_picked_up_by_next_turn(tmp_path):
    data = tmp_path / "data"
    app = ChatApp(data, chain=CountingChain())
    assert app.memory.recall("falcon mascot") == []
    _drop(data, "falcon.txt", STUDY_TEXT)
    app.turn("what time is it right now?")   # any turn triggers the scan
    hits = app.memory.recall("falcon mascot peregrine")
    assert any("Zara" in hit["text"] for hit in hits)


def test_learned_content_survives_restart(tmp_path):
    data = tmp_path / "data"
    _drop(data, "falcon.txt", STUDY_TEXT)
    ChatApp(data, chain=CountingChain())
    assert (data / "learned.jsonl").exists()
    app2 = ChatApp(data, chain=CountingChain())
    hits = app2.memory.recall("invoices seventh month")
    assert any("seventh" in hit["text"] for hit in hits)


def test_learn_text_paste_flow(tmp_path):
    data = tmp_path / "data"
    app = ChatApp(data, chain=CountingChain())
    out = app.memory.learn_text(STUDY_TEXT, title="Falcon Notes")
    assert out["stored"] is True and out["chunks"] >= 1
    assert out["learned_from"].startswith("Falcon-Notes-")
    hits = app.memory.recall("falcon mascot")
    assert any("Zara" in hit["text"] for hit in hits)


def test_learn_text_tool_reaches_the_store(tmp_path):
    data = tmp_path / "data"
    app = ChatApp(data, chain=CountingChain())
    ops = bs._memory_ops(app.memory)
    out = ops["learn_text"](text=STUDY_TEXT, title="Falcon")
    assert out["stored"] is True
    hits = app.memory.recall("peregrine zara")
    assert any("Zara" in hit["text"] for hit in hits)


def test_changed_file_is_reingested(tmp_path):
    data = tmp_path / "data"
    path = _drop(data, "notes.txt", "The codename is Alpha.")
    app = ChatApp(data, chain=CountingChain())
    assert any("Alpha" in h["text"]
               for h in app.memory.recall("codename"))
    path.write_text("The codename is Beta now, fully replaced text.",
                    encoding="utf-8")
    app.turn("what time is it right now?")
    hits = app.memory.recall("codename")
    assert any("Beta" in h["text"] for h in hits)
