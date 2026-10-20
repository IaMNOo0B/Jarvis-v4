"""Tests for jarvis.keys — fingerprints, presets, storage, NO network."""

import socket

import pytest

from jarvis.keys import (
    PRESETS,
    KeysError,
    NotAnLLMKeyError,
    UnknownKeyError,
    add_key,
    fingerprint_detail,
    fingerprint_key,
    get_keys,
    list_keys,
)
from jarvis.secrets import SecretStore

EXPECTED_PROVIDERS = {
    "groq", "openrouter", "nvidia", "mistral", "cerebras", "cohere",
    "gemini", "deepseek", "xai", "together", "fireworks", "perplexity",
    "chutes",
}

AQ_LONG = "AQ." + "A" * 51    # 54 chars — the real new AI Studio shape
AQ_BOUNDARY = "AQ." + "A" * 37  # exactly 40 — gemini
AQ_SHORT = "AQ." + "A" * 36   # 39 — unknown
AIZA_CLASSIC = "AIza" + "x" * 35  # 39 chars — classic Google shape


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------
def test_presets_are_the_13():
    assert set(PRESETS) == EXPECTED_PROVIDERS
    for name, preset in PRESETS.items():
        assert preset["base_url"].startswith("https://"), name
        assert preset["model"], name


def test_gemini_preset_is_the_flash_hotfix():
    # Regression guard: the free tier has no Pro — flash is the default.
    assert PRESETS["gemini"]["model"] == "gemini-2.5-flash"
    assert "generativelanguage.googleapis.com" in PRESETS["gemini"]["base_url"]


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,provider",
    [
        ("sk-or-v1-abcdef", "openrouter"),
        ("gsk_abcdef", "groq"),
        ("nvapi-abcdef", "nvidia"),
        ("xai-abcdef", "xai"),
        ("pplx-abcdef", "perplexity"),
        ("csk-abcdef", "cerebras"),
        (AIZA_CLASSIC, "gemini"),
        (AQ_LONG, "gemini"),
        (AQ_BOUNDARY, "gemini"),
        ("ghp_abcdef", "github"),
        ("github_pat_abcdef", "github"),
        ("  sk-or-v1-padded  ", "openrouter"),  # whitespace is stripped
    ],
)
def test_fingerprint_hits(raw, provider):
    assert fingerprint_key(raw) == provider


@pytest.mark.parametrize(
    "raw",
    [
        AQ_SHORT,            # AQ. but too short — unknown, never guessed
        "sk-bareambiguous",  # shared by openai/deepseek/together
        "AIza-short",        # AIza prefix but implausibly short
        "",
        "   ",
        "random-garbage",
        None,
        12345,
    ],
)
def test_fingerprint_unknowns(raw):
    assert fingerprint_key(raw) is None
    assert fingerprint_detail(raw)["kind"] == "unknown"


def test_github_is_identified_but_not_llm():
    detail = fingerprint_detail("ghp_something")
    assert detail == {"provider": "github", "kind": "not-llm"}
    assert fingerprint_detail(AQ_LONG)["kind"] == "llm"


# ---------------------------------------------------------------------------
# add_key / list_keys over a real SecretStore
# ---------------------------------------------------------------------------
def test_add_and_list_round_trip(tmp_path):
    store = SecretStore(tmp_path)
    assert add_key(store, "sk-or-v1-AAA") == "openrouter"
    assert add_key(store, "sk-or-v1-BBB") == "openrouter"
    assert add_key(store, "gsk_CCC") == "groq"
    assert list_keys(store) == {"openrouter": 2, "groq": 1}
    assert get_keys(store, "openrouter") == ["sk-or-v1-AAA", "sk-or-v1-BBB"]
    assert get_keys(store, "groq") == ["gsk_CCC"]
    # values live only behind get_keys — names stay clean
    assert all("AAA" not in n and "BBB" not in n for n in store.names())


def test_add_key_aq_gemini(tmp_path):
    store = SecretStore(tmp_path)
    assert add_key(store, AQ_LONG) == "gemini"
    assert list_keys(store) == {"gemini": 1}


def test_explicit_provider_for_ambiguous_shapes(tmp_path):
    store = SecretStore(tmp_path)
    assert add_key(store, "sk-bare", provider="deepseek") == "deepseek"
    assert add_key(store, "some-mistral-shape", provider="mistral") == "mistral"
    assert list_keys(store) == {"deepseek": 1, "mistral": 1}


def test_unknown_key_refused_and_nothing_stored(tmp_path):
    store = SecretStore(tmp_path)
    raw = "totally-unrecognized-DISTINCTIVE"
    with pytest.raises(UnknownKeyError) as ei:
        add_key(store, raw)
    assert raw not in str(ei.value)  # errors never echo the key
    assert store.names() == []


def test_github_key_refused_both_ways(tmp_path):
    store = SecretStore(tmp_path)
    with pytest.raises(NotAnLLMKeyError):
        add_key(store, "ghp_leakguard")
    with pytest.raises(NotAnLLMKeyError):
        add_key(store, "whatever", provider="github")
    assert store.names() == []


def test_bad_explicit_provider(tmp_path):
    store = SecretStore(tmp_path)
    with pytest.raises(KeysError):
        add_key(store, "whatever", provider="not-a-provider")
    assert store.names() == []


# ---------------------------------------------------------------------------
# The privacy rule: /key performs NO network access, enforced by sabotage
# ---------------------------------------------------------------------------
def test_no_network_anywhere(tmp_path, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("network use in keys.py is forbidden")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    monkeypatch.setattr(socket, "getaddrinfo", explode)

    store = SecretStore(tmp_path)
    assert fingerprint_key(AQ_LONG) == "gemini"
    assert add_key(store, "sk-or-v1-NETLESS") == "openrouter"
    assert list_keys(store) == {"openrouter": 1}
