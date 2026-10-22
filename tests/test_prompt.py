"""Prompt assembly tests (Steps 11/17): byte-stable prefix, budget ladder."""

import pytest

from jarvis.prompt import (PERSONA, PROTOCOL, PromptBudgetError,
                           PromptBuilder, render_system, summarize_dropped,
                           tool_views)
from jarvis.prompt_stability import check_stability, split_stable


def _tools():
    return tool_views([
        {"name": "now", "description": "Current date and time.",
         "parameters": {"type": "object", "properties": {}}},
        {"type": "function",
         "function": {"name": "web_search", "description": "Search the web.",
                      "parameters": {"type": "object",
                                     "properties": {"query": {"type": "string"}}}}},
    ])


def test_stable_prefix_byte_identical_across_turns():
    b = PromptBuilder(_tools())
    a1 = b.build([], "kya haal hai?", memory_lines=["Owner's name is Owais"])
    a2 = b.build([{"role": "user", "content": "pehle wala sawal"},
                  {"role": "assistant", "content": "jawab"}],
                 "aur ab ye sawal?", memory_lines=["Owner likes green tea"])
    s1 = a1.messages[0]["content"]
    s2 = a2.messages[0]["content"]
    report = check_stability([s1, s2])
    assert report.ok, report.summary()
    p1, _ = split_stable(s1)
    p2, _ = split_stable(s2)
    assert p1 == p2
    assert p1.startswith("## PERSONA")


def test_memory_lives_below_the_boundary():
    system = render_system(_tools(), memory_lines=["fact one"])
    prefix, tail = split_stable(system)
    assert "fact one" not in prefix
    assert "fact one" in tail
    assert PERSONA in prefix and PROTOCOL in prefix


def test_tool_descriptions_shrink_but_tools_never_drop():
    long_desc = "x" * 500
    views = tool_views([{"name": "big_tool", "description": long_desc,
                         "parameters": {"type": "object", "properties": {}}}])
    b = PromptBuilder(views, soft_cap=620, hard_budget=5000)
    out = b.build([], "hello")
    assert any("descriptions capped" in a for a in out.ladder_actions)
    assert len(out.tools) == 1
    desc = out.tools[0]["function"]["description"]
    assert 0 < len(desc) <= 140
    assert out.estimate <= 620


def test_ladder_trims_history_middle_and_persona_survives():
    views = tool_views([])
    b = PromptBuilder(views)  # default soft 3300 / hard 4000
    history = []
    for i in range(40):
        history.append({"role": "user", "content": f"message {i} " + "la " * 120})
        history.append({"role": "assistant", "content": "ok " * 160})
    out = b.build(history, "final question?")
    assert out.dropped > 0
    assert out.estimate <= 4000
    system = out.messages[0]["content"]
    assert system.startswith("## PERSONA")
    assert "## PROTOCOL" in system
    # First exchange and the latest turns survive the trim.
    contents = [str(m.get("content", "")) for m in out.messages]
    assert any("message 0" in c for c in contents)
    assert any("final question?" in c for c in contents)


def test_ladder_summarizes_when_trimming_is_not_enough():
    views = tool_views([])
    b = PromptBuilder(views)
    history = []
    for i in range(10):
        history.append({"role": "user", "content": f"topic {i} " + "word " * 380})
        history.append({"role": "assistant", "content": "reply " * 380})
    out = b.build(history, "ab final jawab do")
    assert out.summary is not None
    assert "Conversation summary" in out.summary
    assert any("Conversation summary" in str(m.get("content", ""))
               for m in out.messages)
    assert out.estimate <= 4000


def test_minimal_prompt_over_hard_budget_raises():
    b = PromptBuilder(tool_views([]), soft_cap=100, hard_budget=300)
    with pytest.raises(PromptBudgetError) as exc:
        b.build([], "hello sir, " + "please " * 60)
    assert "hard budget" in str(exc.value)


def test_under_budget_turn_uses_no_ladder():
    b = PromptBuilder(_tools())
    out = b.build([], "hi")
    assert out.ladder_actions == []
    assert out.dropped == 0
    assert out.estimate <= 3300


def test_summarize_dropped_is_deterministic():
    msgs = [{"role": "user", "content": "pehla sawal ye tha"},
            {"role": "assistant", "content": "ye jawab tha"}]
    assert summarize_dropped(msgs) == summarize_dropped(msgs)
    assert "2 earlier message(s)" in summarize_dropped(msgs)


# ---------------------------------------------------------------------------
# Rules annex (ECC rules install): pack cards ride the DYNAMIC
# tail only; the ladder drops the annex whole (step a3) before
# it ever touches history.
# ---------------------------------------------------------------------------

def _python_annex():
    from pathlib import Path as _Path

    import jarvis.rules as _rules
    root = _Path(__file__).resolve().parent.parent
    return _rules.load_rule_packs(root, ["python"])


def test_rules_annex_below_dynamic_boundary():
    annex = _python_annex()
    assert "PEP 8" in annex
    plain = render_system(_tools(), memory_lines=["fact one"])
    with_annex = render_system(_tools(), memory_lines=["fact one"],
                               rules_annex=annex)
    p1, t1 = split_stable(plain)
    p2, t2 = split_stable(with_annex)
    assert p1 == p2                    # prefix byte-identical
    assert "PEP 8" not in t1
    assert "PEP 8" in t2               # card below the boundary
    # The annex rides AFTER the memory excerpts.
    assert t2.index("fact one") < t2.index("PEP 8")


def test_rules_annex_absent_by_default():
    system = render_system(_tools())
    assert "PACK:" not in system
    out = PromptBuilder(_tools()).build([], "hi")
    assert out.rules_annex_included is False
    assert "PACK:" not in out.messages[0]["content"]


def test_ladder_drops_annex_before_history():
    views = _tools()
    history = []
    for i in range(6):
        role = "user" if i % 2 == 0 else "assistant"
        history.append({"role": role,
                        "content": f"baat {i} " + "lafz " * 40})
    probe = PromptBuilder(views, soft_cap=10 ** 9,
                          hard_budget=10 ** 9)
    base = probe.build(history, "aakhri sawal")
    # A soft cap the plain build fits but the annexed build
    # does not: the annex alone must absorb the difference.
    b = PromptBuilder(views, soft_cap=base.estimate + 5,
                      hard_budget=4000)
    out = b.build(history, "aakhri sawal",
                  rules_annex=_python_annex())
    assert any("dropped rules annex" in a
               for a in out.ladder_actions)
    assert out.rules_annex_included is False
    assert "PACK:" not in out.messages[0]["content"]
    # History was never trimmed — head and tail survive whole.
    assert not any("trimmed" in a for a in out.ladder_actions)
    contents = [str(m.get("content", "")) for m in out.messages]
    assert any("baat 0" in c for c in contents)
    assert any("baat 5" in c for c in contents)


def test_annex_survives_when_budget_allows():
    b = PromptBuilder(_tools())
    out = b.build([], "python script likho",
                  rules_annex=_python_annex())
    assert out.rules_annex_included is True
    assert not any("annex" in a for a in out.ladder_actions)
    assert "PEP 8" in out.messages[0]["content"]
