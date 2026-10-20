"""Behaviour eval harness (S5) against v4: the case bank loads, the
scorers judge deterministic fakes correctly, and the full stub-mode
bank reproduces the measured v4 baseline (pinned — a change in these
numbers must be a deliberate, explained event, never a silent drift).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from jarvis.behaviour_eval import (DEFAULT_CASES as CASES_PATH, TurnObservation,
                                   is_deflection, load_cases, main,
                                   render_report, run_cases, score_case)

REPO_ROOT = Path(__file__).resolve().parent.parent

# The measured v4 stub baseline (evals/BASELINE-V4.md). The four
# failures share one root: the v4 StubProvider is an honest offline
# stub that never requests tools, so brain-driven tool cases are
# provider-mode territory. If a future change moves a case across
# this line, update the pin AND the baseline in the same commit.
# Baseline history: the first v4 stub run scored 7/11 — the four tool
# cases could not fire because the production StubProvider never
# requests tools (routing was unmeasurable, not failing). Since the
# harness seats the EvalBrainProvider test double (jarvis.eval_brain)
# and pc.control joined the owner profile, the full bank passes
# end-to-end. These sets pin THAT measured baseline: any future
# regression moves a case between the sets and fails this test loudly.
EXPECTED_PASS = {
    "fail-how-are-u-finance",
    "fail-bland-deflection",
    "fail-open-chrome-no-action",
    "fail-reminder-not-created",
    "fail-long-conversation-silence",
    "fail-provider-404-honest-fallback",
    "ctrl-time",
    "ctrl-web-search",
    "ctrl-remember-fact",
    "ctrl-recall-in-conversation",
    "ctrl-open-notepad",
}
EXPECTED_FAIL = set()


def test_real_bank_loads_with_expected_shape():
    cases = load_cases(CASES_PATH)
    assert len(cases) == 11
    assert len({c["id"] for c in cases}) == 11
    counts = {}
    for case in cases:
        counts[case["category"]] = counts.get(case["category"], 0) + 1
        assert case["origin"]  # every case carries its provenance
    assert counts == {"specification": 7, "coordination": 1,
                      "verification": 2, "over_refusal": 1}


def test_loader_rejects_bad_cases(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({
        "id": "x", "category": "specification", "origin": "test",
        "turn": "hi",
        "expectation": {"kind": "vibes", "detail": {}}}) + "\n",
        encoding="utf-8")
    with pytest.raises(ValueError):
        load_cases(bad)
    bad.write_text("{not json\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_cases(bad)


def test_is_deflection():
    # The scorer's definition: a bare assistant cliché and nothing
    # more (short). Hinglish evasion is caught by the cases' tool /
    # effect checks, not by this helper.
    assert is_deflection("How can I assist you today?")
    assert is_deflection("How may I help you today, sir?")
    assert not is_deflection(
        "Sir, doctor wali call ka note bana diya hai aur kal subah "
        "9 baje ka reminder bhi set hai.")
    assert not is_deflection(
        "I have to be straight with you, sir — I didn't actually run "
        "any tool for that, so nothing was done.")
    assert not is_deflection("x" * 200)  # too long to be a brush-off


class _FakeMemory:
    def __init__(self, hits):
        self._hits = hits

    def recall(self, query, k=5):
        return self._hits


class _FakeApp:
    def __init__(self, hits=()):
        self.memory = _FakeMemory(list(hits))

    def execute_tool(self, name, args=None):
        return {"reminders": []}


def _case(kind, detail):
    return {"id": "t", "category": "specification", "origin": "unit",
            "turn": "x", "expectation": {"kind": kind, "detail": detail}}


def test_score_tool_called_needs_the_call_and_the_verdict():
    obs = TurnObservation(reply="done")
    passed, reasons = score_case(
        _FakeApp(), _case("tool_called", {"tool": "time_now"}), obs)
    assert not passed and "never called" in reasons[0]

    obs.tool_calls = [{"tool": "time_now", "args": {},
                       "gate_verdict": "deny"}]
    passed, reasons = score_case(
        _FakeApp(), _case("tool_called", {"tool": "time_now"}), obs)
    assert not passed and "DENIED" in reasons[0]

    obs.tool_calls = [{"tool": "time_now", "args": {},
                       "gate_verdict": "allow"}]
    passed, _ = score_case(
        _FakeApp(), _case("tool_called", {"tool": "time_now"}), obs)
    assert passed


def test_score_memory_written():
    case = _case("memory_written", {"contains": "Owais"})
    passed, _ = score_case(_FakeApp(), case, TurnObservation(reply="ok"))
    assert not passed
    passed, _ = score_case(
        _FakeApp([{"text": "naam = Owais"}]), case,
        TurnObservation(reply="ok"))
    assert passed


def test_full_bank_stub_mode_reproduces_the_baseline(tmp_path):
    cases_copy = tmp_path / "cases.jsonl"
    shutil.copy(CASES_PATH, cases_copy)
    cases = load_cases(cases_copy)
    results = run_cases(cases, mode="stub", data_dir=tmp_path / "app-data")
    passed = {r.case_id for r in results if r.passed}
    failed = {r.case_id for r in results if not r.passed}
    assert passed == EXPECTED_PASS
    assert failed == EXPECTED_FAIL
    report = render_report(results, mode="stub", case_count=len(cases))
    assert "11/11 passed" in report


def test_cli_regenerates_the_report(tmp_path):
    cases_copy = tmp_path / "cases.jsonl"
    shutil.copy(CASES_PATH, cases_copy)
    out = tmp_path / "BASELINE-V4.md"
    rc = main(["--mode", "stub", "--cases", str(cases_copy),
               "--report", str(out)])
    assert rc == 0  # honest: every case passed, so the exit code says so
    text = out.read_text(encoding="utf-8")
    assert "Totals: 11/11 passed" in text
    assert "fail-open-chrome-no-action" in text
