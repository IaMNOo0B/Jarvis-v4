"""The gate — fail-closed evaluation of every tool call (Steps 13/14/18).

Design (from the ai-eng study, P13 action-bound approvals + P18 AI
Control / trusted editing + this repo's own eval BASELINE):

- The model proposes; the gate disposes. Every tool call is evaluated
  BEFORE it runs, in a fixed order, and every refusal is final:
    1. unknown tool            -> deny (fail closed: no catalog entry,
                                   no call — this is LAW 2's runtime half)
    2. IFC                       -> an untrusted origin may inform an
                                   answer but may never ORIGINATE an
                                   action (jarvis.ifc's one rule)
    3. permission                -> the tool's capability must be in the
                                   turn's allowances (LAW 1's runtime
                                   half — a tool the turn does not permit
                                   is denied here even if it was somehow
                                   advertised)
    4. schema                    -> args validated with
                                   jarvis.constrained; the denial carries
                                   the exact JSON-pointer paths
    5. destructive classes       -> fs.delete / process.kill /
                                   message.send (or manifest-flagged)
                                   never run on the model's say-so:
                                   verdict require_approval + an HMAC
                                   approval token bound to
                                   (tool + canonical-JSON arg digest +
                                   120 s expiry + one-time nonce)
- verify_approval recomputes the digest: ONE changed argument and the
  token is worthless. A verified nonce is burned — in memory AND in
  the journal — so a token can never be replayed.
- Every decision is journaled (kind gate.decision) with the args
  DIGEST, never the raw args: the journal is the audit record, and
  audits do not need the payload, only its fingerprint.

Stdlib only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from jarvis import constrained, ifc

ALLOW = "allow"
ALLOW_WITH_UNDO = "allow_with_undo"
REQUIRE_APPROVAL = "require_approval"
DENY = "deny"

# Capability strings whose exercise is destructive by class: calling a
# tool that carries one of these permissions always needs the owner.
DESTRUCTIVE_PERMISSIONS = frozenset({"fs.delete", "process.kill", "message.send"})

APPROVAL_TTL_SECONDS = 120


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def args_digest(args: dict) -> str:
    """SHA-256 of the canonical-JSON encoding of a tool-call's args."""
    return hashlib.sha256(_canonical_json(args or {}).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ToolInfo:
    name: str
    permission: str
    schema: Optional[dict] = None
    destructive: bool = False
    undoable: bool = False

    @property
    def is_destructive(self) -> bool:
        return self.destructive or self.permission in DESTRUCTIVE_PERMISSIONS


@dataclass
class Decision:
    verdict: str                     # ALLOW | ALLOW_WITH_UNDO | REQUIRE_APPROVAL | DENY
    tool_name: str
    reason: str
    approval_token: Optional[str] = None
    missing_capabilities: list = field(default_factory=list)
    schema_errors: list = field(default_factory=list)  # list[str] display paths

    @property
    def allowed(self) -> bool:
        return self.verdict in (ALLOW, ALLOW_WITH_UNDO)


class Gate:
    """Fail-closed tool-call evaluator.

    `catalog` maps tool name -> ToolInfo. The skills registry fills it
    via register_tool() as skills register; a tool with no catalog
    entry is unknown and therefore denied — always.
    """

    def __init__(self, journal, secret_key: bytes,
                 now_fn: Callable[[], float] = time.time):
        if not secret_key:
            raise ValueError("gate requires a non-empty secret key")
        self.journal = journal
        self._key = bytes(secret_key)
        self._now = now_fn
        self.catalog: dict[str, ToolInfo] = {}
        # nonce -> token expiry. The expiry rides along so the
        # sets can be PRUNED (audit 2026-10-22: both sets grew
        # forever — every token ever issued or burned, kept
        # for the life of the process). Pruning an expired
        # burned nonce is safe: verify_approval rejects an
        # expired token on its exp BEFORE the burned check.
        self._burned_nonces: dict[str, int] = {}
        self._issued_nonces: dict[str, int] = {}
        self._rebuild_nonce_state()

    # -- catalog --------------------------------------------------------

    def register_tool(self, name: str, permission: str,
                      schema: Optional[dict] = None,
                      destructive: bool = False,
                      undoable: bool = False) -> None:
        self.catalog[name] = ToolInfo(
            name=name, permission=permission, schema=schema,
            destructive=destructive, undoable=undoable)

    def knows(self, tool_name: str) -> bool:
        return tool_name in self.catalog

    # -- evaluation -----------------------------------------------------

    def evaluate(self, tool_name: str, args: Optional[dict],
                 origin_label: str, allowances) -> Decision:
        args = args or {}
        allowances = set(allowances or ())
        info = self.catalog.get(tool_name)

        if info is None:
            return self._decide(DENY, tool_name, args, origin_label,
                                f"unknown tool: {tool_name!r} — no catalog "
                                f"entry, fail closed")

        # IFC first among the substantive checks: labels decide.
        if origin_label not in (ifc.TRUSTED, ifc.OWNER_ORIGIN):
            return self._decide(
                DENY, tool_name, args, origin_label,
                "untrusted origin may not originate an action (IFC): "
                f"origin_label={origin_label!r}")

        if info.permission not in allowances:
            missing = [info.permission]
            return self._decide(
                DENY, tool_name, args, origin_label,
                f"missing capabilities: {missing}",
                missing_capabilities=missing)

        if info.schema:
            errors = constrained.validate(args, info.schema)
            if errors:
                displays = [e.display for e in errors]
                return self._decide(
                    DENY, tool_name, args, origin_label,
                    "invalid arguments: " + "; ".join(displays),
                    schema_errors=displays)

        if info.is_destructive:
            token = self._issue_token(tool_name, args)
            return self._decide(
                REQUIRE_APPROVAL, tool_name, args, origin_label,
                f"destructive class ({info.permission}) — owner approval "
                f"required; token expires in {APPROVAL_TTL_SECONDS}s",
                approval_token=token)

        if info.undoable:
            return self._decide(ALLOW_WITH_UNDO, tool_name, args,
                                origin_label,
                                "permitted; effect is undoable")
        return self._decide(ALLOW, tool_name, args, origin_label, "permitted")

    # -- approval tokens --------------------------------------------------

    def _issue_token(self, tool_name: str, args: dict) -> str:
        nonce = secrets.token_hex(16)
        payload = {
            "tool": tool_name,
            "digest": args_digest(args),
            "exp": int(self._now()) + APPROVAL_TTL_SECONDS,
            "nonce": nonce,
        }
        body = base64.urlsafe_b64encode(
            _canonical_json(payload).encode("utf-8")).decode("ascii")
        sig = hmac.new(self._key, body.encode("ascii"),
                       hashlib.sha256).hexdigest()
        self._prune_nonces()
        self._issued_nonces[nonce] = payload["exp"]
        self.journal.append("gate.approval_issued", {
            "tool": tool_name,
            "args_digest": payload["digest"],
            "nonce": nonce,
            "exp": payload["exp"],
        })
        return f"{body}.{sig}"

    def verify_approval(self, token: Optional[str], tool_name: str,
                        args: Optional[dict]) -> bool:
        """True only for a genuine, unexpired, unspent token bound to
        exactly this tool and exactly these args. On success the nonce
        is burned — the same token can never verify twice."""
        if not token or "." not in token:
            return False
        body, _, sig = token.partition(".")
        expected = hmac.new(self._key, body.encode("ascii", "ignore"),
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return False
        try:
            payload = json.loads(
                base64.urlsafe_b64decode(body.encode("ascii")).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return False
        if payload.get("tool") != tool_name:
            return False
        if payload.get("digest") != args_digest(args or {}):
            return False
        if int(payload.get("exp", 0)) <= int(self._now()):
            return False
        nonce = payload.get("nonce")
        if not nonce or nonce in self._burned_nonces:
            return False
        # Burn it: in memory AND in the journal (the audit record).
        self._prune_nonces()
        self._burned_nonces[nonce] = int(payload.get("exp", 0))
        self.journal.append("gate.approval_consumed", {
            "tool": tool_name,
            "args_digest": payload["digest"],
            "nonce": nonce,
        })
        return True

    # -- internals --------------------------------------------------------

    def _prune_nonces(self) -> None:
        """Drop expired entries from both nonce sets. A burned
        nonce whose token has expired can never verify again
        anyway (the exp check runs first), so forgetting it
        reopens nothing."""
        now = int(self._now())
        self._issued_nonces = {
            n: exp for n, exp in self._issued_nonces.items()
            if exp > now}
        self._burned_nonces = {
            n: exp for n, exp in self._burned_nonces.items()
            if exp > now}

    def _rebuild_nonce_state(self) -> None:
        """Rebuild issued/burned nonce sets from the journal so a
        restart cannot resurrect a spent token."""
        if self.journal is None:
            return
        try:
            records = list(self.journal.replay())
        except Exception:
            return
        for rec in records:
            payload = rec.get("payload") or {}
            nonce = payload.get("nonce")
            if not nonce:
                continue
            if rec.get("kind") == "gate.approval_issued":
                self._issued_nonces[nonce] = int(
                    payload.get("exp", 0))
            elif rec.get("kind") == "gate.approval_consumed":
                # The consumed event carries no exp; borrow it
                # from the issued record. Unknown exp = keep
                # forever (today's behaviour — safe side).
                self._burned_nonces[nonce] = \
                    self._issued_nonces.get(nonce, 2 ** 62)
        self._prune_nonces()

    def _decide(self, verdict: str, tool_name: str, args: dict,
                origin_label: str, reason: str,
                approval_token: Optional[str] = None,
                missing_capabilities: Optional[list] = None,
                schema_errors: Optional[list] = None) -> Decision:
        decision = Decision(
            verdict=verdict, tool_name=tool_name, reason=reason,
            approval_token=approval_token,
            missing_capabilities=missing_capabilities or [],
            schema_errors=schema_errors or [])
        if self.journal is not None:
            self.journal.append("gate.decision", {
                "tool": tool_name,
                "verdict": verdict,
                "reason": reason,
                "origin_label": origin_label,
                "args_digest": args_digest(args),
            })
        return decision
