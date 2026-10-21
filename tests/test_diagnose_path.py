"""The evidence path — when a brain dies on the owner's PC, its
reason must survive in the ledger and be readable back through a
brain-free /diagnose (owner cannot run terminal diagnostics;
2026-10-21 Ollama mystery). Plus the loopback-proxy bypass."""
import io
import json

import pytest

from jarvis.goodput import GoodputLedger
from jarvis.providers import (FallbackChain, ProviderDown, ProviderReply,
                              _is_loopback, urllib_transport)


class DeadProvider:
    name = "ollama"

    def complete(self, messages, tools=None):
        raise ProviderDown("unreachable: [Errno 61] Connection refused")


class EmptyProvider:
    name = "pollinations"

    def complete(self, messages, tools=None):
        return ProviderReply(text="", tool_calls=[], model="m",
                             usage={}, provider=self.name)


class StubLike:
    name = "stub"

    def complete(self, messages, tools=None):
        return ProviderReply(text="stub here", tool_calls=[], model="s",
                             usage={}, provider=self.name)


def test_failure_reasons_land_in_the_ledger():
    ledger = GoodputLedger()
    chain = FallbackChain([DeadProvider(), EmptyProvider(), StubLike()],
                          ledger=ledger)
    reply = chain.complete([{"role": "user", "content": "hi"}])
    assert reply.text == "stub here"
    rows = {r["provider"]: r for r in ledger.to_rows()}
    assert "Connection refused" in rows["ollama"]["error"]
    assert rows["pollinations"]["error"] == "empty reply"


def test_ledger_rows_roundtrip_keeps_error(tmp_path):
    ledger = GoodputLedger()
    ledger.record("ollama", 12.0, False, False, error="HTTP 404: model")
    path = ledger.save_jsonl(tmp_path / "g.jsonl")
    back = GoodputLedger.load_jsonl(path)
    assert back.to_rows()[0]["error"] == "HTTP 404: model"


def test_loopback_detection():
    assert _is_loopback("http://localhost:11434/api/chat")
    assert _is_loopback("http://127.0.0.1:11434/api/chat")
    assert _is_loopback("http://[::1]:11434/api/chat")
    assert not _is_loopback("https://text.pollinations.ai/openai")


def test_loopback_transport_never_uses_a_proxy(monkeypatch):
    used = {}

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    class FakeOpener:
        def open(self, req, timeout=None):
            used["proxy_handlers"] = "captured"
            return FakeResp()

    def fake_build_opener(*handlers):
        used["handlers"] = handlers
        return FakeOpener()

    monkeypatch.setattr("urllib.request.build_opener", fake_build_opener)
    status, raw = urllib_transport(
        "http://localhost:11434/api/chat", b"{}", {}, 5.0)
    assert status == 200
    handlers = used["handlers"]
    assert any(type(h).__name__ == "ProxyHandler" and not h.proxies
               for h in handlers)


def test_diagnose_command_names_the_error(tmp_path):
    from jarvis.chat import ChatApp
    from jarvis.providers import FallbackChain, StubProvider

    class CountingChain:
        name = "counting"

        def __init__(self):
            self.inner = FallbackChain([StubProvider()])

        def complete(self, messages, tools=None):
            return self.inner.complete(messages, tools)

    data = tmp_path / "data"
    data.mkdir(parents=True)
    rows = [{"provider": "ollama", "latency_ms": 5.0, "ok": False,
             "quality_ok": False, "ts": 1.0,
             "error": "unreachable: timed out"}] * 3
    (data / "goodput.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    app = ChatApp(data, chain=CountingChain())
    reply = app.handle_command("/diagnose")
    assert "ollama" in reply
    assert "timed out" in reply
