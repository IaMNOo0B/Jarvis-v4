"""Tests for jarvis.config — atomic writes, typed getters, journal hook."""

import json
import os

import pytest

from jarvis.config import Config, ConfigError, load
from jarvis.journal import Journal


def test_load_contract_and_defaults(tmp_path):
    cfg = load(tmp_path)  # spine contract: load(data_dir) -> Config
    assert isinstance(cfg, Config)
    assert cfg.get("brain", "model") is None
    assert cfg.get("brain", "model", "qwen2.5:7b") == "qwen2.5:7b"
    assert cfg.getint("limits", "rounds", 3) == 3
    assert cfg.getbool("voice", "enabled", True) is True
    assert cfg.getfloat("voice", "rate", 1.0) == 1.0
    assert cfg.sections() == []


def test_set_persists_across_loads(tmp_path):
    cfg = load(tmp_path)
    cfg.set("brain", "model", "qwen2.5:7b")
    cfg.set("limits", "rounds", 3)
    cfg.set("voice", "enabled", True)
    again = load(tmp_path)
    assert again.get("brain", "model") == "qwen2.5:7b"
    assert again.getint("limits", "rounds") == 3
    assert again.getbool("voice", "enabled") is True
    assert again.sections() == ["brain", "limits", "voice"]
    assert again.keys("brain") == ["model"]
    on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert on_disk["limits"]["rounds"] == 3


def test_typed_getters_convert_and_fall_back(tmp_path):
    cfg = load(tmp_path)
    cfg.set("s", "i", 7)
    cfg.set("s", "i_str", "42")
    cfg.set("s", "f_int", 2.0)
    cfg.set("s", "bad", "not-a-number")
    cfg.set("s", "truthy", "yes")
    cfg.set("s", "falsy", "off")
    cfg.set("s", "one", 1)
    cfg.set("s", "pi", "3.5")
    assert cfg.getint("s", "i") == 7
    assert cfg.getint("s", "i_str") == 42
    assert cfg.getint("s", "f_int") == 2
    assert cfg.getint("s", "bad", 9) == 9
    assert cfg.getint("s", "truthy", 9) == 9  # bool-ish strings are not ints
    assert cfg.getbool("s", "truthy") is True
    assert cfg.getbool("s", "falsy") is False
    assert cfg.getbool("s", "one") is True
    assert cfg.getbool("s", "bad", False) is False
    assert cfg.getfloat("s", "pi") == 3.5
    assert cfg.getfloat("s", "bad", 0.25) == 0.25
    assert cfg.getbool("s", "i", "dflt") == "dflt"  # 7 is not a bool source


def test_failed_write_leaves_original_untouched(tmp_path, monkeypatch):
    cfg = load(tmp_path)
    cfg.set("brain", "model", "original")
    before = (tmp_path / "config.json").read_bytes()

    def boom(src, dst):
        raise OSError("simulated crash mid-replace")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        cfg.set("brain", "model", "torn")
    monkeypatch.undo()
    assert (tmp_path / "config.json").read_bytes() == before
    assert cfg.get("brain", "model") == "original"
    leftovers = [p.name for p in tmp_path.iterdir() if "tmp" in p.name]
    assert leftovers == []


def test_corrupt_config_is_loud_not_silent(tmp_path):
    (tmp_path / "config.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        load(tmp_path)


def test_journal_hook_records_change_without_value(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    cfg = load(tmp_path, journal=journal)
    cfg.set("brain", "api_style", "DISTINCTIVE-VALUE-123")
    recs = [r for r in journal.replay() if r["kind"] == "config.changed"]
    assert len(recs) == 1
    assert recs[0]["payload"] == {"section": "brain", "key": "api_style"}
    raw = (tmp_path / "journal.jsonl").read_text(encoding="utf-8")
    assert "DISTINCTIVE-VALUE-123" not in raw


def test_attach_journal_after_load(tmp_path):
    cfg = load(tmp_path)
    cfg.set("a", "b", 1)  # no journal yet — must not crash
    journal = Journal(str(tmp_path / "j.jsonl"))
    cfg.attach_journal(journal)
    cfg.set("a", "c", 2)
    assert journal.seq == 1


def test_no_journal_no_problem_and_as_dict_is_a_copy(tmp_path):
    cfg = load(tmp_path)
    cfg.set("x", "y", {"nested": [1, 2]})
    snapshot = cfg.as_dict()
    snapshot["x"]["y"]["nested"].append(99)
    assert cfg.get("x", "y") == {"nested": [1, 2]}


def test_set_rejects_bad_inputs(tmp_path):
    cfg = load(tmp_path)
    with pytest.raises(ConfigError):
        cfg.set("", "k", 1)
    with pytest.raises(ConfigError):
        cfg.set("s", "", 1)
    with pytest.raises(ConfigError):
        cfg.set("s", "k", object())  # not JSON-serializable
