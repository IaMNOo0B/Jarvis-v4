"""The app UI (fresh build, 2026-10-22): the golden-orb page
over the SAME ChatApp core. Hermetic: an ephemeral port and the
counting stub chain. The boot-first startup flow itself is
covered by test_ui_startup.py."""
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


def test_page_is_the_orb_design(ui):
    status, body = _get(ui + "/")
    assert status == 200
    assert "JARVIS" in body
    assert "/assets/jarvis-core.jpg" in body   # the owner's orb
    assert "orb-ring" in body
    assert "/api/chat" in body and "/api/boot" in body
    assert "Start ho raha hai" in body         # the boot face
    # The old HUD-canvas design is gone, by the owner's order.
    assert "orbFx" not in body and "ring-core" not in body


def test_orb_asset_is_served_as_jpeg(ui):
    with urllib.request.urlopen(ui + "/assets/jarvis-core.jpg",
                                timeout=5) as resp:
        assert resp.status == 200
        assert resp.headers.get_content_type() == "image/jpeg"
        data = resp.read()
    assert data[:3] == b"\xff\xd8\xff"         # real JPEG bytes
    assert len(data) > 10_000


def test_boot_endpoint_reports_ready_for_a_built_app(ui):
    status, body = _get(ui + "/api/boot")
    boot = json.loads(body)
    assert status == 200
    assert boot["state"] == "ready" and boot["error"] == ""


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
    assert state["boot"] == "ready"


def test_bad_post_is_an_honest_400(ui):
    req = urllib.request.Request(ui + "/api/chat", data=b"not json",
                                 method="POST")
    try:
        urllib.request.urlopen(req, timeout=5)
        raise AssertionError("expected HTTPError")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
