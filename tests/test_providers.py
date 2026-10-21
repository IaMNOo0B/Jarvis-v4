"""Spine S2 — providers + fallback chain.

Every HTTP call goes through a scripted FakeTransport: zero network.
The timeout assertions are the regression test for the v2 clobber bug
(the chain's 5 s default once overwrote the compat provider's 60 s, and
the Ollama seat's 120 s, killing keyed calls before the first token).
"""

import io
import json
import socket
import urllib.error

import pytest

from jarvis.goodput import GoodputLedger
from jarvis.providers import (
    ChainExhaustedError, CompatProvider, FallbackChain, OllamaProvider,
    PollinationsProvider, ProviderDown, ProviderHTTPError, ProviderTimeout,
    StubProvider, build_chain, build_fallback_chain, urllib_transport,
)

MSGS = [{"role": "user", "content": "hello"}]


def compat_body(text="compat answer", model="m-compat", tool_calls=None):
    msg = {"role": "assistant", "content": text}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return json.dumps({"model": model, "choices": [{"message": msg}],
                       "usage": {"prompt_tokens": 3,
                                 "completion_tokens": 4}}).encode()


def ollama_body(text="ollama answer", model="qwen2.5:7b-instruct-q4_K_M",
                tool_calls=None):
    msg = {"role": "assistant", "content": text}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return json.dumps({"model": model, "message": msg,
                       "prompt_eval_count": 5,
                       "eval_count": 6}).encode()


class FakeTransport:
    """Routes by URL substring. Each route value is (status, bytes) or
    an exception instance to raise. Records every call verbatim."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []  # (url, body_bytes, headers, timeout)

    def __call__(self, url, body, headers, timeout):
        self.calls.append((url, body, dict(headers), timeout))
        for needle, result in self.routes.items():
            if needle in url:
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"FakeTransport: no route for {url}")

    def timeouts_for(self, needle):
        return [c[3] for c in self.calls if needle in c[0]]


# ---------------------------------------------------------------------------
# Chain construction: order + skip-missing-key
# ---------------------------------------------------------------------------

def test_chain_order_and_skip_missing_keys():
    chain = build_chain({}, {"groq": "k-groq"})
    assert [p.name for p in chain] == ["groq", "ollama", "pollinations",
                                       "stub"]

def test_chain_follows_preset_order():
    # Chain order = the PRESETS table order (owned by jarvis.keys):
    # openrouter first (the owner's proven provider; smartest-first
    # doctrine), then groq, ..., gemini, deepseek, ...
    chain = build_chain({}, {"groq": "k", "openrouter": "k",
                             "deepseek": "k", "gemini": "k"})
    names = [p.name for p in chain]
    assert names[:4] == ["openrouter", "groq", "gemini", "deepseek"]
    assert names[4:] == ["ollama", "pollinations", "stub"]

def test_chain_no_keys_still_has_local_ladder():
    chain = build_chain({}, {})
    assert [p.name for p in chain] == ["ollama", "pollinations", "stub"]

def test_chain_key_may_be_a_rotation_list():
    chain = build_chain({}, {"groq": ["first-key", "second-key"]})
    groq = chain[0]
    assert groq.name == "groq" and groq.api_key == "first-key"

def test_chain_custom_provider_from_config():
    cfg = {"providers": [{"name": "mybox", "base_url": "http://x.test/v1",
                          "model": "mm", "timeout": 33}]}
    chain = build_chain(cfg, {"mybox": "k"})
    names = [p.name for p in chain]
    assert names[0] == "mybox" and chain[0].timeout == 33.0
    # custom provider without a key is skipped like any other
    chain2 = build_chain(cfg, {})
    assert "mybox" not in [p.name for p in chain2]


# ---------------------------------------------------------------------------
# The v2 clobber-bug regression: the PROVIDER's timeout reaches transport
# ---------------------------------------------------------------------------

def test_compat_timeout_is_the_providers_own_60():
    ft = FakeTransport({"openrouter": (200, compat_body())})
    chain = build_fallback_chain({}, {"openrouter": "k"}, GoodputLedger(),
                                 transport=ft)
    reply = chain.complete(MSGS)
    assert reply.provider == "openrouter"
    assert ft.timeouts_for("openrouter") == [60.0]

def test_ollama_timeout_is_120_after_compat_404():
    ft = FakeTransport({"openrouter": (404, b"model not found"),
                        "11434": (200, ollama_body())})
    chain = build_fallback_chain({}, {"openrouter": "k"}, GoodputLedger(),
                                 transport=ft)
    reply = chain.complete(MSGS)
    assert reply.provider == "ollama"
    assert ft.timeouts_for("openrouter") == [60.0]
    assert ft.timeouts_for("11434") == [120.0]

def test_ollama_provider_timeout_attribute_default():
    assert OllamaProvider().timeout == 120.0
    assert StubProvider().name == "stub"


# ---------------------------------------------------------------------------
# Fall-through behaviour + goodput records
# ---------------------------------------------------------------------------

def _rows(ledger):
    return [(r["provider"], r["ok"], r["quality_ok"])
            for r in ledger.to_rows()]


def test_404_falls_through_and_every_attempt_is_recorded():
    led = GoodputLedger()
    ft = FakeTransport({"openrouter": (404, b"nope"),
                        "11434": (200, ollama_body())})
    chain = build_fallback_chain({}, {"openrouter": "k"}, led, transport=ft)
    reply = chain.complete(MSGS)
    assert reply.text == "ollama answer"
    assert _rows(led) == [("openrouter", False, False),
                          ("ollama", True, True)]

def test_empty_reply_falls_through_as_soft_failure():
    led = GoodputLedger()
    ft = FakeTransport({"openrouter": (200, compat_body(text="")),
                        "11434": (200, ollama_body())})
    chain = build_fallback_chain({}, {"openrouter": "k"}, led, transport=ft)
    reply = chain.complete(MSGS)
    assert reply.provider == "ollama"
    # empty: the call "succeeded" but quality failed — recorded so
    assert _rows(led) == [("openrouter", True, False),
                          ("ollama", True, True)]

def test_provider_timeout_falls_through():
    led = GoodputLedger()
    ft = FakeTransport({"openrouter": ProviderTimeout("slow"),
                        "11434": (200, ollama_body())})
    chain = build_fallback_chain({}, {"openrouter": "k"}, led, transport=ft)
    assert chain.complete(MSGS).provider == "ollama"
    assert _rows(led)[0] == ("openrouter", False, False)

def test_stub_answers_when_everything_is_down():
    led = GoodputLedger()
    down = ProviderDown("connection refused")
    ft = FakeTransport({"openrouter": down, "11434": down,
                        "pollinations": down})
    chain = build_fallback_chain({}, {"openrouter": "k"}, led, transport=ft)
    reply = chain.complete(MSGS)
    assert reply.provider == "stub"
    assert "stub" in reply.text.lower()
    # one ledger record per attempt, stub included
    assert _rows(led) == [("openrouter", False, False),
                          ("ollama", False, False),
                          ("pollinations", False, False),
                          ("stub", True, True)]

def test_chain_exhausted_without_stub_raises_with_attempts():
    led = GoodputLedger()
    ft = FakeTransport({"11434": (503, b"busy")})
    chain = FallbackChain([OllamaProvider(transport=ft)], ledger=led)
    with pytest.raises(ChainExhaustedError) as ei:
        chain.complete(MSGS)
    assert ei.value.attempts == ["ollama"]
    assert _rows(led) == [("ollama", False, False)]

def test_chain_deadline_stops_new_attempts():
    ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0])
    clock = lambda: next(ticks, 100.0)
    ft = FakeTransport({"11434": (500, b"x"), "pollinations": (200,
                        compat_body())})
    chain = FallbackChain(
        [OllamaProvider(transport=ft), PollinationsProvider(transport=ft)],
        ledger=GoodputLedger(), total_deadline_s=10.0, clock=clock)
    with pytest.raises(ChainExhaustedError):
        chain.complete(MSGS)
    # pollinations was never attempted — the deadline had already blown
    assert ft.timeouts_for("pollinations") == []


# ---------------------------------------------------------------------------
# Provider protocol details
# ---------------------------------------------------------------------------

def test_compat_tool_calls_and_usage_mapping():
    tc = [{"id": "c1", "type": "function",
           "function": {"name": "open_app",
                        "arguments": "{\"app\": \"chrome\"}"}}]
    ft = FakeTransport({"groq": (200, compat_body(text="", tool_calls=tc))})
    p = CompatProvider("groq", "https://api.groq.com/openai/v1", "m",
                       api_key="k", transport=ft)
    reply = p.complete(MSGS, tools=[{"type": "function"}])
    assert reply.tool_calls[0].name == "open_app"
    assert reply.tool_calls[0].arguments == {"app": "chrome"}
    assert reply.usage == {"prompt_tokens": 3, "completion_tokens": 4}
    assert not reply.is_empty()  # tool calls make an empty-text reply real

def test_compat_sends_bearer_key_and_own_timeout():
    ft = FakeTransport({"groq": (200, compat_body())})
    p = CompatProvider("groq", "https://api.groq.com/openai/v1", "m",
                       api_key="sekret", timeout=42.0, transport=ft)
    p.complete(MSGS)
    url, body, headers, timeout = ft.calls[0]
    assert headers["Authorization"] == "Bearer sekret"
    assert timeout == 42.0

def test_ollama_tool_calls_mapping():
    tc = [{"function": {"name": "set_reminder",
                        "arguments": {"text": "call mom", "in_min": 5}}}]
    ft = FakeTransport({"11434": (200, ollama_body(text="",
                                                   tool_calls=tc))})
    p = OllamaProvider(transport=ft)
    reply = p.complete(MSGS)
    assert reply.tool_calls[0].name == "set_reminder"
    assert reply.tool_calls[0].arguments == {"text": "call mom",
                                             "in_min": 5}
    assert reply.usage == {"prompt_tokens": 5, "completion_tokens": 6}

def test_http_error_carries_status():
    ft = FakeTransport({"groq": (429, b"rate limited")})
    p = CompatProvider("groq", "https://api.groq.com/openai/v1", "m",
                       api_key="k", transport=ft)
    with pytest.raises(ProviderHTTPError) as ei:
        p.complete(MSGS)
    assert ei.value.status == 429

def test_stub_is_deterministic():
    s = StubProvider()
    a = s.complete(MSGS)
    b = s.complete(MSGS)
    assert a.text == b.text and "hello" in a.text


# ---------------------------------------------------------------------------
# Default urllib transport: error mapping without any network
# ---------------------------------------------------------------------------

def test_urllib_transport_timeout_maps_to_provider_timeout(monkeypatch):
    def boom(req, timeout=None):
        raise socket.timeout("timed out")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(ProviderTimeout):
        # Non-loopback: loopback traffic bypasses proxies AND the
        # patched urlopen by design (it uses a direct opener).
        urllib_transport("http://192.0.2.1:1/x", b"{}", {}, 3.0)

def test_urllib_transport_refused_maps_to_provider_down(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(ProviderDown):
        urllib_transport("http://192.0.2.1:1/x", b"{}", {}, 3.0)

def test_urllib_transport_http_error_returns_status(monkeypatch):
    err = urllib.error.HTTPError("http://x", 404, "nf", {},
                                 io.BytesIO(b"model not found"))
    def boom(req, timeout=None):
        raise err
    monkeypatch.setattr("urllib.request.urlopen", boom)
    status, payload = urllib_transport("http://x", b"{}", {}, 3.0)
    assert status == 404 and payload == b"model not found"
