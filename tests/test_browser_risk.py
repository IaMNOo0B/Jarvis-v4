"""Browser click safety after the 2026-10-22 audit: the
payment gate judges the card context too, the text-click
path is gated like click_ref, and typing verifies itself."""

import json

from jarvis import browser_cdp as b


def test_continue_inside_a_payment_card_is_high_risk():
    # The audit's probe: checkout's final button is a bland
    # "Continue" inside a card full of money signals.
    card = "Total $40, enter CVV, billing address"
    assert b.classify_click_risk("Continue", card) == "high"
    assert b.classify_click_risk("Submit", "$120/month") == "high"


def test_plain_continue_on_a_plain_card_stays_low():
    assert b.classify_click_risk(
        "Continue", "Step 2 of 3: your preferences") == "low"
    assert b.classify_click_risk("Read more", "A blog post") \
        == "low"


def test_paid_subscribe_is_not_exempted_by_the_word_free():
    ctx = "Free shipping on first box. Then billed monthly " \
          "via card on file"
    assert b.classify_click_risk("Subscribe", ctx) == "high"
    # A genuinely free subscribe still passes.
    assert b.classify_click_risk(
        "Subscribe", "Free forever, no card needed") == "low"


def test_new_risky_verbs_are_high():
    for label in ("Donate", "Authorize payment", "Withdraw",
                  "Place bid"):
        assert b.classify_click_risk(label, "") == "high", label


def test_text_click_is_risk_gated(monkeypatch):
    class FakeSession:
        def evaluate(self, expr):
            if "return JSON.stringify" in expr and \
                    "holder" in expr:
                return json.dumps(
                    {"label": "Buy now",
                     "card": "Total $10"})
            raise AssertionError("click must not run")

    monkeypatch.setattr(b, "_ensure_session",
                        lambda: FakeSession())
    out = b.op_browser_click("Buy now")
    assert out["ok"] is False and out["risk"] == "high"


def test_snapshot_clears_stale_refs_first():
    assert "removeAttribute('data-jarvis-ref')" in b.SNAPSHOT_JS


def test_type_reports_hint_miss_instead_of_typing_anywhere(
        monkeypatch):
    class FakeSession:
        def evaluate(self, expr):
            return json.dumps({"error": "hint-nomatch"})

    monkeypatch.setattr(b, "_ensure_session",
                        lambda: FakeSession())
    out = b.op_browser_type("secret", into="email")
    assert "error" in out and "nahi likha" in out["error"]


def test_type_verifies_the_readback(monkeypatch):
    class FakeSession:
        def evaluate(self, expr):
            return json.dumps({"field": "Search", "value": ""})

    monkeypatch.setattr(b, "_ensure_session",
                        lambda: FakeSession())
    out = b.op_browser_type("hello", into="search")
    assert out["ok"] is False and "readback" in out
