"""Build identity stamp: the running build names itself, so a stale
folder can never be mistaken for a fresh one. The stamp rides both
/api/state shapes (booting and ready) and the page status line."""
import json
import threading
import urllib.request

import pytest

from jarvis.chat import ChatApp
from jarvis.providers import FallbackChain, StubProvider
from jarvis.ui import BootState, build_server
from jarvis.version import BUILD_STAMP


class _Chain:
    name = "counting"

    def __init__(self):
        self.inner = FallbackChain([StubProvider()])

    def complete(self, messages, tools=None):
        return self.inner.complete(messages, tools)


@pytest.fixture()
def ui(tmp_path):
    app = ChatApp(tmp_path / "data", chain=_Chain())
    server = build_server(app, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, resp.read().decode()


def test_stamp_is_a_short_tag():
    assert BUILD_STAMP and len(BUILD_STAMP) <= 40
    assert " " not in BUILD_STAMP


def test_ready_state_carries_the_stamp(ui):
    status, body = _get(ui + "/api/state")
    assert status == 200
    assert json.loads(body)["build"] == BUILD_STAMP


def test_booting_state_carries_the_stamp(tmp_path):
    boot = BootState(tmp_path / "data")  # never marked ready
    server = build_server(boot=boot, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _get(
            f"http://127.0.0.1:{server.server_port}/api/state")
        assert status == 200
        assert json.loads(body)["build"] == BUILD_STAMP
    finally:
        server.shutdown()


def test_page_status_line_renders_the_stamp(ui):
    status, body = _get(ui + "/")
    assert status == 200
    assert "build ' + s.build" in body  # JS appends it when present
