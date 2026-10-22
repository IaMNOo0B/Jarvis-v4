"""Startup hardening (owner's PC, 2026-10-22): the new build
would not open at all and no evidence survived anywhere the
owner could reach. Now: background services start AFTER the
server exists and can never veto it, and any startup crash
leaves its traceback in startup-error.txt (data dir + app
folder) for the owner to paste back."""

import jarvis.chat as chat_mod
import jarvis.ui as ui


def test_main_records_a_chatapp_crash_and_returns_1(tmp_path,
                                                   monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("his real data broke construction")

    monkeypatch.setattr(ui, "ChatApp", boom)
    monkeypatch.chdir(tmp_path)
    code = ui.main(["--data-dir", str(tmp_path), "--no-open"])
    assert code == 1
    saved = (tmp_path / "startup-error.txt").read_text(
        encoding="utf-8")
    assert "his real data broke construction" in saved
    assert "Traceback" in saved


def test_background_services_cannot_veto_the_ui(tmp_path,
                                                monkeypatch):
    class FakeApp:
        data_dir = tmp_path

    def raiser(app):
        raise RuntimeError("telegram blocked startup")

    monkeypatch.setattr(ui, "_maybe_auto_watch", raiser)
    monkeypatch.setattr(chat_mod, "start_scheduler", raiser)
    monkeypatch.setattr(chat_mod, "start_telegram", raiser)
    monkeypatch.chdir(tmp_path)
    # Must not raise even when every starter raises.
    ui._start_background_services(FakeApp())
    saved = (tmp_path / "startup-error.txt").read_text(
        encoding="utf-8")
    # One timestamped record per failed starter (three).
    assert saved.count("--- 20") == 3
    assert "telegram blocked startup" in saved


def test_background_services_run_the_happy_path(tmp_path,
                                                monkeypatch):
    seen = []

    class FakeApp:
        data_dir = tmp_path

    monkeypatch.setattr(ui, "_maybe_auto_watch", seen.append)
    monkeypatch.setattr(chat_mod, "start_scheduler", seen.append)
    monkeypatch.setattr(chat_mod, "start_telegram", seen.append)
    ui._start_background_services(FakeApp())
    assert len(seen) == 3
