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
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

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

    return {"remember_fact": remember_fact, "recall_memory": recall_memory}


_DDG_HTML = "https://html.duckduckgo.com/html/?q="
_RESULT_LINK_RE = re.compile(
    r'class="result__a"[^>]*href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>',
    re.S)
_RESULT_SNIPPET_RE = re.compile(
    r'class="result__snippet"[^>]*>(?P<snippet>.*?)</(?:a|td)>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(fragment: str) -> str:
    return html.unescape(_TAG_RE.sub("", fragment)).strip()


def _real_target(ddg_url: str) -> str:
    """DuckDuckGo wraps result links; unwrap to the real target."""
    parsed = urllib.parse.urlparse(html.unescape(ddg_url))
    qs = urllib.parse.parse_qs(parsed.query)
    if "uddg" in qs:
        return qs["uddg"][0]
    return html.unescape(ddg_url)


def op_web_search(query: str, limit: int = 5):
    """Real search via DuckDuckGo's HTML endpoint. Honest errors."""
    url = _DDG_HTML + urllib.parse.quote_plus(query)
    request = urllib.request.Request(
        url, headers={"User-Agent": "JARVIS-v4/1.0 (stdlib urllib)"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            page = response.read().decode("utf-8", errors="replace")
    except Exception as exc:  # network down, blocked, timeout — say so
        return {"error": f"web_search failed: {type(exc).__name__}: {exc}"}
    links = list(_RESULT_LINK_RE.finditer(page))
    snippets = [_clean(m.group("snippet"))
                for m in _RESULT_SNIPPET_RE.finditer(page)]
    results = []
    for i, match in enumerate(links[:limit]):
        results.append({
            "title": _clean(match.group("title")),
            "url": _real_target(match.group("url")),
            "snippet": snippets[i] if i < len(snippets) else "",
        })
    if not results:
        return {"error": "web_search failed: no results parsed from "
                         "DuckDuckGo HTML (page shape may have changed)",
                "results": []}
    return {"results": results}


def op_open_app(name: str):
    """Launch an application by name. Real on Windows; honest refusal
    (as a result, not an exception) where no driver exists."""
    if sys.platform == "win32":
        try:
            os.startfile(name)  # noqa: S606 — owner's own machine, his app
            return {"launched": name}
        except (OSError, AttributeError):
            try:
                subprocess.Popen([name])  # noqa: S603 — same reasoning
                return {"launched": name}
            except OSError as exc:
                return {"error": f"open_app failed to launch {name!r}: {exc}"}
    return {"error": f"driver not present on this OS ({sys.platform}): "
                     f"open_app can only launch {name!r} on Windows"}


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
    "version": "1.0.0",
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
    ],
}

WEB_MANIFEST = {
    "name": "core.web",
    "version": "1.0.0",
    "tools": [{
        "name": "web_search",
        "op": "web_search",
        "permission": "net.outbound",
        "schema": {"type": "object",
                   "properties": {"query": {"type": "string", "minLength": 1},
                                  "limit": {"type": "integer",
                                            "minimum": 1, "maximum": 10}},
                   "required": ["query"],
                   "additionalProperties": False},
    }],
}

PC_MANIFEST = {
    "name": "core.pc",
    "version": "1.0.0",
    "tools": [{
        "name": "open_app",
        "op": "open_app",
        "permission": "pc.control",
        "undoable": True,
        "schema": {"type": "object",
                   "properties": {"name": {"type": "string", "minLength": 1}},
                   "required": ["name"],
                   "additionalProperties": False},
    }],
}

ALL_MANIFESTS = (TIME_MANIFEST, MEMORY_MANIFEST, WEB_MANIFEST, PC_MANIFEST)

# Every capability the first-party skills can ask for — the sample
# allowances a fully-permitted owner turn would carry.
ALL_PERMISSIONS = frozenset({
    "clock.read", "memory.read", "memory.write", "net.outbound", "pc.control",
})


def build_default_registry(gate=None, journal=None, memory=None) -> SkillRegistry:
    """Registry with all first-party skills registered AND approved
    (first-party code is trusted by the app itself; third-party skills
    must earn approval through the owner's flow)."""
    registry = SkillRegistry(gate=gate, journal=journal)
    registry.register(TIME_MANIFEST, ops={"time_now": op_time_now})
    registry.register(MEMORY_MANIFEST, ops=_memory_ops(memory))
    registry.register(WEB_MANIFEST, ops={"web_search": op_web_search})
    registry.register(PC_MANIFEST, ops={"open_app": op_open_app})
    for manifest in ALL_MANIFESTS:
        registry.approve(manifest["name"])
    return registry
