"""Self-upgrade loop (the owner's standing order): market watch
with dedupe + auto-learn, and propose_skill with its quality gate —
sandbox import, ops match, manifest validation, live-tool clash —
then AUTOMATIC promotion and a live load on the next start.
Hermetic: fake web search, tmp data dirs."""
import pytest

from jarvis import builtin_skills as bs
from jarvis.chat import ChatApp, _upgrade_ops
from jarvis.providers import FallbackChain, StubProvider


class CountingChain:
    name = "counting"

    def __init__(self):
        self.inner = FallbackChain([StubProvider()])
        self.calls = 0

    def complete(self, messages, tools=None):
        self.calls += 1
        return self.inner.complete(messages, tools)


GOOD_MANIFEST = {
    "name": "user.echo",
    "version": "1.0.0",
    "tools": [{
        "name": "echo_back", "op": "echo_back",
        "permission": "files.read",
        "schema": {"type": "object",
                   "properties": {"text": {"type": "string"}},
                   "additionalProperties": False},
    }],
}
GOOD_CODE = (
    "def echo_back(text=''):\n"
    "    return {'echo': text}\n"
    "OPS = {'echo_back': echo_back}\n"
)


@pytest.fixture()
def app(tmp_path):
    return ChatApp(tmp_path / "data", chain=CountingChain())


def _ops(app):
    return _upgrade_ops(app.parts.learned, app.data_dir, app.journal,
                        app.registry, app.allowances)


def test_watch_dedupes_and_learns(app, monkeypatch):
    monkeypatch.setattr(bs, "op_web_search", lambda q, limit=5: {
        "results": [{"title": "New Voice Model Zed",
                     "url": "https://z.example/zed", "snippet": ""}]})
    ops = _ops(app)
    first = ops["watch_for_upgrades"](topics=["tts voices"])
    assert first["count"] == 1
    second = ops["watch_for_upgrades"](topics=["tts voices"])
    assert second["count"] == 0            # already seen — not news
    hits = app.memory.recall("New Voice Model Zed")
    assert any("Zed" in hit["text"] for hit in hits)   # it learned it


def test_good_skill_passes_gate_and_promotes(app):
    out = _ops(app)["propose_skill"]("echo", GOOD_MANIFEST, GOOD_CODE)
    assert out["promoted"] is True
    assert (app.data_dir / "skills_live" / "echo"
            / "skill.py").exists()


def test_promoted_skill_is_live_on_next_start(tmp_path):
    data = tmp_path / "data"
    app1 = ChatApp(data, chain=CountingChain())
    _ops(app1)["propose_skill"]("echo", GOOD_MANIFEST, GOOD_CODE)
    app2 = ChatApp(data, chain=CountingChain())
    names = {t["name"]
             for t in app2.registry.advertised_tools(app2.allowances)}
    assert "echo_back" in names


def test_broken_code_is_rejected_at_import(app):
    out = _ops(app)["propose_skill"](
        "broken", GOOD_MANIFEST, "def nope(:\n")
    assert out["promoted"] is False and out["stage"] == "import"
    assert not (app.data_dir / "skills_live" / "broken").exists()


def test_ops_mismatch_is_rejected(app):
    code = "OPS = {'something_else': lambda: {}}\n"
    out = _ops(app)["propose_skill"]("mismatch", GOOD_MANIFEST, code)
    assert out["promoted"] is False and out["stage"] == "ops"


def test_tool_name_clash_is_rejected(app):
    manifest = {
        "name": "user.clash", "version": "1.0.0",
        "tools": [{"name": "time_now", "op": "time_now",
                   "permission": "clock.read",
                   "schema": {"type": "object", "properties": {},
                              "additionalProperties": False}}],
    }
    code = "OPS = {'time_now': lambda: {}}\n"
    out = _ops(app)["propose_skill"]("clash", manifest, code)
    assert out["promoted"] is False and out["stage"] == "clash"


def test_upgrade_report_tells_the_story(app):
    ops = _ops(app)
    ops["propose_skill"]("echo", GOOD_MANIFEST, GOOD_CODE)
    ops["propose_skill"]("broken", GOOD_MANIFEST, "def nope(:\n")
    report = ops["upgrade_report"]()
    assert report["promoted_skills"] == ["user.echo"]
    assert report["live_skill_folders"] == ["echo"]
    assert report["rejected"][-1]["stage"] == "import"


def test_upgrade_tools_are_advertised(app):
    names = {t["name"]
             for t in app.registry.advertised_tools(app.allowances)}
    assert {"watch_for_upgrades", "propose_skill",
            "upgrade_report"} <= names
