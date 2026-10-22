"""Batch-6 audit regressions: chain fall-through, transport
conversion, Gemini ladder, key rotation, framed-result cap,
PromptBudgetError retry, keys dedupe, router honesty,
providers overrides (2026-10-22)."""

import http.client
import json

import pytest

from jarvis import keys as keys_mod
from jarvis import providers, router
from jarvis.brain import Brain
from jarvis.chat import ChatApp
from jarvis.prompt import PromptBudgetError
from jarvis.providers import (FallbackChain, GeminiProvider,
                              ProviderDown, ProviderReply)


class _BoomProvider:
    name = "boom"

    def complete(self, messages, tools=None):
        raise http.client.IncompleteRead(b"partial", 3)


class _OkProvider:
    name = "ok"

    def complete(self, messages, tools=None):
        return ProviderReply(text="jawab", tool_calls=[],
                             model="m", usage={}, provider="ok")


def test_chain_falls_through_on_any_exception():
    chain = FallbackChain([_BoomProvider(), _OkProvider()])
    reply = chain.complete([{"role": "user", "content": "hi"}])
    assert reply.text == "jawab" and reply.provider == "ok"


def test_urllib_transport_converts_incomplete_read(monkeypatch):
    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            raise http.client.IncompleteRead(b"x", 1)

    monkeypatch.setattr(providers.urllib.request, "urlopen",
                        lambda req, timeout=None: Resp())
    with pytest.raises(ProviderDown):
        providers.urllib_transport("https://x.test/v1", b"{}",
                                   {}, 5.0)


def _gemini_transport(failures):
    """failures: {model: exception-or-status} — a scripted
    ladder. Returns (transport, calls)."""
    calls = []

    def transport(url, body, headers, timeout):
        model = url.split("/models/")[1].split(":")[0]
        calls.append((model, headers.get("x-goog-api-key")))
        failure = failures.get(model)
        if isinstance(failure, Exception):
            raise failure
        if isinstance(failure, int):
            return failure, b'{"error": "x"}'
        return 200, json.dumps({"candidates": [{
            "content": {"parts": [{"text": f"{model} bola"}]},
            "finishReason": "STOP"}]}).encode()

    return transport, calls


def test_gemini_ladder_advances_on_timeout():
    transport, _calls = _gemini_transport(
        {"gemini-2.5-flash": providers.ProviderTimeout("slow")})
    gem = GeminiProvider("k1", transport=transport)
    reply = gem.complete([{"role": "user", "content": "hi"}])
    # The first rung timed out; the NEXT rung answered.
    assert reply.text == "gemini-2.5-flash-lite bola"


def test_gemini_rotates_key_before_resting_rung():
    transport, calls = _gemini_transport(
        {"gemini-2.5-flash": None})

    def picky(url, body, headers, timeout):
        calls.append((url, headers.get("x-goog-api-key")))
        if headers.get("x-goog-api-key") == "dead-key":
            return 401, b'{"error": "bad key"}'
        return 200, json.dumps({"candidates": [{
            "content": {"parts": [{"text": "zinda key"}]},
            "finishReason": "STOP"}]}).encode()

    gem = GeminiProvider("dead-key", transport=picky,
                         api_keys=["dead-key", "live-key"])
    reply = gem.complete([{"role": "user", "content": "hi"}])
    assert reply.text == "zinda key"
    assert gem.api_key == "live-key"
    # Same rung retried with the second key — no rung rested.
    assert [k for _u, k in calls] == ["dead-key", "live-key"]


def test_gemini_no_candidates_advances_rung():
    def transport(url, body, headers, timeout):
        model = url.split("/models/")[1].split(":")[0]
        if model == "gemini-2.5-flash":
            return 200, b'{"promptFeedback": {"blockReason": "X"}}'
        return 200, json.dumps({"candidates": [{
            "content": {"parts": [{"text": "dusra rung"}]},
            "finishReason": "STOP"}]}).encode()

    gem = GeminiProvider("k", transport=transport)
    reply = gem.complete([{"role": "user", "content": "hi"}])
    assert reply.text == "dusra rung"


class _DictStore:
    def __init__(self):
        self.data = {}

    def names(self):
        return list(self.data)

    def get(self, name):
        return self.data.get(name)

    def set(self, name, value):
        self.data[name] = value


def test_add_key_dedupes_identical_refiling():
    store = _DictStore()
    key = "gsk_" + "a" * 30
    keys_mod.add_key(store, key, provider="groq")
    keys_mod.add_key(store, key, provider="groq")
    assert keys_mod.list_keys(store) == {"groq": 1}
    other = "gsk_" + "b" * 30
    keys_mod.add_key(store, other, provider="groq")
    assert keys_mod.list_keys(store) == {"groq": 2}


def test_framed_tool_result_is_capped():
    brain = Brain(_OkProvider(), [], lambda n, a: {},
                  journal=None, memory=None)
    framed = brain._frame_tool_result("web_fetch",
                                      {"body": "x" * 20000})
    assert len(framed) < 6000
    assert "kata" in framed


def test_turn_survives_prompt_budget_error(tmp_path):
    app = ChatApp(tmp_path)
    calls = {"n": 0}

    class RaisingBrain:
        def respond(self, history, text, correction=False,
                    rules_annex=""):
            calls["n"] += 1
            if history:
                raise PromptBudgetError("too big")
            raise PromptBudgetError("still too big")

    app._brain_for = lambda text: RaisingBrain()
    reply = app.turn("koi baat")
    assert calls["n"] == 2          # retried once, empty history
    assert "budget" in reply        # honest answer, no raise


def test_router_domains_match_schema_selection():
    text = "ek reminder banao aur calendar check karo"
    domains = router.routed_domains(text)
    schemas = [{"name": "t", "skill": s}
               for s in router.DOMAIN_STEMS]
    subset = router.route_schemas(text, schemas)
    if subset is not None:
        chosen = {s["skill"] for s in subset}
        for d in domains:
            assert d in chosen


def test_route_schemas_never_returns_empty_list():
    schemas = [{"name": "x", "skill": "core.unknown"}]
    assert router.route_schemas("reminder banao", schemas) is None


def test_preset_override_from_providers_entry():
    cfg = {"providers": [{"name": "groq",
                          "model": "custom-model",
                          "timeout": 9}]}
    chain = providers.build_chain(cfg, {"groq": "k"})
    groq = chain[0]
    assert groq.model == "custom-model"
    assert groq.timeout == 9.0
    # The named entry is an override, not ALSO a custom
    # provider duplicate.
    assert [p.name for p in chain].count("groq") == 1
