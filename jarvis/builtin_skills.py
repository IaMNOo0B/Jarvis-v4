"""First-party skills — shipped with JARVIS, approved by the app itself.

Every op here is REAL code that runs (LAW 2's positive side — the v2
failure was a permitted, advertised tool whose executor had no such
op; these ops exist, and the contract test proves it):

- time_now      the real clock.
- remember_fact / recall_memory — delegate to the memory object the
  app injects (anything with remember(text) / recall(query)); a small
  in-process store stands in when none is injected yet.
- web_search    a real DuckDuckGo HTML fetch over urllib (8 s
  timeout) with result extraction. On failure it returns an honest
  error string — the op exists and ran; the network said no.
- open_app      on Windows, a real launch via os.startfile /
  subprocess. On any other OS it returns an honest "driver not present
  on this OS" result — a real code path with a real answer, never an
  unknown-op exception.

Stdlib only.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from jarvis.skills import SkillRegistry

# ---------------------------------------------------------------------
# ops
# ---------------------------------------------------------------------


def op_time_now(**_ignored):
    now = datetime.now(timezone.utc)
    return {"iso": now.isoformat(), "unix": now.timestamp()}


class _FallbackMemory:
    """In-process fact store used until the app injects its memory."""

    def __init__(self):
        self.facts: list[str] = []

    def remember(self, text: str):
        self.facts.append(text)
        return {"stored": text}

    def recall(self, query: str):
        q = (query or "").lower()
        hits = [f for f in self.facts if q in f.lower()] or list(self.facts)
        return {"facts": hits}


def _memory_ops(memory):
    store = memory if memory is not None else _FallbackMemory()

    def remember_fact(text: str):
        result = store.remember(text)
        return result if isinstance(result, dict) else {"stored": text}

    def recall_memory(query: str = ""):
        result = store.recall(query)
        return result if isinstance(result, dict) else {"facts": result}

    def learn_text(text: str, title: str = "pasted"):
        fn = getattr(store, "learn_text", None)
        if callable(fn):
            result = fn(text, title)
            return result if isinstance(result, dict) else {"stored": text}
        store.remember(text)
        return {"stored": text,
                "note": "no document store attached; kept as a fact"}

    def learn_url(url: str):
        """"Learn this page" in ONE step: fetch the page in code,
        store its readable text into the learned store, where the
        inbox pipeline chunks and indexes it — the page becomes
        searchable knowledge, not a one-turn glimpse."""
        fetched = op_web_fetch(str(url), max_chars=20000)
        if fetched.get("error"):
            return {"ok": False, "error": fetched["error"]}
        content = str(fetched.get("content")
                      or fetched.get("text") or "")
        if not content.strip():
            return {"ok": False,
                    "error": "page se padhne layak text nahi mila, "
                             "sir"}
        fn = getattr(store, "learn_text", None)
        if callable(fn):
            result = fn(content, title=str(url))
            out = result if isinstance(result, dict) else {}
            out.update({"ok": True, "learned_from": str(url),
                        "chars": len(content)})
            return out
        store.remember(content[:500])
        return {"ok": True, "learned_from": str(url),
                "note": "no document store attached; kept the gist "
                        "as a fact"}

    return {"remember_fact": remember_fact,
            "recall_memory": recall_memory,
            "learn_text": learn_text,
            "learn_url": learn_url}


_DDG_HTML = "https://html.duckduckgo.com/html/?q="
_DDG_LITE = "https://lite.duckduckgo.com/lite/?q="
_BING = "https://www.bing.com/search?q="
_RESULT_LINK_RE = re.compile(
    r'class="result__a"[^>]*href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>',
    re.S)
_RESULT_SNIPPET_RE = re.compile(
    r'class="result__snippet"[^>]*>(?P<snippet>.*?)</(?:a|td)>', re.S)
_LITE_LINK_RE = re.compile(
    r"class='result-link'[^>]*href=\"(?P<url>[^\"]+)\"[^>]*>"
    r"(?P<title>.*?)</a>", re.S)
_LITE_SNIPPET_RE = re.compile(
    r"class='result-snippet'[^>]*>(?P<snippet>.*?)</td>", re.S)
_BING_BLOCK_RE = re.compile(
    r'<li class="b_algo".*?<h2><a[^>]*href="(?P<url>[^"]+)"[^>]*>'
    r'(?P<title>.*?)</a></h2>(?P<rest>.*?)</li>', re.S)
_BING_SNIPPET_RE = re.compile(r"<p[^>]*>(?P<snippet>.*?)</p>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
# A realistic browser UA: several search engines quietly serve a
# block page to obvious bot agents (that, not "no internet", is
# what an empty result list usually means).
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0 Safari/537.36")


def _clean(fragment: str) -> str:
    return html.unescape(_TAG_RE.sub("", fragment)).strip()


def _real_target(url: str) -> str:
    """DuckDuckGo wraps result links in a redirect (uddg=...)."""
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if "uddg" in query:
            return query["uddg"][0]
    except Exception:
        pass
    return url


def _fetch_page(url: str, timeout: int = 8) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _bing_target(url: str) -> str:
    """Bing wraps result links in a /ck/a redirect whose `u`
    parameter carries the real URL (base64url, 'a1'-prefixed).
    Unwrap it — a brain following the wrapper gets a tracker
    page, not the result."""
    try:
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(html.unescape(url)).query)
        raw = (query.get("u") or [""])[0]
        if raw.startswith("a1"):
            import base64
            padded = raw[2:] + "=" * (-len(raw[2:]) % 4)
            decoded = base64.urlsafe_b64decode(padded).decode(
                "utf-8", errors="strict")
            if decoded.startswith(("http://", "https://")):
                return decoded
    except Exception:
        pass
    return html.unescape(url)


def _parse_bing(page: str, limit: int) -> list:
    results = []
    for match in _BING_BLOCK_RE.finditer(page):
        snippet = _BING_SNIPPET_RE.search(match.group("rest"))
        results.append({
            "title": _clean(match.group("title")),
            "url": _bing_target(match.group("url")),
            "snippet": _clean(snippet.group("snippet")) if snippet
                       else "",
        })
        if len(results) >= limit:
            break
    return results


def _parse_ddg(page: str, limit: int, lite: bool = False) -> list:
    link_re = _LITE_LINK_RE if lite else _RESULT_LINK_RE
    snip_re = _LITE_SNIPPET_RE if lite else _RESULT_SNIPPET_RE
    snippets = [_clean(m.group("snippet"))
                for m in snip_re.finditer(page)]
    results = []
    for i, match in enumerate(link_re.finditer(page)):
        results.append({
            "title": _clean(match.group("title")),
            "url": _real_target(match.group("url")),
            "snippet": snippets[i] if i < len(snippets) else "",
        })
        if len(results) >= limit:
            break
    return results


def _search_backends(query: str, limit: int, fetch) -> list:
    """(name, url, parser) in the order they are tried. Bing goes
    first: it answers from datacenter and residential IPs alike,
    where DuckDuckGo often serves a block page with zero results
    (live-verified 2026-10-22 from the build VM)."""
    q = urllib.parse.quote_plus(query)
    return [
        ("bing", _BING + q, lambda p: _parse_bing(p, limit)),
        ("duckduckgo", _DDG_HTML + q,
         lambda p: _parse_ddg(p, limit)),
        ("duckduckgo-lite", _DDG_LITE + q,
         lambda p: _parse_ddg(p, limit, lite=True)),
    ]


def op_web_search(query: str, limit: int = 5, fetch=None):
    """Search the web through three backends in turn — the first
    that returns real results wins. A single search engine going
    quiet must never again read as 'no internet': if every backend
    fails, the error names each one and why."""
    fetch = fetch or _fetch_page
    tried = []
    for name, url, parse in _search_backends(query, limit, fetch):
        try:
            page = fetch(url)
        except Exception as exc:  # network down, timeout — next one
            tried.append(f"{name}: {type(exc).__name__}")
            continue
        results = parse(page)
        if results:
            return {"results": results, "backend": name}
        tried.append(f"{name}: no results (blocked page?)")
    return {"error": "web_search failed on every backend — "
                     + "; ".join(tried),
            "results": []}


_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>",
                              re.IGNORECASE | re.DOTALL)
_BLOCK_BREAK_RE = re.compile(r"<(br|/p|/div|/li|/tr|/h[1-6])[^>]*>",
                             re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(page: str) -> str:
    page = _SCRIPT_STYLE_RE.sub(" ", page)
    page = _BLOCK_BREAK_RE.sub("\n", page)
    text = html.unescape(_TAG_RE.sub(" ", page))
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def op_web_fetch(url: str, max_chars: int = 12000):
    """Fetch one page and return its readable text — the 'open the
    page and read it' half of internet access (web_search finds,
    web_fetch reads). Honest errors, never a fabricated page."""
    target = str(url).strip()
    if not target.startswith(("http://", "https://")):
        return {"error": "web_fetch failed: the address must start "
                         "with http:// or https://"}
    request = urllib.request.Request(
        target, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read(2_000_000)
            ctype = ""
            if response.headers is not None:
                ctype = response.headers.get_content_type() or ""
    except Exception as exc:  # network down, blocked, timeout — say so
        return {"error": f"web_fetch failed: {type(exc).__name__}: {exc}"}
    text = raw.decode("utf-8", errors="replace")
    if "html" in ctype or text.lstrip().startswith("<"):
        text = _html_to_text(text)
    text = text.strip()
    return {"url": target, "text": text[:max_chars],
            "truncated": len(text) > max_chars}


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_META_DESC_RE = re.compile(
    r'<meta[^>]+name="description"[^>]+content="([^"]*)"', re.I)
_HEADING_RE = re.compile(r"<h([1-3])[^>]*>(.*?)</h\1>", re.I | re.S)
_LINK_RE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                      re.I | re.S)


def op_web_scrape(url: str, max_chars: int = 12000):
    """A page taken apart, not just read: title, description,
    headings, links and the readable text — the scraper half of
    internet access, for when the brain needs a page's structure
    (prices in headings, links to follow), not only its prose."""
    target = str(url).strip()
    if not target.startswith(("http://", "https://")):
        return {"error": "web_scrape failed: the address must "
                         "start with http:// or https://"}
    try:
        page = _fetch_page(target, timeout=10)
    except Exception as exc:
        return {"error": f"web_scrape failed: "
                         f"{type(exc).__name__}: {exc}"}
    title = _TITLE_RE.search(page)
    desc = _META_DESC_RE.search(page)
    headings = [_clean(h.group(2)) for h in
                _HEADING_RE.finditer(page)]
    links, seen = [], set()
    for match in _LINK_RE.finditer(page):
        absolute = urllib.parse.urljoin(target, match.group(1))
        label = _clean(match.group(2))
        if absolute.startswith(("http://", "https://")) \
                and absolute not in seen and label:
            seen.add(absolute)
            links.append({"text": label[:80], "url": absolute})
        if len(links) >= 40:
            break
    return {"ok": True, "url": target,
            "title": _clean(title.group(1)) if title else "",
            "description": html.unescape(desc.group(1)).strip()
                           if desc else "",
            "headings": [h for h in headings if h][:20],
            "links": links,
            "text": _html_to_text(page).strip()[:max_chars]}


_BROWSER_ENGINES = {
    "google": "https://www.google.com/search?q=",
    "bing": "https://www.bing.com/search?q=",
    "duckduckgo": "https://duckduckgo.com/?q=",
}


def op_search_browser(query: str, engine: str = "google"):
    """Search through the REAL browser — the road plain HTTP
    cannot take: Google's results only exist after its scripts
    run (live-verified 2026-10-22: the fetched page holds no
    results at all). The controlled Chrome/Edge opens the
    engine's results page and its text comes back."""
    base = _BROWSER_ENGINES.get(str(engine).lower())
    if base is None:
        return {"error": f"search_browser failed: engine "
                         f"'{engine}' nahi hai — google, bing ya "
                         "duckduckgo bolo, sir"}
    url = base + urllib.parse.quote_plus(str(query))
    opened = op_browser_open(url)
    if opened.get("error"):
        return {"ok": False, "error": opened["error"]}
    page = op_browser_text(12000)
    if page.get("error"):
        return {"ok": False, "error": page["error"]}
    return {"ok": True, "engine": str(engine).lower(), "url": url,
            "results_text": page.get("text", "")}


def _json_get(url: str, ua: str, timeout: int = 10):
    request = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8",
                                                  errors="replace"))


def op_wikipedia(query: str):
    """Wikipedia, through its own API — the encyclopedia road:
    search, then the top article's plain-text extract. Keyless,
    structured, and citable (the article URL rides along)."""
    api = "https://en.wikipedia.org/w/api.php"
    ua = "JARVIS-v4/1.0 (personal assistant; owner use)"
    try:
        found = _json_get(
            api + "?action=query&list=search&srlimit=5&format=json"
            "&srsearch=" + urllib.parse.quote_plus(str(query)), ua)
        hits = ((found.get("query") or {}).get("search")) or []
        if not hits:
            return {"ok": False,
                    "error": f"Wikipedia par '{query}' nahi mila, "
                             "sir"}
        title = hits[0]["title"]
        page = _json_get(
            api + "?action=query&prop=extracts&explaintext=1"
            "&format=json&titles="
            + urllib.parse.quote_plus(title), ua)
        pages = ((page.get("query") or {}).get("pages")) or {}
        extract = ""
        for value in pages.values():
            extract = str(value.get("extract") or "")
            break
    except Exception as exc:
        return {"ok": False,
                "error": f"wikipedia failed: "
                         f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "title": title,
            "url": "https://en.wikipedia.org/wiki/"
                   + urllib.parse.quote(title.replace(" ", "_")),
            "extract": extract[:6000],
            "more": [h["title"] for h in hits[1:]]}


def op_github_search(query: str, limit: int = 5):
    """GitHub's public search — the code road: repositories by
    relevance, with stars and descriptions, keyless."""
    try:
        data = _json_get(
            "https://api.github.com/search/repositories?per_page="
            + str(max(1, min(10, int(limit)))) + "&q="
            + urllib.parse.quote_plus(str(query)), "JARVIS-v4")
    except Exception as exc:
        return {"ok": False,
                "error": f"github search failed: "
                         f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "repos": [{
        "name": item.get("full_name", ""),
        "stars": item.get("stargazers_count", 0),
        "description": item.get("description") or "",
        "url": item.get("html_url", ""),
    } for item in (data.get("items") or [])]}


def op_github_repo(full_name: str):
    """One repository read properly: its facts plus its README —
    how JARVIS studies a project before learning from it (the
    Reverse-Engineer-Anything habit: source evidence first)."""
    name = str(full_name).strip().strip("/")
    if name.count("/") != 1:
        return {"ok": False,
                "error": "repo ka naam 'owner/repo' jaisa chahiye, "
                         "sir"}
    try:
        meta = _json_get(f"https://api.github.com/repos/{name}",
                         "JARVIS-v4")
        readme = ""
        try:
            request = urllib.request.Request(
                f"https://api.github.com/repos/{name}/readme",
                headers={"User-Agent": "JARVIS-v4",
                         "Accept": "application/vnd.github.raw+json"})
            with urllib.request.urlopen(request, timeout=10) as resp:
                readme = resp.read().decode("utf-8",
                                            errors="replace")
        except Exception:
            readme = ""
    except Exception as exc:
        return {"ok": False,
                "error": f"github repo failed: "
                         f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "name": meta.get("full_name", name),
            "stars": meta.get("stargazers_count", 0),
            "language": meta.get("language") or "",
            "description": meta.get("description") or "",
            "url": meta.get("html_url", ""),
            "readme": readme[:8000]}


def op_web_fetch_tor(url: str, max_chars: int = 12000):
    """Fetch a page through Tor (the owner's local Tor proxy,
    ports 9050/9150) — the private road, and the only road to
    .onion addresses. No Tor running -> an honest error, never a
    silent clearnet fallback."""
    from jarvis.tornet import fetch_via_tor
    text, error = fetch_via_tor(str(url).strip())
    if text is None:
        return {"ok": False, "error": error}
    if text.lstrip().startswith("<"):
        text = _html_to_text(text)
    return {"ok": True, "via": "tor", "url": str(url),
            "text": text.strip()[:max_chars]}


# ----------------------------------------------------------------------
# knowledge search — the shipped library (knowledge/) made reachable.
# The folder sat in the repo unread for too long; this op indexes it
# with the repo's own BM25 (jarvis.rag) and returns sourced excerpts.
# ----------------------------------------------------------------------

_KNOWLEDGE_CACHE: dict = {"stamp": None, "index": None, "docs": []}


def _knowledge_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "knowledge"


def _knowledge_index(root: Path):
    from jarvis.rag import BM25Index

    files = sorted(p for p in root.rglob("*")
                   if p.is_file() and p.suffix.lower() in (".md", ".txt"))
    stamp = tuple((str(p), p.stat().st_size, int(p.stat().st_mtime))
                  for p in files)
    if _KNOWLEDGE_CACHE["stamp"] == stamp and \
            _KNOWLEDGE_CACHE["index"] is not None:
        return _KNOWLEDGE_CACHE["index"], _KNOWLEDGE_CACHE["docs"]
    docs = []
    for path in files:
        try:
            docs.append((str(path.relative_to(root)),
                         path.read_text(encoding="utf-8",
                                        errors="replace")))
        except OSError:
            continue
    index = BM25Index([text for _name, text in docs]) if docs else None
    _KNOWLEDGE_CACHE.update({"stamp": stamp, "index": index,
                             "docs": docs})
    return index, docs


def _excerpt(text: str, query: str, width: int = 600) -> str:
    lowered = text.lower()
    for term in re.findall(r"[a-z0-9]+", query.lower()):
        at = lowered.find(term)
        if at >= 0:
            start = max(0, at - width // 3)
            return text[start:start + width].strip()
    return text[:width].strip()


def op_knowledge_search(query: str, limit: int = 4):
    """Search JARVIS's own knowledge library; sourced excerpts back."""
    root = _knowledge_dir()
    if not root.exists():
        return {"error": "knowledge_search failed: no knowledge "
                         "folder ships with this build", "results": []}
    index, docs = _knowledge_index(root)
    if index is None or not docs:
        return {"error": "knowledge_search failed: the knowledge "
                         "library is empty", "results": []}
    results = []
    for doc_idx, score in index.search(str(query), top_k=int(limit)):
        if score <= 0:
            continue
        name, text = docs[doc_idx]
        results.append({"source": name,
                        "excerpt": _excerpt(text, str(query)),
                        "score": round(float(score), 3)})
    if not results:
        return {"results": [],
                "note": "nothing in the knowledge library matched"}
    return {"results": results}


_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}


def op_list_files(path: str = ""):
    """List one folder on the owner's PC (his standing order: full
    access — JARVIS may look anywhere he could look himself)."""
    target = Path(path).expanduser() if str(path).strip() \
        else Path.home()
    if not target.exists():
        return {"error": f"list_files failed: no such folder: {target}"}
    if not target.is_dir():
        return {"error": f"list_files failed: not a folder: {target}"}
    entries = []
    try:
        children = sorted(target.iterdir(),
                          key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        return {"error": f"list_files failed: {type(exc).__name__}: {exc}"}
    for child in children[:200]:
        try:
            size = child.stat().st_size if child.is_file() else None
        except OSError:
            size = None
        entries.append({"name": child.name,
                        "kind": "dir" if child.is_dir() else "file",
                        "size": size})
    return {"path": str(target), "entries": entries,
            "truncated": len(children) > 200}


def op_read_file(path: str, max_chars: int = 12000):
    """Read a text file's contents. Binary files are reported as
    binary (with their size) instead of dumping garbage."""
    target = Path(str(path)).expanduser()
    if not target.exists():
        return {"error": f"read_file failed: no such file: {target}"}
    if not target.is_file():
        return {"error": f"read_file failed: not a file: {target}"}
    try:
        raw = target.read_bytes()[:2_000_000]
    except OSError as exc:
        return {"error": f"read_file failed: {type(exc).__name__}: {exc}"}
    if b"\x00" in raw[:8192]:
        return {"path": str(target), "binary": True,
                "size": target.stat().st_size,
                "note": "binary file — contents not shown as text"}
    text = raw.decode("utf-8", errors="replace")
    return {"path": str(target), "text": text[:max_chars],
            "truncated": len(text) > max_chars}


def op_search_files(root: str = "", query: str = "", limit: int = 20):
    """Find files by name anywhere under a root (default: home)."""
    base = Path(root).expanduser() if str(root).strip() else Path.home()
    if not base.exists():
        return {"error": f"search_files failed: no such folder: {base}"}
    needle = str(query).lower()
    matches, scanned = [], 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            scanned += 1
            if scanned > 20000:
                return {"root": str(base), "matches": matches,
                        "note": "scan limit reached"}
            if needle in name.lower():
                matches.append(str(Path(dirpath) / name))
                if len(matches) >= int(limit):
                    return {"root": str(base), "matches": matches}
    return {"root": str(base), "matches": matches}


def op_write_file(path: str, text: str):
    """Write a text file (creating folders as needed). The owner's
    PC, the owner's order: JARVIS does the work end-to-end instead of
    handing him a file to save himself."""
    target = Path(str(path)).expanduser()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(text), encoding="utf-8")
    except OSError as exc:
        return {"error": f"write_file failed: {type(exc).__name__}: {exc}"}
    return {"written": str(target), "chars": len(str(text))}


_ENV_ALLOW = ("PATH", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP",
              "USERPROFILE", "HOME", "LANG", "PATHEXT", "COMSPEC")


def _scrubbed_env() -> dict:
    """Only the variables a program needs to run — never the owner's
    API keys or session secrets. (The v2 lesson, kept: code JARVIS
    runs must not be able to read the keyring's environment.)"""
    return {k: os.environ[k] for k in _ENV_ALLOW if k in os.environ}


def op_run_code(code: str, timeout: int = 30, cwd: str = ""):
    """Run Python code and report honestly what happened: exit code,
    stdout, stderr. Sandboxed working folder, scrubbed environment,
    hard timeout — real execution, bounded blast radius."""
    if str(cwd).strip():
        workdir = Path(str(cwd)).expanduser()
    else:
        workdir = Path(tempfile.mkdtemp(prefix="jarvis-code-"))
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        script = workdir / "_run.py"
        script.write_text(str(code), encoding="utf-8")
    except OSError as exc:
        return {"error": f"run_code failed: {type(exc).__name__}: {exc}"}
    limit = max(1, min(int(timeout), 120))
    try:
        proc = subprocess.run(
            [sys.executable, str(script)], capture_output=True,
            text=True, timeout=limit, env=_scrubbed_env(),
            cwd=str(workdir))
    except subprocess.TimeoutExpired:
        return {"ok": False, "cwd": str(workdir),
                "error": f"run_code timed out after {limit}s — "
                         "the code was stopped"}
    return {"ok": proc.returncode == 0, "exit_code": proc.returncode,
            "stdout": proc.stdout[-6000:],
            "stderr": proc.stderr[-3000:], "cwd": str(workdir)}


def op_run_shell(command: str, timeout: int = 30, cwd: str = ""):
    """Run ONE shell command in the terminal — the coding
    agent's hands (Antigravity parity: editor + terminal +
    browser). Windows runs PowerShell, everywhere else sh.
    Same rails as run_code: scrubbed environment, hard
    timeout, honest exit code and output tails. The gate
    (code.run) decides who may call this; the knowledge
    pack (knowledge/linux-terminal.md) teaches JARVIS how
    to drive a shell well."""
    cmd = str(command or "").strip()
    if not cmd:
        return {"error": "run_shell: khali command"}
    if str(cwd).strip():
        workdir = Path(str(cwd)).expanduser()
    else:
        workdir = Path(tempfile.mkdtemp(prefix="jarvis-shell-"))
    try:
        workdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"error": f"run_shell failed: "
                         f"{type(exc).__name__}: {exc}"}
    if os.name == "nt":
        argv = ["powershell", "-NoProfile", "-NonInteractive",
                "-Command", cmd]
    else:
        argv = ["/bin/sh", "-c", cmd]
    limit = max(1, min(int(timeout), 120))
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=limit, env=_scrubbed_env(),
            cwd=str(workdir))
    except subprocess.TimeoutExpired:
        return {"ok": False, "cwd": str(workdir),
                "error": f"run_shell timed out after {limit}s "
                         "— command rok di gayi"}
    except OSError as exc:
        return {"error": f"run_shell failed: "
                         f"{type(exc).__name__}: {exc}"}
    return {"ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": proc.stdout[-6000:],
            "stderr": proc.stderr[-3000:],
            "cwd": str(workdir)}


def op_create_project(path: str, files: list):
    """Create a project folder from [{path, content}, ...] — the
    'build it' half of the code skill. Paths may not escape the
    project folder."""
    base = Path(str(path)).expanduser()
    written = []
    try:
        for item in files or []:
            rel = Path(str(item["path"]))
            if rel.is_absolute() or ".." in rel.parts:
                return {"error": "create_project refused an unsafe "
                                 f"path: {item['path']}"}
            target = base / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(item["content"]), encoding="utf-8")
            written.append(str(target))
    except (OSError, KeyError, TypeError) as exc:
        return {"error": f"create_project failed: "
                         f"{type(exc).__name__}: {exc}"}
    return {"project": str(base), "written": written}


def op_browser_launch():
    from jarvis.browser_cdp import op_browser_launch as _launch
    return _launch()


def op_browser_open(url: str):
    from jarvis.browser_cdp import op_browser_open as _open
    return _open(url)


def op_browser_text(max_chars: int = 12000):
    from jarvis.browser_cdp import op_browser_text as _text
    return _text(max_chars)


def op_browser_click(text: str, confirm: bool = False):
    from jarvis.browser_cdp import op_browser_click as _click
    return _click(text, confirm=confirm)


def op_browser_snapshot(max_chars: int = 12000):
    from jarvis.browser_cdp import op_browser_snapshot as _snap
    return _snap(max_chars)


def op_browser_screenshot():
    from jarvis.browser_cdp import op_browser_screenshot as _shot
    return _shot()


def op_browser_click_ref(ref: str, confirm: bool = False):
    from jarvis.browser_cdp import op_browser_click_ref as _click_ref
    return _click_ref(ref, confirm)


def op_browser_type(text: str, into: str = ""):
    from jarvis.browser_cdp import op_browser_type as _type
    return _type(text, into)


def op_browser_status():
    from jarvis.browser_cdp import op_browser_status as _status
    return _status()


def op_open_app(name: str):
    """Launch an application by name — FOUR routes, tried in
    order (the options rule: ye nahi to woh): os.startfile,
    direct spawn, PowerShell Start-Process, and finally the
    Start-menu AppsFolder lookup by display name. Real on
    Windows; honest refusal where no driver exists."""
    if sys.platform != "win32":
        return {"error": f"driver not present on this OS "
                         f"({sys.platform}): open_app can only "
                         f"launch {name!r} on Windows"}
    try:
        os.startfile(name)  # noqa: S606 — owner's own machine
        return {"launched": name, "route": "startfile"}
    except (OSError, AttributeError):
        pass
    try:
        subprocess.Popen([name])  # noqa: S603 — same reasoning
        return {"launched": name, "route": "spawn"}
    except OSError:
        pass
    from jarvis.pc import run_ps
    out = run_ps(f"Start-Process '{str(name).replace(chr(39), chr(39) * 2)}'")
    if not out.get("error"):
        return {"launched": name, "route": "powershell"}
    # Last route: the Start menu's own catalog (Get-StartApps).
    # An EXACT name match wins; a substring match is the
    # fallback — and the result reports the app that ACTUALLY
    # launched, not the name that was asked for (audit
    # 2026-10-22: the first substring hit launched and the
    # requested name was reported as if it had).
    safe = str(name).replace("'", "''")
    like = safe.replace("`", "``").replace("*", "`*").replace(
        "?", "`?").replace("[", "`[").replace("]", "`]")
    lookup = run_ps(
        "$a = Get-StartApps; "
        f"$m = $a | Where-Object {{$_.Name -ieq '{safe}'}} | "
        "Select-Object -First 1; "
        f"if (-not $m) {{ $m = $a | Where-Object {{$_.Name "
        f"-like '*{like}*'}} | Select-Object -First 1 }}; "
        "if ($m) { $m.Name + '|' + $m.AppID }")
    found = (lookup.get("stdout") or "").strip()
    matched, _, app_id = found.partition("|")
    if app_id and not lookup.get("error"):
        try:
            os.startfile(f"shell:AppsFolder\\{app_id}")
            return {"launched": matched or name,
                    "requested": name, "route": "appsfolder",
                    "app_id": app_id}
        except OSError:
            pass
    return {"error": f"open_app failed to launch {name!r} — "
                     "chaaron raaste band mile, sir"}


def op_open_app_legacy(name: str):
    """The ORIGINAL open_app, preserved exactly as it first
    shipped (the owner's version doctrine: upgrades never
    delete — the old tool stays, callable forever). Two plain
    routes: os.startfile, then a direct spawn. The current
    open_app keeps these as its first two routes and adds
    PowerShell + AppsFolder after them; this legacy tool is
    the untouched original for when the plain way is wanted
    — and it is open_app's registered fallback route."""
    if sys.platform == "win32":
        try:
            os.startfile(name)  # noqa: S606 — owner's own machine
            return {"launched": name}
        except (OSError, AttributeError):
            try:
                subprocess.Popen([name])  # noqa: S603 — same
                return {"launched": name}
            except OSError as exc:
                return {"error": f"open_app failed to launch "
                                 f"{name!r}: {exc}"}
    return {"error": f"driver not present on this OS "
                     f"({sys.platform}): open_app can only "
                     f"launch {name!r} on Windows"}


# -- the PC domain library (jarvis/pc.py) -------------------------

def op_pc_info():
    from jarvis.pc import op_pc_info as _f
    return _f()


def op_process_list(name: str = ""):
    from jarvis.pc import op_process_list as _f
    return _f(name)


def op_window_list():
    from jarvis.pc import op_window_list as _f
    return _f()


def op_disk_usage():
    from jarvis.pc import op_disk_usage as _f
    return _f()


def op_battery_status():
    from jarvis.pc import op_battery_status as _f
    return _f()


def op_clipboard_read():
    from jarvis.pc import op_clipboard_read as _f
    return _f()


def op_clipboard_write(text: str):
    from jarvis.pc import op_clipboard_write as _f
    return _f(text)


def op_volume_get():
    from jarvis.pc import op_volume_get as _f
    return _f()


def op_volume_set(level: int):
    from jarvis.pc import op_volume_set as _f
    return _f(level)


def op_process_kill(target: str):
    from jarvis.pc import op_process_kill as _f
    return _f(target)


def op_empty_recycle_bin():
    from jarvis.pc import op_empty_recycle_bin as _f
    return _f()


def op_lock_pc():
    from jarvis.pc import op_lock_pc as _f
    return _f()


# ---------------------------------------------------------------------
# manifests
# ---------------------------------------------------------------------

TIME_MANIFEST = {
    "name": "core.time",
    "version": "1.0.0",
    "tools": [{
        "name": "time_now",
        "op": "time_now",
        "permission": "clock.read",
        "schema": {"type": "object", "properties": {},
                   "additionalProperties": False},
    }],
}

MEMORY_MANIFEST = {
    "name": "core.memory",
    "version": "1.1.0",
    "tools": [
        {
            "name": "remember_fact",
            "op": "remember_fact",
            "permission": "memory.write",
            "undoable": True,
            "schema": {"type": "object",
                       "properties": {"text": {"type": "string",
                                               "minLength": 1}},
                       "required": ["text"],
                       "additionalProperties": False},
        },
        {
            "name": "recall_memory",
            "op": "recall_memory",
            "permission": "memory.read",
            "schema": {"type": "object",
                       "properties": {"query": {"type": "string"}},
                       "additionalProperties": False},
        },
        {
            "name": "learn_text",
            "op": "learn_text",
            "permission": "memory.write",
            "schema": {"type": "object",
                       "properties": {"text": {"type": "string",
                                               "minLength": 1},
                                      "title": {"type": "string"}},
                       "required": ["text"],
                       "additionalProperties": False},
        },
        {
            "name": "learn_url",
            "op": "learn_url",
            "permission": "memory.write",
            "schema": {"type": "object",
                       "properties": {"url": {"type": "string",
                                              "minLength": 1}},
                       "required": ["url"],
                       "additionalProperties": False},
        },
    ],
}

WEB_MANIFEST = {
    "name": "core.web",
    "version": "1.1.0",
    "tools": [
        {
            "name": "web_search",
            "op": "web_search",
            "permission": "net.outbound",
            "schema": {"type": "object",
                       "properties": {"query": {"type": "string", "minLength": 1},
                                      "limit": {"type": "integer",
                                                "minimum": 1, "maximum": 10}},
                       "required": ["query"],
                       "additionalProperties": False},
        },
        {
            "name": "web_fetch",
            "op": "web_fetch",
            "permission": "net.outbound",
            "schema": {"type": "object",
                       "properties": {"url": {"type": "string", "minLength": 1},
                                      "max_chars": {"type": "integer",
                                                    "minimum": 500,
                                                    "maximum": 40000}},
                       "required": ["url"],
                       "additionalProperties": False},
        },
        {
            "name": "web_scrape",
            "op": "web_scrape",
            "permission": "net.outbound",
            "schema": {"type": "object",
                       "properties": {"url": {"type": "string", "minLength": 1},
                                      "max_chars": {"type": "integer",
                                                    "minimum": 500,
                                                    "maximum": 40000}},
                       "required": ["url"],
                       "additionalProperties": False},
        },
        {
            "name": "search_browser",
            "op": "search_browser",
            "permission": "browser.control",
            "schema": {"type": "object",
                       "properties": {"query": {"type": "string", "minLength": 1},
                                      "engine": {"type": "string"}},
                       "required": ["query"],
                       "additionalProperties": False},
        },
        {
            "name": "web_fetch_tor",
            "op": "web_fetch_tor",
            "permission": "net.outbound",
            "schema": {"type": "object",
                       "properties": {"url": {"type": "string", "minLength": 1},
                                      "max_chars": {"type": "integer",
                                                    "minimum": 500,
                                                    "maximum": 40000}},
                       "required": ["url"],
                       "additionalProperties": False},
        },
        {
            "name": "wikipedia",
            "op": "wikipedia",
            "permission": "net.outbound",
            "schema": {"type": "object",
                       "properties": {"query": {"type": "string", "minLength": 1}},
                       "required": ["query"],
                       "additionalProperties": False},
        },
        {
            "name": "github_search",
            "op": "github_search",
            "permission": "net.outbound",
            "schema": {"type": "object",
                       "properties": {"query": {"type": "string", "minLength": 1},
                                      "limit": {"type": "integer",
                                                "minimum": 1, "maximum": 10}},
                       "required": ["query"],
                       "additionalProperties": False},
        },
        {
            "name": "github_repo",
            "op": "github_repo",
            "permission": "net.outbound",
            "schema": {"type": "object",
                       "properties": {"full_name": {"type": "string", "minLength": 3}},
                       "required": ["full_name"],
                       "additionalProperties": False},
        },
    ],
}

KNOWLEDGE_MANIFEST = {
    "name": "core.knowledge",
    "version": "1.0.0",
    "tools": [{
        "name": "knowledge_search",
        "op": "knowledge_search",
        "permission": "knowledge.read",
        "schema": {"type": "object",
                   "properties": {"query": {"type": "string", "minLength": 1},
                                  "limit": {"type": "integer",
                                            "minimum": 1, "maximum": 8}},
                   "required": ["query"],
                   "additionalProperties": False},
    }],
}

FILES_MANIFEST = {
    "name": "core.files",
    "version": "1.0.0",
    "tools": [
        {
            "name": "list_files",
            "op": "list_files",
            "permission": "files.read",
            "schema": {"type": "object",
                       "properties": {"path": {"type": "string"}},
                       "additionalProperties": False},
        },
        {
            "name": "read_file",
            "op": "read_file",
            "permission": "files.read",
            "schema": {"type": "object",
                       "properties": {"path": {"type": "string",
                                               "minLength": 1},
                                      "max_chars": {"type": "integer",
                                                    "minimum": 500,
                                                    "maximum": 40000}},
                       "required": ["path"],
                       "additionalProperties": False},
        },
        {
            "name": "search_files",
            "op": "search_files",
            "permission": "files.read",
            "schema": {"type": "object",
                       "properties": {"root": {"type": "string"},
                                      "query": {"type": "string",
                                                "minLength": 1},
                                      "limit": {"type": "integer",
                                                "minimum": 1,
                                                "maximum": 50}},
                       "required": ["query"],
                       "additionalProperties": False},
        },
        {
            "name": "write_file",
            "op": "write_file",
            "permission": "files.write",
            "schema": {"type": "object",
                       "properties": {"path": {"type": "string",
                                               "minLength": 1},
                                      "text": {"type": "string"}},
                       "required": ["path", "text"],
                       "additionalProperties": False},
        },
    ],
}

CODE_MANIFEST = {
    "name": "core.code",
    "version": "1.0.0",
    "tools": [
        {
            "name": "run_code",
            "op": "run_code",
            "permission": "code.run",
            "schema": {"type": "object",
                       "properties": {"code": {"type": "string",
                                               "minLength": 1},
                                      "timeout": {"type": "integer",
                                                  "minimum": 1,
                                                  "maximum": 120},
                                      "cwd": {"type": "string"}},
                       "required": ["code"],
                       "additionalProperties": False},
        },
        {
            "name": "run_shell",
            "op": "run_shell",
            "permission": "code.run",
            "schema": {"type": "object",
                       "properties": {
                           "command": {"type": "string",
                                       "minLength": 1},
                           "timeout": {"type": "integer",
                                       "minimum": 1,
                                       "maximum": 120},
                           "cwd": {"type": "string"}},
                       "required": ["command"],
                       "additionalProperties": False},
        },
        {
            "name": "create_project",
            "op": "create_project",
            "permission": "files.write",
            "schema": {"type": "object",
                       "properties": {
                           "path": {"type": "string", "minLength": 1},
                           "files": {"type": "array",
                                     "items": {"type": "object",
                                               "properties": {
                                                   "path": {"type": "string"},
                                                   "content": {"type": "string"}},
                                               "required": ["path",
                                                            "content"]}}},
                       "required": ["path", "files"],
                       "additionalProperties": False},
        },
    ],
}

BROWSER_MANIFEST = {
    "name": "core.browser",
    "version": "1.0.0",
    "tools": [
        {
            "name": "browser_launch",
            "op": "browser_launch",
            "permission": "browser.control",
            "schema": {"type": "object", "properties": {},
                       "additionalProperties": False},
        },
        {
            "name": "browser_open",
            "op": "browser_open",
            "permission": "browser.control",
            "schema": {"type": "object",
                       "properties": {"url": {"type": "string",
                                              "minLength": 1}},
                       "required": ["url"],
                       "additionalProperties": False},
        },
        {
            "name": "browser_text",
            "op": "browser_text",
            "permission": "browser.control",
            "schema": {"type": "object",
                       "properties": {"max_chars": {"type": "integer",
                                                    "minimum": 500,
                                                    "maximum": 40000}},
                       "additionalProperties": False},
        },
        {
            "name": "browser_click",
            "op": "browser_click",
            "permission": "browser.control",
            "schema": {"type": "object",
                       "properties": {"text": {"type": "string",
                                               "minLength": 1},
                                      "confirm": {
                                          "type": "boolean"}},
                       "required": ["text"],
                       "additionalProperties": False},
        },
        {
            "name": "browser_type",
            "op": "browser_type",
            "permission": "browser.control",
            "schema": {"type": "object",
                       "properties": {"text": {"type": "string"},
                                      "into": {"type": "string"}},
                       "required": ["text"],
                       "additionalProperties": False},
        },
        {
            "name": "browser_status",
            "op": "browser_status",
            "permission": "browser.control",
            "schema": {"type": "object", "properties": {},
                       "additionalProperties": False},
        },
        {
            "name": "browser_snapshot",
            "op": "browser_snapshot",
            "permission": "browser.control",
            "schema": {"type": "object",
                       "properties": {"max_chars": {"type": "integer"}},
                       "additionalProperties": False},
        },
        {
            "name": "browser_screenshot",
            "op": "browser_screenshot",
            "permission": "browser.control",
            "schema": {"type": "object", "properties": {},
                       "additionalProperties": False},
        },
        {
            "name": "browser_click_ref",
            "op": "browser_click_ref",
            "permission": "browser.control",
            "schema": {"type": "object",
                       "properties": {"ref": {"type": "string",
                                              "minLength": 2},
                                      "confirm": {"type": "boolean"}},
                       "required": ["ref"],
                       "additionalProperties": False},
        },
    ],
}

def _pc_tool(name, properties=None, required=(), **flags):
    tool = {"name": name, "op": name, "permission": "pc.control",
            "schema": {"type": "object",
                       "properties": properties or {},
                       "required": list(required),
                       "additionalProperties": False}}
    tool.update(flags)
    return tool


PC_MANIFEST = {
    "name": "core.pc",
    "version": "1.1.0",
    "tools": [
        {"name": "open_app",
         "op": "open_app",
         "permission": "pc.control",
         "undoable": True,
         "schema": {"type": "object",
                    "properties": {"name": {"type": "string", "minLength": 1}},
                    "required": ["name"],
                    "additionalProperties": False}},
        {"name": "open_app_legacy",
         "op": "open_app_legacy",
         "permission": "pc.control",
         "undoable": True,
         "schema": {"type": "object",
                    "properties": {"name": {"type": "string", "minLength": 1}},
                    "required": ["name"],
                    "additionalProperties": False}},
        _pc_tool("pc_info"),
        _pc_tool("process_list",
                 {"name": {"type": "string"}}),
        _pc_tool("window_list"),
        _pc_tool("disk_usage"),
        _pc_tool("battery_status"),
        _pc_tool("clipboard_read"),
        _pc_tool("clipboard_write",
                 {"text": {"type": "string", "minLength": 1}},
                 ("text",)),
        _pc_tool("volume_get"),
        _pc_tool("volume_set",
                 {"level": {"type": "integer", "minimum": 0,
                            "maximum": 100}},
                 ("level",)),
        _pc_tool("process_kill",
                 {"target": {"type": "string", "minLength": 1}},
                 ("target",), destructive=True),
        _pc_tool("empty_recycle_bin", destructive=True),
        _pc_tool("lock_pc", destructive=True),
    ],
}

ALL_MANIFESTS = (TIME_MANIFEST, MEMORY_MANIFEST, WEB_MANIFEST,
                 KNOWLEDGE_MANIFEST, FILES_MANIFEST, CODE_MANIFEST,
                 BROWSER_MANIFEST, PC_MANIFEST)

# Every capability the first-party skills can ask for — the sample
# allowances a fully-permitted owner turn would carry.
ALL_PERMISSIONS = frozenset({
    "clock.read", "memory.read", "memory.write", "net.outbound",
    "pc.control", "knowledge.read", "files.read", "files.write",
    "code.run", "browser.control",
})


def build_default_registry(gate=None, journal=None, memory=None) -> SkillRegistry:
    """Registry with all first-party skills registered AND approved
    (first-party code is trusted by the app itself; third-party skills
    must earn approval through the owner's flow)."""
    registry = SkillRegistry(gate=gate, journal=journal)
    registry.register(TIME_MANIFEST, ops={"time_now": op_time_now})
    registry.register(MEMORY_MANIFEST, ops=_memory_ops(memory))
    registry.register(WEB_MANIFEST, ops={"web_search": op_web_search,
                                         "web_fetch": op_web_fetch,
                                         "web_scrape": op_web_scrape,
                                         "search_browser":
                                             op_search_browser,
                                         "web_fetch_tor":
                                             op_web_fetch_tor,
                                         "wikipedia": op_wikipedia,
                                         "github_search":
                                             op_github_search,
                                         "github_repo":
                                             op_github_repo})
    registry.register(KNOWLEDGE_MANIFEST,
                      ops={"knowledge_search": op_knowledge_search})
    registry.register(FILES_MANIFEST,
                      ops={"list_files": op_list_files,
                           "read_file": op_read_file,
                           "search_files": op_search_files,
                           "write_file": op_write_file})
    registry.register(CODE_MANIFEST,
                      ops={"run_code": op_run_code,
                           "run_shell": op_run_shell,
                           "create_project": op_create_project})
    registry.register(BROWSER_MANIFEST,
                      ops={"browser_launch": op_browser_launch,
                           "browser_open": op_browser_open,
                           "browser_text": op_browser_text,
                           "browser_click": op_browser_click,
                           "browser_type": op_browser_type,
                           "browser_status": op_browser_status,
                           "browser_snapshot": op_browser_snapshot,
                           "browser_screenshot": op_browser_screenshot,
                           "browser_click_ref": op_browser_click_ref})
    registry.register(PC_MANIFEST, ops={
        "open_app": op_open_app,
        "open_app_legacy": op_open_app_legacy,
        "pc_info": op_pc_info,
        "process_list": op_process_list,
        "window_list": op_window_list,
        "disk_usage": op_disk_usage,
        "battery_status": op_battery_status,
        "clipboard_read": op_clipboard_read,
        "clipboard_write": op_clipboard_write,
        "volume_get": op_volume_get,
        "volume_set": op_volume_set,
        "process_kill": op_process_kill,
        "empty_recycle_bin": op_empty_recycle_bin,
        "lock_pc": op_lock_pc})
    for manifest in ALL_MANIFESTS:
        registry.approve(manifest["name"])
    return registry
