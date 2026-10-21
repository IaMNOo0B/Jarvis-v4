"""JARVIS v4 chat core — the spine wiring (S5).

`build_app` is the ONE place the spine is assembled:

    journal -> config -> secrets -> gate -> memory -> registry
    (builtin skills + the app's reminder skill) -> chain (providers)
    -> gate-mediated dispatch -> brain

For each part, build_app prefers the dedicated module (jarvis.config,
jarvis.secrets, jarvis.providers, jarvis.brain, jarvis.gate,
jarvis.skills / jarvis.builtin_skills) and falls back to
jarvis._defaults, which implements the same contract. Re-pointing a
part is a one-line change here, nowhere else.

A turn (ChatApp.turn):
  1. journal user.message (IFC-labeled, origin "owner") + note it in
     conversation history;
  2. a deterministic fast-path router answers zero-LLM commands —
     time/date questions (via the `time_now` tool, through the same
     gate-mediated dispatch as everything else), "yaad rakho /
     remember ..." (memory.learn), recall questions (memory.recall);
  3. everything else goes to Brain.respond with the advertised tools
     and the gate-mediated dispatch: gate allow -> execute; deny ->
     honest error result; require_approval -> in this headless core,
     an honest "needs your approval" tool result (the approval token
     is NEVER handed to the model; the GUI approval UX is a later
     step);
  4. journal assistant.reply + note the reply in history.

ALLOWANCES (DEFAULT_ALLOWANCES): the capabilities a turn may execute
without asking — clock.read, memory.read, memory.write,
reminder.read, reminder.write, net.outbound (net.outbound is in the
set so the approved web_search skill actually works — LAW 1: an
approved skill whose permission is never granted is a dead skill).
pc.control IS in the set: this is the owner's own PC and his standing
doctrine is that JARVIS acts (PC control was ON by default in v2; his
core frustration was a JARVIS that would not act). Destructive PC
operations still require a gate approval token — being advertised is
not being unguarded.

CLI:  python -m jarvis.chat --data-dir DIR [--once "text"]
Stdlib only.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import _defaults, ifc, rag
from .goodput import GoodputLedger
from .journal import Journal
from .memory import Memory

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# See module docstring.
DEFAULT_ALLOWANCES = frozenset({
    "clock.read",
    "memory.read",
    "memory.write",
    "reminder.read",
    "reminder.write",
    "net.outbound",
    "pc.control",
    "knowledge.read",
    "files.read",
    "files.write",
    "system.diagnose",
    "code.run",
    "self.upgrade",
    "notify.send",
    "browser.control",
})


# --------------------------------------------------------------------------- #
# The app's own reminder skill. builtin_skills ships time/memory/web/pc
# but no reminders, and "remind me" is core owner behaviour (it is in
# the eval case bank, from the owner's own live requests) — so the app
# registers this first-party skill itself, approved, in build_app.
# --------------------------------------------------------------------------- #

REMINDER_MANIFEST = {
    "name": "core.reminders",
    "version": "1.0.0",
    "tools": [
        {
            "name": "reminder_set",
            "op": "reminder_set",
            "permission": "reminder.write",
            "undoable": True,
            "schema": {"type": "object",
                       "properties": {
                           "in_minutes": {"type": "integer", "minimum": 1},
                           "text": {"type": "string", "minLength": 1}},
                       "required": ["text"],
                       "additionalProperties": False},
        },
        {
            "name": "reminder_list",
            "op": "reminder_list",
            "permission": "reminder.read",
            "schema": {"type": "object", "properties": {},
                       "additionalProperties": False},
        },
    ],
}

DIAGNOSE_MANIFEST = {
    "name": "core.diagnose",
    "version": "1.0.0",
    "tools": [{
        "name": "diagnose",
        "op": "diagnose",
        "permission": "system.diagnose",
        "schema": {"type": "object", "properties": {},
                   "additionalProperties": False},
    }],
}


def _diagnose_op(data_dir: Path, secrets):
    """Self-diagnosis from JARVIS's own records — no guessing: the
    preflight health checks, the persisted goodput ledger (which
    brains answered, which never did), the keys on file, and the
    journal's recent errors, distilled into plain findings. The brain
    calls this when things keep failing or the owner asks what is
    wrong; the owner never runs a diagnostic command himself."""
    data = Path(data_dir)

    def diagnose():
        from . import preflight
        root = Path(__file__).resolve().parent.parent
        checks = preflight.run_checks(root=root, data_dir=data)
        failed = [{"check": c["name"], "detail": c["detail"],
                   "fix": c.get("next_fix", "")}
                  for c in checks if not c["ok"]]

        providers = {}
        gp_path = data / "goodput.jsonl"
        if gp_path.exists():
            try:
                for line in gp_path.read_text(
                        encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    slot = providers.setdefault(
                        str(row["provider"]),
                        {"attempts": 0, "answered": 0})
                    slot["attempts"] += 1
                    if row.get("ok"):
                        slot["answered"] += 1
            except Exception:
                providers = {}

        keys_filed = {}
        try:
            keys_filed = _load_keys_module().list_keys(secrets)
        except Exception:
            keys_filed = {}

        recent_errors = []
        journal_path = data / "journal.jsonl"
        if journal_path.exists():
            try:
                lines = journal_path.read_text(
                    encoding="utf-8").splitlines()[-300:]
                for line in lines:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    payload = rec.get("payload") or {}
                    if isinstance(payload, dict) and payload.get("error"):
                        recent_errors.append({
                            "kind": rec.get("kind"),
                            "error": str(payload["error"])[:200]})
                recent_errors = recent_errors[-8:]
            except Exception:
                recent_errors = []

        findings = []
        for name, slot in sorted(providers.items()):
            if name == "stub":
                continue
            if slot["attempts"] >= 3 and slot["answered"] == 0:
                findings.append(
                    f"{name} ko {slot['attempts']} baar bulaya gaya, "
                    "ek bhi jawab nahi aaya — ye dimag abhi kaam nahi "
                    "kar raha (model/server/key ki kharabi ho sakti hai).")
        if providers.get("stub", {}).get("answered", 0) > 0 and \
                not any(s["answered"] for n, s in providers.items()
                        if n != "stub"):
            findings.append(
                "Aakhri jawab nakli stub dimag se aaye — koi asli "
                "dimag pahuch me nahi tha.")
        if not keys_filed:
            findings.append(
                "Koi cloud key lagi hi nahi hai — dimag sirf offline "
                "aur keyless backup par hai. /key se ek key lagane se "
                "sabse tez dimag jud jayega.")
        for item in failed:
            if "Ollama" in item["check"] and "not" in item["detail"]:
                findings.append(
                    "Ollama band hai ya chal nahi raha — offline dimag "
                    "uplabdh nahi hai.")
        if not findings:
            findings.append(
                "Koi badi kharabi nahi mili — sab systems apne record "
                "ke hisaab se theek hain.")
        return {"checks_failed": failed, "providers": providers,
                "keys_filed": keys_filed,
                "recent_errors": recent_errors, "findings": findings}

    return diagnose


RESEARCH_MANIFEST = {
    "name": "core.research",
    "version": "1.0.0",
    "tools": [{
        "name": "deep_research",
        "op": "deep_research",
        "permission": "net.outbound",
        "schema": {"type": "object",
                   "properties": {"topic": {"type": "string",
                                            "minLength": 1},
                                  "depth": {"type": "integer",
                                            "minimum": 1, "maximum": 2}},
                   "required": ["topic"],
                   "additionalProperties": False},
    }],
}


def _research_op(learned: LearnedStore):
    """Deep research the way the owner taught it: several angles,
    multiple sources, READ the pages (not just the snippets), keep
    the receipts — and save the study into the inbox, where the
    auto-learn system turns it into knowledge JARVIS keeps."""
    def deep_research(topic: str, depth: int = 1):
        from . import builtin_skills as bs
        topic = str(topic).strip()
        angles = [topic, f"{topic} how it works",
                  f"{topic} best practices examples"]
        if int(depth) >= 2:
            angles += [f"{topic} tutorial step by step",
                       f"{topic} common mistakes"]
        seen, sources = set(), []
        search_errors = []
        for query in angles:
            found = bs.op_web_search(query, limit=5)
            if "error" in found:
                search_errors.append(found["error"])
            for hit in found.get("results", []):
                if hit["url"] not in seen:
                    seen.add(hit["url"])
                    sources.append(hit)
        if not sources and search_errors:
            return {"error": "deep_research failed: search hi nahi "
                             f"ho paya — {search_errors[0]}",
                    "sources_found": 0, "fetch_failed": 0}
        page_cap = 4 if int(depth) <= 1 else 8
        read, failed = [], 0
        for src in sources[:page_cap]:
            page = bs.op_web_fetch(src["url"], max_chars=6000)
            if "error" in page:
                failed += 1
                continue
            read.append({"title": src["title"], "url": src["url"],
                         "text": page["text"]})
        if not read:
            return {"error": "deep_research failed: sources mile "
                             f"{len(sources)}, lekin ek bhi page "
                             "padha nahi ja saka",
                    "sources_found": len(sources),
                    "fetch_failed": failed}
        note = [f"# Research: {topic}", "", "## Sources"]
        for item in read:
            note.append(f"- {item['title']} — {item['url']}")
        for item in read:
            note += ["", f"## {item['title']}", item["url"], "",
                     item["text"][:4000]]
        saved = learned.add_document("\n".join(note),
                                     title=f"research-{topic}")
        return {"topic": topic, "sources_found": len(sources),
                "pages_read": len(read), "fetch_failed": failed,
                "saved_to": saved["learned_from"],
                "note": "ye research inbox me save ho gayi hai — "
                        "JARVIS ne ise seekh liya hai aur aage ke "
                        "jawabon me ye istemal hogi",
                "excerpts": [{"title": it["title"], "url": it["url"],
                              "excerpt": it["text"][:400]}
                             for it in read]}

    return deep_research


NOTIFY_MANIFEST = {
    "name": "core.notify",
    "version": "1.0.0",
    "tools": [{
        "name": "notify",
        "op": "notify",
        "permission": "notify.send",
        "schema": {"type": "object",
                   "properties": {"title": {"type": "string",
                                            "minLength": 1},
                                  "body": {"type": "string"}},
                   "required": ["title"],
                   "additionalProperties": False},
    }],
}


def _notify(data_dir, title, body="", kind="info") -> dict:
    """Append one notification — the channel everything important
    uses to reach the owner (reminders coming due, upgrade watch
    findings, skills promoted, the brain flagging something). The
    UI badge and the CLI both read this one file."""
    row = {"id": uuid.uuid4().hex[:8], "ts": time.time(),
           "title": str(title)[:200], "body": str(body)[:1000],
           "kind": str(kind), "read": False}
    try:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        with (Path(data_dir) / "notifications.jsonl").open(
                "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return row


def _read_notifications(data_dir, limit: int = 50) -> list:
    path = Path(data_dir) / "notifications.jsonl"
    rows = []
    if path.exists():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        except Exception:
            return []
    return rows[-limit:]


def _unread_count(data_dir) -> int:
    return sum(1 for r in _read_notifications(data_dir, limit=500)
               if not r.get("read"))


def _mark_notifications_read(data_dir) -> None:
    path = Path(data_dir) / "notifications.jsonl"
    rows = _read_notifications(data_dir, limit=1000)
    try:
        with path.open("w", encoding="utf-8") as fh:
            for row in rows:
                row["read"] = True
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _due_reminder_notifications(data_dir) -> int:
    """Reminders whose time has come become notifications — once
    each (a `notified` flag on the reminder row prevents repeats)."""
    path = Path(data_dir) / "reminders.json"
    if not path.exists():
        return 0
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    now = datetime.now(timezone.utc)
    created, changed = 0, False
    for row in rows:
        if row.get("done") or row.get("notified"):
            continue
        try:
            due = datetime.fromisoformat(str(row["due"]))
        except Exception:
            continue
        if due <= now:
            _notify(data_dir, "Reminder",
                    str(row.get("text", "")), kind="reminder")
            row["notified"] = True
            created += 1
            changed = True
    if changed:
        try:
            path.write_text(json.dumps(rows, ensure_ascii=False),
                            encoding="utf-8")
        except Exception:
            pass
    return created


def _reminder_ops(data_dir: Path):
    path = Path(data_dir) / "reminders.json"

    def _load() -> list:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return []

    def _save(rows: list) -> None:
        path.write_text(json.dumps(rows, ensure_ascii=False),
                        encoding="utf-8")

    def reminder_set(in_minutes: int = 10, text: str = ""):
        from datetime import timedelta
        due = datetime.now(timezone.utc) + timedelta(
            minutes=max(1, int(in_minutes)))
        row = {"id": uuid.uuid4().hex[:8], "text": str(text)[:500],
               "due": due.isoformat(), "done": False}
        rows = _load()
        rows.append(row)
        _save(rows)
        return {"id": row["id"], "due": row["due"], "text": row["text"]}

    def reminder_list():
        return {"reminders": [r for r in _load() if not r.get("done")]}

    return {"reminder_set": reminder_set, "reminder_list": reminder_list}


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #

def _resolve_module(modules, name):
    """The sibling module for a spine part, or jarvis._defaults."""
    if modules is not None and hasattr(modules, name):
        return getattr(modules, name)
    try:
        return importlib.import_module(f"jarvis.{name}")
    except ImportError:
        return _defaults


def _try_module(modules, name):
    """Like _resolve_module but returns None instead of the defaults —
    for optional modules (builtin_skills) with their own fallback
    chain."""
    if modules is not None and hasattr(modules, name):
        return getattr(modules, name)
    try:
        return importlib.import_module(f"jarvis.{name}")
    except ImportError:
        return None


def _build_registry(modules, gate, journal, memory, data_dir):
    builtins = _try_module(modules, "builtin_skills")
    if builtins is not None and hasattr(builtins,
                                        "build_default_registry"):
        return builtins.build_default_registry(
            gate=gate, journal=journal, memory=memory)
    skills_mod = _resolve_module(modules, "skills")
    if hasattr(skills_mod, "build_default_registry"):
        return skills_mod.build_default_registry(data_dir)
    registry_cls = getattr(skills_mod, "SkillRegistry", None)
    if registry_cls is not None and skills_mod is not _defaults:
        try:
            return registry_cls(gate=gate, journal=journal)
        except TypeError:
            return registry_cls()
    return _defaults.build_default_registry(data_dir)


def _build_chain(providers_mod, config, secrets, goodput):
    if hasattr(providers_mod, "build_fallback_chain"):
        return providers_mod.build_fallback_chain(config, secrets,
                                                  goodput)
    built = providers_mod.build_chain(config, secrets, goodput)
    if hasattr(built, "complete"):
        return built
    return providers_mod.FallbackChain(built, ledger=goodput)


def _load_keys_module():
    """jarvis.keys, or None when this build cannot file keys."""
    try:
        from jarvis import keys as keys_mod
        return keys_mod
    except Exception:
        return None


class _ChainSecrets:
    """What the provider chain sees when it asks for a provider's key.

    Keys filed through /key live under llm:<provider>:<n> and are read
    back through jarvis.keys.get_keys — the provider layer's one
    deliberate value exit-point. Plain-named secrets (the config /
    custom-provider path) still resolve through the store itself.
    Without this adapter the chain asked the store for a secret
    literally named after the provider, never found the filed keys,
    and silently ran keyless.
    """

    def __init__(self, store):
        self._store = store

    def _lookup(self, name):
        keys_mod = _load_keys_module()
        if keys_mod is not None:
            try:
                values = keys_mod.get_keys(self._store, name)
            except Exception:
                values = []
            if values:
                return values
        return self._store.get(name)

    def get(self, name):
        return self._lookup(name)

    def get_key(self, name):
        return self._lookup(name)


class _BrainChainAdapter:
    """Reply-shape glue between providers and brain.

    jarvis.providers answers with ProviderReply{text, tool_calls of
    ToolCall objects}; jarvis.brain.normalize_reply parses dicts,
    strings, or its own ProviderReply{content, ...}. This adapter is
    the translation, in the one wiring place: a providers-shaped reply
    becomes {"content", "tool_calls": [{name, args}], "provider"};
    anything already dict/str/brain-shaped passes through untouched.
    Exceptions propagate — a dead chain must look dead."""

    def __init__(self, inner):
        self.inner = inner
        self.name = getattr(inner, "name", "chain")
        self._seen_provider: Optional[str] = None

    @property
    def last_provider(self):
        return getattr(self.inner, "last_provider", None) \
            or self._seen_provider

    def complete(self, messages, tools=None):
        raw = self.inner.complete(messages, tools)
        if isinstance(raw, dict):
            if raw.get("provider"):
                self._seen_provider = str(raw["provider"])
            return raw
        if raw is None or isinstance(raw, str):
            return raw
        text = getattr(raw, "text", None)
        if text is None:
            return raw  # already brain-shaped
        calls = []
        for call in getattr(raw, "tool_calls", None) or []:
            if isinstance(call, dict):
                calls.append(call)
            else:
                calls.append({
                    "name": getattr(call, "name", ""),
                    "args": getattr(call, "arguments", None) or {}})
        provider = getattr(raw, "provider", "") or ""
        if provider:
            self._seen_provider = str(provider)
        return {"content": text or "", "tool_calls": calls,
                "provider": provider}


def _result_ok(result) -> bool:
    """Did a dispatch result succeed? (Same rule the brain's audit
    uses: an explicit ok=False, or an error payload without ok=True,
    is a failure.)"""
    if isinstance(result, dict):
        if result.get("ok") is False:
            return False
        if "error" in result and result.get("ok") is not True:
            return False
    return True


def make_dispatch(gate, registry, journal, allowances,
                  pass_decision=False, refused_exc=()):
    """The gate-mediated dispatch every tool call flows through.

    Journals tool.call (with the gate verdict) and tool.result — the
    journal is how behaviour is OBSERVED (the eval harness scores
    these events, never the model's claims).
    """
    def dispatch(name, args=None, origin="owner"):
        args = dict(args or {})
        decision = gate.evaluate(name, args, origin, allowances)
        verdict = getattr(decision, "verdict", "deny")
        journal.append("tool.call", {
            "tool": name, "args": args, "gate_verdict": verdict,
            "origin": origin})
        if verdict in ("allow", "allow_with_undo"):
            try:
                if pass_decision:
                    result = registry.dispatch(name, args,
                                               decision=decision)
                else:
                    result = registry.dispatch(name, args)
            except refused_exc as exc:
                result = {"ok": False, "error": str(exc)}
        elif verdict == "require_approval":
            result = {"ok": False, "needs_approval": True,
                      "error": f"{name} needs your approval before I "
                               f"run it, sir."}
        else:
            result = {"ok": False,
                      "error": f"gate denied {name}: "
                               f"{getattr(decision, 'reason', '')}"}
        if not isinstance(result, dict):
            result = {"ok": True, "result": result}
        journal.append("tool.result", {
            "tool": name, "ok": _result_ok(result),
            "needs_approval": bool(result.get("needs_approval"))})
        return result
    return dispatch


# --------------------------------------------------------------------------- #
# Learned documents — the owner's own study material.
#
# The owner drops .txt/.md files into data/inbox (or pastes long text in
# chat and says "learn this" — the learn_text tool writes it into the
# same inbox). scan() ingests anything new or changed AUTOMATICALLY:
# no command, no ceremony. Content is chunked with the repo's own
# parent-child chunker, persisted to data/learned.jsonl, and served to
# the brain through the memory facade below as owner-trust excerpts —
# so what he teaches JARVIS is actually present when JARVIS answers.
# --------------------------------------------------------------------------- #

class LearnedStore:
    """Inbox watcher + chunk store + BM25 search over learned text."""

    def __init__(self, data_dir, journal=None):
        self.data_dir = Path(data_dir)
        self.inbox = self.data_dir / "inbox"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.store_path = self.data_dir / "learned.jsonl"
        self.journal = journal
        self._records = {}   # filename -> {"stamp": [size, mtime],
                             #              "chunks": [text, ...]}
        self._index = None
        self._index_len = -1
        self._load()

    # -- persistence ----------------------------------------------------

    def _load(self):
        if not self.store_path.exists():
            return
        try:
            for line in self.store_path.read_text(
                    encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                self._records[str(row["file"])] = {
                    "stamp": list(row["stamp"]),
                    "chunks": [str(c) for c in row["chunks"]],
                }
        except Exception:
            self._records = {}

    def _save(self):
        try:
            with self.store_path.open("w", encoding="utf-8") as fh:
                for name in sorted(self._records):
                    rec = self._records[name]
                    fh.write(json.dumps({
                        "file": name, "stamp": rec["stamp"],
                        "chunks": rec["chunks"]}) + "\n")
        except Exception:
            pass  # persistence must never break the conversation

    # -- ingest -----------------------------------------------------------

    def scan(self) -> int:
        """Ingest new/changed inbox files. Returns files ingested."""
        ingested = 0
        seen = set()
        for path in sorted(self.inbox.iterdir()):
            if not path.is_file() or \
                    path.suffix.lower() not in (".txt", ".md"):
                continue
            stat = path.stat()
            stamp = [stat.st_size, int(stat.st_mtime)]
            seen.add(path.name)
            known = self._records.get(path.name)
            if known is not None and known["stamp"] == stamp:
                continue
            try:
                text = path.read_text(encoding="utf-8",
                                      errors="replace")
            except OSError:
                continue
            chunks = [parent.text for parent in
                      rag.chunk_parent_child(text) if parent.text.strip()]
            if not chunks:
                continue
            self._records[path.name] = {"stamp": stamp,
                                        "chunks": chunks}
            ingested += 1
            if self.journal is not None:
                self.journal.append("learn.ingest", {
                    "file": path.name, "chunks": len(chunks),
                    "chars": len(text)})
        for name in list(self._records):
            if name not in seen and not (self.inbox / name).exists():
                del self._records[name]   # owner removed the file
                ingested += 1
        if ingested:
            self._save()
        return ingested

    # -- retrieval --------------------------------------------------------

    def _docs(self):
        docs = []
        for name in sorted(self._records):
            for chunk in self._records[name]["chunks"]:
                docs.append((name, chunk))
        return docs

    def search(self, query: str, k: int = 4):
        """Top learned chunks for a query: [{file, text, score}]."""
        docs = self._docs()
        if not docs:
            return []
        if self._index is None or self._index_len != len(docs):
            self._index = rag.BM25Index([text for _n, text in docs])
            self._index_len = len(docs)
        out = []
        for idx, score in self._index.search(str(query),
                                             top_k=int(k)):
            if score <= 0:
                continue
            name, text = docs[idx]
            out.append({"file": name, "text": text,
                        "score": round(float(score), 4)})
        return out

    def add_document(self, text: str, title: str = "pasted") -> dict:
        """Write pasted text into the inbox and ingest it at once."""
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", str(title)).strip("-")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        name = f"{safe[:40] or 'pasted'}-{stamp}.txt"
        (self.inbox / name).write_text(str(text), encoding="utf-8")
        self.scan()
        rec = self._records.get(name, {"chunks": []})
        return {"learned_from": name, "chunks": len(rec["chunks"]),
                "stored": True}


class _LearnedMemory:
    """Memory facade: facts (Memory) + learned documents (LearnedStore)
    behind the one interface the brain, the registry ops and the CLI
    commands already use. Learned chunks arrive from the owner himself,
    so they carry owner trust and are labelled with their source file."""

    def __init__(self, memory, learned: LearnedStore):
        self._memory = memory
        self._learned = learned

    def __getattr__(self, name):
        return getattr(self._memory, name)

    def scan_learned(self) -> int:
        return self._learned.scan()

    def learn_text(self, text: str, title: str = "pasted") -> dict:
        return self._learned.add_document(text, title)

    def remember(self, text: str) -> dict:
        return self._memory.remember(text)

    def recall(self, query: str, k: int = 5):
        facts = self._memory.recall(query, k=k)
        out = list(facts)
        for hit in self._learned.search(query, k=max(0, k - len(out))):
            out.append({
                "text": hit["text"], "claim": hit["text"],
                "source": f"learned:{hit['file']}",
                "source_trust": "owner",
                "score": hit["score"], "id": f"learned:{hit['file']}",
            })
        return out

    def excerpts(self, query: str, k: int = 5):
        lines = self._memory.excerpts(query, k=k)
        seen = set(lines)
        for hit in self._learned.search(query, k=k):
            line = f"[{hit['file']}] {hit['text']}"
            if line not in seen:
                lines.append(line)
                seen.add(line)
            if len(lines) >= 8:
                break
        return lines[:8]


@dataclass
class AppParts:
    journal: Journal
    config: object
    secrets: object
    goodput: GoodputLedger
    chain: object
    registry: object
    gate: object
    memory: Memory
    brain: object
    dispatch: object
    allowances: frozenset
    learned: object = None


UPGRADE_MANIFEST = {
    "name": "core.upgrade",
    "version": "1.0.0",
    "tools": [
        {
            "name": "watch_for_upgrades",
            "op": "watch_for_upgrades",
            "permission": "net.outbound",
            "schema": {"type": "object",
                       "properties": {"topics": {
                           "type": "array",
                           "items": {"type": "string"}}},
                       "additionalProperties": False},
        },
        {
            "name": "propose_skill",
            "op": "propose_skill",
            "permission": "self.upgrade",
            "schema": {"type": "object",
                       "properties": {
                           "name": {"type": "string", "minLength": 1},
                           "manifest": {"type": "object"},
                           "code": {"type": "string", "minLength": 1}},
                       "required": ["name", "manifest", "code"],
                       "additionalProperties": False},
        },
        {
            "name": "upgrade_report",
            "op": "upgrade_report",
            "permission": "system.diagnose",
            "schema": {"type": "object", "properties": {},
                       "additionalProperties": False},
        },
    ],
}

DEFAULT_WATCH_TOPICS = [
    "new open source local LLM models",
    "open source text to speech voices new",
    "wake word detection open source",
    "AI agent frameworks new tools release",
    "whisper speech recognition improvements",
    "AI assistant memory systems long term memory",
    "open source AI agent skills tools",
    "LLM tool calling reliability agents",
]


def _upgrade_log_path(data_dir) -> Path:
    return Path(data_dir) / "upgrades.jsonl"


def _read_upgrade_log(data_dir) -> list:
    path = _upgrade_log_path(data_dir)
    rows = []
    if path.exists():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        except Exception:
            return []
    return rows


def _append_upgrade_log(data_dir, row: dict) -> None:
    try:
        with _upgrade_log_path(data_dir).open("a",
                                              encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _watch_cycle(learned: LearnedStore, data_dir, topics=None) -> dict:
    """One market-watch cycle (the owner's standing self-upgrade
    order): search his world's topics, keep only what is NEW, log it,
    and learn it — findings land in the inbox and become knowledge."""
    from . import builtin_skills as bs
    topics = list(topics) if topics else list(DEFAULT_WATCH_TOPICS)
    rows = _read_upgrade_log(data_dir)
    seen = {r.get("url") for r in rows if r.get("type") == "watch"}
    new, errors = [], []
    for topic in topics:
        found = bs.op_web_search(str(topic), limit=5)
        if "error" in found:
            errors.append(found["error"])
            continue
        for hit in found.get("results", []):
            if hit["url"] not in seen:
                seen.add(hit["url"])
                new.append({"topic": str(topic),
                            "title": hit["title"], "url": hit["url"]})
    ts = time.time()
    for item in new:
        _append_upgrade_log(data_dir, {"type": "watch", "ts": ts,
                                       **item})
    _append_upgrade_log(data_dir, {"type": "watch_run", "ts": ts,
                                   "found": len(new)})
    # Depth, per the owner's standing order (research zyada, roz,
    # khud ke liye achhi cheez): the top findings are not just
    # listed — their pages are READ and the content is learned.
    for item in new[:3]:
        page = bs.op_web_fetch(item["url"], max_chars=3500)
        if "error" not in page:
            item["content"] = page["text"]
    learned_as = None
    if new:
        lines = ["# Upgrade watch — nayi cheezein jo mili", ""]
        for item in new:
            lines.append(f"- [{item['topic']}] {item['title']} — "
                         f"{item['url']}")
            if item.get("content"):
                lines += ["", item["content"][:3000], ""]
        learned_as = learned.add_document(
            "\n".join(lines), title="upgrade-watch")["learned_from"]
        _notify(data_dir, "Upgrade watch",
                f"{len(new)} nayi cheezein mili — pehli: "
                f"{new[0]['title']}", kind="upgrade")
    return {"new_findings": new, "count": len(new),
            "topics_checked": len(topics),
            "search_errors": errors[:1], "learned_as": learned_as,
            "note": "nayi findings inbox me seekh li gayi hain; "
                    "jo kaam ki lage, propose_skill se JARVIS khud "
                    "me naya skill laga sakta hai"}


def _upgrade_ops(learned: LearnedStore, data_dir, journal, registry,
                 allowances):
    data = Path(data_dir)

    def watch_for_upgrades(topics=None):
        return _watch_cycle(learned, data, topics)

    def propose_skill(name: str, manifest: dict, code: str):
        """Self-improvement with the quality gate the owner accepted:
        the candidate skill is staged, its code is imported in the
        scrubbed sandbox (never in-process), its manifest must pass
        the registry's own validation, its OPS must match the
        manifest, and its tool names must not clash with live tools.
        Pass everything -> promoted AUTOMATICALLY (his standing
        order: no asking, no restrictions); fail anything -> it
        stays staged with the reason recorded. Live next start."""
        from . import builtin_skills as bs
        from .skills import SkillRegistry, SkillError
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-",
                      str(name)).strip("-").lower() or "skill"
        staging = data / "upgrades" / "pending" / slug
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "skill.py").write_text(str(code), encoding="utf-8")
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1),
            encoding="utf-8")

        def reject(stage: str, reason: str) -> dict:
            journal.append("upgrade.rejected", {
                "skill": str(name), "stage": stage, "reason": reason})
            _append_upgrade_log(data, {
                "type": "rejected", "ts": time.time(),
                "skill": str(name), "stage": stage, "reason": reason})
            return {"promoted": False, "stage": stage,
                    "reason": reason, "kept_in": str(staging),
                    "note": "skill live nahi hua — wajah upar hai; "
                            "code staging me rakha hai"}

        harness = (
            "import importlib.util, json\n"
            "spec = importlib.util.spec_from_file_location("
            "'candidate', 'skill.py')\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "print(json.dumps(sorted(getattr(mod, 'OPS', {}))))\n")
        run = bs.op_run_code(harness, cwd=str(staging), timeout=30)
        if not run.get("ok"):
            return reject("import", (run.get("stderr") or
                                     run.get("error") or
                                     "import failed")[-400:])
        try:
            ops_keys = set(json.loads(run["stdout"].strip()
                                      .splitlines()[-1]))
        except Exception:
            return reject("import", "OPS list padhi nahi ja saki")
        try:
            manifest_ops = {t["op"] for t in manifest["tools"]}
            tool_names = [t["name"] for t in manifest["tools"]]
        except Exception as exc:
            return reject("manifest", f"manifest shape: {exc}")
        if ops_keys != manifest_ops:
            return reject("ops", f"code ke OPS {sorted(ops_keys)} "
                                 f"manifest ke ops "
                                 f"{sorted(manifest_ops)} se match "
                                 "nahi karte")
        try:
            SkillRegistry().register(
                manifest, ops={k: (lambda: None) for k in ops_keys})
        except SkillError as exc:
            return reject("manifest", str(exc))
        live = {t["name"] for t in
                registry.advertised_tools(allowances)}
        clash = [n for n in tool_names if n in live]
        if clash:
            return reject("clash", f"ye tools pehle se live hain: "
                                   f"{clash}")
        dest = data / "skills_live" / slug
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "skill.py").write_text(str(code), encoding="utf-8")
        (dest / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1),
            encoding="utf-8")
        journal.append("upgrade.promoted", {
            "skill": manifest.get("name"), "slug": slug,
            "tools": tool_names})
        _notify(data, "Naya skill taiyaar",
                f"{manifest.get('name')} agle start se live hoga "
                f"(tools: {', '.join(tool_names)})", kind="upgrade")
        _append_upgrade_log(data, {
            "type": "promoted", "ts": time.time(),
            "skill": manifest.get("name"), "tools": tool_names})
        return {"promoted": True, "skill": manifest.get("name"),
                "tools": tool_names,
                "checks_passed": ["sandbox import", "ops match",
                                  "manifest valid", "no tool clash"],
                "available": "agle start se ye skill live hogi"}

    def upgrade_report():
        rows = _read_upgrade_log(data)
        live_dir = data / "skills_live"
        live = sorted(p.name for p in live_dir.iterdir()
                      if p.is_dir()) if live_dir.exists() else []
        pending_dir = data / "upgrades" / "pending"
        pending = sorted(p.name for p in pending_dir.iterdir()
                         if p.is_dir()) if pending_dir.exists() else []
        last_watch = None
        for row in rows:
            if row.get("type") == "watch_run":
                last_watch = row.get("ts")
        return {
            "watch_findings_total":
                sum(1 for r in rows if r.get("type") == "watch"),
            "last_watch_at": last_watch,
            "promoted_skills":
                [r.get("skill") for r in rows
                 if r.get("type") == "promoted"],
            "rejected":
                [{"skill": r.get("skill"), "stage": r.get("stage")}
                 for r in rows if r.get("type") == "rejected"][-5:],
            "live_skill_folders": live, "pending_skills": pending}

    return {"watch_for_upgrades": watch_for_upgrades,
            "propose_skill": propose_skill,
            "upgrade_report": upgrade_report}


def _load_live_skills(registry, data_dir, journal) -> None:
    """Register skills JARVIS promoted for itself (data/skills_live).
    Each one passed the propose_skill gate when it was promoted;
    anything malformed is skipped and journaled, never fatal."""
    import importlib.util
    live_dir = Path(data_dir) / "skills_live"
    if not live_dir.exists():
        return
    for folder in sorted(p for p in live_dir.iterdir() if p.is_dir()):
        try:
            manifest = json.loads((folder / "manifest.json")
                                  .read_text(encoding="utf-8"))
            module_name = str(manifest.get("module", "skill.py"))
            spec = importlib.util.spec_from_file_location(
                f"live_skill_{folder.name}", folder / module_name)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            ops = getattr(module, "OPS", None)
            if not isinstance(ops, dict):
                raise ValueError("skill module has no OPS dict")
            registry.register(manifest, ops=ops)
            registry.approve(manifest["name"])
            journal.append("upgrade.live_skill_loaded",
                           {"skill": manifest["name"],
                            "folder": folder.name})
        except Exception as exc:
            journal.append("upgrade.live_skill_skipped", {
                "folder": folder.name,
                "error": f"{type(exc).__name__}: {exc}"})


def _maybe_auto_watch(app) -> None:
    """The standing order, running itself: when JARVIS starts (CLI
    or UI) and the last market watch is over ~20 hours old, one watch
    cycle runs quietly in the background. Never blocks startup,
    never raises."""
    try:
        rows = _read_upgrade_log(app.data_dir)
        last = max((r.get("ts", 0) for r in rows
                    if r.get("type") == "watch_run"), default=0)
        if time.time() - last < 20 * 3600:
            return
        learned = app.parts.learned
        if learned is None:
            return
        threading.Thread(target=_watch_cycle,
                         args=(learned, app.data_dir),
                         daemon=True).start()
    except Exception:
        pass


def build_app(data_dir, *, chain=None, modules=None,
              allowances=None) -> AppParts:
    """Assemble the whole spine for one data dir. THE wiring place.

    `chain` overrides the provider chain (the eval harness's stub
    seam); `modules` overrides sibling-module resolution (tests).
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    allowances = frozenset(allowances) if allowances is not None \
        else DEFAULT_ALLOWANCES

    journal = Journal(str(data_dir / "journal.jsonl"))
    config = _resolve_module(modules, "config").load(data_dir)
    secrets = _resolve_module(modules, "secrets").SecretStore(data_dir)

    gate_mod = _resolve_module(modules, "gate")
    gate_key = secrets.get("gate_key")
    if not gate_key:
        gate_key = uuid.uuid4().hex
        secrets.set("gate_key", gate_key)
    if gate_mod is _defaults:
        gate = gate_mod.Gate(journal, gate_key)
    else:
        gate = gate_mod.Gate(journal, str(gate_key).encode("utf-8"))

    memory = Memory(data_dir, journal=journal)
    learned = LearnedStore(data_dir, journal=journal)
    memory = _LearnedMemory(memory, learned)
    learned.scan()   # ingest anything he dropped in while JARVIS was off

    registry = _build_registry(modules, gate, journal, memory, data_dir)
    # The app's own reminder skill (see the manifest's note above).
    if hasattr(registry, "register") and hasattr(registry, "approve"):
        try:
            registry.register(REMINDER_MANIFEST,
                              ops=_reminder_ops(data_dir))
            registry.approve(REMINDER_MANIFEST["name"])
        except Exception as exc:  # a sibling got there first, etc.
            journal.append("chat.reminder_skill_skipped",
                           {"error": f"{type(exc).__name__}: {exc}"})
    if hasattr(registry, "register") and hasattr(registry, "approve"):
        try:
            registry.register(DIAGNOSE_MANIFEST,
                              ops={"diagnose": _diagnose_op(data_dir,
                                                            secrets)})
            registry.approve(DIAGNOSE_MANIFEST["name"])
        except Exception as exc:  # a sibling got there first, etc.
            journal.append("chat.diagnose_skill_skipped",
                           {"error": f"{type(exc).__name__}: {exc}"})
    if hasattr(registry, "register") and hasattr(registry, "approve"):
        try:
            registry.register(RESEARCH_MANIFEST,
                              ops={"deep_research":
                                   _research_op(learned)})
            registry.approve(RESEARCH_MANIFEST["name"])
        except Exception as exc:  # a sibling got there first, etc.
            journal.append("chat.research_skill_skipped",
                           {"error": f"{type(exc).__name__}: {exc}"})
    # Self-upgrade: skills JARVIS promoted for itself load first,
    # then the upgrade tools themselves are registered.
    try:
        _load_live_skills(registry, data_dir, journal)
    except Exception as exc:
        journal.append("chat.live_skills_skipped",
                       {"error": f"{type(exc).__name__}: {exc}"})
    if hasattr(registry, "register") and hasattr(registry, "approve"):
        try:
            registry.register(UPGRADE_MANIFEST,
                              ops=_upgrade_ops(learned, data_dir,
                                               journal, registry,
                                               allowances))
            registry.approve(UPGRADE_MANIFEST["name"])
        except Exception as exc:  # a sibling got there first, etc.
            journal.append("chat.upgrade_skill_skipped",
                           {"error": f"{type(exc).__name__}: {exc}"})
    if hasattr(registry, "register") and hasattr(registry, "approve"):
        try:
            def _notify_op(title: str, body: str = ""):
                return _notify(data_dir, title, body, kind="brain")
            registry.register(NOTIFY_MANIFEST,
                              ops={"notify": _notify_op})
            registry.approve(NOTIFY_MANIFEST["name"])
        except Exception as exc:  # a sibling got there first, etc.
            journal.append("chat.notify_skill_skipped",
                           {"error": f"{type(exc).__name__}: {exc}"})

    goodput = GoodputLedger()
    goodput_path = data_dir / "goodput.jsonl"
    if goodput_path.exists():
        try:
            goodput = GoodputLedger.load_jsonl(goodput_path)
        except Exception:
            goodput = GoodputLedger()

    if chain is None:
        chain = _build_chain(_resolve_module(modules, "providers"),
                             config, _ChainSecrets(secrets), goodput)

    chain = _BrainChainAdapter(chain)

    skills_mod = _resolve_module(modules, "skills")
    refused_exc = getattr(skills_mod, "DispatchRefused", ())
    if not (isinstance(refused_exc, type)
            and issubclass(refused_exc, BaseException)):
        refused_exc = ()
    pass_decision = skills_mod is not _defaults
    dispatch = make_dispatch(gate, registry, journal, allowances,
                             pass_decision=pass_decision,
                             refused_exc=refused_exc)
    tool_schemas = registry.advertised_tools(allowances)
    brain = _resolve_module(modules, "brain").Brain(
        chain, tool_schemas, dispatch, journal, memory)

    return AppParts(journal=journal, config=config, secrets=secrets,
                    goodput=goodput, chain=chain, registry=registry,
                    gate=gate, memory=memory, brain=brain,
                    dispatch=dispatch, allowances=allowances,
                    learned=learned)


# --------------------------------------------------------------------------- #
# Fast-path router — deterministic, zero-LLM commands
# --------------------------------------------------------------------------- #

_TIME_RE = re.compile(
    r"(what('s| is) the time|what time is it|time kya hai|kitna baje|"
    r"samay kya hai|current time|abhi time)", re.I)
_DATE_RE = re.compile(
    r"(what('s| is) (the |today'?s )?date|aaj ki (date|tarikh)|"
    r"tarikh kya hai|what day is (it|today))", re.I)
_LEARN_PREFIX_RE = re.compile(
    r"^(?:yaad rakho|yaad rakh lo|yaad kar lo|yaad karna|"
    r"remember(?: this)?|note that)[:\s]+(.+)$", re.I | re.S)
_LEARN_SUFFIX_RE = re.compile(
    r"^(.+?)[,;]?\s*(?:yaad rakhna|yaad rakho|yaad rakh lo|"
    r"remember it|remember that)\.?$", re.I | re.S)
_RECALL_RE = re.compile(
    r"(mera naam kya hai|what is my name|mera naam batao|"
    r"kya (tumhe )?yaad hai|do you remember|recall)", re.I)


class ChatApp:
    """The v4 chat application: one data dir, one journal, real turns."""

    def __init__(self, data_dir, *, chain=None, modules=None,
                 allowances=None):
        self.data_dir = Path(data_dir)
        parts = build_app(self.data_dir, chain=chain, modules=modules,
                          allowances=allowances)
        self.parts = parts
        self.journal = parts.journal
        self.config = parts.config
        self.secrets = parts.secrets
        self.goodput = parts.goodput
        self.chain = parts.chain
        self.registry = parts.registry
        self.gate = parts.gate
        self.memory = parts.memory
        self.brain = parts.brain
        self.allowances = parts.allowances
        self.history: list = []
        self.last_provider = "none"

    # -- tool execution (the same gate-mediated path the brain uses) --

    def execute_tool(self, name: str, args: Optional[dict] = None,
                     origin: str = "owner") -> dict:
        return self.parts.dispatch(name, args or {}, origin=origin)

    # -- commands ---------------------------------------------------------

    def handle_command(self, command: str) -> str:
        """Slash commands. `/memory <text>` stores a fact (the app's
        own memory surface); `/memory` lists current facts."""
        text = str(command).strip()
        if text == "/memory":
            facts = self.memory.facts.current_facts()
            if not facts:
                return "No facts stored yet, sir."
            return "\n".join(f"- {f['claim']}" for f in facts)
        if text.startswith("/memory "):
            fact = text[len("/memory "):].strip()
            if not fact:
                return "Nothing to store, sir."
            self.memory.learn(fact, source="owner")
            return f"Yaad rakh liya, sir: {fact}"
        if text == "/notifications":
            rows = [r for r in _read_notifications(self.data_dir)
                    if not r.get("read")]
            if not rows:
                return "No new notifications, sir."
            _mark_notifications_read(self.data_dir)
            return "\n".join(f"🔔 {r['title']} — {r['body']}"
                             for r in rows[-10:])
        if text == "/keys":
            keys_mod = _load_keys_module()
            if keys_mod is None:
                return "Key support is unavailable in this build, sir."
            counts = keys_mod.list_keys(self.secrets)
            if not counts:
                return ("No provider keys stored yet, sir. "
                        "Add one with /key <key>.")
            lines = ["Stored provider keys, sir:"]
            for name in sorted(counts):
                lines.append(f"- {name}: {counts[name]}")
            return "\n".join(lines)
        if text == "/key" or text.startswith("/key "):
            keys_mod = _load_keys_module()
            if keys_mod is None:
                return "Key support is unavailable in this build, sir."
            rest = text[len("/key"):].strip()
            if not rest:
                return ("Usage, sir: /key <key> — or /key <provider> "
                        "<key> when the key's shape is ambiguous.")
            parts = rest.split(None, 1)
            provider = None
            raw = rest
            if len(parts) == 2:
                provider, raw = parts
            try:
                saved = keys_mod.add_key(self.secrets, raw, provider)
            except keys_mod.KeysError as exc:
                return f"That key was not stored, sir: {exc}"
            return (f"Key stored for {saved}, sir. Restart JARVIS and "
                    "it joins the brain chain.")
        return f"Unknown command, sir: {text.split()[0] if text else ''}"

    # -- the turn -----------------------------------------------------------

    def turn(self, user_text: str) -> str:
        text = str(user_text)
        # Automatic learning: anything new in data/inbox is ingested
        # before this turn is answered — no command, no ceremony.
        try:
            self.memory.scan_learned()
        except Exception:
            pass
        labeled = ifc.label_content(text, origin=ifc.OWNER_ORIGIN)
        self.journal.append("user.message", {
            "text": text, "origin": labeled.origin,
            "label": labeled.label})
        self.memory.note_turn("user", text)

        reply = self._fast_path(text)
        if reply is not None:
            via = "fast_path"
            self.last_provider = "fast-path"
        else:
            rows_before = len(self.goodput.to_rows())
            result = self.brain.respond(self.history, text)
            reply = str(result.reply)
            via = "brain"
            rows = self.goodput.to_rows()
            if len(rows) > rows_before:
                self.last_provider = str(rows[-1]["provider"])
            else:
                self.last_provider = (
                    getattr(self.chain, "last_provider", None)
                    or getattr(self.chain, "name", None) or "unknown")

        # Reminders whose time has come surface as notifications.
        try:
            _due_reminder_notifications(self.data_dir)
        except Exception:
            pass
        self.journal.append("assistant.reply", {
            "text": reply, "via": via, "provider": self.last_provider})
        # The goodput ledger is the diagnostic record (which brain
        # answered, which failed, why). It used to be loaded at startup
        # but NEVER saved, so data/goodput.jsonl simply never existed
        # on the owner's PC — persist it after every turn, silently:
        # a failed save must never break the conversation.
        try:
            self.goodput.save_jsonl(self.data_dir / "goodput.jsonl")
        except Exception:
            pass
        self.memory.note_turn("assistant", reply)
        self.history.append({"role": "user", "content": text})
        self.history.append({"role": "assistant", "content": reply})
        return reply

    # -- fast path ------------------------------------------------------------

    def _fast_path(self, text: str) -> Optional[str]:
        low = text.strip()
        if _TIME_RE.search(low) or _DATE_RE.search(low):
            result = self.execute_tool("time_now", {})
            return self._format_now(result)
        m = _LEARN_PREFIX_RE.match(low) or _LEARN_SUFFIX_RE.match(low)
        if m and not low.rstrip().endswith("?"):
            statement = m.group(1).strip(" .")
            if statement:
                self.memory.learn(statement, source="owner")
                return f"Yaad rakh liya, sir: {statement}"
        if _RECALL_RE.search(low):
            hits = self.memory.recall(low, k=1)
            if hits:
                return f"Ji sir, yaad hai: {hits[0]['text']}"
        return None

    @staticmethod
    def _format_now(result: dict) -> str:
        iso = result.get("iso") or result.get("local") or ""
        try:
            moment = datetime.fromisoformat(str(iso)).astimezone()
            return (f"It's {moment.strftime('%I:%M %p')}, sir — "
                    f"{moment.strftime('%A, %d %B %Y')}.")
        except ValueError:
            return f"It's {iso}, sir."


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="jarvis.chat",
        description="JARVIS v4 chat core (headless).")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--once", default=None,
                        help="run one turn and print the reply")
    args = parser.parse_args(argv)

    app = ChatApp(args.data_dir)
    if args.once is not None:
        print(app.turn(args.once))
        return 0
    _maybe_auto_watch(app)
    print("JARVIS v4 — type 'exit' to quit.")
    while True:
        try:
            line = input("you> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if line.strip().lower() in ("exit", "quit"):
            break
        if line.strip().startswith("/"):
            print(app.handle_command(line.strip()))
        else:
            print(f"JARVIS: {app.turn(line)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
