"""Browser snapshots + click risk: refs the brain can point at,
risk classified from label + surrounding card text BEFORE a
click, risky clicks refused without the owner's confirm."""

from jarvis.browser_cdp import _format_snapshot, classify_click_risk
from jarvis.chat import ChatApp


def test_risk_classification():
    assert classify_click_risk("Sign in") == "low"
    assert classify_click_risk("Buy now") == "high"
    assert classify_click_risk("Delete account") == "high"
    assert classify_click_risk("Subscribe",
                               "Free forever, no card needed") == "low"
    assert classify_click_risk(
        "Subscribe", "Pro plan $12/month, card required") == "high"
    assert classify_click_risk("Checkout",
                               "Your total is $40") == "high"


def test_snapshot_formatting_budget_and_risk_flags():
    data = {"total": 3, "text": "x" * 5000, "elements": [
        {"ref": "e1", "tag": "a", "label": "Home", "card": "",
         "form": ""},
        {"ref": "e2", "tag": "button", "label": "Buy now",
         "card": "Shoes $50", "form": ""},
        {"ref": "e3", "tag": "input", "label": "", "card": "",
         "form": "yes"}]}
    out = _format_snapshot(data, max_chars=12000)
    assert out["elements"][1]["risk"] == "high"
    assert any("[RISKY]" in line for line in out["lines"])
    assert any("[form]" in line for line in out["lines"])
    assert len(out["text"]) <= 12000


def test_snapshot_overflow_line_is_explicit():
    elements = [{"ref": f"e{i}", "tag": "a", "label": "link " * 10,
                 "card": "", "form": ""} for i in range(1, 60)]
    out = _format_snapshot({"total": 59, "text": "",
                            "elements": elements}, max_chars=600)
    assert any("more elements not shown" in line
               for line in out["lines"])


def test_app_advertises_snapshot_tools(tmp_path):
    app = ChatApp(tmp_path)
    names = {t["name"] for t in
             app.registry.advertised_tools(app.allowances)}
    assert "browser_snapshot" in names
    assert "browser_click_ref" in names
