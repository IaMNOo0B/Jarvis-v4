"""Step 17 — provider goodput accounting.

Goodput = fraction of requests meeting ALL bars at once (call ok AND
quality ok AND latency within SLO). The scripted sets below are chosen so
every expected fraction is computable by hand — and so that MEAN latency
would give the wrong verdict (the study's trap, 17-08).
"""

import pytest

from jarvis.goodput import GoodputLedger, RequestRecord


def _ollama_ledger():
    led = GoodputLedger()
    # 5 requests, SLO 1000 ms. Good: 100ms ok, 200ms ok, 400ms ok.
    # NOT good: 9000ms (ok but way over SLO), 300ms (fast but bad quality).
    led.record("ollama", 100, True, True, ts=1000.0)
    led.record("ollama", 200, True, True, ts=1001.0)
    led.record("ollama", 9000, True, True, ts=1002.0)
    led.record("ollama", 300, True, False, ts=1003.0)
    led.record("ollama", 400, True, True, ts=1004.0)
    return led


def test_goodput_fraction_math():
    led = _ollama_ledger()
    # 3 of 5 meet ALL bars — even though 4/5 are "ok" and mean latency
    # (2000ms) would flatter nobody, the fraction is the honest 0.6.
    assert led.goodput("ollama", 1000) == pytest.approx(0.6)


def test_failed_call_never_counts():
    led = GoodputLedger()
    led.record("gemini", 50, False, False, ts=1.0)   # fast, but the call failed
    led.record("gemini", 60, True, True, ts=2.0)
    assert led.goodput("gemini", 1000) == pytest.approx(0.5)


def test_no_data_is_none_not_zero():
    led = _ollama_ledger()
    assert led.goodput("pollinations", 1000) is None
    assert GoodputLedger().weakest_provider(1000) is None


def test_report_percentiles_and_rates():
    led = _ollama_ledger()
    stats = led.report(1000)["ollama"]
    assert stats["n"] == 5 and stats["good"] == 3
    assert stats["goodput"] == pytest.approx(0.6)
    assert stats["ok_rate"] == pytest.approx(1.0)
    assert stats["quality_rate"] == pytest.approx(0.8)
    # sorted latencies: 100, 200, 300, 400, 9000 (nearest-rank)
    assert stats["p50_ms"] == 300
    assert stats["p90_ms"] == 9000
    assert stats["p99_ms"] == 9000


def test_weakest_provider_ranking():
    led = _ollama_ledger()  # ollama goodput 0.6 at SLO 1000
    for i in range(4):
        led.record("gemini", 100 + i, True, True, ts=2000.0 + i)
    assert led.goodput("gemini", 1000) == pytest.approx(1.0)
    assert led.weakest_provider(1000) == "ollama"


def test_last_n_window_is_per_provider():
    led = GoodputLedger()
    for i in range(3):
        led.record("omniroute", 5000, True, True, ts=float(i))       # slow trio
    for i in range(2):
        led.record("omniroute", 100, True, True, ts=10.0 + i)        # fast pair
    assert led.goodput("omniroute", 1000) == pytest.approx(0.4)      # all five
    assert led.goodput("omniroute", 1000, last_n=2) == pytest.approx(1.0)


def test_time_window():
    led = _ollama_ledger()  # ts 1000..1004
    # Window ending at 1004 covering the last 2 seconds: ts >= 1002.5 →
    # records at 1003 (bad quality) and 1004 (good) → 1/2.
    assert led.goodput("ollama", 1000, window_s=1.5, now=1004.0) == \
        pytest.approx(0.5)


def test_jsonl_round_trip(tmp_path):
    led = _ollama_ledger()
    path = led.save_jsonl(tmp_path / "gp.jsonl")
    back = GoodputLedger.load_jsonl(path)
    assert len(back) == 5
    assert back.goodput("ollama", 1000) == pytest.approx(0.6)
    assert back.to_rows() == led.to_rows()


def test_record_defaults_timestamp_and_type():
    rec = RequestRecord("x", 12, True, True, ts=5.0)
    assert rec.meets_slo(12) and not rec.meets_slo(11)
