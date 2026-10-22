"""Automation self-use (the owner's order: no commands —
JARVIS recognizes and runs routes itself, and saves routes
for jobs that repeat). A matched no-param route runs BEFORE
the brain is even consulted; a param route becomes a hint in
the brain's turn; a repeated 3+ tool job earns its route on
the second occurrence and runs itself on the third."""

import time

from jarvis.automations import match_automations
from jarvis.chat import ChatApp


class MarkerChain:
    name = "marker"

    def __init__(self):
        self.calls = 0
        self.last_user = ""

    def complete(self, messages, tools=None):
        self.calls += 1
        for m in reversed(messages):
            if m.get("role") == "user":
                self.last_user = str(m.get("content", ""))
                break
        return {"content": "BRAIN USED"}


def _mk_route(app, name="roz-entry", title="Roz ki entry"):
    out = app.execute_tool("create_automation", {
        "name": name, "title": title,
        "steps": [
            {"tool": "time_now", "args": {}},
            {"tool": "track_add",
             "args": {"table": "roz",
                      "record": {"note": "entry"}}}],
        "params": []})
    assert out["ok"] is True


def test_matcher_needs_a_real_phrase():
    rows = [{"name": "x", "title": "Waqt",
             "triggers": ["waqt"]},
            {"name": "y", "title": "Roz ki entry",
             "triggers": ["roz ki entry"]}]
    assert match_automations(rows, "aaj waqt kya hai") == []
    hits = match_automations(rows, "roz ki entry kar do")
    assert hits and hits[0][0]["name"] == "y"


def test_matched_route_runs_without_the_brain(tmp_path):
    chain = MarkerChain()
    app = ChatApp(tmp_path, chain=chain)
    _mk_route(app)
    # The route self-runs from the ACT band: seed the
    # automations domain's lived record first (an unproven
    # domain is verify-band since batch 7 and goes to the
    # brain instead — by design).
    for _ in range(4):
        row = app.calibrator.record("core.automations",
                                    "route chalega", 0.7)
        app.calibrator.resolve(row["id"], True)
    reply = app.turn("roz ki entry kar do aaj ki")
    assert reply.startswith("🤖 Automation 'roz-entry'")
    assert chain.calls == 0            # the brain never woke
    rows = app.execute_tool("track_list", {"table": "roz"})
    assert rows  # the route's step really wrote its record


def test_negation_blocks_the_auto_run(tmp_path):
    chain = MarkerChain()
    app = ChatApp(tmp_path, chain=chain)
    _mk_route(app)
    reply = app.turn("roz ki entry mat karna aaj")
    assert reply == "BRAIN USED"
    assert chain.calls >= 1


def test_param_route_becomes_a_brain_hint(tmp_path):
    chain = MarkerChain()
    app = ChatApp(tmp_path, chain=chain)
    out = app.execute_tool("create_automation", {
        "name": "subah-check", "title": "Subah check",
        "steps": [{"tool": "time_now", "args": {}}],
        "params": ["msg"]})
    assert out["ok"] is True
    reply = app.turn("subah check chalao")
    # The brain handled the turn (not the mechanical runner)...
    assert not reply.startswith("🤖 Automation")
    # ...and the hint rode inside its user message. (The
    # marker's bare "BRAIN USED" gets rewritten by the
    # CompletionAudit for claiming without acting — the
    # hint's arrival is what this test pins.)
    assert "Automation note" in chain.last_user
    assert "subah-check" in chain.last_user


class WorkerChain:
    name = "worker"

    def complete(self, messages, tools=None):
        if any(m.get("role") == "tool" for m in messages):
            return {"content": "kaam ho gaya"}
        return {"content": "", "tool_calls": [
            {"id": "1", "name": "time_now", "arguments": {}},
            {"id": "2", "name": "track_add",
             "arguments": {"table": "dohraya",
                           "record": {"note": "x"}}},
            {"id": "3", "name": "time_now", "arguments": {}}]}


def test_repeated_job_earns_a_route_then_runs_itself(tmp_path):
    app = ChatApp(tmp_path, chain=WorkerChain())
    text = "tracker me waqt ke sath note jodo phir waqt dekho"
    app.turn(text)
    assert app.execute_tool(
        "list_automations", {})["automations"] == []
    reply2 = app.turn(text)
    autos = app.execute_tool(
        "list_automations", {})["automations"]
    assert len(autos) == 1 and autos[0]["runs"] == 0
    assert "route bana kar rakh diya" in reply2
    reply3 = app.turn(text)      # third time: it runs itself
    autos = app.execute_tool(
        "list_automations", {})["automations"]
    assert len(autos) == 1 and autos[0]["runs"] == 1
    assert reply3.startswith("🤖 Automation")
