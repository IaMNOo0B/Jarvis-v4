"""RLCD, the whole loop: the log score pays honest doubt over
false certainty; per-domain maps correct stated confidence; the
policy turns calibrated belief into act / verify / ask; and the
feedback lines reach the brain's context."""

import math

from jarvis.calibration import Calibrator, log_score
from jarvis.chat import ChatApp


def test_log_score_rewards_calibrated_doubt():
    assert log_score(0.9, True) > log_score(0.6, True)
    assert log_score(0.6, False) > log_score(0.9, False)
    # Honest doubt on a loss beats confident error — the point.
    assert log_score(0.55, False) > log_score(0.95, False)
    assert math.isclose(log_score(0.5, True), math.log(0.5))


def test_calibration_map_corrects_an_overconfident_domain(tmp_path):
    cal = Calibrator(tmp_path)
    for i in range(10):                    # says 90%, right half
        row = cal.record("web", f"claim {i}", 0.9)
        cal.resolve(row["id"], right=(i % 2 == 0))
    value = cal.calibrated("web", 0.9)
    assert value < 0.7                     # the map pulls it down
    verdict = cal.policy("web", 0.9)
    assert verdict["action"] in ("verify", "ask")
    stats = cal.stats("web")
    assert stats["resolved"] == 10 and stats["bias"] > 0.3


def test_unknown_domain_keeps_stated_and_acts(tmp_path):
    cal = Calibrator(tmp_path)
    assert cal.calibrated("naya", 0.9) == 0.9
    assert cal.policy("naya", 0.9)["action"] == "act"
    assert cal.policy("naya", 0.3)["action"] == "ask"


def test_feedback_lines_name_the_miscalibration(tmp_path):
    cal = Calibrator(tmp_path)
    for i in range(6):
        row = cal.record("prices", f"rate {i}", 0.85)
        cal.resolve(row["id"], right=(i == 0))
    lines = cal.feedback_lines()
    assert lines and "prices" in lines[0]
    assert "zyada bolta hai" in lines[0]


def test_app_wiring_tools_commands_and_context(tmp_path):
    app = ChatApp(tmp_path)
    names = {t["name"] for t in
             app.registry.advertised_tools(app.allowances)}
    assert {"record_decision", "resolve_decision",
            "check_confidence"} <= names
    out = app.execute_tool("record_decision", {
        "domain": "web", "claim": "ye page sahi hai",
        "confidence": 0.9})
    decision_id = out["decision_id"]
    done = app.execute_tool("resolve_decision", {
        "decision_id": decision_id, "right": False})
    assert done["ok"] is True and done["score"] < -2.0
    report = app.handle_command("/calibration")
    assert "web" in report
    again = app.handle_command(f"/decided {decision_id} sahi")
    assert "mili nahi" in again            # already resolved
