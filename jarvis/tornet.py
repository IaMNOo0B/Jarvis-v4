"""Tor road — JARVIS v4 (the owner asked for every road in).

A minimal SOCKS5 client in pure stdlib, so JARVIS can fetch
through a Tor proxy already running on the owner's PC (the Tor
service on 127.0.0.1:9050, or Tor Browser on 9150) — including
http .onion addresses, which only exist on that road.

Honesty rules, like everywhere else in v4:
- No Tor running -> a plain error naming both ports and what to
  start. Nothing is faked, nothing silently falls back to the
  open web (a fetch the owner asked to keep on Tor must never
  leak onto the clearnet because Tor was down).
- This module only FETCHES through Tor; it does not install or
  start Tor itself.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import urllib.parse
from typing import Optional, Tuple

TOR_PORTS = (9050, 9150)


def _read_exact(sock: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise OSError("socks5: connection closed early")
        data += chunk
    return data


def socks5_connect(host: str, port: int,
                   proxy_host: str = "127.0.0.1",
                   proxy_port: int = 9050,
                   timeout: float = 12.0) -> socket.socket:
    """Open a TCP connection to host:port through a SOCKS5 proxy
    (no-auth, remote DNS — the proxy resolves the name, which is
    what makes .onion and leak-free lookups work)."""
    sock = socket.create_connection((proxy_host, proxy_port),
                                    timeout=timeout)
    try:
        sock.sendall(b"\x05\x01\x00")            # VER, 1 method, NO-AUTH
        if _read_exact(sock, 2) != b"\x05\x00":
            raise OSError("socks5: proxy refused no-auth")
        encoded = host.encode("idna")
        request = (b"\x05\x01\x00\x03" + bytes([len(encoded)])
                   + encoded + port.to_bytes(2, "big"))
        sock.sendall(request)
        head = _read_exact(sock, 4)
        if head[1] != 0x00:
            raise OSError(f"socks5: connect failed, code {head[1]}")
        atyp = head[3]
        if atyp == 0x01:
            _read_exact(sock, 4)
        elif atyp == 0x03:
            _read_exact(sock, _read_exact(sock, 1)[0])
        elif atyp == 0x04:
            _read_exact(sock, 16)
        _read_exact(sock, 2)                     # bound port
        return sock
    except Exception:
        sock.close()
        raise


def fetch_via_tor(url: str, timeout: float = 25.0,
                  ports: Tuple[int, ...] = TOR_PORTS
                  ) -> Tuple[Optional[str], str]:
    """(page_text, '') or (None, honest error). Tries each Tor
    port in turn; speaks plain HTTP/HTTPS over the tunnel via
    http.client, so redirects are NOT followed silently — a 30x
    body is returned as-is and the caller decides."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None, "tor fetch: url samajh nahi aaya, sir"
    target_port = parsed.port or (443 if parsed.scheme == "https"
                                  else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    last_error = ""
    last_kind = "no-tor"
    for proxy_port in ports:
        raw = None
        try:
            # The caller's timeout rules BOTH phases (audit
            # 2026-10-22: the connect silently capped itself
            # at 15s whatever the caller asked).
            raw = socks5_connect(parsed.hostname, target_port,
                                 proxy_port=proxy_port,
                                 timeout=timeout)
            if parsed.scheme == "https":
                context = ssl.create_default_context()
                raw = context.wrap_socket(
                    raw, server_hostname=parsed.hostname)
            conn = http.client.HTTPConnection(
                parsed.hostname, target_port, timeout=timeout)
            conn.sock = raw
            conn.request("GET", path, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; "
                              "Win64; x64)",
                "Accept": "text/html,application/xhtml+xml"})
            response = conn.getresponse()
            body = response.read()
            status = response.status
            conn.close()
            if status >= 400:
                # An HTTP error is an ERROR-shaped result,
                # not a page (audit 2026-10-22: 404/500
                # bodies came back as successes).
                return None, (f"tor fetch: {parsed.hostname} ne "
                              f"HTTP {status} diya, sir")
            return body.decode("utf-8", errors="replace"), ""
        except Exception as exc:
            # Name the failure for what it IS: a refused proxy
            # connection means Tor is not running; a timeout
            # AFTER connecting means the road is up but slow
            # — the old message blamed Tor for both.
            if isinstance(exc, (socket.timeout, TimeoutError)):
                last_kind = "timeout"
            elif isinstance(exc, ConnectionRefusedError):
                last_kind = "no-tor"
            else:
                last_kind = "error"
            last_error = f"{type(exc).__name__}: {exc}"
            try:
                if raw is not None:
                    raw.close()
            except Exception:
                pass
    if last_kind == "timeout":
        return None, ("tor fetch failed — Tor mila, par jawab "
                      f"time par nahi aaya ({last_error}). "
                      "Raasta chalu hai, bas sust hai, sir.")
    return None, ("tor fetch failed — Tor chalta hua nahi mila "
                  f"(ports {', '.join(str(p) for p in ports)} "
                  f"aazmaye; aakhri galti: {last_error}). Tor "
                  "Browser ya Tor service chalu karo, sir — bina "
                  "Tor ke ye raasta khamosh rahega, clearnet par "
                  "chupke se nahi jayega.")
