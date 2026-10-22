"""Corrections — Nova's learn-from-being-wrong pipeline, wired.

Before Wave 1.3: Brain.respond accepted correction=True and the
FactStore implemented supersedes=, but no caller ever set the flag
or passed supersedes — the machinery was dormant. These tests pin
the detector, the extractor, the lesson store, and the full causal
flip: wrong fact stored -> owner corrects -> golden pair captured,
lesson stored, wrong fact superseded, recall now serves the fix.
"""

import json

from jarvis.chat import ChatApp
from jarvis.corrections import extract_correction, looks_like_correction
from jarvis.memory import Memory


# -- stage 1: the regex pre-filter ---------------------------------------------

def test_detector_catches_english_corrections():
    assert looks_like_correction(
        "No, that's wrong — the capital is Canberra.")
    assert looks_like_correction("Actually it's Tuesday, not Monday.")
    assert looks_like_correction("You're wrong about the date.")
    assert looks_like_correction("I said the meeting is at 5.")


def test_detector_catches_hinglish_corrections():
    assert looks_like_correction("galat hai, meeting Monday ko hai")
    assert looks_like_correction("aisa nahi hota, dobara dekho")
    assert looks_like_correction("maine kaha tha kal aana hai")


def test_detector_ignores_ordinary_turns():
    assert not looks_like_correction("What's the weather today?")
    assert not looks_like_correction("Actually, I was thinking "
                                     "about pasta tonight.")
    assert not looks_like_correction("No, I don't want tea, thanks.")
    assert not looks_like_correction("open chrome")
    assert not looks_like_correction("")


# -- stage 2: the structured extraction -----------------------------------------

_CORRECTION_JSON = json.dumps({
    "is_correction": True,
    "wrong": "The capital of Australia is Sydney",
    "correct": "The capital of Australia is Canberra",
    "lesson": "The capital of Australia is Canberra, not Sydney"})


def _complete_returning(content):
    def complete(messages, tools):
        return {"content": content}
    return complete


def test_extract_confirmed_correction():
    found = extract_correction(_complete_returning(_CORRECTION_JSON),
                               "No, that's wrong...",
                               "The capital of Australia is Sydney.")
    assert found == {
        "wrong": "The capital of Australia is Sydney",
        "correct": "The capital of Australia is Canberra",
        "lesson": "The capital of Australia is Canberra, not Sydney"}


def test_extract_rejects_a_topic_change():
    found = extract_correction(
        _complete_returning('{"is_correction": false}'),
        "Actually, I was thinking about pasta tonight.",
        "Dinner is at 8.")
    assert found is None


def test_extract_survives_garbage_and_errors():
    assert extract_correction(_complete_returning("not json"),
                              "galat hai", "prev") is None
    assert extract_correction(_complete_returning(""), "galat hai",
                              "prev") is None

    def boom(messages, tools):
        raise RuntimeError("chain down")
    assert extract_correction(boom, "galat hai", "prev") is None


# -- the lesson store -----------------------------------------------------------

def test_learn_lesson_supersedes_the_wrong_fact(tmp_path):
    mem = Memory(tmp_path / "data")
    wrong = mem.learn("The capital of Australia is Sydney",
                      source="owner")
    lesson = mem.learn_lesson(
        "The capital of Australia is Sydney",
        "The capital of Australia is Canberra",
        "The capital of Australia is Canberra, not Sydney")
    current_ids = {f["id"] for f in mem.facts.current_facts()}
    assert wrong["id"] not in current_ids          # no longer current
    assert lesson["id"] in current_ids
    assert mem.facts.superseded_by(wrong["id"])["id"] == lesson["id"]
    top = mem.recall("capital of Australia", k=1)
    assert top and "Canberra" in top[0]["text"]    # the flip


def test_learn_lesson_without_a_match_just_stores(tmp_path):
    mem = Memory(tmp_path / "data")
    mem.learn("The owner lives in Pune", source="owner")
    lesson = mem.learn_lesson("Tea is served at 9", "Tea is at 10",
                              "Tea is served at 10, not 9")
    assert lesson["supersedes"] is None
    assert len(mem.facts.current_facts()) == 2


# -- the full app-level flip ------------------------------------------------------

class _ScriptedChain:
    """Chain fake: pops scripted replies, then a calm default."""

    def __init__(self, script=()):
        self.script = list(script)

    def complete(self, messages, tools=None):
        if self.script:
            return self.script.pop(0)
        return {"content": "Understood, sir."}


def test_app_correction_turn_flips_memory(tmp_path):
    app = ChatApp(tmp_path)
    wrong = app.memory.learn("The capital of Australia is Sydney",
                             source="owner")
    app.history.append({"role": "user",
                        "content": "Australia ki capital kya hai?"})
    app.history.append({"role": "assistant",
                        "content": "The capital of Australia is "
                                   "Sydney, sir."})
    # The detector's confirmation call rides app.chain; the brain's
    # own chain is scripted for determinism.
    app.chain = _ScriptedChain([{"content": _CORRECTION_JSON}])
    app.brain.chain = _ScriptedChain()

    reply = app.turn("No, that's wrong — the capital of Australia "
                     "is Canberra, not Sydney.")
    assert reply  # the turn still answers normally

    pairs = tmp_path / "golden_pairs.jsonl"
    assert pairs.exists()
    pair = json.loads(pairs.read_text(encoding="utf-8").splitlines()[0])
    assert "Canberra" in pair["chosen"]
    assert "Sydney" in pair["rejected"]

    replacement = app.memory.facts.superseded_by(wrong["id"])
    assert replacement is not None
    assert "Canberra" in replacement["claim"]
    top = app.memory.recall("capital of Australia", k=1)
    assert top and "Canberra" in top[0]["text"]


def test_app_ordinary_turn_stores_no_lesson(tmp_path):
    app = ChatApp(tmp_path)
    app.chain = _ScriptedChain()
    app.brain.chain = _ScriptedChain()
    app.turn("Hello, how are you?")
    assert not (tmp_path / "golden_pairs.jsonl").exists()
    kinds = [e["kind"] for e in app.journal.replay()]
    assert "correction.detected" not in kinds


# -- fast-path atomic launches (Dodaios: no LLM round trip) ---------------------

def test_launch_target_parsing():
    parse = ChatApp._launch_target
    assert parse("open chrome") == "chrome"
    assert parse("Open Google Chrome please") == "google chrome"
    assert parse("chrome kholo") == "chrome"
    assert parse("notepad khol do") == "notepad"
    assert parse("launch visual studio code") == "visual studio code"
    # compound / question / non-app requests stay with the brain
    assert parse("open chrome and play music") is None
    assert parse("can you open chrome?") is None
    assert parse("start the timer") is None
    assert parse("open my presentation file from yesterday") is None
    assert parse("what is chrome") is None


def test_fast_path_launch_executes_open_app(tmp_path, monkeypatch):
    app = ChatApp(tmp_path)
    calls = []
    monkeypatch.setattr(
        app, "execute_tool",
        lambda name, args: calls.append((name, args)) or
        {"ok": True, "launched": args["name"]})
    reply = app.turn("chrome kholo")
    assert calls == [("open_app", {"name": "chrome"})]
    assert reply == "Opening chrome, sir."
    assert app.last_provider == "fast-path"


# -- the evidence rule (Dodaios): quotes ground the facts -------------------------

from jarvis.memory import evidence_problems


def test_evidence_problems_catches_invented_numbers_and_links():
    claim = "Lesson: the fee is 500 rupees. (Correct: see https://x.example.)"
    assert evidence_problems(claim, "the fee is five hundred") != []
    ok = "nahi, fee 500 rupees hai aur link https://x.example hai"
    assert evidence_problems(claim, ok) == []
    assert evidence_problems(claim, None) == []
    assert evidence_problems(claim, "short") != []


def test_lesson_with_bad_evidence_cannot_supersede(tmp_path):
    # The rule binds claims carrying numbers/links: a lesson whose
    # figures appear nowhere in the owner's own words is stored
    # but may NOT supersede the fact it claims to correct.
    mem = Memory(tmp_path / "data")
    wrong = mem.learn("The workshop fee is 400 rupees", source="owner")
    lesson = mem.learn_lesson(
        "The workshop fee is 400 rupees",
        "The workshop fee is 500 rupees",
        "The workshop fee is 500 rupees, not 400",
        evidence="haan workshop ki hi baat ho rahi thi yahan")
    assert lesson["supersedes"] is None
    assert wrong["id"] in {f["id"] for f in mem.facts.current_facts()}
    assert lesson["evidence"] is not None
    # The same lesson WITH the owner's words carrying the figures
    # supersedes normally.
    lesson2 = mem.learn_lesson(
        "The workshop fee is 400 rupees",
        "The workshop fee is 500 rupees",
        "The workshop fee is 500 rupees, not 400",
        evidence="galat hai, fee 400 nahi 500 rupees hai")
    assert lesson2["supersedes"] == wrong["id"]
