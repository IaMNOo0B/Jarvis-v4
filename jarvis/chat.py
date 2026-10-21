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
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import _defaults, ifc
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
                    dispatch=dispatch, allowances=allowances)


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
