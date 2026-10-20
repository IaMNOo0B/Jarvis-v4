"""EvalBrainProvider — the eval harness's deterministic router.

Pins: it routes the banked everyday requests to the right tools,
never calls a tool that was not offered, completes the loop after a
tool result, and — the honesty pin — is NEVER in the production
chain.
"""

from jarvis.eval_brain import EvalBrainProvider
from jarvis.providers import build_chain

TOOLS = [{"name": "open_app"}, {"name": "web_search"},
         {"name": "reminder_set"}, {"name": "time_now"}]


def _turn(text, tools=TOOLS):
    return EvalBrainProvider().complete(
        [{"role": "user", "content": text}], tools=tools)


def test_open_chrome_routes_to_open_app():
    reply = _turn("open chrome")
    assert [c.name for c in reply.tool_calls] == ["open_app"]
    assert reply.tool_calls[0].arguments["name"] == "chrome"


def test_open_notepad_routes():
    reply = _turn("open notepad")
    assert reply.tool_calls[0].arguments["name"] == "notepad"


def test_web_search_routes_with_query():
    reply = _turn("web search about muse ai")
    assert reply.tool_calls[0].name == "web_search"
    assert "muse" in reply.tool_calls[0].arguments["query"]


def test_hinglish_reminder_routes_with_topic_and_minutes():
    reply = _turn("10 minute me mujhe paani peene ka reminder laga do")
    call = reply.tool_calls[0]
    assert call.name == "reminder_set"
    assert call.arguments["in_minutes"] == 10
    assert "paani" in call.arguments["text"]


def test_no_tool_call_when_tool_not_offered():
    reply = _turn("open chrome", tools=[{"name": "time_now"}])
    assert reply.tool_calls == []
    assert len(reply.text) > 10  # substantive fallback, not silence


def test_tool_result_completes_the_loop():
    reply = EvalBrainProvider().complete(
        [{"role": "user", "content": "open chrome"},
         {"role": "assistant", "content": ""},
         {"role": "tool", "content": "{\"ok\": true}"}],
        tools=TOOLS)
    assert reply.tool_calls == []
    assert len(reply.text) > 15


def test_greeting_is_substantive():
    reply = _turn("how are u?")
    assert reply.tool_calls == []
    assert len(reply.text) >= 8


def test_eval_brain_is_never_in_the_production_chain():
    class _Cfg:
        def get(self, section, key, default=None):
            return default

    class _Secrets:
        def get(self, name):
            return None

        def names(self):
            return []

    names = [p.name for p in build_chain(_Cfg(), _Secrets(), None)]
    assert "eval-brain" not in names
