"""Instincts (ECC Continuous Learning v2, JARVIS-bound):
atomic trigger->action patterns with a confidence that
rises only on INDEPENDENT evidence, promotion session ->
standing on repeated proof, decay for the long-unused,
pruning for the faded — and a hard-capped return path
into the brain's context."""

import time

from jarvis.instincts import (DECAY_AFTER_S, InstinctBook)

DAY = 24 * 3600
T0 = 1_700_000_000.0


def _book(tmp_path):
    return InstinctBook(tmp_path)


def test_create_starts_tentative(tmp_path):
    book = _book(tmp_path)
    row = book.observe("email bhejna ho", "pehle draft dikhana",
                       "email", "owner ne kaha drafts pehle",
                       now=T0)
    assert row["confidence"] == 0.3
    assert row["scope"] == "session"
    assert row["evidence"] == ["owner ne kaha drafts pehle"]
    assert book.get(row["id"])["trigger"] == "email bhejna ho"


def test_reinforce_needs_independent_evidence(tmp_path):
    book = _book(tmp_path)
    book.observe("t", "a", "d", "note-1", now=T0)
    row = book.observe("t", "a", "d", "note-1", now=T0 + 10)
    assert row["confidence"] == 0.3        # same note: no bump
    assert row["last_used"] == T0 + 10     # but it WAS used
    row = book.observe("t", "a", "d", "note-2", now=T0 + 20)
    assert row["confidence"] == 0.4
    assert row["evidence"] == ["note-1", "note-2"]


def test_confidence_caps_at_point_nine(tmp_path):
    book = _book(tmp_path)
    row = None
    for i in range(12):
        row = book.observe("t", "a", "d", f"note-{i}",
                           now=T0 + i)
    assert row["confidence"] == 0.9


def test_negative_evidence_lowers_confidence(tmp_path):
    book = _book(tmp_path)
    book.observe("t", "a", "d", "good-1", now=T0)
    book.observe("t", "a", "d", "good-2", now=T0 + 1)
    row = book.observe("t", "a", "d", "ye pattern fail hua",
                       negative=True, now=T0 + 2)
    assert row["confidence"] == 0.25       # 0.4 - 0.15
    assert "ye pattern fail hua" in row["evidence"]


def test_promotion_needs_confidence_and_evidence(tmp_path):
    book = _book(tmp_path)
    # 0.3 + 5 x 0.1 = 0.8 with 6 evidence notes -> standing.
    for i in range(6):
        book.observe("t", "a", "d", f"note-{i}", now=T0 + i)
    out = book.maintain(now=T0 + 100)
    assert out["promoted"] == 1
    assert book.all()[0]["scope"] == "standing"
    # High confidence alone is not enough: 2 notes only.
    book2 = InstinctBook(tmp_path / "b2")
    book2.observe("x", "y", "d", "only-1", now=T0)
    row = book2.observe("x", "y", "d", "only-2", now=T0 + 1)
    row = book2.observe("x", "y", "d", "only-2", negative=True,
                        now=T0 + 2)
    out = book2.maintain(now=T0 + 100)
    assert out["promoted"] == 0


def test_decay_after_ninety_days_then_prune(tmp_path):
    book = _book(tmp_path)
    book.observe("t", "a", "d", "note-1", now=T0)
    out = book.maintain(now=T0 + 30 * DAY)
    assert out == {"decayed": 0, "promoted": 0, "pruned": 0}
    out = book.maintain(now=T0 + 91 * DAY)
    assert out["decayed"] == 1
    assert book.all()[0]["confidence"] == 0.2
    # No double-decay for the same disuse window.
    out = book.maintain(now=T0 + 92 * DAY)
    assert out["decayed"] == 0
    out = book.maintain(now=T0 + 200 * DAY)
    assert out["decayed"] == 1 and out["pruned"] == 1
    assert book.all() == []


def test_matching_and_context_lines_are_capped(tmp_path):
    book = _book(tmp_path)
    book.observe("email bhejna ho", "draft pehle dikhao",
                 "email", "n1", now=T0)
    book.observe("email delete karna ho", "pehle poochho",
                 "email", "n2", now=T0)
    book.observe("gaana chalana ho", "spotify kholo",
                 "music", "n3", now=T0)
    hits = book.matching("mujhe ek email bhejna hai")
    assert len(hits) == 2                       # cap at 2
    assert all(h["domain"] == "email" for h in hits)
    lines = book.context_lines("email bhejna hai")
    assert len(lines) == 2
    assert "Seekha pattern [email]" in lines[0]
    assert book.context_lines("quantum physics padhao") == []


def test_torn_lines_never_hide_the_store(tmp_path):
    book = _book(tmp_path)
    book.observe("t", "a", "d", "n1", now=T0)
    with book.path.open("a", encoding="utf-8") as fh:
        fh.write('{"id": "torn", "confidence": \n')
        fh.write("not json at all\n")
    book.observe("t2", "a2", "d", "n2", now=T0 + 1)
    assert len(book.all()) == 2


class _ScriptedChain:
    """Chain fake: pops scripted replies, then a calm default."""

    def __init__(self, script=()):
        self.script = list(script)

    def complete(self, messages, tools=None):
        if self.script:
            return self.script.pop(0)
        return {"content": "Understood, sir."}


_CORRECTION_JSON = None  # built in the test (needs json import)


def test_app_turn_feeds_instincts_from_corrections(tmp_path):
    """The wired feed: an owner correction both stores a
    memory lesson (the old path) and observes an instinct."""
    import json

    from jarvis.chat import ChatApp

    correction_json = json.dumps({
        "is_correction": True,
        "wrong": "The capital of Australia is Sydney",
        "correct": "The capital of Australia is Canberra",
        "lesson": "The capital of Australia is Canberra, "
                  "not Sydney"})
    app = ChatApp(tmp_path)
    app.history.append({"role": "user",
                        "content": "Australia ki capital kya hai?"})
    app.history.append({"role": "assistant",
                        "content": "The capital of Australia is "
                                   "Sydney, sir."})
    app.chain = _ScriptedChain([{"content": correction_json}])
    app.brain.chain = _ScriptedChain()
    reply = app.turn("No, that's wrong — the capital of "
                     "Australia is Canberra, not Sydney.")
    assert reply
    rows = app.instincts.all()
    assert len(rows) == 1
    assert rows[0]["domain"] == "correction"
    assert "Canberra" in rows[0]["action"]


def test_instincts_surface_in_brain_context(tmp_path):
    from jarvis.chat import ChatApp

    app = ChatApp(tmp_path)
    for i in range(3):
        app.instincts.observe(
            "email bhejna ho", "pehle draft dikhana", "email",
            f"saboot {i}")
    lines = app.memory.excerpts("ek email bhejna hai")
    assert any("Seekha pattern [email]" in line
               for line in lines)


def test_instincts_command_lists_and_prunes(tmp_path):
    from jarvis.chat import ChatApp

    app = ChatApp(tmp_path)
    assert "koi instinct nahi" in app.handle_command(
        "/instincts").lower()
    app.instincts.observe("t", "a", "d", "n1", now=T0)
    out = app.handle_command("/instincts")
    assert "0.30" in out and "[d]" in out
    out = app.handle_command("/instincts prune")
    assert "prune" in out.lower()
