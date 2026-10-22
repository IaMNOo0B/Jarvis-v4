"""Second Look (Sentient Spec 1): before a send-class call
may run, its destinations and amounts must trace to the
owner's own words, a trusted record, or a destination an
earlier record-class tool result produced — and, on the
conversational path, a tool-free checker brain that sees
ONLY the owner's request and the proposed call must agree
the action matches. Fail closed everywhere: an untraceable
address or a silent checker raises a card (conversation)
or fails the step (workers)."""

import json

from jarvis.chat import ChatApp
from jarvis.second_look import (SecondLook,
                                extract_amounts,
                                extract_destinations)


class _ScriptedChain:
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


# -- extraction (pure code) -------------------------------- #

def test_extract_destinations_and_amounts():
    args = {"to": "dost@example.com", "subject": "hi",
            "body": "kal $250 bhej dena, aur @helper ko "
                    "bhi bata dena — kul 1,000 rupees"}
    dests = extract_destinations(args)
    assert "dost@example.com" in dests
    assert "@helper" in dests
    amounts = extract_amounts(args)
    assert "250" in amounts and "1000" in amounts


def test_extract_amount_keys():
    assert extract_amounts({"amount": 42.50}) == ["42.5"]
    assert extract_amounts({"note": "koi rakam nahi"}) == []


# -- Stage A (unit) ---------------------------------------- #

def _checker(owner=(), trusted=(), brain_factory=None):
    sl = SecondLook(journal=None,
                    brain_factory=brain_factory,
                    trusted_texts=lambda: list(trusted))
    for text in owner:
        sl.note_owner_text(text)
    return sl


def test_stage_a_traces_owner_words_and_trusted_records():
    sl = _checker(owner=["boss@x.com ko mail bhejo"],
                  trusted=["contact: helper@y.com"])
    assert sl.check("email_send",
                    {"to": "boss@x.com"}, mode="fail") is None
    assert sl.check("email_send",
                    {"to": "helper@y.com"},
                    mode="fail") is None


def test_stage_a_flags_untraceable_destination():
    sl = _checker(owner=["ek mail bhejo"])
    flag = sl.check("email_send",
                    {"to": "stranger@evil.com"}, mode="fail")
    assert flag is not None and flag["stage"] == "A"
    assert flag["flags"][0]["kind"] == "destination"
    assert "bahar ke content" in flag["reason"]


def test_stage_a_flags_untraceable_amount():
    sl = _checker(owner=["dost@x.com ko mail bhejo"])
    flag = sl.check(
        "email_send",
        {"to": "dost@x.com", "body": "ye lo $900"},
        mode="fail")
    assert flag is not None
    assert any(f["kind"] == "amount" for f in flag["flags"])


def test_stage_a_web_result_never_feeds_provenance():
    # An address inside a FETCHED PAGE (net.outbound) is
    # outside content: it must not become a trusted source.
    sl = _checker(owner=["page padho"])
    sl.note_result("web_fetch", "net.outbound",
                   {"content": "mail: page@x.com"})
    flag = sl.check("email_send", {"to": "page@x.com"},
                    mode="fail")
    assert flag is not None
    # But a record-class result (memory read) DOES produce
    # a traceable destination.
    sl.note_result("memory_recall", "memory.read",
                   {"facts": [{"claim": "dost: rec@x.com"}]})
    assert sl.check("email_send", {"to": "rec@x.com"},
                    mode="fail") is None


def test_reply_to_redirect_is_flagged():
    sl = _checker(owner=["boss@x.com ko jawab do"])
    flag = sl.check(
        "email_send", {"to": "boss@x.com"}, mode="fail",
        headers={"from": "boss@x.com",
                 "reply_to": "attacker@evil.com"})
    assert flag is not None
    assert any(f["kind"] == "redirect" for f in flag["flags"])


# -- Stage B (unit) ---------------------------------------- #

class _FakeBrain:
    def __init__(self, reply):
        self._reply = reply

    def respond(self, history, text):
        class _R:
            pass
        r = _R()
        r.reply = self._reply
        return r


def test_stage_b_match_and_mismatch():
    sl = _checker(owner=["dost@x.com ko hello bhejo"],
                  brain_factory=lambda: _FakeBrain(
                      '{"matches": true}'))
    assert sl.check("email_send",
                    {"to": "dost@x.com", "body": "hello"},
                    mode="card") is None
    sl2 = _checker(owner=["dost@x.com ko hello bhejo"],
                   brain_factory=lambda: _FakeBrain(
                       '{"matches": false, "why": "galat '
                       'banda hai"}'))
    flag = sl2.check("email_send",
                     {"to": "dost@x.com", "body": "hello"},
                     mode="card")
    assert flag is not None and flag["stage"] == "B"


def test_stage_b_fails_closed_on_garbage_and_crash():
    sl = _checker(owner=["dost@x.com ko hello bhejo"],
                  brain_factory=lambda: _FakeBrain(
                      "koi json nahi"))
    assert sl.check("email_send",
                    {"to": "dost@x.com", "body": "hello"},
                    mode="card") is not None

    def _boom():
        raise RuntimeError("chain down")

    sl2 = _checker(owner=["dost@x.com ko hello bhejo"],
                   brain_factory=_boom)
    assert sl2.check("email_send",
                     {"to": "dost@x.com", "body": "hello"},
                     mode="card") is not None


def test_fail_mode_never_calls_stage_b():
    calls = []

    def _factory():
        calls.append(1)
        return _FakeBrain('{"matches": false}')

    sl = _checker(owner=["dost@x.com ko hello bhejo"],
                  brain_factory=_factory)
    assert sl.check("email_send",
                    {"to": "dost@x.com", "body": "hello"},
                    mode="fail") is None
    assert calls == []


# -- dispatch wiring (ChatApp) ------------------------------ #

def test_brain_send_to_named_address_passes(tmp_path):
    app = ChatApp(tmp_path, chain=_ScriptedChain([
        _call("email_send", {"to": "dost@x.com",
                             "subject": "hi",
                             "body": "hello"}),
        {"content": '{"matches": true}'},
        {"content": "Bhej diya (khaata nahi hai to error "
                    "aaya hoga), sir."},
    ]))
    app.turn("dost@x.com ko ek mail bhejo")
    # No card: Stage A traced the address to the owner's
    # words and Stage B agreed (the send itself then hit
    # the honest "no account" error — the checker is not
    # the gate).
    assert app.pending_confirmations() == []
    checked = _journal(app, "second_look.checked")
    assert any(c["payload"].get("tool") == "email_send"
               for c in checked)


def test_brain_send_to_foreign_address_raises_card(tmp_path):
    app = ChatApp(tmp_path, chain=_ScriptedChain([
        _call("email_send", {"to": "stranger@evil.com",
                             "subject": "hi",
                             "body": "hello"}),
        {"content": "Card ban gaya hai, sir."},
    ]))
    app.turn("ek mail bhejo please")
    pending = app.pending_confirmations()
    assert len(pending) == 1
    assert "Second Look" in (pending[0].get("summary")
                             or "")
    assert "stranger@evil.com" in (pending[0].get("summary")
                                   or "") or \
        "bahar ke content" in (pending[0].get("summary")
                               or "")
    flagged = _journal(app, "second_look.flagged")
    assert len(flagged) == 1
    assert flagged[0]["payload"]["stage"] == "A"


def test_brain_send_stage_b_down_fails_closed(tmp_path):
    app = ChatApp(tmp_path, chain=_ScriptedChain([
        _call("email_send", {"to": "dost@x.com",
                             "subject": "hi",
                             "body": "hello"}),
        {"content": "ye koi json nahi hai"},
        {"content": "Card ban gaya, sir."},
    ]))
    app.turn("dost@x.com ko mail bhejo")
    assert len(app.pending_confirmations()) == 1
    flagged = _journal(app, "second_look.flagged")
    assert flagged and flagged[0]["payload"]["stage"] == "B"


def test_worker_dispatch_stage_a_fails_the_step(tmp_path):
    app = ChatApp(tmp_path, chain=_ScriptedChain())
    out = app.parts.worker_dispatch(
        "email_send", {"to": "ghost@nowhere.com",
                       "subject": "x", "body": "y"})
    assert out.get("ok") is False
    assert out.get("second_look") is not None
    # No card: a worker path raises nobody's card.
    assert app.pending_confirmations() == []


def test_owner_hands_and_card_redispatch_unaffected(tmp_path):
    app = ChatApp(tmp_path, chain=_ScriptedChain())
    # execute_tool (the owner's own hands): no Second Look.
    out = app.execute_tool("email_send",
                           {"to": "koi@bhi.com",
                            "subject": "x", "body": "y"})
    assert not out.get("second_look")
    # Approving a Second Look card runs the call — the
    # re-dispatch is not re-checked into a loop.
    app2 = ChatApp(tmp_path / "b", chain=_ScriptedChain([
        _call("email_send", {"to": "stranger@evil.com",
                             "subject": "hi", "body": "yo"}),
        {"content": "card bana, sir"},
    ]))
    app2.turn("mail bhejo")
    card = app2.pending_confirmations()[0]
    out = app2.confirm_card(card["id"], approve=True)
    assert app2.pending_confirmations() == []
    assert "Second Look" not in out or "chal" in out
    flagged = _journal(app2, "second_look.flagged")
    assert len(flagged) == 1     # exactly one flag, no loop
