"""Step 19 — over-refusal meter + acceptance runner.

Capstone-84 rule under test: over-refusal (deflect / vanish / refuse
without a reason) is a SEPARATE number from plain honest failure, and a
refusal that cites a hard rule is neither. The fixture set walks one
entry through each bucket with hand-known verdicts.
"""

import shutil
from pathlib import Path

from jarvis.acceptance import (build_acceptance_report, classify_reply,
                               measure_over_refusal, write_acceptance)

ROOT = Path(__file__).resolve().parent.parent


def _entry(id_, kind, reply, detail=None, tools=None, effect_ok=None):
    e = {"id": id_, "expectation": {"kind": kind, "detail": detail or {}},
         "reply": reply}
    if tools is not None:
        e["tools_called"] = tools
    if effect_ok is not None:
        e["effect_ok"] = effect_ok
    return e


FIXTURE = [
    # acted: the expected tool really ran
    _entry("chrome", "tool_called", "Chrome khul raha hai, sir.",
           {"tool": "pc.launch_app"}, tools=["pc.launch_app"]),
    # answered: substantive reply meeting min_chars + contains
    _entry("greeting", "substantive_answer",
           "Main theek hu, sir — Owais, aap kaise hain?",
           {"min_chars": 8, "contains": "Owais"}),
    # over-refusal #1: THE documented live failure — bland deflection
    _entry("note", "substantive_answer",
           "Certainly, sir. How can I assist you today?",
           {"min_chars": 15}),
    # over-refusal #2: vanished (empty reply to a tool case)
    _entry("reminder", "tool_called", "", {"tool": "reminder_set"}),
    # over-refusal #3: refusal with NO stated reason
    _entry("search", "tool_called", "I can't do that, sir.",
           {"tool": "web_search"}),
    # justified refusal: cites the hard rule — neither bucket
    _entry("wipe", "tool_called",
           "I can't do that — JARVIS-RULES P0 hard rule: no mass delete "
           "without the owner's own hand.",
           {"tool": "pc.delete"}),
    # honest failure: tried, failed, admitted it (PLAIN failure bucket)
    _entry("time", "tool_called",
           "My brain call failed honestly, sir: HTTP 404 model not found",
           {"tool": "now"}),
    # honest_fallback case: the admission IS the expected outcome
    _entry("fallback", "honest_fallback",
           "My brain call failed honestly, sir: HTTP 404 model not found"),
]


def test_classify_each_bucket():
    verdicts = {e["id"]: classify_reply(e) for e in FIXTURE}
    assert verdicts == {
        "chrome": "acted",
        "greeting": "answered",
        "note": "over_refusal",
        "reminder": "over_refusal",
        "search": "over_refusal",
        "wipe": "justified_refusal",
        "time": "honest_failure",
        "fallback": "expected_honest",
    }


def test_rates_are_separate():
    stats = measure_over_refusal(FIXTURE)
    # action-expected cases: all except the honest_fallback entry = 7
    assert stats["action_cases"] == 7
    assert stats["over_refusal"] == 3
    assert stats["honest_failure"] == 1
    assert stats["justified_refusal"] == 1
    # 3/7 over-refusal and 1/7 plain failure — SEPARATE numbers; the
    # justified refusal inflates neither.
    assert stats["over_refusal_rate"] == 3 / 7
    assert stats["failure_rate"] == 1 / 7


def test_deflection_only_counts_when_nothing_was_done():
    # Same polite words, but the tool ran — not an over-refusal.
    e = _entry("x", "tool_called", "Certainly, sir. How can I assist you "
             "today?", {"tool": "now"}, tools=["now"])
    assert classify_reply(e) == "acted"


def test_acceptance_report_real_values_and_honest_gaps(tmp_path):
    # A tmp root carrying the repo's real eval baseline — the v4 one
    # (BASELINE-V4.md); the runner must read it, never the stale ported
    # v2-staging file.
    (tmp_path / "evals").mkdir()
    shutil.copy(ROOT / "evals" / "BASELINE-V4.md", tmp_path / "evals" /
                "BASELINE-V4.md")
    report = build_acceptance_report(tmp_path)
    # Real baseline numbers parsed from the repo's v4 file (11/11 today).
    assert "11/11 passed" in report
    # Honest gaps are marked, not faked: goodput + live over-refusal rate
    # + live journal cannot exist in a bare checkout.
    assert "NOT-MEASURED" in report
    assert report.count("NOT-MEASURED") >= 3
    # The journal integrity DRILL is a real local measurement.
    assert "Integrity drill" in report and "VERIFIED" in report
    # Preflight really ran against this root.
    assert "## 1. Preflight" in report


def test_write_acceptance_round_trip(tmp_path):
    out = write_acceptance(tmp_path)
    assert out.name == "ACCEPTANCE.md" and out.exists()
    assert "ACCEPTANCE REPORT" in out.read_text(encoding="utf-8")
