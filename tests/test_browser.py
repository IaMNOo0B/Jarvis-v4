"""Chromium control (CDP): a minimal stdlib WebSocket client +
CDP session, verified against an in-process protocol fake (the
build VM has no browser; the live-Chrome pass is PC-side), plus the
honest no-Chrome path. No live browser is touched here."""
import base64
import hashlib
import json
import socket
import struct
import threading

import pytest

from jarvis import browser_cdp
from jarvis.chat import ChatApp
from jarvis.providers import FallbackChain, StubProvider


class CountingChain:
    name = "counting"

    def __init__(self):
        self.inner = FallbackChain([StubProvider()])
        self.calls = 0

    def complete(self, messages, tools=None):
        self.calls += 1
        return self.inner.complete(messages, tools)


def _fake_cdp_server():
    """One-shot fake: WS handshake, read one masked frame, answer
    with a CDP-shaped result. Returns (port, received_messages)."""
    received = []
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve():
        conn, _ = server.accept()
        request = b""
        while b"\r\n\r\n" not in request:
            request += conn.recv(4096)
        key = next(line.split(":", 1)[1].strip()
                   for line in request.decode().split("\r\n")
                   if line.lower().startswith("sec-websocket-key"))
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
            .encode()).digest()).decode()
        conn.sendall((
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        head = conn.recv(2)
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", conn.recv(2))[0]
        mask = conn.recv(4)
        payload = b""
        while len(payload) < length:
            payload += conn.recv(length - len(payload))
        plain = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        message = json.loads(plain.decode())
        received.append(message)
        answer = json.dumps({
            "id": message["id"],
            "result": {"result": {"type": "string",
                                  "value": "fake-page-text"}}}
        ).encode()
        conn.sendall(bytes([0x81, len(answer)]) + answer)
        conn.close()
        server.close()

    threading.Thread(target=serve, daemon=True).start()
    return port, received


def test_ws_handshake_and_cdp_round_trip():
    port, received = _fake_cdp_server()
    session = browser_cdp.CDPSession(f"ws://127.0.0.1:{port}/page/1")
    value = session.evaluate("document.body.innerText")
    assert value == "fake-page-text"
    assert received[0]["method"] == "Runtime.evaluate"
    assert received[0]["params"]["returnByValue"] is True


def test_browser_launch_without_chrome_is_honest(monkeypatch):
    monkeypatch.setattr(browser_cdp, "_find_chrome", lambda: None)
    monkeypatch.setattr(browser_cdp, "_cdp_http",
                        lambda *a, **k: (_ for _ in ()).throw(
                            ConnectionError("no")))
    out = browser_cdp.op_browser_launch()
    assert "error" in out and "mila hi nahi" in out["error"]


def test_browser_status_when_never_launched(monkeypatch):
    monkeypatch.setattr(browser_cdp, "_ws", None)
    out = browser_cdp.op_browser_status()
    assert out["connected"] is False


def test_browser_tools_are_advertised(tmp_path):
    app = ChatApp(tmp_path / "data", chain=CountingChain())
    names = {t["name"]
             for t in app.registry.advertised_tools(app.allowances)}
    assert {"browser_launch", "browser_open", "browser_text",
            "browser_click", "browser_type",
            "browser_status"} <= names
