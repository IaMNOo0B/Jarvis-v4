"""Proactivity chain (Sentient Spec 2): Formulator ->
Searcher -> Reasoner -> Standardizer, with the owner's
hard rules in code — a deterministic prefilter drops bulk
matter before any model, silence is the default, a
near-duplicate is dropped, confidence below the
calibrator's verify band is dropped silently, per-type
feedback scores accumulate, and a risky proposed action
rides a confirmation card (nothing executes from the
chain itself). The deterministic _maybe_nudge remains as
the fallback beneath it."""

import json
from types import SimpleNamespace

from jarvis.calibration import Calibrator
from jarvis.chat import ChatApp, _maybe_proactive
from jarvis.proactivity import (SuggestionStore, feedback,
                                prefilter, run_chain,
                                settle_card_outcomes)


class _FakeBrain:
    def __init__(self, reply):
        self._reply = reply

    def respond(self, history, text):
        return SimpleNamespace(reply=self._reply)


def _chain(tmp_path, *, reasoner_reply, standardizer="x",
           searcher_report="report: sab theek hai",
           risky=None, calibrator=None, notes=None):
    def _run(trigger):
        return run_chain(
            data_dir=tmp_path,
            trigger=trigger,
            formulate_factory=lambda: _FakeBrain(
                '{"questions": ["kya kal free ho?", '
                '"kaunsi file?"]}'),
            searcher=lambda qs: searcher_report,
            reason_factory=lambda: _FakeBrain(reasoner_reply),
            standardize_factory=lambda: _FakeBrain(
                standardizer),
            calibrator=calibrator,
            notify=(lambda t, b: notes.append((t, b)))
            if notes is not None else None,
            card_sink=lambda tool, args: {
                "id": "card42", "tool": tool, "args": args},
            action_is_risky=risky or (lambda name: False))
    return _run


_WORTH = json.dumps({
    "worth_it": True, "confidence": 0.9,
    "reason": "waqt kam hai",
    "suggestion": "Kal ki meeting ki file aaj hi taiyaar "
                  "kar lo, sir.",
    "type_hint": "taiyaari wali salah",
    "action": None})


def test_prefilter_drops_bulk_keeps_heartbeat():
    assert prefilter({"kind": "email",
                      "text": "Newsletter: sale is live"}) \
        is False
    assert prefilter({"kind": "email",
                      "text": "aapki invoice receipt"}) \
        is False
    assert prefilter({"kind": "heartbeat",
                      "text": "check-in"}) is True
    assert prefilter({"kind": "email",
                      "text": "boss ka zaroori mail"}) is True


def test_chain_silent_when_reasoner_says_no(tmp_path):
    notes = []
    row = _chain(tmp_path, reasoner_reply=json.dumps(
        {"worth_it": False}), notes=notes)(
        {"kind": "heartbeat", "text": "check-in"})
    assert row is None and notes == []
    assert SuggestionStore(tmp_path).all() == []


def test_chain_full_pass_notifies_and_stores(tmp_path):
    notes = []
    row = _chain(tmp_path, reasoner_reply=_WORTH,
                 standardizer="meeting_prep", notes=notes)(
        {"kind": "heartbeat", "text": "kal meeting hai"})
    assert row is not None
    assert row["type"] == "meeting_prep"
    assert row["status"] == "shown"
    assert notes and "taiyaar" in notes[0][1]
    assert "/suggest ok " + row["id"] in notes[0][1]
    store = SuggestionStore(tmp_path)
    assert "meeting_prep" in store.known_types()


def test_chain_drops_near_duplicate(tmp_path):
    run = _chain(tmp_path, reasoner_reply=_WORTH,
                 standardizer="meeting_prep")
    trigger = {"kind": "heartbeat", "text": "kal meeting"}
    assert run(trigger) is not None
    assert run(trigger) is None        # same suggestion
    assert len(SuggestionStore(tmp_path).all()) == 1


def test_chain_below_verify_band_dropped_silently(tmp_path):
    cal = Calibrator(tmp_path)
    # A fresh domain is unproven: stated 0.5 sits in the
    # ask band -> dropped, and no claim is recorded.
    low = json.dumps({
        "worth_it": True, "confidence": 0.5, "reason": "x",
        "suggestion": "kuch kar lo", "type_hint": "aam",
        "action": None})
    notes = []
    row = _chain(tmp_path, reasoner_reply=low,
                 calibrator=cal, notes=notes)(
        {"kind": "heartbeat", "text": "check-in"})
    assert row is None and notes == []
    assert cal.pending(limit=50) == []


def test_risky_action_rides_a_card_only_in_act_band(
        tmp_path):
    cal = Calibrator(tmp_path)
    with_action = json.dumps({
        "worth_it": True, "confidence": 0.95, "reason": "x",
        "suggestion": "Boss ko mail bhej dun?",
        "type_hint": "mail salah",
        "action": {"tool": "email_send",
                   "args": {"to": "boss@x.com"}}})
    row = _chain(tmp_path, reasoner_reply=with_action,
                 standardizer="mail_followup",
                 calibrator=cal,
                 risky=lambda name: name == "email_send")(
        {"kind": "heartbeat", "text": "boss ka mail"})
    assert row is not None
    assert row["card_id"] == "card42"
    # The decision was recorded for the calibration loop.
    assert row["decision_id"]


def test_feedback_scores_type_and_resolves_decision(
        tmp_path):
    cal = Calibrator(tmp_path)
    row = _chain(tmp_path, reasoner_reply=_WORTH,
                 standardizer="meeting_prep", calibrator=cal)(
        {"kind": "heartbeat", "text": "kal meeting"})
    assert cal.pending(limit=5)
    out = feedback(tmp_path, row["id"], True,
                   calibrator=cal)
    assert out["status"] == "accepted"
    store = SuggestionStore(tmp_path)
    assert store.score("meeting_prep") > 0.5
    assert cal.pending(limit=5) == []
    out2 = feedback(tmp_path, row["id"], False,
                    calibrator=cal)
    assert out2 is None          # already decided


def test_settle_card_outcomes(tmp_path):
    row = _chain(tmp_path, reasoner_reply=json.dumps({
        "worth_it": True, "confidence": 0.95, "reason": "x",
        "suggestion": "mail bhej dun?",
        "type_hint": "mail", "action": {
            "tool": "email_send", "args": {}}}),
        standardizer="mail_followup",
        risky=lambda name: True)(
        {"kind": "heartbeat", "text": "mail"})
    assert row["card_id"] == "card42"
    assert settle_card_outcomes(
        tmp_path, lambda cid: "pending") == 0
    assert settle_card_outcomes(
        tmp_path, lambda cid: "used") == 1
    store = SuggestionStore(tmp_path)
    assert store.get(row["id"])["status"] == "accepted"
    assert store.score("mail_followup") > 0.5


def test_standardizer_falls_back_to_sanitized_hint(
        tmp_path):
    row = _chain(
        tmp_path, reasoner_reply=_WORTH,
        standardizer="Ye Koi Type Nahi!!")(
        {"kind": "heartbeat", "text": "kal meeting"})
    # The model's free text is unusable as a type; the
    # hint "taiyaari wali salah" is sanitized instead.
    assert row["type"] == "taiyaari_wali_salah"


def test_maybe_proactive_runs_once_with_waiting_items(
        tmp_path, monkeypatch):
    import jarvis.chat as chat_mod
    app = ChatApp(tmp_path, chain=_FakeChain())
    # Something waiting: a pending card.
    card = app.parts.dispatch  # noqa: F841 (wiring exists)
    from jarvis.confirmations import ConfirmationBook
    ConfirmationBook(tmp_path).add("email_send",
                                   {"to": "a@b.c"})
    ran = []
    monkeypatch.setattr(
        chat_mod, "_run_proactive_chain",
        lambda app_, trigger: ran.append(trigger))
    _maybe_proactive(app)
    assert len(ran) == 1
    assert ran[0]["kind"] == "heartbeat"
    _maybe_proactive(app)          # same day: no second run
    assert len(ran) == 1


class _FakeChain:
    def complete(self, messages, tools=None):
        return {"content": "Understood, sir."}


def test_suggest_commands(tmp_path):
    app = ChatApp(tmp_path, chain=_FakeChain())
    row = _chain(tmp_path, reasoner_reply=_WORTH,
                 standardizer="meeting_prep")(
        {"kind": "heartbeat", "text": "kal meeting"})
    out = app.handle_command("/suggest")
    assert row["id"] in out
    out = app.handle_command(f"/suggest ok {row['id']}")
    assert "note" in out.lower()
    out = app.handle_command("/suggest no " + row["id"])
    assert "mili nahi" in out     # already decided above
