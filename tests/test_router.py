"""Route-then-specialize (the Kodee lesson): the brain sees
the domain it needs, not the whole registry — measured in the
actual system prompt a turn sends. No signal = the full set:
routing may focus the brain, never starve it."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.chat import ChatApp
from jarvis.router import route_schemas, routed_domains


class CaptureChain:
    name = "capture"

    def __init__(self):
        self.system = ""

    def complete(self, messages, tools=None):
        for m in messages:
            if m.get("role") == "system":
                self.system = str(m.get("content", ""))
                break
        return {"content": "theek hai, sir"}


def test_router_picks_domains_and_keeps_core():
    schemas = [
        {"name": "browser_open", "skill": "core.browser"},
        {"name": "email_send", "skill": "core.mail"},
        {"name": "time_now", "skill": "core.time"},
        {"name": "recall", "skill": "core.memory"},
    ]
    subset = route_schemas("browser me site khol do", schemas)
    names = {s["name"] for s in subset}
    assert "browser_open" in names          # the called domain
    assert "time_now" in names and "recall" in names  # core rides
    assert "email_send" not in names         # the rest is masked


def test_router_silence_means_full_set():
    schemas = [{"name": "email_send", "skill": "core.mail"}]
    assert route_schemas("kuch acha sa batao", schemas) is None
    assert routed_domains("kuch acha sa batao") == []


def test_turn_prompt_is_specialized(tmp_path):
    chain = CaptureChain()
    app = ChatApp(tmp_path, chain=chain)
    app.turn("browser me ja kar website khol do please")
    assert "browser_open" in chain.system
    assert "email_send" not in chain.system
    assert "time_now" in chain.system       # core always rides


def test_vague_turn_gets_everything(tmp_path):
    chain = CaptureChain()
    app = ChatApp(tmp_path, chain=chain)
    app.turn("kuch batao apne baare me")
    assert "email_send" in chain.system
    assert "browser_open" in chain.system
