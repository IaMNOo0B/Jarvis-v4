"""Tests for jarvis.spend — spend-velocity trip (Step 15, P15-L13)."""

import pytest

from jarvis.spend import SpendError, SpendGuard


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_spend_under_limit_flows():
    g = SpendGuard(velocity_limit=50.0, clock=FakeClock())
    assert g.allow(30.0)
    assert g.record(30.0) is True
    assert g.record(15.0) is True
    assert g.tripped is False
    assert g.status()["window_total"] == 45.0


def test_trip_fires_exactly_at_breach_and_latches():
    clock = FakeClock()
    g = SpendGuard(velocity_limit=50.0, clock=clock)
    assert g.record(30.0) is True
    assert g.allow(20.0) is True       # 30+20 = 50, exactly at the cap
    assert g.allow(20.01) is False     # one paisa over: refused by check
    assert g.tripped is False          # a mere check never trips
    assert g.record(25.0) is False     # 30+25 breaches: trip + refuse
    assert g.tripped is True
    assert g.record(1.0) is False      # latched: everything refused
    assert g.allow(1.0) is False

    clock.advance(3600)                # an hour passes...
    assert g.tripped is True           # ...the latch does NOT forgive
    assert g.allow(1.0) is False

    g.reset()                          # only the owner's explicit reset
    assert g.tripped is False
    assert g.allow(1.0) is True


def test_window_ages_out_old_spend():
    clock = FakeClock()
    g = SpendGuard(velocity_limit=50.0, window_seconds=600, clock=clock)
    assert g.record(45.0) is True
    assert g.allow(10.0) is False      # window still holds the 45
    clock.advance(601)                 # outside the rolling window now
    assert g.allow(10.0) is True
    assert g.record(10.0) is True
    assert g.tripped is False


def test_daily_cap_blocks_without_latching():
    clock = FakeClock()
    g = SpendGuard(velocity_limit=1000.0, daily_cap=100.0, clock=clock)
    assert g.record(80.0) is True
    assert g.record(30.0) is False     # daily cap refuses
    assert g.tripped is False          # ...but does not latch a trip
    assert g.record(20.0) is True      # 80+20 = 100, exactly at cap
    clock.advance(86400)               # next day
    assert g.record(30.0) is True      # daily budget is fresh again


def test_only_owner_may_reset():
    g = SpendGuard(velocity_limit=10.0, clock=FakeClock())
    assert g.record(11.0) is False
    assert g.tripped is True
    with pytest.raises(SpendError):
        g.reset(by="the-model-itself")
    assert g.tripped is True


def test_trip_survives_restart_via_state_file(tmp_path):
    path = str(tmp_path / "spend.json")
    clock = FakeClock()
    g = SpendGuard(velocity_limit=10.0, state_path=path, clock=clock)
    assert g.record(11.0) is False
    assert g.tripped is True
    g2 = SpendGuard(velocity_limit=10.0, state_path=path, clock=clock)
    assert g2.tripped is True          # restart does not forgive a trip
    g2.reset()
    g3 = SpendGuard(velocity_limit=10.0, state_path=path, clock=clock)
    assert g3.tripped is False


def test_invalid_amounts_rejected():
    g = SpendGuard(velocity_limit=10.0, clock=FakeClock())
    with pytest.raises(SpendError):
        g.record(0.0)
    with pytest.raises(SpendError):
        g.record(-5.0)
    with pytest.raises(SpendError):
        SpendGuard(velocity_limit=0.0)
