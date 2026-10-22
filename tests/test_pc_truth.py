"""PC ops must not claim what they did not do — the
2026-10-22 audit found volume/clipboard/kill reporting
success on PowerShell's silent failures, and a wildcard
kill path that could stop every process on the machine."""

import sys

import pytest

from jarvis import pc


pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="fake-runner tests")


class FakePS:
    """Script-aware fake PowerShell: answers each script by
    what it asks, from a tiny world model."""

    def __init__(self, volume=30, alive=True, clip=""):
        self.volume = volume
        self.alive = alive
        self.clip = clip
        self.scripts = []

    def __call__(self, script):
        self.scripts.append(script)
        if "Get-Clipboard" in script:
            return {"rc": 0, "stdout": self.clip, "stderr": ""}
        if "Set-Clipboard" in script:
            return {"rc": 0, "stdout": "", "stderr": ""}
        if "[AudioVol]::Set" in script:
            return {"rc": 0, "stdout": str(self.volume),
                    "stderr": ""}
        if "[AudioVol]::Get" in script:
            return {"rc": 0, "stdout": str(self.volume),
                    "stderr": ""}
        if "Stop-Process" in script:
            return {"rc": 0, "stdout": "", "stderr": ""}
        return {"rc": 0, "stdout": "", "stderr": ""}


def _win(monkeypatch):
    monkeypatch.setattr(pc.sys, "platform", "win32")


def test_kill_refuses_wildcards(monkeypatch):
    _win(monkeypatch)
    fake = FakePS()
    for target in ("*", "c*", "chrom?", "[a]ny"):
        out = pc.op_process_kill(target, runner=fake)
        assert "error" in out, target
    assert fake.scripts == []          # nothing ever ran


def test_kill_uses_exact_match_not_name_pattern(monkeypatch):
    _win(monkeypatch)
    fake = FakePS()
    out = pc.op_process_kill("chrome", runner=fake)
    assert out["ok"] is True and out["verified"] is True
    script = fake.scripts[0]
    assert "-ieq 'chrome'" in script
    assert "Get-Process -Name" not in script
    assert "-ErrorAction Stop" in script


def test_volume_set_readback_mismatch_is_a_failure(monkeypatch):
    _win(monkeypatch)
    fake = FakePS(volume=30)           # set 'succeeds', level stays
    out = pc.op_volume_set(80, runner=fake)
    assert out["ok"] is False and out["volume_now"] == 30


def test_volume_set_verified_when_readback_matches(monkeypatch):
    _win(monkeypatch)
    fake = FakePS(volume=80)
    out = pc.op_volume_set(80, runner=fake)
    assert out["ok"] is True and out["verified"] is True


def test_clipboard_write_verifies_by_reading_back(monkeypatch):
    _win(monkeypatch)
    fake = FakePS(clip="purana text")
    out = pc.op_clipboard_write("naya text", runner=fake)
    assert out["ok"] is False
    fake2 = FakePS(clip="naya text")
    out2 = pc.op_clipboard_write("naya text", runner=fake2)
    assert out2["ok"] is True and out2["verified"] is True


def test_clipboard_read_flags_truncation(monkeypatch):
    _win(monkeypatch)
    fake = FakePS(clip="x" * 2500)
    out = pc.op_clipboard_read(runner=fake)
    assert out["truncated"] is True
    assert len(out["clipboard"]) == 2000


def test_lock_pc_does_not_claim_verification(monkeypatch):
    _win(monkeypatch)
    out = pc.op_lock_pc(runner=FakePS())
    assert out["ok"] is True and out["verified"] is False
