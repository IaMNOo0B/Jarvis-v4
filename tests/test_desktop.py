"""Desktop lane laws: stale refs are refused, actions are
verified, and the live backend never pretends on non-Windows."""
import pytest

from jarvis.desktop import (DesktopLane, ElementRef, FakeBackend,
                            WindowsBackend)


def _screen():
    return FakeBackend([
        ElementRef("ok-btn", 1, "OK", "button", (400, 500, 80, 30),
                   "tok-ok"),
        ElementRef("name-box", 1, "Name", "edit", (100, 200, 300, 30),
                   "tok-name"),
    ])


def test_fresh_ref_acts():
    lane = DesktopLane(_screen())
    snap, found = lane.ground("OK")
    assert found and lane.act(found[0], "click")["ok"]


def test_stale_ref_is_refused_and_nothing_is_clicked():
    backend = _screen()
    lane = DesktopLane(backend)
    _, found = lane.ground("OK")
    backend.move()  # screen re-rendered: old refs are ghosts now
    result = lane.act(found[0], "click")
    assert result["ok"] is False and result["error"] == "stale_ref"
    assert backend.actions == []


def test_verified_action_retries_once_on_a_fresh_ref():
    backend = _screen()
    lane = DesktopLane(backend)
    _, found = lane.ground("OK")
    backend.move()  # ref goes stale between ground and act
    result = lane.act_verified(found[0], "click")
    assert result["ok"] is True and result["verified"] is True
    assert backend.actions == [("ok-btn", "click")]


def test_verified_action_fails_honestly_when_expectation_misses():
    backend = _screen()
    lane = DesktopLane(backend)
    _, found = lane.ground("OK")
    result = lane.act_verified(found[0], "click",
                               expect_gone="OK")
    assert result["ok"] is False
    assert result["error"] == "verify_failed"


class _ClosingBackend(FakeBackend):
    """Clicking OK closes the dialog: the control vanishes."""

    def act(self, ref, action):
        result = super().act(ref, action)
        if ref.ref_id == "ok-btn" and action == "click":
            self.remove("ok-btn")
        return result


def test_verified_action_confirms_a_dialog_really_closed():
    backend = _ClosingBackend([
        ElementRef("ok-btn", 1, "OK", "button", (400, 500, 80, 30),
                   "tok-ok"),
    ])
    lane = DesktopLane(backend)
    _, found = lane.ground("OK")
    result = lane.act_verified(found[0], "click", expect_gone="OK")
    assert result["ok"] is True and result["verified"] is True


def test_windows_backend_is_honest_off_windows():
    lane = DesktopLane(WindowsBackend())
    result = lane.act(
        ElementRef("x", 1, "X", "button", (0, 0, 10, 10)), "click")
    assert result["ok"] is False and result["error"] == "windows_only"
