"""Santa dual review (ECC Wave B): every judgement that
gates real work is made TWICE, by independent fresh-context
reviewers who never see each other — a plain judge and a
skeptic paid to find the failure. Unanimity or it fails,
with both notes kept as evidence. Proven here for build
verification checks and for the team critic."""

import json

from jarvis.chat import (ChatApp, _build_check_dual,
                         _dual_critic)


class _ReviewChain:
    """Answers reviewer prompts by framing: the skeptic's
    prompt carries the shak-guard marker, the plain
    judge's doesn't. Verdicts are set per test."""

    judge = {"ok": True, "note": "saboot saaf hai"}
    skeptic = {"ok": True, "note": "dhundhne par khaami "
                                  "nahi mili"}

    def complete(self, messages, tools=None):
        text = json.dumps(messages, ensure_ascii=False)
        if "shak-guard" in text or "SECOND reviewer" in text:
            verdict = self.skeptic
        else:
            verdict = self.judge
        return {"content": json.dumps(verdict),
                "tool_calls": []}


def _app(tmp_path, judge, skeptic):
    chain = _ReviewChain()
    chain.judge = judge
    chain.skeptic = skeptic
    return ChatApp(tmp_path, chain=chain)


_ITEM = {"kind": "check", "detail": "calc.py me add hai"}


def test_build_check_unanimous_passes(tmp_path):
    app = _app(tmp_path,
               {"ok": True, "note": "add maujood hai"},
               {"ok": True, "note": "khaami nahi mili"})
    got = _build_check_dual(app.parts, "calculator banao",
                            _ITEM, "diff: calc.py bana")
    assert got["ok"] is True
    assert "[parakh-1]" in got["note"]
    assert "[shak-guard]" in got["note"]


def test_build_check_skeptic_veto_fails_with_both_notes(
        tmp_path):
    app = _app(tmp_path,
               {"ok": True, "note": "theek lagta hai"},
               {"ok": False,
                "note": "add hai par test file khali hai"})
    got = _build_check_dual(app.parts, "calculator banao",
                            _ITEM, "diff: calc.py bana")
    assert got["ok"] is False
    assert "theek lagta hai" in got["note"]
    assert "test file khali hai" in got["note"]


def test_build_check_judge_veto_also_fails(tmp_path):
    app = _app(tmp_path,
               {"ok": False, "note": "add function hi nahi"},
               {"ok": True, "note": "khaami nahi mili"})
    got = _build_check_dual(app.parts, "calculator banao",
                            _ITEM, "diff: khali")
    assert got["ok"] is False


_SUBTASK = {"title": "hissa 1", "files": ["a.py"],
            "brief": "a.py banao"}


def test_team_dual_critic_unanimous_passes(tmp_path):
    app = _app(tmp_path, {"ok": True}, {"ok": True})
    verdict = _dual_critic(app.parts, "task", _SUBTASK,
                           "--- a.py ---\nx = 1", "banaya")
    assert verdict == {"ok": True}


def test_team_dual_critic_split_blocks_with_both_voices(
        tmp_path):
    app = _app(tmp_path,
               {"ok": True},
               {"ok": False, "issues": "edge case tootta hai"})
    verdict = _dual_critic(app.parts, "task", _SUBTASK,
                           "--- a.py ---\nx = 1", "banaya")
    assert verdict["ok"] is False
    assert "[shak-guard]" in verdict["issues"]
    assert "edge case tootta hai" in verdict["issues"]


def test_team_dual_critic_judge_veto_blocks(tmp_path):
    app = _app(tmp_path,
               {"ok": False, "issues": "brief adhoora hai"},
               {"ok": True})
    verdict = _dual_critic(app.parts, "task", _SUBTASK,
                           "--- a.py ---\nx = 1", "banaya")
    assert verdict["ok"] is False
    assert "[critic]" in verdict["issues"]
    assert "brief adhoora hai" in verdict["issues"]
