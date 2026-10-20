"""Spine defaults — contract implementations used until the dedicated
spine modules (jarvis.config / secrets / providers / brain / gate /
skills) land.

`jarvis.chat.build_app` is the ONE wiring place: for each spine part it
prefers the real module and falls back to the implementation here.
Every class in this file implements exactly the shared contract:

  config.load(data_dir) -> Config.get(section, key, default)
  SecretStore(data_dir).get/.set/.names
  build_chain(config, secrets, goodput) -> chain.complete(messages, tools)
  Brain(chain, tool_schemas, dispatch, journal, memory).respond(...)
  Gate(journal, key).evaluate(...) -> Decision
  SkillRegistry.advertised_tools(allowances) / .dispatch(name, args)

These defaults are honest, working, stdlib-only implementations — not
mocks. Their limits are stated where they bite: the default secret
store is an owner-only FILE (the real one uses the OS keyring), and
the default chain is the deterministic StubProvider alone, so a fresh
checkout talks — and says it is a stub — until real providers land.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class ProviderHTTPError(Exception):
    """A provider answered with an HTTP error status (mirrors the
    jarvis.brain.ProviderHTTPError the eval harness injects)."""

    def __init__(self, status: int, message: str = ""):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


# --------------------------------------------------------------------------- #
# Config (contract: config.load(data_dir) -> .get(section, key, default))
# --------------------------------------------------------------------------- #

class Config:
    """JSON-backed config: <data_dir>/config.json as {section: {key: v}}."""

    def __init__(self, data_dir):
        self.data_dir = str(data_dir)
        self.path = os.path.join(self.data_dir, "config.json")
        self._data: Dict[str, Any] = {}
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                self._data = json.load(f)

    def get(self, section: str, key: str, default=None):
        return (self._data.get(section) or {}).get(key, default)


def load(data_dir) -> Config:
    return Config(data_dir)


# --------------------------------------------------------------------------- #
# Secrets (contract: SecretStore(data_dir).get/.set/.names)
# --------------------------------------------------------------------------- #

class SecretStore:
    """Owner-only file store — DEV FALLBACK for the keyring-backed
    store. Values are never journaled and never printed."""

    def __init__(self, data_dir):
        self.path = os.path.join(str(data_dir), "secrets.json")
        self._data: Dict[str, str] = {}
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                self._data = json.load(f)

    def get(self, name: str, default=None):
        return self._data.get(name, default)

    def set(self, name: str, value: str) -> None:
        self._data[name] = str(value)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._data, f)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def names(self) -> List[str]:
        return sorted(self._data)


# --------------------------------------------------------------------------- #
# Providers (contract: chain.complete(messages, tools) -> reply dict)
#
# Reply format (what Brain parses): either a plain string, or a dict
# {"content": str|None, "tool_calls": [{"name":..., "args": {...}}]}.
# --------------------------------------------------------------------------- #

class StubProvider:
    """Deterministic competent stand-in in the model seat.

    Routes the everyday intents the case bank exercises to real tools
    with well-formed args, answers greetings directly, and answers
    FROM tool results on the follow-up round. It proves the pipeline;
    it is not evidence about any real model.
    """

    name = "stub"

    def complete(self, messages, tools=None):
        tool_seen = [m for m in messages if m.get("role") == "tool"]
        if tool_seen:
            return {"content": self._answer_from_tool(tool_seen[-1])}
        user_text = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_text = str(m.get("content", ""))
                break
        low = user_text.lower().strip()

        if "reminder" in low or "yaad dila" in low:
            minutes = 10
            m = re.search(r"(\d+)\s*(?:minute|min)", low)
            if m:
                minutes = max(1, int(m.group(1)))
            text = re.sub(r"^\d+\s*(?:minute|min)\s*(?:me|mein)?\s*", "",
                          user_text, flags=re.IGNORECASE)
            text = re.sub(
                r"(?:ka|ke)?\s*reminder\s*(?:laga do|lagaa do|set kar do)?\s*$",
                "", text, flags=re.IGNORECASE).strip(" :") or user_text
            return {"tool_calls": [{"name": "reminder_set",
                                    "args": {"in_minutes": minutes,
                                             "text": text[:200]}}]}
        if low.startswith("open "):
            return {"tool_calls": [{"name": "pc.launch_app",
                                    "args": {"name":
                                              user_text[5:].strip()}}]}
        if "web search" in low or low.startswith("search "):
            query = re.sub(r"^.*?web search\s*", "", low)
            query = re.sub(r"^search\s*", "", query)
            query = re.sub(r"^about\s+", "", query).strip() or user_text
            return {"tool_calls": [{"name": "web_search",
                                    "args": {"query": query}}]}
        if "time" in low:
            return {"tool_calls": [{"name": "now", "args": {}}]}
        if "note" in low and ("bana" in low or "likh" in low
                              or low.startswith("note")):
            content = user_text.split(":", 1)[1].strip() \
                if ":" in user_text else user_text
            return {"tool_calls": [{"name": "notes_write",
                                    "args": {"path": "note.txt",
                                             "content": content}}]}
        if "naam kya" in low or "my name" in low:
            blob = "\n".join(str(m.get("content", "")) for m in messages)
            if "Owais" in blob:
                return {"content": "Aapka naam Owais hai, sir."}
            return {"content":
                    "Aapne abhi tak apna naam nahi bataya, sir."}
        if ("how are u" in low or "how are you" in low or "kaise ho" in low
                or low in ("hi", "hello", "hey")):
            return {"content": "I'm doing well, sir — all systems "
                               "nominal. Aap kaise hain?"}
        if "2 plus 2" in low:
            return {"content": "2 plus 2 hota hai 4, sir."}
        return {"content": "Understood, sir. (stub provider — a real "
                           "model connects via the provider chain.)"}

    @staticmethod
    def _answer_from_tool(tool_msg) -> str:
        content = str(tool_msg.get("content", ""))
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            data = None
        if isinstance(data, dict):
            if data.get("needs_approval"):
                return ("That action needs your approval first, sir — "
                        "I haven't run it yet.")
            if data.get("ok") is False or "error" in data:
                return (f"That didn't work, sir: "
                        f"{data.get('error', 'unknown error')}")
            if "iso" in data:
                iso = str(data["iso"]).replace("T", " ").split("+")[0]
                return f"It's {iso} UTC, sir."
            if "written" in data:
                return "Noted down, sir."
            if data.get("id") and "due" in data:
                return f"Reminder set, sir (id {data.get('id')})."
            if "results" in data:
                return ("Here's what I found, sir — "
                        f"{len(data['results'])} result(s) from the web.")
            return f"Done, sir. ({json.dumps(data)[:160]})"
        if content.startswith("error:"):
            return f"That didn't work, sir: {content[6:].strip()[:200]}"
        return f"Done, sir: {content[:200]}"


class FallbackChain:
    """Ordered provider fallback with goodput accounting."""

    def __init__(self, providers, goodput=None):
        self.providers = list(providers)
        self.goodput = goodput
        self.last_provider: Optional[str] = None

    def complete(self, messages, tools=None):
        last_exc: Optional[Exception] = None
        for provider in self.providers:
            name = getattr(provider, "name", type(provider).__name__)
            started = time.monotonic()
            try:
                reply = provider.complete(messages, tools)
            except Exception as exc:
                last_exc = exc
                if self.goodput is not None:
                    self.goodput.record(
                        name, (time.monotonic() - started) * 1000.0,
                        ok=False, quality_ok=False)
                continue
            self.last_provider = name
            if self.goodput is not None:
                has_body = isinstance(reply, str) and bool(reply.strip())
                if isinstance(reply, dict):
                    has_body = bool(reply.get("content")
                                    or reply.get("tool_calls"))
                self.goodput.record(
                    name, (time.monotonic() - started) * 1000.0,
                    ok=True, quality_ok=has_body)
            return reply
        if last_exc is not None:
            raise last_exc
        raise ProviderHTTPError(0, "provider chain is empty")


def build_chain(config, secrets, goodput):
    """Default chain until jarvis.providers lands: the stub alone, so
    the app runs deterministically and honestly labels its brain."""
    return FallbackChain([StubProvider()], goodput=goodput)


# --------------------------------------------------------------------------- #
# Gate (contract: Gate(journal, key).evaluate(...) -> Decision)
# --------------------------------------------------------------------------- #

# What each built-in tool needs. The registry declares the same table;
# the gate keeps its own copy because a gate that asks the tool what
# the tool needs is not a gate.
TOOL_CAPABILITIES = {
    "now": "time.read",
    "reminder_set": "reminder.write",
    "reminder_list": "reminder.read",
    "notes_write": "notes.write",
    "web_search": "net.outbound",
    "pc.launch_app": "pc.control",
}


@dataclass
class Decision:
    verdict: str  # "allow" | "require_approval" | "deny"
    reason: str = ""
    tool: str = ""


class Gate:
    """Capability-allowance gate.

    Policy (deterministic, fail-closed):
    - content whose origin is not the owner may never originate an
      action (the IFC rule) -> deny;
    - unknown tool -> deny;
    - the tool's capability is in the turn's allowances -> allow;
    - otherwise -> require_approval (the owner can ratify; headless
      callers turn that into an honest "needs your approval" result).

    The HMAC key is held for approval-token signing (the real gate's
    job); every decision is journaled.
    """

    def __init__(self, journal, key):
        self.journal = journal
        self._key = key

    def evaluate(self, tool_name: str, args: Optional[dict] = None,
                 origin_label: str = "owner",
                 allowances=frozenset()) -> Decision:
        if origin_label not in ("owner", "trusted"):
            decision = Decision(
                "deny", f"origin {origin_label!r} may not originate "
                "actions", tool_name)
        elif tool_name not in TOOL_CAPABILITIES:
            decision = Decision("deny", f"unknown tool {tool_name!r}",
                                tool_name)
        elif TOOL_CAPABILITIES[tool_name] in allowances:
            decision = Decision(
                "allow",
                f"capability {TOOL_CAPABILITIES[tool_name]!r} allowed",
                tool_name)
        else:
            decision = Decision(
                "require_approval",
                f"capability {TOOL_CAPABILITIES[tool_name]!r} is not "
                "in the default allowances", tool_name)
        if self.journal is not None:
            self.journal.append("gate.decision", {
                "tool": tool_name, "verdict": decision.verdict,
                "reason": decision.reason, "origin": origin_label})
        return decision


# --------------------------------------------------------------------------- #
# Skills (contract: SkillRegistry.advertised_tools / .dispatch)
# --------------------------------------------------------------------------- #

@dataclass
class _Tool:
    name: str
    description: str
    parameters: dict
    capability: str
    handler: Callable[[dict], dict]
    approved: bool = True


class SkillRegistry:
    """Approved built-in skills. Advertisement lists every approved
    tool (with its capability); ALLOWANCES are enforced by the gate
    at dispatch time, not by hiding tools from the brain — a tool the
    brain cannot see is a tool the owner can never approve."""

    def __init__(self):
        self._tools: Dict[str, _Tool] = {}

    def register(self, name, description, parameters, capability,
                 handler, approved=True):
        self._tools[name] = _Tool(name, description, parameters,
                                  capability, handler, approved)

    def advertised_tools(self, allowances=None) -> List[dict]:
        return [{"name": t.name, "description": t.description,
                 "parameters": t.parameters, "capability": t.capability}
                for t in self._tools.values() if t.approved]

    def dispatch(self, name: str, args: Optional[dict] = None) -> dict:
        tool = self._tools.get(name)
        if tool is None or not tool.approved:
            return {"ok": False, "error": f"unknown tool {name!r}"}
        try:
            result = tool.handler(dict(args or {}))
        except Exception as exc:
            return {"ok": False,
                    "error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(result, dict):
            result = {"ok": True, "result": result}
        result.setdefault("ok", True)
        return result


def _reminders_path(data_dir: str) -> str:
    return os.path.join(data_dir, "reminders.json")


def _load_reminders(data_dir: str) -> list:
    path = _reminders_path(data_dir)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_reminders(data_dir: str, rows: list) -> None:
    with open(_reminders_path(data_dir), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)


def build_default_registry(data_dir) -> SkillRegistry:
    """The built-in skills, approved. Handlers are real: reminders
    persist to the data dir, notes are sandboxed inside it, web_search
    really fetches (and fails honestly when the network won't)."""
    data_dir = str(data_dir)
    registry = SkillRegistry()

    def now(args):
        utc = datetime.now(timezone.utc)
        return {"iso": utc.isoformat(),
                "local": datetime.now().astimezone().isoformat(),
                "unix": utc.timestamp()}

    def reminder_set(args):
        minutes = max(1, int(args.get("in_minutes", 10)))
        due = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        row = {"id": uuid.uuid4().hex[:8],
               "text": str(args.get("text", ""))[:500],
               "due": due.isoformat(), "done": False}
        rows = _load_reminders(data_dir)
        rows.append(row)
        _save_reminders(data_dir, rows)
        return {"id": row["id"], "due": row["due"], "text": row["text"]}

    def reminder_list(args):
        pending = [r for r in _load_reminders(data_dir)
                   if not r.get("done")]
        return {"reminders": pending}

    def notes_write(args):
        notes_dir = os.path.join(data_dir, "notes")
        os.makedirs(notes_dir, exist_ok=True)
        target = os.path.normpath(
            os.path.join(notes_dir, str(args.get("path", "note.txt"))))
        if os.path.commonpath([notes_dir, target]) != notes_dir:
            return {"ok": False,
                    "error": "note path escapes the notes sandbox"}
        with open(target, "w", encoding="utf-8") as f:
            f.write(str(args.get("content", "")))
        return {"written": os.path.relpath(target, data_dir)}

    def web_search(args):
        query = str(args.get("query", "")).strip()
        if not query:
            return {"ok": False, "error": "empty query"}
        url = ("https://lite.duckduckgo.com/lite/?q="
               + urllib.parse.quote_plus(query))
        req = urllib.request.Request(
            url, headers={"User-Agent": "JARVIS-v4/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                html = resp.read().decode("utf-8", "replace")
        except Exception as exc:
            return {"ok": False,
                    "error": f"web search failed: {exc}"}
        results = []
        for m in re.finditer(
                r'<a[^>]+class="result-link"[^>]*href="([^"]+)"[^>]*>'
                r"(.*?)</a>", html, re.S):
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if title:
                results.append({"title": title, "url": m.group(1)})
            if len(results) >= 5:
                break
        return {"query": query, "results": results}

    def pc_launch_app(args):
        name = str(args.get("name", "")).strip()
        if not name:
            return {"ok": False, "error": "no app name given"}
        if os.name != "nt":
            return {"ok": False,
                    "error": "pc control is not available on this "
                             "platform (Windows only)"}
        path = shutil.which(name) or shutil.which(name + ".exe")
        if path is None:
            return {"ok": False,
                    "error": f"app not found on PATH: {name}"}
        os.startfile(path)  # noqa: S606 - owner-approved action only
        return {"launched": name, "path": path}

    registry.register(
        "now", "Current date and time (UTC + local).", 
        {"type": "object", "properties": {}}, "time.read", now)
    registry.register(
        "reminder_set", "Set a reminder N minutes from now.",
        {"type": "object",
         "properties": {"in_minutes": {"type": "integer"},
                        "text": {"type": "string"}},
         "required": ["text"]},
        "reminder.write", reminder_set)
    registry.register(
        "reminder_list", "List pending reminders.",
        {"type": "object", "properties": {}}, "reminder.read",
        reminder_list)
    registry.register(
        "notes_write", "Write a note file inside the data dir's notes "
        "sandbox.", {"type": "object",
                     "properties": {"path": {"type": "string"},
                                    "content": {"type": "string"}},
                     "required": ["content"]},
        "notes.write", notes_write)
    registry.register(
        "web_search", "Search the web for a query.",
        {"type": "object",
         "properties": {"query": {"type": "string"}},
         "required": ["query"]},
        "net.outbound", web_search)
    registry.register(
        "pc.launch_app", "Launch a desktop application by name "
        "(Windows).", {"type": "object",
                       "properties": {"name": {"type": "string"}},
                       "required": ["name"]},
        "pc.control", pc_launch_app)
    return registry


# --------------------------------------------------------------------------- #
# Brain (contract: Brain(chain, tool_schemas, dispatch, journal, memory)
#         .respond(history, user_text) -> BrainResult{reply, ...})
# --------------------------------------------------------------------------- #

PERSONA = (
    "You are JARVIS, a personal AI assistant for your owner, Owais. "
    "Be brief, warm, and competent; address him as 'sir' sparingly. "
    "Use the tools when a request needs an action — never claim an "
    "action happened unless a tool result confirms it. If a tool "
    "result says an action needs approval or failed, say so plainly."
)


@dataclass
class BrainResult:
    reply: str
    tool_calls: List[str] = field(default_factory=list)
    provider: str = "unknown"


class Brain:
    """Minimal ReAct brain over the provider chain.

    The system prompt is byte-stable across turns (no timestamps, no
    per-turn data in the persona block) — per-turn facts ride in a
    separate message, so provider prefix caches survive.
    """

    MAX_ROUNDS = 3
    HISTORY_WINDOW = 20

    def __init__(self, chain, tool_schemas, dispatch, journal, memory):
        self.chain = chain  # the eval harness's fault-injection seam
        self.tool_schemas = tool_schemas
        self.dispatch = dispatch
        self.journal = journal
        self.memory = memory

    def respond(self, history, user_text) -> BrainResult:
        messages: List[dict] = [{"role": "system", "content": PERSONA}]
        facts = []
        if self.memory is not None:
            try:
                facts = self.memory.recall(str(user_text), k=3)
            except Exception:
                facts = []
        if facts:
            joined = "; ".join(f["text"] for f in facts)
            messages.append({"role": "system",
                             "content": f"Known facts: {joined}"})
        messages.extend(list(history)[-self.HISTORY_WINDOW:])
        messages.append({"role": "user", "content": str(user_text)})

        called: List[str] = []
        for _round in range(self.MAX_ROUNDS):
            try:
                reply = self.chain.complete(messages, self.tool_schemas)
            except Exception as exc:
                text = (f"I couldn't reach a working brain just now, "
                        f"sir — the provider call failed "
                        f"({type(exc).__name__}: {exc}). Nothing was "
                        f"done, and I won't pretend otherwise.")
                self._journal_reply(text)
                return BrainResult(reply=text, tool_calls=called,
                                   provider=self._provider_name())
            if isinstance(reply, str):
                reply = {"content": reply}
            calls = reply.get("tool_calls") or []
            if not calls:
                text = str(reply.get("content") or "")
                self._journal_reply(text)
                return BrainResult(reply=text, tool_calls=called,
                                   provider=self._provider_name())
            messages.append({"role": "assistant", "content": "",
                             "tool_calls": calls})
            for call in calls:
                name = str(call.get("name", ""))
                args = call.get("args") or {}
                called.append(name)
                result = self.dispatch(name, args)
                messages.append({
                    "role": "tool", "name": name,
                    "content": json.dumps(result, ensure_ascii=False,
                                          default=str)})
        text = ("I stopped after several tool rounds without a final "
                "answer, sir — saying so honestly rather than looping.")
        self._journal_reply(text)
        return BrainResult(reply=text, tool_calls=called,
                           provider=self._provider_name())

    def _provider_name(self) -> str:
        return (getattr(self.chain, "last_provider", None)
                or getattr(self.chain, "name", "chain"))

    def _journal_reply(self, text: str) -> None:
        if self.journal is not None:
            self.journal.append("brain.reply", {
                "text": text, "provider": self._provider_name()})
