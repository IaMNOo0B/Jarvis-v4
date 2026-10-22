"""The three live provider defects from the owner's PC
/diagnose (2026-10-22), each proven through a REAL Brain
turn over a fake transport — not just unit pokes:

A. Gemini 400 'Function call is missing a thought_signature'
   — the signature must ride back on the round-2
   functionCall part, keyed by the provider's own call id.
B. Pollinations 400 ToolDescription — every advertised
   tool's OpenAI payload must carry a STRING description.
C. Ollama 400 'can't find closing }' — the round-2 payload
   must carry tool-call arguments as OBJECTS, tool results
   as role 'tool'.
"""

import json

from jarvis.chat import ChatApp
from jarvis.prompt import tool_views
from jarvis.providers import (FallbackChain, GeminiProvider,
                              OllamaProvider)


class Script:
    """Fake transport answering from a list of (status, body)
    steps, capturing every request body it receives."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.bodies = []

    def __call__(self, url, body, headers, timeout):
        self.bodies.append(json.loads(body.decode()))
        status, raw = self.steps.pop(0)
        return status, raw


def _gemini_body(parts):
    return json.dumps({
        "candidates": [{"content": {"role": "model",
                                     "parts": parts}}],
        "usageMetadata": {"promptTokenCount": 10,
                          "candidatesTokenCount": 4}}).encode()


# -- A + the functionResponse name fix --------------------------------

def test_gemini_thought_signature_rides_back(tmp_path):
    transport = Script([
        (200, _gemini_body([
            {"functionCall": {"name": "time_now", "args": {}},
             "thoughtSignature": "SIG-OWNER-TURN-1"}])),
        (200, _gemini_body([{"text": "Waqt bata diya, sir."}])),
    ])
    provider = GeminiProvider("AIzaTESTKEY",
                              models=["gemini-2.5-flash"],
                              transport=transport)
    app = ChatApp(tmp_path, chain=FallbackChain([provider]))
    reply = app.turn("waqt kya hai")
    assert "Waqt bata diya" in reply
    assert len(transport.bodies) == 2
    contents = transport.bodies[1]["contents"]
    call_parts = [p for c in contents for p in c["parts"]
                  if "functionCall" in p]
    assert len(call_parts) == 1
    assert call_parts[0]["functionCall"]["name"] == "time_now"
    assert call_parts[0]["thoughtSignature"] == \
        "SIG-OWNER-TURN-1"
    # The functionResponse is bound by the tool's real NAME
    # (the audit's round-2 bug: every response was named
    # the literal string "tool").
    resp_parts = [p for c in contents for p in c["parts"]
                  if "functionResponse" in p]
    assert len(resp_parts) == 1
    assert resp_parts[0]["functionResponse"]["name"] == \
        "time_now"


def test_signature_never_leaks_into_shared_messages(tmp_path):
    # The signature lives only in the Gemini payload — the
    # brain's own messages (what every OTHER provider would
    # receive on a fallback) must not contain it.
    transport = Script([
        (200, _gemini_body([
            {"functionCall": {"name": "time_now", "args": {}},
             "thoughtSignature": "SIG-KEEP-PRIVATE"}])),
        (200, _gemini_body([{"text": "done"}])),
    ])
    provider = GeminiProvider("AIzaTESTKEY",
                              models=["gemini-2.5-flash"],
                              transport=transport)
    captured = {}

    class SpyChain:
        name = "spy"

        def complete(self, messages, tools=None):
            captured["messages"] = messages
            return provider.complete(messages, tools)

    app = ChatApp(tmp_path, chain=SpyChain())
    app.turn("waqt kya hai")
    blob = json.dumps(captured["messages"], default=str)
    assert "SIG-KEEP-PRIVATE" not in blob
    assert "thoughtSignature" not in blob


# -- B ---------------------------------------------------------------

def test_every_advertised_tool_has_a_string_description(tmp_path):
    app = ChatApp(tmp_path)
    schemas = app.registry.advertised_tools(app.allowances)
    assert schemas
    for view in tool_views(schemas):
        fn = view.openai()["function"]
        assert isinstance(fn.get("description"), str), \
            f"{view.name} ships without a string description"
        # And under every diet rung of the ladder.
        for kwargs in ({"desc_cap": 40}, {"compact": True},
                       {"names_only": True}):
            fn = view.openai(**kwargs)["function"]
            assert isinstance(fn.get("description"), str)


# -- C ---------------------------------------------------------------

def _ollama_body(message):
    return json.dumps({"model": "qwen2.5:7b-instruct-q4_K_M",
                       "message": message,
                       "prompt_eval_count": 10,
                       "eval_count": 4}).encode()


def test_ollama_tool_turn_round_two_shape(tmp_path):
    transport = Script([
        (200, _ollama_body({
            "role": "assistant", "content": "",
            "tool_calls": [{"function": {
                "name": "time_now", "arguments": {}}}]})),
        (200, _ollama_body({
            "role": "assistant",
            "content": "Waqt bata diya, sir."})),
    ])
    provider = OllamaProvider(transport=transport)
    app = ChatApp(tmp_path, chain=FallbackChain([provider]))
    reply = app.turn("waqt kya hai")
    assert "Waqt bata diya" in reply
    assert len(transport.bodies) == 2
    messages = transport.bodies[1]["messages"]
    assistant = [m for m in messages
                 if m.get("role") == "assistant"
                 and m.get("tool_calls")]
    assert len(assistant) == 1
    fn = assistant[0]["tool_calls"][0]["function"]
    assert fn["name"] == "time_now"
    # THE fix: arguments travel as an OBJECT, never the
    # brain's JSON string that Ollama's template choked on.
    assert isinstance(fn["arguments"], dict)
    results = [m for m in messages if m.get("role") == "tool"]
    assert len(results) == 1
    assert results[0].get("tool_name") == "time_now"
    # The whole payload is valid Ollama-shaped JSON data.
    json.dumps(transport.bodies[1])


def test_ollama_arguments_object_survives_nested_args():
    from jarvis.providers import _ollama_payload
    messages = [
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "call_1_0", "type": "function",
                         "function": {
                             "name": "write_file",
                             "arguments": json.dumps(
                                 {"path": "a/b.txt",
                                  "opts": {"x": [1, 2]}})}}]},
        {"role": "tool", "tool_call_id": "call_1_0",
         "name": "write_file", "content": "ok"},
    ]
    payload = _ollama_payload("m", messages, None)
    args = payload["messages"][0]["tool_calls"][0] \
        ["function"]["arguments"]
    assert args == {"path": "a/b.txt", "opts": {"x": [1, 2]}}


# -- D: wake-model resolution -----------------------------------------

def test_wake_resolution_finds_bundled_and_app_models(tmp_path,
                                                     monkeypatch):
    from jarvis import wake
    app_models = tmp_path / "models"
    app_models.mkdir()
    (app_models / "hey_jarvis_v0.1.onnx").write_bytes(b"onnx")
    assert wake.resolve_wake_model(
        "", models_dir=app_models).endswith("hey_jarvis_v0.1.onnx")
    # An explicit existing .onnx path wins outright.
    custom = tmp_path / "hello_jarvis.onnx"
    custom.write_bytes(b"onnx")
    assert wake.resolve_wake_model(str(custom)) == str(custom)
    # Nothing anywhere -> None, and the status names the search.
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(wake, "_package_models_dir", lambda: None)
    monkeypatch.setattr(wake, "_app_models_dir", lambda: empty)
    assert wake.resolve_wake_model("", models_dir=empty) is None
    status = wake.wake_model_status("", models_dir=empty)
    assert status["found"] is False
    assert str(empty) in status["searched"]


def test_preflight_models_check_uses_detector_resolution(tmp_path):
    from jarvis import preflight
    models = tmp_path / "models"
    models.mkdir()
    (models / "hey_jarvis.onnx").write_bytes(b"onnx")
    result = preflight.check_models_dir(models)
    assert result["ok"] is True
    assert "hey_jarvis.onnx" in result["detail"]
    missing = preflight.check_models_dir(tmp_path / "nope-not-here")
    # The app models dir is still empty on this VM and the
    # package is not installed: the check must be an honest
    # miss that names where it looked — never the old false
    # 'folder' framing.
    assert missing["ok"] is False
    assert "searched" in missing["detail"]
