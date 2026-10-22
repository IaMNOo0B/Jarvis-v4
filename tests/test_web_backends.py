"""Web search survives a dead backend: Bing first, DuckDuckGo
behind it, lite last — and the error, when everything fails,
names each backend. learn_url files a fetched page in one step."""

from jarvis.builtin_skills import op_web_search
from jarvis.chat import ChatApp

BING_PAGE = """
<li class="b_algo"><h2><a href="https://example.com/a">Alpha</a></h2>
<p>Alpha snippet</p></li>
<li class="b_algo"><h2><a href="https://example.com/b">Beta</a></h2>
<p>Beta snippet</p></li>"""

BLOCK_PAGE = "<html><body>anomaly challenge, no results</body></html>"


def _fetch_from(mapping):
    def fetch(url):
        for key, page in mapping.items():
            if key in url:
                if isinstance(page, Exception):
                    raise page
                return page
        raise AssertionError(f"unexpected url {url}")
    return fetch


def test_bing_answers_first():
    out = op_web_search("q", fetch=_fetch_from({"bing.com": BING_PAGE}))
    assert out["backend"] == "bing"
    assert [r["title"] for r in out["results"]] == ["Alpha", "Beta"]


def test_blocked_bing_falls_through_to_ddg():
    ddg = ('<a class="result__a" href="https://x.example/">X</a>'
           '<a class="result__snippet">snip</a>')
    out = op_web_search("q", fetch=_fetch_from(
        {"bing.com": BLOCK_PAGE, "html.duckduckgo": ddg,
         "lite.duckduckgo": BLOCK_PAGE}))
    assert out["backend"] == "duckduckgo"
    assert out["results"][0]["title"] == "X"


def test_total_failure_names_every_backend():
    out = op_web_search("q", fetch=_fetch_from(
        {"bing.com": TimeoutError("slow"),
         "html.duckduckgo": BLOCK_PAGE,
         "lite.duckduckgo": BLOCK_PAGE}))
    assert "bing" in out["error"] and "duckduckgo" in out["error"]
    assert out["results"] == []


def test_app_learn_url_stores_the_page(tmp_path, monkeypatch):
    import jarvis.builtin_skills as bs
    monkeypatch.setattr(
        bs, "op_web_fetch",
        lambda url, max_chars=12000: {
            "content": "Quokkas live on Rottnest Island."})
    app = ChatApp(tmp_path)
    out = app.execute_tool("learn_url",
                           {"url": "https://example.com/quokka"})
    assert out["ok"] is True
    hits = app.memory.recall("quokkas Rottnest", k=5)
    assert any("Quokkas" in h["claim"] for h in hits) or \
        app.parts.learned.search("quokkas", k=3)
