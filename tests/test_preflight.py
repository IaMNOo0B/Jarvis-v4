"""Tests for jarvis.preflight (Step 00 — route-aware preflight)."""

import sys
from pathlib import Path

import pytest

from jarvis import preflight
from jarvis.preflight import (
    LEVEL_OPTIONAL,
    LEVEL_REQUIRED,
    check_data_dir,
    check_ollama,
    check_package,
    check_python_version,
    exit_code,
    render_checklist,
    render_explain,
    run_checks,
)


@pytest.fixture()
def good_root(tmp_path):
    """A minimal healthy JARVIS folder: package + writable data + models."""
    (tmp_path / "jarvis").mkdir()
    (tmp_path / "jarvis" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "models").mkdir()
    return tmp_path


def _by_name(results, fragment):
    for r in results:
        if fragment.lower() in r["name"].lower():
            return r
    raise AssertionError(f"no check matching {fragment!r} in {[r['name'] for r in results]}")


class TestPythonVersion:
    def test_current_python_passes(self):
        r = check_python_version()
        assert r["ok"] is True
        assert r["level"] == LEVEL_REQUIRED
        assert r["next_fix"] == ""

    def test_old_python_fails_with_fix(self):
        r = check_python_version((3, 8, 10))
        assert r["ok"] is False
        assert "python.org" in r["next_fix"]

    def test_boundary_3_11_passes(self):
        assert check_python_version((3, 11, 0))["ok"] is True

    def test_result_shape(self):
        r = check_python_version()
        for key in ("name", "level", "ok", "detail", "next_fix"):
            assert key in r


class TestPackageCheck:
    def test_present(self, good_root):
        assert check_package(good_root)["ok"] is True

    def test_missing(self, tmp_path):
        r = check_package(tmp_path)
        assert r["ok"] is False
        assert r["level"] == LEVEL_REQUIRED
        assert r["next_fix"]  # exact fix text present


class TestDataDir:
    def test_present_and_writable(self, tmp_path):
        d = tmp_path / "data"
        d.mkdir()
        r = check_data_dir(d)
        assert r["ok"] is True
        assert not list(d.iterdir())  # probe file was cleaned up

    def test_missing_folder_is_created_and_passes(self, tmp_path):
        # A fresh GitHub ZIP has no data/ folder — JARVIS makes its
        # own instead of blocking the owner (real failure, 2026-10-21).
        target = tmp_path / "data"
        r = check_data_dir(target)
        assert r["ok"] is True
        assert target.is_dir()

    def test_path_is_a_file_fails(self, tmp_path):
        f = tmp_path / "data"
        f.write_text("not a folder", encoding="utf-8")
        r = check_data_dir(f)
        assert r["ok"] is False
        assert r["next_fix"]

    def test_writable_probe_on_missing_dir_is_false(self, tmp_path):
        assert preflight._probe_writable(tmp_path / "nope") is False


class TestOllamaOptional:
    def test_probe_up(self):
        r = check_ollama(probe=lambda: (True, "running"))
        assert r["ok"] is True
        assert r["level"] == LEVEL_OPTIONAL

    def test_probe_down_has_fix_but_stays_optional(self):
        r = check_ollama(probe=lambda: (False, "not answering"))
        assert r["ok"] is False
        assert r["level"] == LEVEL_OPTIONAL
        assert r["next_fix"]

    def test_broken_probe_never_crashes(self):
        def boom():
            raise RuntimeError("kaboom")

        r = check_ollama(probe=boom)
        assert r["ok"] is False


class TestRunChecksAndExitCode:
    def test_all_good_exits_zero(self, good_root):
        results = run_checks(
            root=good_root,
            ollama_probe=lambda: (True, "up"),
            spec_finder=lambda module: True,
        )
        assert exit_code(results) == 0
        assert all(r["ok"] for r in results)

    def test_optional_failures_do_not_block(self, good_root):
        results = run_checks(
            root=good_root,
            ollama_probe=lambda: (False, "down"),
            spec_finder=lambda module: False,
        )
        failed = [r for r in results if not r["ok"]]
        assert failed, "expected the injected optional failures"
        assert all(r["level"] == LEVEL_OPTIONAL for r in failed)
        assert exit_code(results) == 0

    def test_missing_data_dir_is_created_not_blocking(self, tmp_path):
        (tmp_path / "jarvis").mkdir()
        (tmp_path / "jarvis" / "__init__.py").write_text("", encoding="utf-8")
        results = run_checks(
            root=tmp_path,
            ollama_probe=lambda: (True, "up"),
            spec_finder=lambda module: True,
        )
        assert exit_code(results) == 0
        data_check = _by_name(results, "Data folder")
        assert data_check["ok"] is True
        assert (tmp_path / "data").is_dir()

    def test_old_python_blocks(self, good_root):
        results = run_checks(
            root=good_root,
            ollama_probe=lambda: (True, "up"),
            spec_finder=lambda module: True,
            python_version=(3, 9, 0),
        )
        assert exit_code(results) == 1


class TestCliAndRendering:
    def test_main_ready(self, good_root, monkeypatch, capsys):
        monkeypatch.setattr(
            preflight, "_default_ollama_probe", lambda: (True, "up")
        )
        monkeypatch.setattr(preflight, "_default_spec_finder", lambda m: True)
        code = preflight.main(["--root", str(good_root)])
        out = capsys.readouterr().out
        assert code == 0
        assert "READY" in out

    def test_main_not_ready_shows_next(self, tmp_path, capsys):
        code = preflight.main(["--root", str(tmp_path)])
        out = capsys.readouterr().out
        assert code == 1
        assert "NOT READY" in out
        assert "Next:" in out

    def test_main_explain_is_plain_language(self, tmp_path, capsys):
        code = preflight.main(["--explain", "--root", str(tmp_path)])
        out = capsys.readouterr().out
        assert code == 1
        assert "Why it matters:" in out
        assert "What to do:" in out

    def test_main_json(self, good_root, monkeypatch, capsys):
        import json

        monkeypatch.setattr(
            preflight, "_default_ollama_probe", lambda: (True, "up")
        )
        monkeypatch.setattr(preflight, "_default_spec_finder", lambda m: True)
        code = preflight.main(["--json", "--root", str(good_root)])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["summary"]["ready"] is True
        assert payload["summary"]["total"] == len(payload["checks"])

    def test_renderers_cover_every_check(self, good_root):
        results = run_checks(
            root=good_root,
            ollama_probe=lambda: (False, "down"),
            spec_finder=lambda module: False,
        )
        checklist = render_checklist(results)
        explain = render_explain(results)
        for r in results:
            assert r["name"] in checklist
            assert r["name"] in explain
