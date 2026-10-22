"""Skill registry — quarantine, approval, drift, dispatch (Steps 13/14).

A skill is a manifest plus ops (callables the app registered). The two
laws this module exists to enforce came from the v2 eval BASELINE,
where both were violated live:

  LAW 1 — a tool may be advertised to the brain ONLY if a permission
          for it exists in the turn's allowances. (v2 failure:
          web_search was advertised but the turn never permitted
          net.outbound, so the gate denied it mid-turn.)
  LAW 2 — a tool may be advertised ONLY if its executor op actually
          exists. (v2 failure: launch_app was permitted and called,
          but the executor had no such op and the call died.)

advertised_tools() enforces both laws by construction: quarantined
skills contribute nothing, a tool whose permission is outside the
turn's allowances is not listed, and a tool whose op callable is
missing is not listed. contract_defects() is the audit half: it names
every manifest promise that breaks a law, so the contract test (and
the owner) can see the defects instead of discovering them live.

Trust lifecycle: a skill registers QUARANTINED — not advertised,
dispatch refuses. approve() pins the manifest hash. If the manifest
changes afterwards (drift — a skill swapped its tools under an
existing approval), the skill falls back to quarantine automatically
on the next state check. Approval never survives an edit.

Dispatch honours the gate: a destructive tool runs ONLY when handed a
gate Decision whose approval token verifies against the exact args
(one changed argument -> the gate refuses -> dispatch refuses).

Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import deque
from typing import Callable, Optional

from .validate import validate_args

# NOVA's rate discipline: a per-tool sliding window. Generous
# for real work, a hard wall for a runaway loop — the second
# wall behind the brain's LoopGuard.
RATE_LIMIT = 240          # calls
RATE_WINDOW = 60.0        # seconds

QUARANTINED = "quarantined"
APPROVED = "approved"


class SkillError(Exception):
    """Registration / lifecycle errors."""


class DispatchRefused(Exception):
    """Raised when dispatch will not run a call. Carries the reason."""

    def __init__(self, tool_name: str, reason: str):
        super().__init__(f"dispatch refused for {tool_name!r}: {reason}")
        self.tool_name = tool_name
        self.reason = reason


def manifest_hash(manifest: dict) -> str:
    """SHA-256 of the canonical JSON of a manifest — the pinned value."""
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class _Tool:
    __slots__ = ("skill", "name", "schema", "op_name", "permission",
                 "destructive", "undoable")

    def __init__(self, skill: str, spec: dict):
        self.skill = skill
        self.name = spec["name"]
        self.schema = spec.get("schema")
        self.op_name = spec.get("op")
        self.permission = spec["permission"]
        self.destructive = bool(spec.get("destructive", False))
        self.undoable = bool(spec.get("undoable", False))

    def is_destructive_class(self) -> bool:
        from jarvis.gate import DESTRUCTIVE_PERMISSIONS
        return self.destructive or self.permission in DESTRUCTIVE_PERMISSIONS


class _Skill:
    __slots__ = ("name", "version", "manifest", "ops", "tools",
                 "state", "approved_hash")

    def __init__(self, manifest: dict, ops: dict):
        self.name = manifest["name"]
        self.version = manifest.get("version", "0")
        self.manifest = manifest
        self.ops = dict(ops or {})
        self.tools = [_Tool(self.name, t) for t in manifest.get("tools", [])]
        self.state = QUARANTINED
        self.approved_hash: Optional[str] = None


class SkillRegistry:
    """Registry of skills, wired to the gate's tool catalog."""

    def __init__(self, gate=None, journal=None):
        self.gate = gate
        self.journal = journal
        self._skills: dict[str, _Skill] = {}
        self._tools: dict[str, _Tool] = {}   # tool name -> _Tool
        self._rate: dict[str, deque] = {}    # tool name -> call times

    # -- rate limiting (NOVA) --------------------------------------------
    def _rate_check(self, tool_name: str) -> bool:
        now = time.monotonic()
        hits = self._rate.setdefault(tool_name, deque())
        while hits and hits[0] <= now - RATE_WINDOW:
            hits.popleft()
        if len(hits) >= RATE_LIMIT:
            return False
        hits.append(now)
        return True

    def rate_stats(self) -> dict:
        """Calls inside the current window, per tool — the
        rate-limit tracker the owner can inspect."""
        now = time.monotonic()
        out = {}
        for name, hits in self._rate.items():
            live = [t for t in hits if t > now - RATE_WINDOW]
            if live:
                out[name] = len(live)
        return out

    # -- registration & lifecycle ----------------------------------------

    def register(self, manifest: dict, ops: Optional[dict] = None) -> str:
        """Register a skill in QUARANTINE. `ops` maps op names (as
        named in the manifest) to the callables that implement them."""
        name = manifest.get("name")
        if not name:
            raise SkillError("manifest has no 'name'")
        if name in self._skills:
            raise SkillError(f"skill {name!r} is already registered")
        skill = _Skill(manifest, ops or {})
        for tool in skill.tools:
            if tool.name in self._tools:
                raise SkillError(
                    f"tool {tool.name!r} is already provided by skill "
                    f"{self._tools[tool.name].skill!r}")
        self._skills[name] = skill
        for tool in skill.tools:
            self._tools[tool.name] = tool
            self._sync_gate(tool)
        self._log("skill.registered",
                  {"skill": name, "tools": [t.name for t in skill.tools]})
        return name

    def update_manifest(self, name: str, manifest: dict,
                        ops: Optional[dict] = None) -> None:
        """Replace a skill's manifest (and optionally its ops). If the
        skill was approved, the drift rule applies on the next state
        check: approval does not survive a changed manifest."""
        skill = self._require(name)
        for tool in skill.tools:
            self._tools.pop(tool.name, None)
        fresh = _Skill(manifest, ops if ops is not None else skill.ops)
        fresh.state = skill.state
        fresh.approved_hash = skill.approved_hash
        self._skills[name] = fresh
        for tool in fresh.tools:
            self._tools[tool.name] = tool
            self._sync_gate(tool)
        self._check_drift(fresh)
        self._log("skill.manifest_updated", {"skill": name})

    def approve(self, name: str) -> None:
        skill = self._require(name)
        skill.approved_hash = manifest_hash(skill.manifest)
        skill.state = APPROVED
        self._log("skill.approved",
                  {"skill": name, "manifest_hash": skill.approved_hash})

    def quarantine(self, name: str) -> None:
        skill = self._require(name)
        skill.state = QUARANTINED
        skill.approved_hash = None
        self._log("skill.quarantined", {"skill": name})

    def state(self, name: str) -> str:
        skill = self._require(name)
        self._check_drift(skill)
        return skill.state

    # -- the two laws ------------------------------------------------------

    def advertised_tools(self, allowances) -> list:
        """Tool schemas the brain may be told about this turn.

        A tool appears ONLY if ALL of these hold:
        - its skill is APPROVED (and not drifted back to quarantine),
        - LAW 1: its permission is in the turn's allowances,
        - LAW 2: its op callable actually exists.
        """
        allowances = set(allowances or ())
        out = []
        for skill in self._skills.values():
            self._check_drift(skill)
            if skill.state != APPROVED:
                continue
            for tool in skill.tools:
                if tool.permission not in allowances:
                    continue                                # LAW 1
                if not callable(skill.ops.get(tool.op_name)):
                    continue                                # LAW 2
                out.append({
                    "name": tool.name,
                    "skill": skill.name,
                    "permission": tool.permission,
                    "destructive": tool.is_destructive_class(),
                    "schema": tool.schema or {"type": "object"},
                })
        return out

    def contract_defects(self, allowances) -> list:
        """Audit every approved skill's manifest promises against the
        two laws. Returns one dict per defect found, naming the law,
        the skill, the tool, and the problem. An empty list means the
        registry honours the contract for these allowances."""
        allowances = set(allowances or ())
        defects = []
        for skill in self._skills.values():
            self._check_drift(skill)
            if skill.state != APPROVED:
                continue
            for tool in skill.tools:
                if tool.permission not in allowances:
                    defects.append({
                        "law": 1, "skill": skill.name, "tool": tool.name,
                        "problem": f"permission {tool.permission!r} is "
                                   f"not in the turn's allowances — the "
                                   f"tool must not be advertised"})
                if not callable(skill.ops.get(tool.op_name)):
                    defects.append({
                        "law": 2, "skill": skill.name, "tool": tool.name,
                        "problem": f"op {tool.op_name!r} does not exist — "
                                   f"the tool must not be advertised"})
        return defects

    # -- dispatch ----------------------------------------------------------

    def dispatch(self, tool_name: str, args: Optional[dict] = None,
                 decision=None):
        """Run a tool call through its registered op.

        Refuses (DispatchRefused) when: the tool is unknown, its skill
        is not approved, its op does not exist, or — for destructive
        tools — no gate Decision with a verifiable approval token for
        exactly these args is presented (the gate's verdict must have
        been honoured, not merely obtained)."""
        args = args or {}
        tool = self._tools.get(tool_name)
        if tool is None:
            raise DispatchRefused(tool_name, "unknown tool")
        skill = self._skills[tool.skill]
        self._check_drift(skill)
        if skill.state != APPROVED:
            raise DispatchRefused(
                tool_name, f"skill {skill.name!r} is {skill.state}")
        op = skill.ops.get(tool.op_name)
        if not callable(op):
            raise DispatchRefused(
                tool_name, f"op {tool.op_name!r} does not exist (LAW 2)")
        if tool.is_destructive_class():
            if decision is None:
                raise DispatchRefused(
                    tool_name, "destructive tool requires a gate Decision")
            if getattr(decision, "verdict", None) != "require_approval":
                raise DispatchRefused(
                    tool_name,
                    f"destructive tool requires a require_approval "
                    f"decision, got {getattr(decision, 'verdict', None)!r}")
            if self.gate is None:
                raise DispatchRefused(
                    tool_name, "no gate bound — cannot verify approval")
            if not self.gate.verify_approval(
                    getattr(decision, "approval_token", None),
                    tool_name, args):
                raise DispatchRefused(
                    tool_name,
                    "approval token does not verify for these exact args")
        problem = validate_args(tool.schema, args)
        if problem:
            raise DispatchRefused(
                tool_name, f"schema check fail: {problem}")
        if not self._rate_check(tool_name):
            raise DispatchRefused(
                tool_name,
                f"rate limit — {tool_name} ki had is minute ke "
                "liye puri ho gayi; thoda ruk kar phir koshish "
                "karo")
        self._log("skill.dispatched", {"skill": skill.name, "tool": tool_name})
        return op(**args)

    # -- internals ---------------------------------------------------------

    def _require(self, name: str) -> _Skill:
        skill = self._skills.get(name)
        if skill is None:
            raise SkillError(f"unknown skill {name!r}")
        return skill

    def _check_drift(self, skill: _Skill) -> None:
        """The drift rule: an approved skill whose manifest no longer
        matches the hash pinned at approval falls back to quarantine."""
        if skill.state == APPROVED and skill.approved_hash is not None:
            if manifest_hash(skill.manifest) != skill.approved_hash:
                skill.state = QUARANTINED
                skill.approved_hash = None
                self._log("skill.drift_quarantined", {"skill": skill.name})

    def _sync_gate(self, tool: _Tool) -> None:
        if self.gate is not None:
            self.gate.register_tool(
                tool.name, permission=tool.permission, schema=tool.schema,
                destructive=tool.destructive, undoable=tool.undoable)

    def _log(self, kind: str, payload: dict) -> None:
        if self.journal is not None:
            self.journal.append(kind, payload)
