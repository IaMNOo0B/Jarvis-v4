"""The Kodee-wave leftovers: failure clustering (E), the
mission TODO recitation + errors-stay-in-context (F), the
browser action verdict (G), and the judge-eval plumbing (E2).
"""

import time

import jarvis.chat as chat_mod
from jarvis.browser_cdp import op_browser_click_ref
import jarvis.browser_cdp as cdp
from jarvis.chat import ChatApp
from jarvis.failures import cluster_errors
from jarvis.judge_eval import score_cases


# -- E: clustering ----------------------------------------------

def test_cluster_errors_groups_and_ranks():
    records = ([{"kind": "tool.call", "error": "TimeoutError: x"}]
               * 3 + [{"kind": "brain", "error": "KeyError: 'a'"}]
               + [{"kind": "tool.call", "error": "TimeoutError: y"}])
    out = cluster_errors(records)
    assert out[0]["count"] == 4 and out[0]["kind"] == "tool.call"
    assert out[1]["count"] == 1


def test_diagnose_carries_clusters(tmp_path):
    app = ChatApp(tmp_path)
    import json
    rows = [{"kind": "tool.result",
             "payload": {"error": "TimeoutError: slow"}}] * 3
    (tmp_path / "journal.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    out = app.execute_tool("diagnose", {})
    assert "failure_clusters" in out


# -- F: mission todo + error context ------------------------------

def _wait(pred, timeout=6.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_mission_prompts_carry_the_todo(tmp_path, monkeypatch):
    app = ChatApp(tmp_path)
    seen = []

    def fake_summary(parts, text):
        seen.append(text)
        return "kadam ho gaya"

    checks = iter([{"state": "continue", "confidence": 0.9},
                   {"state": "done", "confidence": 0.95}])
    monkeypatch.setattr(chat_mod, "_delegated_summary",
                        fake_summary)
    monkeypatch.setattr(chat_mod, "_mission_check",
                        lambda p, g, log: next(checks))
    app.execute_tool("run_mission", {"goal": "todo wala goal"})
    assert _wait(lambda: len(seen) >= 2)
    assert "[x] kadam 1" in seen[1] and "TODO:" in seen[1]
    assert "[ ] kadam 2" in seen[1]


# -- G: click verdict ----------------------------------------------

class _FakeSession:
    def __init__(self):
        self.state = {"href": "http://x.test/a", "len": 100}

    def evaluate(self, script):
        if "data-jarvis-ref" in script and "closest" in script:
            import json as _j
            return _j.dumps({"label": "Jaao", "card": ""})
        if "location.href" in script:
            return f"{self.state['href']}|{self.state['len']}"
        if ".click()" in script:
            self.state["len"] = 260        # the page changed
            return "clicked"
        return ""

    def call(self, method, params=None):
        return {}


def test_click_ref_reports_the_effect(monkeypatch):
    fake = _FakeSession()
    monkeypatch.setattr(cdp, "_ensure_session", lambda: fake)
    monkeypatch.setattr(cdp, "_refresh_current",
                        lambda session: None)
    monkeypatch.setattr(cdp.time, "sleep", lambda s: None)
    out = op_browser_click_ref("e3")
    assert out["clicked"] == "Jaao"
    assert out["effect"]["changed"] is True


# -- E2: judge plumbing ---------------------------------------------

class _JudgeChain:
    def __init__(self):
        self.n = 0

    def complete(self, messages, tools=None):
        self.n += 1
        if self.n % 2 == 1:
            return {"content": "ek imaandaar jawab"}
        return {"content": "[1, 0]"}


def test_judge_scoring_plumbing():
    cases = [{"id": "c1", "prompt": "p",
              "rubric": ["line one", "line two"]}]
    result = score_cases(_JudgeChain(), cases)
    assert result["earned"] == 1 and result["total"] == 2
