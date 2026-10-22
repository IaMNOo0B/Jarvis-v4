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
import re
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


# ---------------------------------------------------------------------------
# Snapshots + click risk (Sentient's browser pattern): visible
# interactive elements get stable refs (e1, e2, ...) the brain can
# point at exactly, and a click's risk is classified from its label
# plus the text of the card around it BEFORE anything is clicked.
# ---------------------------------------------------------------------------

SNAPSHOT_JS = """
(() => {
  const out = {elements: [], text: ''};
  const els = document.querySelectorAll(
    'a, button, [role="button"], input, select, textarea, [onclick]');
  let n = 0;
  for (const el of els) {
    if (n >= 300) break;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const label = (el.innerText || el.value || el.placeholder ||
      el.getAttribute('aria-label') || '').trim()
      .replace(/\\s+/g, ' ').slice(0, 80);
    n += 1;
    const ref = 'e' + n;
    el.setAttribute('data-jarvis-ref', ref);
    const holder = el.closest('div, section, li, form');
    const card = holder ? (holder.innerText || '')
      .replace(/\\s+/g, ' ').slice(0, 160) : '';
    const form = el.closest('form');
    out.elements.push({ref, tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || '', label, card,
      form: form ? 'yes' : ''});
  }
  out.total = n;
  const body = document.body ? document.body.innerText : '';
  const main = document.querySelector('main, article');
  out.text = (main && main.innerText.length > body.length * 0.3)
    ? main.innerText : body;
  return JSON.stringify(out);
})()"""

_RISKY_CLICK_RE = re.compile(
    r"\b(buy|pay|purchase|checkout|place order|order now|subscribe|"
    r"delete|remove|confirm|transfer|send|post|publish|cancel|"
    r"unsubscribe|sign up|signup|register|book now)\b", re.I)
_PAYMENT_SIGNAL_RE = re.compile(
    r"(cvv|cvc|billing|payment method|\$|€|£|₹|\brs\.?\s*\d|"
    r"per month|/month|\d+\s*(rupees|dollars))", re.I)


def classify_click_risk(label: str, context: str = "") -> str:
    """'high' when the click smells like money or commitment: a
    risky verb in the label, or payment signals around it. A bare
    'Subscribe' is a purchase UNLESS the surrounding text proves
    it is free and carries no money signals (Sentient's rule)."""
    label = str(label or "")
    text = f"{label} {context or ''}"
    if _RISKY_CLICK_RE.search(label):
        if re.search(r"subscribe", label, re.I) \
                and re.search(r"\bfree\b", text, re.I) \
                and not _PAYMENT_SIGNAL_RE.search(text):
            return "low"
        return "high"
    if _PAYMENT_SIGNAL_RE.search(label):
        return "high"
    return "low"


def _format_snapshot(data: dict, max_chars: int = 12000) -> dict:
    elements, lines = [], []
    budget = int(max_chars * 0.65)      # elements get ~65% (Sentient)
    used = 0
    for el in data.get("elements", []):
        risk = classify_click_risk(el.get("label", ""),
                                   el.get("card", ""))
        line = (f"[{el.get('ref', '?')}] {el.get('tag', '')} "
                f"\"{el.get('label', '')}\""
                + (" [form]" if el.get("form") else "")
                + (" [RISKY]" if risk == "high" else ""))
        if used + len(line) > budget:
            break
        used += len(line) + 1
        lines.append(line)
        elements.append({"ref": el.get("ref"), "tag": el.get("tag"),
                         "label": el.get("label"), "risk": risk})
    total = int(data.get("total", len(data.get("elements", []))))
    if total > len(elements):
        lines.append(f"... {total - len(elements)} more elements "
                     f"not shown")
    text_budget = max(0, max_chars - used)
    text = str(data.get("text", ""))[:text_budget]
    return {"elements": elements, "lines": lines, "text": text,
            "truncated": len(str(data.get("text", ""))) > text_budget}


def op_browser_snapshot(max_chars: int = 12000):
    with _lock:
        try:
            session = _ensure_session()
            raw = session.evaluate(SNAPSHOT_JS)
            _refresh_current(session)
        except (WSError, OSError) as exc:
            return {"error": f"browser_snapshot failed: {exc}"}
    try:
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except ValueError:
        return {"error": "browser_snapshot failed: page snapshot "
                         "samajh nahi aaya"}
    out = _format_snapshot(data, max_chars=max_chars)
    return {"url": _current["url"], "title": _current["title"],
            "elements": out["elements"], "snapshot": out["lines"],
            "text": out["text"], "truncated": out["truncated"]}


def op_browser_click_ref(ref: str, confirm: bool = False):
    """Click the element a snapshot tagged. Risk is classified
    BEFORE the click: a high-risk click without confirm=True is
    refused, with the reason, so the brain can ask the owner."""
    ref = str(ref).strip()
    if not re.fullmatch(r"e\d{1,3}", ref):
        return {"error": f"browser_click_ref failed: '{ref}' koi "
                         "snapshot ref nahi hai (e1, e2, ...)"}
    probe = f"""
(() => {{
  const el = document.querySelector('[data-jarvis-ref="{ref}"]');
  if (!el) return null;
  const holder = el.closest('div, section, li, form');
  return JSON.stringify({{label: (el.innerText || el.value || ''),
    card: holder ? (holder.innerText || '') : ''}});
}})()"""
    with _lock:
        try:
            session = _ensure_session()
            raw = session.evaluate(probe)
            info = json.loads(raw) if raw else None
            if info is None:
                return {"error": f"browser_click_ref failed: ref "
                                 f"{ref} page par nahi mila — naya "
                                 "snapshot lo"}
            risk = classify_click_risk(info.get("label", ""),
                                       info.get("card", ""))
            if risk == "high" and not confirm:
                return {"ok": False, "risk": "high",
                        "label": info.get("label", ""),
                        "error": "yeh click risky hai (paisa ya "
                                 "commitment wala) — owner se confirm "
                                 "karke confirm=True ke saath bhejo"}
            clicked = session.evaluate(
                f"document.querySelector('[data-jarvis-ref=\"{ref}\""
                "]).click(); 'clicked'")
            time.sleep(0.6)
            _refresh_current(session)
        except (WSError, OSError) as exc:
            return {"error": f"browser_click_ref failed: {exc}"}
    return {"clicked": info.get("label", ""), "risk": risk,
            "url": _current["url"], "title": _current["title"]}


def op_browser_screenshot():
    """The page as pixels: a PNG through CDP Page.captureScreenshot,
    base64 out — the vision tools' raw material."""
    with _lock:
        try:
            session = _ensure_session()
            result = session.call("Page.captureScreenshot",
                                  {"format": "png"})
            _refresh_current(session)
        except (WSError, OSError) as exc:
            return {"error": f"browser_screenshot failed: {exc}"}
    data = (result or {}).get("data")
    if not data:
        return {"error": "browser_screenshot failed: page ne koi "
                         "tasveer nahi di"}
    return {"ok": True, "png_b64": data, "url": _current["url"],
            "title": _current["title"]}


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
