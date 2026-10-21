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

    return {"remember_fact": remember_fact,
            "recall_memory": recall_memory,
            "learn_text": learn_text}


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
        target, headers={"User-Agent": "JARVIS-v4/1.0 (stdlib urllib)"})
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

ALL_MANIFESTS = (TIME_MANIFEST, MEMORY_MANIFEST, WEB_MANIFEST,
                 KNOWLEDGE_MANIFEST, FILES_MANIFEST, CODE_MANIFEST,
                 PC_MANIFEST)

# Every capability the first-party skills can ask for — the sample
# allowances a fully-permitted owner turn would carry.
ALL_PERMISSIONS = frozenset({
    "clock.read", "memory.read", "memory.write", "net.outbound",
    "pc.control", "knowledge.read", "files.read", "files.write",
    "code.run",
})


def build_default_registry(gate=None, journal=None, memory=None) -> SkillRegistry:
    """Registry with all first-party skills registered AND approved
    (first-party code is trusted by the app itself; third-party skills
    must earn approval through the owner's flow)."""
    registry = SkillRegistry(gate=gate, journal=journal)
    registry.register(TIME_MANIFEST, ops={"time_now": op_time_now})
    registry.register(MEMORY_MANIFEST, ops=_memory_ops(memory))
    registry.register(WEB_MANIFEST, ops={"web_search": op_web_search,
                                         "web_fetch": op_web_fetch})
    registry.register(KNOWLEDGE_MANIFEST,
                      ops={"knowledge_search": op_knowledge_search})
    registry.register(FILES_MANIFEST,
                      ops={"list_files": op_list_files,
                           "read_file": op_read_file,
                           "search_files": op_search_files,
                           "write_file": op_write_file})
    registry.register(CODE_MANIFEST,
                      ops={"run_code": op_run_code,
                           "create_project": op_create_project})
    registry.register(PC_MANIFEST, ops={"open_app": op_open_app})
    for manifest in ALL_MANIFESTS:
        registry.approve(manifest["name"])
    return registry
