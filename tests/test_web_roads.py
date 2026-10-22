"""The extra roads: scrape takes a page apart, browser-search
rides the controlled browser to engines plain HTTP cannot read,
and the Tor road speaks real SOCKS5 — or says honestly that no
Tor is running."""

import socket
import threading

import jarvis.builtin_skills as bs
from jarvis.chat import ChatApp
from jarvis.tornet import fetch_via_tor

PAGE = """<html><head><title>Rate List</title>
<meta name="description" content="2026 rates"></head><body>
<h1>Prices</h1><h2>Basic — 500 rupees</h2>
<a href="/details">See details</a>
<p>Some honest body text lives here.</p></body></html>"""


def test_scrape_takes_the_page_apart(monkeypatch):
    monkeypatch.setattr(bs, "_fetch_page", lambda url, timeout=8: PAGE)
    out = bs.op_web_scrape("https://example.com/rates")
    assert out["title"] == "Rate List"
    assert out["description"] == "2026 rates"
    assert "Prices" in out["headings"]
    assert out["links"][0]["url"] == "https://example.com/details"
    assert "honest body text" in out["text"]


def test_search_browser_rides_the_browser(monkeypatch):
    monkeypatch.setattr(bs, "op_browser_open",
                        lambda url: {"ok": True, "url": url})
    monkeypatch.setattr(bs, "op_browser_text",
                        lambda n=12000: {"text": "result one "
                                                 "result two"})
    out = bs.op_search_browser("quokkas", engine="google")
    assert out["ok"] is True and out["engine"] == "google"
    assert "result one" in out["results_text"]
    bad = bs.op_search_browser("quokkas", engine="altavista")
    assert "error" in bad


def _fake_tor_server():
    """A one-shot fake: SOCKS5 handshake, then an HTTP origin."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve():
        conn, _ = server.accept()
        conn.recv(3)                       # greeting
        conn.sendall(b"\x05\x00")
        req = conn.recv(4096)              # CONNECT request
        conn.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50")
        conn.recv(4096)                    # the HTTP request
        body = b"<html><body>tor works</body></html>"
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: "
                     + str(len(body)).encode()
                     + b"\r\nConnection: close\r\n\r\n" + body)
        conn.close()
        server.close()

    threading.Thread(target=serve, daemon=True).start()
    return port


def test_tor_fetch_through_a_fake_proxy():
    port = _fake_tor_server()
    text, error = fetch_via_tor("http://example.onion/page",
                                ports=(port,))
    assert error == "" and "tor works" in text


def test_tor_fetch_is_honest_without_tor():
    text, error = fetch_via_tor("http://example.com/",
                                ports=(1,))   # nothing listens there
    assert text is None and "Tor" in error
    assert "clearnet" in error


def test_app_advertises_the_new_roads(tmp_path):
    app = ChatApp(tmp_path)
    names = {t["name"] for t in
             app.registry.advertised_tools(app.allowances)}
    assert {"web_scrape", "search_browser", "web_fetch_tor"} <= names
    out = app.execute_tool("web_fetch_tor",
                           {"url": "http://example.com/"})
    assert out["ok"] is False and "Tor" in out["error"]
