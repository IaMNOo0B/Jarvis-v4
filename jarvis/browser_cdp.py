"""Chromium control via the Chrome DevTools Protocol (CDP).

The owner's order: full browser control — open pages, read them,
click, type. CDP is how real tools drive Chrome; it needs a
WebSocket, and the stdlib has no WebSocket client, so this module
carries a minimal RFC 6455 client (handshake, masked sends, frame
parsing) — enough for CDP's JSON messages, nothing more.

Chrome must run with --remote-debugging-port; browser_launch starts
it that way with a JARVIS-owned profile folder (his logins in his
normal profile are untouched). Ops are honest: no Chrome found, no
connection, element not found — each says so, nothing is faked.

Unit-verified against a protocol fake; the live-Chrome pass is a
PC-side verification (the build VM has no browser).
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

DEBUG_PORT = 9222
_lock = threading.Lock()
_ws = None          # WSConnection to the page target
_current = {"url": "", "title": ""}


# ---------------------------------------------------------------------------
# minimal WebSocket client (RFC 6455)
# ---------------------------------------------------------------------------

class WSError(Exception):
    pass


class WSConnection:
    def __init__(self, host: str, port: int, path: str, timeout=15):
        self.sock = socket.create_connection((host, port),
                                             timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(request.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WSError("handshake closed early")
            response += chunk
        if b"101" not in response.split(b"\r\n", 1)[0]:
            raise WSError(f"handshake refused: "
                          f"{response[:120]!r}")
        self._buf = response.split(b"\r\n\r\n", 1)[1]

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WSError("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send_text(self, text: str) -> None:
        payload = text.encode()
        mask = os.urandom(4)
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def _send_pong(self, payload: bytes) -> None:
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes([0x8A, 0x80 | len(payload)]) + mask
                          + masked)

    def recv_text(self) -> str:
        message = b""
        while True:
            head = self._read(2)
            fin, opcode = head[0] & 0x80, head[0] & 0x0F
            masked, length = head[1] & 0x80, head[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if masked else None
            payload = self._read(length) if length else b""
            if mask:
                payload = bytes(b ^ mask[i % 4]
                                for i, b in enumerate(payload))
            if opcode == 9:            # ping -> pong, keep waiting
                self._send_pong(payload)
                continue
            if opcode == 8:
                raise WSError("server closed the connection")
            message += payload
            if fin:
                return message.decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def ws_connect(ws_url: str, timeout=15) -> WSConnection:
    """ws://host:port/path -> WSConnection."""
    rest = ws_url[len("ws://"):]
    hostport, _, path = rest.partition("/")
    host, _, port = hostport.partition(":")
    return WSConnection(host, int(port or 80), "/" + path,
                        timeout=timeout)


# ---------------------------------------------------------------------------
# CDP session
# ---------------------------------------------------------------------------

class CDPSession:
    def __init__(self, ws_url: str):
        self.ws = ws_connect(ws_url)
        self._next_id = 0

    def call(self, method: str, params: dict | None = None,
             timeout=20):
        self._next_id += 1
        call_id = self._next_id
        self.ws.sock.settimeout(timeout)
        self.ws.send_text(json.dumps(
            {"id": call_id, "method": method,
             "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = json.loads(self.ws.recv_text())
            if message.get("id") == call_id:
                if "error" in message:
                    raise WSError(f"{method}: "
                                  f"{message['error'].get('message')}")
                return message.get("result", {})
        raise WSError(f"{method}: no answer within {timeout}s")

    def evaluate(self, expression: str, timeout=20):
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
            timeout=timeout)
        inner = result.get("result", {})
        if inner.get("subtype") == "error" or \
                result.get("exceptionDetails"):
            raise WSError(f"page error: "
                          f"{inner.get('description', 'unknown')}")
        return inner.get("value")


def _cdp_http(path: str, method="GET", timeout=5):
    req = urllib.request.Request(
        f"http://127.0.0.1:{DEBUG_PORT}{path}", method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _find_chrome() -> str | None:
    candidates = []
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles", ""),
                     os.environ.get("ProgramFiles(x86)", ""),
                     os.environ.get("LocalAppData", "")):
            if base:
                candidates.append(str(Path(base) / "Google" / "Chrome"
                                      / "Application" / "chrome.exe"))
                candidates.append(str(Path(base) / "Microsoft" / "Edge"
                                      / "Application" / "msedge.exe"))
                candidates.append(str(Path(base) / "Chromium"
                                      / "Application" / "chrome.exe"))
    for name in ("google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "chrome", "msedge"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _profile_dir() -> Path:
    root = Path(__file__).resolve().parent.parent
    return root / "data" / "browser-profile"


def _connect_page() -> CDPSession:
    targets = _cdp_http("/json/list")
    page = next((t for t in targets if t.get("type") == "page"),
                None)
    if page is None:
        created = _cdp_http("/json/new?about:blank", method="PUT")
        page = created
    return CDPSession(page["webSocketDebuggerUrl"])


def _ensure_session() -> CDPSession:
    global _ws
    if _ws is not None:
        return _ws
    try:
        _ws = _connect_page()
        return _ws
    except Exception:
        pass
    launched = op_browser_launch()
    if "error" in launched:
        raise WSError(launched["error"])
    _ws = _connect_page()
    return _ws


def _refresh_current(session: CDPSession) -> None:
    try:
        _current["url"] = session.evaluate("location.href") or ""
        _current["title"] = session.evaluate("document.title") or ""
    except WSError:
        pass


# ---------------------------------------------------------------------------
# ops
# ---------------------------------------------------------------------------

def op_browser_launch():
    """Start Chrome/Chromium with remote debugging on a JARVIS
    profile, if it is not already running that way."""
    try:
        _cdp_http("/json/version", timeout=2)
        return {"launched": False, "note": "browser pehle se "
                                            "debugging mode me hai"}
    except Exception:
        pass
    exe = _find_chrome()
    if not exe:
        return {"error": "browser_launch failed: Chrome/Chromium "
                         "is PC par mila hi nahi — pehle Chrome "
                         "install karna hoga"}
    profile = _profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.Popen(
            [exe, f"--remote-debugging-port={DEBUG_PORT}",
             f"--user-data-dir={profile}", "--no-first-run",
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        return {"error": f"browser_launch failed: "
                         f"{type(exc).__name__}: {exc}"}
    deadline = time.time() + 12
    while time.time() < deadline:
        try:
            _cdp_http("/json/version", timeout=2)
            return {"launched": True, "browser": exe,
                    "profile": str(profile)}
        except Exception:
            time.sleep(0.5)
    return {"error": "browser_launch failed: browser start hua "
                     "lekin debugging port par jawab nahi aaya"}


def op_browser_open(url: str):
    target = str(url).strip()
    if not target.startswith(("http://", "https://")):
        target = "https://" + target
    with _lock:
        try:
            session = _ensure_session()
            session.call("Page.enable")
            session.call("Page.navigate", {"url": target})
            deadline = time.time() + 8
            while time.time() < deadline:
                state = session.evaluate("document.readyState")
                if state in ("complete", "interactive"):
                    break
                time.sleep(0.4)
            _refresh_current(session)
        except (WSError, OSError) as exc:
            return {"error": f"browser_open failed: {exc}"}
    return {"opened": target, "title": _current["title"],
            "url": _current["url"]}


def op_browser_text(max_chars: int = 12000):
    with _lock:
        try:
            session = _ensure_session()
            text = session.evaluate(
                "document.body ? document.body.innerText : ''")
            _refresh_current(session)
        except (WSError, OSError) as exc:
            return {"error": f"browser_text failed: {exc}"}
    text = str(text or "")
    return {"url": _current["url"], "title": _current["title"],
            "text": text[:max_chars],
            "truncated": len(text) > max_chars}


def op_browser_click(text: str):
    needle = json.dumps(str(text))
    script = f"""
(() => {{
  const needle = {needle}.toLowerCase();
  const els = document.querySelectorAll(
    'a, button, [role="button"], input[type="submit"], ' +
    'input[type="button"]');
  for (const el of els) {{
    const label = (el.innerText || el.value || '').toLowerCase();
    if (label.includes(needle)) {{ el.click(); return el.innerText
      || el.value || 'clicked'; }}
  }}
  return null;
}})()"""
    with _lock:
        try:
            session = _ensure_session()
            clicked = session.evaluate(script)
            time.sleep(0.6)
            _refresh_current(session)
        except (WSError, OSError) as exc:
            return {"error": f"browser_click failed: {exc}"}
    if clicked is None:
        return {"error": f"browser_click failed: '{text}' likha "
                         "koi button/link page par nahi mila"}
    return {"clicked": clicked, "url": _current["url"],
            "title": _current["title"]}


def op_browser_type(text: str, into: str = ""):
    script = f"""
(() => {{
  const value = {json.dumps(str(text))};
  const hint = {json.dumps(str(into).lower())};
  let box = document.activeElement;
  if (!box || !('value' in box)) {{
    const boxes = [...document.querySelectorAll(
      'input[type="text"], input[type="search"], input:not([type]),'
      + ' textarea')].filter(el => el.offsetParent !== null);
    box = boxes.find(el => ((el.placeholder || '') + ' ' +
      (el.name || '') + ' ' + (el.getAttribute('aria-label') || ''))
        .toLowerCase().includes(hint)) || boxes[0];
  }}
  if (!box) return null;
  box.focus();
  box.value = value;
  box.dispatchEvent(new Event('input', {{bubbles: true}}));
  box.dispatchEvent(new Event('change', {{bubbles: true}}));
  return box.placeholder || box.name || 'field';
}})()"""
    with _lock:
        try:
            session = _ensure_session()
            filled = session.evaluate(script)
        except (WSError, OSError) as exc:
            return {"error": f"browser_type failed: {exc}"}
    if filled is None:
        return {"error": "browser_type failed: page par koi text "
                         "field nahi mila"}
    return {"typed_into": filled, "text": str(text)}


def op_browser_status():
    global _ws
    if _ws is None:
        return {"connected": False,
                "note": "browser abhi connected nahi — browser_open "
                        "ya browser_launch se shuru hoga"}
    with _lock:
        try:
            _refresh_current(_ws)
        except (WSError, OSError):
            _ws = None
            return {"connected": False,
                    "note": "connection toot gaya tha"}
    return {"connected": True, "url": _current["url"],
            "title": _current["title"]}
