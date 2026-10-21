"""Minimal WebSocket client for the Gemini Live API — stdlib only.

The Mark-LVI build reaches Live through the google-genai SDK; v4
carries no SDK, so the wire is spoken directly (the same hand-rolled
RFC 6455 approach that already drives Chromium in browser_cdp.py,
here with TLS for wss:// and text+binary frames). Client frames are
masked as the RFC demands; server frames arrive unmasked.
"""

from __future__ import annotations

import base64
import os
import socket
import ssl
import struct
import urllib.parse
from typing import Optional, Tuple

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0, 1, 2, 8, 9, 10


class WSError(Exception):
    """The socket or handshake failed; the message carries the cause
    (the Live reconnect ladder classifies failures by its text)."""


class WSClosed(WSError):
    def __init__(self, code: int, reason: str = ""):
        super().__init__(f"websocket closed: {code} {reason}".strip())
        self.code = code
        self.reason = reason


class WSConnection:
    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buf = b""
        self._frag_op: Optional[int] = None
        self._frag_data = bytearray()

    # -- construction ----------------------------------------------------

    @classmethod
    def connect(cls, url: str, timeout: float = 20.0) -> "WSConnection":
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or ""
        secure = parsed.scheme == "wss"
        port = parsed.port or (443 if secure else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        try:
            raw = socket.create_connection((host, port), timeout=timeout)
            if secure:
                ctx = ssl.create_default_context()
                raw = ctx.wrap_socket(raw, server_hostname=host)
        except OSError as e:
            raise WSError(f"connect failed: {e}") from e
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        try:
            raw.sendall(request.encode())
            response = b""
            while b"\r\n\r\n" not in response:
                chunk = raw.recv(4096)
                if not chunk:
                    raise WSError("handshake: connection closed")
                response += chunk
        except OSError as e:
            raise WSError(f"handshake failed: {e}") from e
        head, _, rest = response.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("utf-8", "replace")
        if " 101" not in status_line:
            raise WSError(f"handshake refused: {status_line} "
                          f"{rest[:160].decode('utf-8', 'replace')}")
        conn = cls(raw)
        conn._buf = rest
        raw.settimeout(None)
        return conn

    # -- framing -----------------------------------------------------------

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise WSClosed(1006, "socket closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n <= 0xFFFF:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        try:
            self._sock.sendall(bytes(header) + masked)
        except OSError as e:
            raise WSError(f"send failed: {e}") from e

    def set_recv_timeout(self, seconds: Optional[float]) -> None:
        """A recv tick: socket.timeout propagates out of recv() with
        any partial frame preserved in the buffer, so callers can run
        watchdogs between ticks without corrupting the stream."""
        self._sock.settimeout(seconds)

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def send_binary(self, data: bytes) -> None:
        self._send_frame(OP_BIN, data)

    def recv(self) -> Tuple[int, bytes]:
        """One whole message (fragments reassembled). Ping is answered
        in-band and never surfaces; close raises WSClosed."""
        while True:
            first = self._read(2)
            fin = bool(first[0] & 0x80)
            opcode = first[0] & 0x0F
            masked = bool(first[1] & 0x80)
            length = first[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if masked else None
            payload = self._read(length) if length else b""
            if mask:
                payload = bytes(b ^ mask[i % 4]
                                for i, b in enumerate(payload))
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_CLOSE:
                code = struct.unpack(">H", payload[:2])[0] \
                    if len(payload) >= 2 else 1005
                raise WSClosed(code, payload[2:].decode("utf-8",
                                                        "replace"))
            if opcode == OP_CONT:
                self._frag_data += payload
                if fin:
                    op, data = self._frag_op, bytes(self._frag_data)
                    self._frag_op, self._frag_data = None, bytearray()
                    return op, data
                continue
            if not fin:
                self._frag_op, self._frag_data = opcode, bytearray(payload)
                continue
            return opcode, payload

    def close(self) -> None:
        try:
            self._send_frame(OP_CLOSE, struct.pack(">H", 1000))
        except WSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
