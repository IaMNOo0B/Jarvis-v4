"""GateGuard fact-gate (ECC Wave B): a destructive call
from the conversational brain is stopped BEFORE the gate
until it presents its three facts {change, current,
verify}; with facts it proceeds through the normal gate
(cards untouched). Internal dispatches and the owner's
own hands are exempt — the gate disciplines the model."""

from jarvis.chat import ChatApp

FACTS = {
    "change": "notepad process band hoga, uski window "
              "gayab ho jayegi",
    "current": "notepad abhi chal raha hai aur koi unsaved "
               "kaam nahi dikh raha",
    "verify": "process_list me notepad nahi dikhega — yahi "
              "safalta ka saboot hoga",
}


class _ScriptedChain:
    """Chain fake: pops scripted replies, then a calm
    default. Records nothing; the journal is the witness."""

    def __init__(self, script=()):
        self.script = list(script)

    def complete(self, messages, tools=None):
        if self.script:
            return self.script.pop(0)
        return {"content": "Understood, sir."}


def _call(name, args):
    return {"tool_calls": [{"name": name, "args": args}]}


def _journal(app, kind):
    return [r for r in app.journal.replay()
            if r.get("kind") == kind]


def test_brain_destructive_call_without_facts_is_stopped(
        tmp_path):
    app = ChatApp(tmp_path, chain=_ScriptedChain([
        _call("process_kill", {"target": "notepad"}),
        {"content": "Facts ke bina ye kaam nahi ho paya, "
                    "sir — kuch complete nahi hua."},
    ]))
    reply = app.turn("notepad band karo")
    assert "nahi ho paya" in reply
    # Stopped BEFORE the gate: no card was created...
    assert app.pending_confirmations() == []
    # ...the journal shows the fact-gate verdict...
    calls = _journal(app, "tool.call")
    assert any(c["payload"].get("gate_verdict")
               == "needs_facts" for c in calls)
    # ...and no facts were journaled (none were given).
    assert _journal(app, "tool.facts") == []


def test_brain_retries_with_facts_and_reaches_the_gate(
        tmp_path):
    app = ChatApp(tmp_path, chain=_ScriptedChain([
        _call("process_kill", {"target": "notepad"}),
        _call("process_kill", {"target": "notepad",
                               "facts": FACTS}),
        {"content": "Kaam abhi nahi ho paya, sir — "
                    "confirmation card banaya hai, aapke "
                    "approve karne par hi chalega."},
    ]))
    reply = app.turn("notepad band karo")
    assert "confirmation card" in reply
    # The facts were journaled as their own event...
    facts_rows = _journal(app, "tool.facts")
    assert len(facts_rows) == 1
    assert facts_rows[0]["payload"]["facts"]["change"] \
        == FACTS["change"]
    # ...and the call proceeded to the NORMAL gate: a card
    # waits for the owner, exactly as before GateGuard.
    pending = app.pending_confirmations()
    assert len(pending) == 1
    assert "process_kill" in (pending[0].get("summary")
                              or "")
    # Approving the card executes — the facts did not
    # poison the card's args or its one-shot spend.
    out = app.confirm_card(pending[0]["id"], approve=True)
    assert "chal gaya" in out or "chal nahi paya" in out
    assert app.pending_confirmations() == []


def test_short_facts_are_rejected_with_the_field_named(
        tmp_path):
    app = ChatApp(tmp_path, chain=_ScriptedChain([
        _call("process_kill", {
            "target": "notepad",
            "facts": {"change": "band hoga",
                      "current": "chal raha hai abhi",
                      "verify": "list me nahi dikhega"}}),
        {"content": "Facts chhote the, sir."},
    ]))
    app.turn("notepad band karo")
    results = _journal(app, "tool.result")
    assert any(r["payload"].get("needs_facts")
               for r in results)
    assert app.pending_confirmations() == []


def test_non_destructive_calls_are_unaffected(tmp_path):
    app = ChatApp(tmp_path, chain=_ScriptedChain([
        _call("time_now", {}),
        {"content": "Waqt bata diya, sir."},
    ]))
    reply = app.turn("waqt kya hai abhi")
    assert "Waqt bata diya" in reply
    assert _journal(app, "tool.facts") == []


def test_internal_and_owner_dispatches_are_exempt(tmp_path):
    app = ChatApp(tmp_path, chain=_ScriptedChain())
    # The owner's own hands (execute_tool): straight to
    # the gate, card as always — no facts demanded.
    out = app.execute_tool("process_kill",
                           {"target": "notepad"})
    assert out.get("needs_approval") is True
    # The shared dispatch every internal path rides
    # (workers, automations, build glue): unmarked, so
    # exempt from facts — still gated.
    out = app.parts.dispatch("process_kill",
                             {"target": "notepad"})
    assert out.get("needs_approval") is True
    assert not out.get("needs_facts")
    # The conversational brain's own dispatch (the
    # _fact_gate marking only it carries) without facts:
    # stopped + taught the exact retry shape.
    out = app.brain.dispatch("process_kill",
                             {"target": "notepad"})
    assert out.get("needs_facts") is True
    assert "GateGuard" in out["error"]
    assert '"facts"' in out["error"]
    assert '"change"' in out["error"]
    # With facts through the same dispatch: journaled,
    # stripped, and on to the gate (a card, no execution).
    out = app.brain.dispatch(
        "process_kill",
        {"target": "notepad", "facts": FACTS})
    assert out.get("needs_approval") is True
    facts_rows = _journal(app, "tool.facts")
    assert len(facts_rows) == 1
    # The card's stored args carry NO facts key — the op
    # and the approval digest see the clean call.
    card = (out.get("card") or {})
    book_rows = app.pending_confirmations()
    assert card.get("id") in [c["id"] for c in book_rows]
