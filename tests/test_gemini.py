"""Native Gemini provider — the Mark-LVI mechanism (owner's order,
2026-10-21): generateContent dialect, x-goog-api-key header, the
system prompt as systemInstruction, tools as functionDeclarations,
and a model ladder that rests failed rungs by error class."""
import json

import pytest

from jarvis.providers import (GeminiProvider, build_chain)

MSGS = [{"role": "system", "content": "You are JARVIS."},
        {"role": "user", "content": "open chrome"}]
TOOLS = [{"name": "open_app", "skill": "core.pc", "permission": "pc",
          "schema": {"type": "object",
                     "properties": {"name": {"type": "string"}},
                     "required": ["name"]}}]


def ok_body(text="Done, sir.", calls=None):
    parts = [{"text": text}]
    for name, args in (calls or []):
        parts.append({"functionCall": {"name": name, "args": args}})
    return json.dumps({
        "candidates": [{"content": {"role": "model", "parts": parts}}],
        "usageMetadata": {"promptTokenCount": 10,
                          "candidatesTokenCount": 4}}).encode()


class Recorder:
    """Fake transport: records (url, payload, headers), answers by
    a per-model script {model: (status, body)}."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    def __call__(self, url, body, headers, timeout):
        payload = json.loads(body.decode())
        self.calls.append((url, payload, headers))
        model = url.split("/models/")[1].split(":")[0]
        status, raw = self.script[model]
        return status, raw


def test_request_is_native_gemini_shape():
    rec = Recorder({"gemini-2.5-flash": (200, ok_body())})
    p = GeminiProvider("AIzaTESTKEY", transport=rec)
    reply = p.complete(MSGS, tools=TOOLS)
    assert reply.text == "Done, sir."
    url, payload, headers = rec.calls[0]
    assert url.endswith("/v1beta/models/gemini-2.5-flash:generateContent")
    assert headers["x-goog-api-key"] == "AIzaTESTKEY"
    assert payload["systemInstruction"]["parts"][0]["text"] == \
        "You are JARVIS."
    assert payload["contents"][0] == {
        "role": "user", "parts": [{"text": "open chrome"}]}
    decl = payload["tools"][0]["functionDeclarations"][0]
    assert decl["name"] == "open_app"
    assert decl["parameters"]["type"] == "OBJECT"
    assert decl["parameters"]["properties"]["name"]["type"] == "STRING"
    assert decl["parameters"]["required"] == ["name"]


def test_function_call_reply_is_parsed():
    rec = Recorder({"gemini-2.5-flash": (
        200, ok_body("", calls=[("open_app", {"name": "chrome"})]))})
    p = GeminiProvider("k", transport=rec)
    reply = p.complete(MSGS, tools=TOOLS)
    assert reply.tool_calls[0].name == "open_app"
    assert reply.tool_calls[0].arguments == {"name": "chrome"}
    assert reply.provider == "gemini"


def test_ladder_steps_down_on_429_and_rests_the_rung():
    rec = Recorder({
        "gemini-2.5-flash": (429, b'{"error":{"message":"quota"}}'),
        "gemini-2.5-flash-lite": (200, ok_body("lite answered"))})
    now = [1000.0]
    p = GeminiProvider("k", transport=rec, clock=lambda: now[0])
    reply = p.complete(MSGS)
    assert reply.text == "lite answered"
    # The rested rung is skipped while its 300 s rest runs.
    rec.calls.clear()
    p.complete(MSGS)
    assert rec.calls[0][0].count("flash-lite") == 1
    assert all("models/gemini-2.5-flash:" not in c[0]
               for c in rec.calls)


def test_no_candidates_is_an_honest_failure():
    rec = Recorder({"gemini-2.5-flash": (
        200, b'{"promptFeedback":{"blockReason":"SAFETY"}}')})
    p = GeminiProvider("k", models=["gemini-2.5-flash"], transport=rec)
    with pytest.raises(Exception) as exc:
        p.complete(MSGS)
    assert "SAFETY" in str(exc.value)


def test_chain_prefers_native_gemini_when_key_filed():
    chain = build_chain(config={}, secrets={"gemini": "AIzaTESTKEY"})
    assert isinstance(chain[0], GeminiProvider)
    chain = build_chain(config={}, secrets={})
    assert not any(isinstance(p, GeminiProvider) for p in chain)
