"""Batch-7 audit regressions: confirmations, dispatch order,
gate nonces, confirm_card, automation bands, delegated
honesty, degraded fail-closed, _format_now (2026-10-22)."""

import time

import pytest

from jarvis import chat as chat_mod
from jarvis.chat import ChatApp, make_dispatch
from jarvis.confirmations import ConfirmationBook
from jarvis.gate import Gate
from jarvis.journal import Journal
from jarvis.skills import DispatchRefused, SkillRegistry

KEY = b"test-secret-key"
OWNER = "owner"


# -- confirmations ------------------------------------------------------

def test_confirmation_books_share_one_lock(tmp_path):
    one = ConfirmationBook(tmp_path)
    two = ConfirmationBook(tmp_path)
    assert one._lock is two._lock


def test_confirmation_add_dedupes_identical_pending(tmp_path):
    book = ConfirmationBook(tmp_path)
    first = book.add("wipe_file", {"path": "a.txt"})
    again = book.add("wipe_file", {"path": "a.txt"})
    assert again["id"] == first["id"]
    assert len(book.pending()) == 1
    other = book.add("wipe_file", {"path": "b.txt"})
    assert other["id"] != first["id"]


def test_confirmation_peek_does_not_consume(tmp_path):
    book = ConfirmationBook(tmp_path)
    card = book.add("wipe_file", {"path": "a.txt"})
    assert book.peek(card["id"])["id"] == card["id"]
    assert book.peek(card["id"]) is not None
    assert book.take(card["id"]) is not None
    assert book.peek(card["id"]) is None


# -- skills dispatch order ------------------------------------------------

def _wipe_manifest():
    return {"name": "test.wipe", "version": "1.0.0",
            "tools": [{"name": "wipe_file", "op": "wipe",
                       "permission": "fs.delete",
                       "schema": {"type": "object",
                                  "properties": {
                                      "path": {"type": "string"}},
                                  "required": ["path"],
                                  "additionalProperties": False}}]}


def test_bad_args_do_not_burn_the_approval_token(tmp_path):
    journal = Journal(str(tmp_path / "j.jsonl"))
    gate = Gate(journal, KEY)
    registry = SkillRegistry(gate=gate, journal=journal)
    wiped = []
    registry.register(_wipe_manifest(),
                      ops={"wipe": lambda path:
                           wiped.append(path) or "wiped"})
    registry.approve("test.wipe")
    decision = gate.evaluate("wipe_file", {"path": "a.txt"},
                             OWNER, {"fs.delete"})
    # Bad args first: refused on SCHEMA — the token must
    # survive for the corrected call.
    with pytest.raises(DispatchRefused) as exc:
        registry.dispatch("wipe_file", {"path": 42},
                          decision=decision)
    assert "schema" in str(exc.value)
    assert registry.dispatch("wipe_file", {"path": "a.txt"},
                             decision=decision) == "wiped"
    assert wiped == ["a.txt"]


# -- gate nonce pruning -----------------------------------------------------

def test_gate_nonces_prune_after_expiry(tmp_path):
    clock = {"t": 1000.0}
    journal = Journal(str(tmp_path / "j.jsonl"))
    gate = Gate(journal, KEY, now_fn=lambda: clock["t"])
    gate.register_tool("wipe_file", "fs.delete", destructive=True)
    first = gate.evaluate("wipe_file", {"path": "a"}, OWNER,
                          {"fs.delete"})
    assert gate.verify_approval(first.approval_token,
                                "wipe_file", {"path": "a"})
    assert gate._burned_nonces and gate._issued_nonces
    clock["t"] += 100000.0     # far past the 120 s TTL
    gate.evaluate("wipe_file", {"path": "b"}, OWNER, {"fs.delete"})
    assert gate._burned_nonces == {}
    assert len(gate._issued_nonces) == 1   # only the fresh one


# -- confirm_card: deny verdict -------------------------------------------

def test_confirm_card_gate_deny_retires_with_honest_reply(tmp_path):
    app = ChatApp(tmp_path)
    book = ConfirmationBook(tmp_path)
    card = book.add("tool_jo_exist_hi_nahi", {"x": 1})
    reply = app.confirm_card(card["id"], approve=True)
    assert "mana hai" in reply
    assert book.peek(card["id"]) is None     # retired, not burned
    assert book.pending() == []


# -- degraded dispatch fails closed -----------------------------------------

class _Gate:
    def evaluate(self, name, args, origin, allowances):
        class D:
            verdict = "allow"
            reason = ""
        return D()


class _Tool:
    def __init__(self, destructive):
        self.destructive = destructive
        self.skill = "core.test"


class _Registry:
    def __init__(self):
        self._tools = {"danger": _Tool(True), "safe": _Tool(False)}
        self.ran = []

    def advertised_tools(self, allowances):
        return [{"name": n, "destructive": t.destructive}
                for n, t in self._tools.items()]

    def dispatch(self, name, args, decision=None):
        self.ran.append(name)
        return {"ok": True}


def test_degraded_dispatch_refuses_destructive(tmp_path):
    journal = Journal(str(tmp_path / "j.jsonl"))
    registry = _Registry()
    dispatch = make_dispatch(_Gate(), registry, journal, set(),
                             degraded=True)
    out = dispatch("danger", {})
    assert out["ok"] is False and out.get("degraded_refusal")
    assert registry.ran == []
    # Non-destructive tools still run in degraded mode.
    assert dispatch("safe", {})["ok"] is True


def test_runtime_destructive_tool_is_ledger_guarded(tmp_path):
    # A destructive tool registered AFTER startup (Forge
    # promotion) must be guarded by the LIVE check.
    from jarvis.idempotency import IdempotencyLedger
    journal = Journal(str(tmp_path / "j.jsonl"))
    ledger = IdempotencyLedger(journal)
    registry = _Registry()
    registry._tools = {"safe": _Tool(False)}   # startup snapshot
    dispatch = make_dispatch(_Gate(), registry, journal, set(),
                             ledger=ledger, session_id="s1")
    registry._tools["danger"] = _Tool(True)    # promoted later
    first = dispatch("danger", {"x": 1})
    second = dispatch("danger", {"x": 1})
    assert first["ok"] and second["ok"]
    assert registry.ran == ["danger"]          # ran ONCE, replayed


# -- _format_now ------------------------------------------------------------

def test_format_now_declines_on_error_payload():
    assert ChatApp._format_now({"error": "boom"}) is None
    assert ChatApp._format_now({}) is None
    out = ChatApp._format_now({"iso": "2026-10-22T15:04:00+05:30"})
    assert out and "sir" in out


# -- delegated honesty --------------------------------------------------------

class _ScriptedChain:
    name = "scripted"

    def __init__(self, reply):
        self._reply = reply

    def complete(self, messages, tools=None):
        return {"content": self._reply, "tool_calls": []}


def test_delegated_run_reports_honest_stop(tmp_path):
    app = ChatApp(tmp_path,
                  chain=_ScriptedChain("Ho gaya, chrome khul "
                                       "gaya aur sab ho gaya"))
    summary, reason = chat_mod._delegated_run(
        app.parts, "open chrome")
    assert reason != "completed"
    assert reason.startswith("honest_stop")


def test_run_delegated_marks_failed_status(tmp_path):
    app = ChatApp(tmp_path,
                  chain=_ScriptedChain("Ho gaya, sab set ho "
                                       "gaya, bhej diya"))
    chat_mod._run_delegated(app.parts, tmp_path, "t-1",
                            "open the mail app and send it")
    rows = chat_mod._task_log_read(tmp_path) \
        if hasattr(chat_mod, "_task_log_read") else None
    if rows is None:
        import json
        from pathlib import Path
        log = Path(tmp_path) / "tasks.jsonl"
        rows = [json.loads(l) for l in
                log.read_text().splitlines() if l.strip()]
    mine = [r for r in rows if r["id"] == "t-1"]
    assert mine and mine[0]["status"] == "failed"


# -- automation calibration bands ---------------------------------------

def _app_with_route(tmp_path, action):
    app = ChatApp(tmp_path)
    from jarvis.automations import AutomationBook
    book = AutomationBook(tmp_path)
    book.create("subah-brief", "roz subah ki brief",
                [{"tool": "time_now", "args": {}}], [],
                triggers=["subah ki brief"])
    app.calibrator.policy = lambda domain, conf: {
        "action": action, "calibrated": 0.66}
    return app


def test_automation_verify_band_hands_route_to_brain(tmp_path):
    app = _app_with_route(tmp_path, "verify")
    reply, hint = app._automation_pre_brain("subah ki brief do")
    assert reply is None
    assert hint is not None and "run_automation" in hint


def test_automation_ask_hint_is_not_double_wrapped(tmp_path):
    app = _app_with_route(tmp_path, "ask")
    reply, hint = app._automation_pre_brain("subah ki brief do")
    assert reply is None and hint is not None
    assert "[Automation note:" not in hint
    assert not hint.rstrip().endswith("]")
