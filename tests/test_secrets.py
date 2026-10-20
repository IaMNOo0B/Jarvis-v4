"""Tests for jarvis.secrets — round-trip, no leaks, perms, one warning."""

import os
import stat

import pytest

from jarvis.secrets import SecretError, SecretStore

VALUE_A = "sk-or-DISTINCTIVE-AAAA"
VALUE_B = "gsk_DISTINCTIVE_BBBB"


def test_round_trip_and_missing(tmp_path):
    store = SecretStore(tmp_path)
    assert store.get("llm:openrouter:1") is None
    store.set("llm:openrouter:1", VALUE_A)
    assert store.get("llm:openrouter:1") == VALUE_A
    store.set("llm:openrouter:1", VALUE_B)  # overwrite
    assert store.get("llm:openrouter:1") == VALUE_B
    # survives a fresh store over the same dir
    assert SecretStore(tmp_path).get("llm:openrouter:1") == VALUE_B


def test_backend_name_is_honest(tmp_path):
    store = SecretStore(tmp_path)
    assert store.backend_name == "local-file (less secure)"


def test_file_permissions_are_0600(tmp_path):
    store = SecretStore(tmp_path)
    store.set("a", VALUE_A)
    mode = stat.S_IMODE(os.stat(tmp_path / "secrets.json").st_mode)
    assert mode == 0o600


def test_names_and_repr_never_leak_values(tmp_path):
    store = SecretStore(tmp_path)
    store.set("llm:openrouter:1", VALUE_A)
    store.set("llm:groq:1", VALUE_B)
    assert store.names() == ["llm:groq:1", "llm:openrouter:1"]
    for surface in (repr(store), str(store), " ".join(store.names())):
        assert VALUE_A not in surface
        assert VALUE_B not in surface
    assert "count=2" in repr(store)
    assert len(store) == 2
    assert "llm:groq:1" in store


def test_warning_printed_once_per_file(tmp_path, capsys):
    SecretStore(tmp_path)
    SecretStore(tmp_path)
    SecretStore(tmp_path)
    err = capsys.readouterr().err
    assert err.count("less secure") == 1


def test_delete(tmp_path):
    store = SecretStore(tmp_path)
    store.set("a", VALUE_A)
    assert store.delete("a") is True
    assert store.get("a") is None
    assert store.delete("a") is False


def test_errors_never_carry_values(tmp_path):
    store = SecretStore(tmp_path)
    with pytest.raises(SecretError):
        store.set("", VALUE_A)
    with pytest.raises(SecretError):
        store.set("name", "")
    # corrupt the file with a distinctive marker, then read
    (tmp_path / "secrets.json").write_text(
        '{"broken": "LEAKME-MARKER", ', encoding="utf-8"
    )
    with pytest.raises(SecretError) as ei:
        store.get("anything")
    assert "LEAKME-MARKER" not in str(ei.value)
