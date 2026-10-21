"""The app UI: same ChatApp core behind a local web page shaped
like the owner's reference photo (ring, states, chat). Hermetic:
an ephemeral port and the counting stub chain."""
import json
import threading
import urllib.request

import pytest

from jarvis.chat import ChatApp
from jarvis.providers import FallbackChain, StubProvider
from jarvis.ui import build_server


class CountingChain:
    name = "counting"

    def __init__(self):
        self.inner = FallbackChain([StubProvider()])
        self.calls = 0

    def complete(self, messages, tools=None):
        self.calls += 1
        return self.inner.complete(messages, tools)


@pytest.fixture()
def ui(tmp_path):
    app = ChatApp(tmp_path / "data", chain=CountingChain())
    server = build_server(app, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, resp.read().decode()


def _post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode())


def test_page_is_the_ring_design(ui):
    status, body = _get(ui + "/")
    assert status == 200
    assert "JARVIS" in body and "ring-core" in body
    assert "Researching" in body       # the state words he asked for
    assert "/api/chat" in body


def test_chat_round_trip_through_http(ui):
    status, data = _post(ui + "/api/chat",
                         {"text": "what time is it right now?"})
    assert status == 200
    assert "reply" in data and ":" in data["reply"]   # a real time


def test_command_works_through_ui(ui):
    status, data = _post(ui + "/api/chat", {"text": "/keys"})
    assert status == 200
    assert "provider keys stored" in data["reply"]


def test_state_endpoint(ui):
    status, body = _get(ui + "/api/state")
    state = json.loads(body)
    assert status == 200
    assert state["tools"] >= 15        # the whole toolbox is wired in


def test_bad_post_is_an_honest_400(ui):
    req = urllib.request.Request(ui + "/api/chat", data=b"not json",
                                 method="POST")
    try:
        urllib.request.urlopen(req, timeout=5)
        raise AssertionError("expected HTTPError")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
