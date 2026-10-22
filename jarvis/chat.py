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
from .corrections import extract_correction, looks_like_correction
from .goodput import GoodputLedger
from .idempotency import IdempotencyConflictError, IdempotencyLedger
from .idempotency import business_key as _idem_business_key
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
    "tasks.delegate",
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
                        {"attempts": 0, "answered": 0,
                         "last_error": ""})
                    slot["attempts"] += 1
                    if row.get("ok"):
                        slot["answered"] += 1
                    if row.get("error"):
                        slot["last_error"] = str(row["error"])[:200]
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
            if slot.get("last_error"):
                findings.append(
                    f"{name} ki aakhri galti: {slot['last_error']}")
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
        from .failures import cluster_errors
        clusters = cluster_errors(recent_errors)
        for c in clusters:
            if c["count"] >= 2:
                findings.append(
                    f"Ek hi wajah baar-baar aa rahi hai: "
                    f"{c['kind']} — {c['error']} "
                    f"({c['count']} baar). Pehle is jhund ko "
                    "theek karna sabse zyada fayda dega.")
        return {"checks_failed": failed, "providers": providers,
                "keys_filed": keys_filed,
                "recent_errors": recent_errors,
                "failure_clusters": clusters,
                "findings": findings}

    return diagnose


DELEGATE_MANIFEST = {
    "name": "core.tasks",
    "version": "1.0.0",
    "tools": [{
        "name": "delegate_task",
        "op": "delegate_task",
        "permission": "tasks.delegate",
        "schema": {"type": "object",
                   "properties": {"task": {"type": "string",
                                           "minLength": 1}},
                   "required": ["task"],
                   "additionalProperties": False},
    }, {
        "name": "delegate_tasks",
        "op": "delegate_tasks",
        "permission": "tasks.delegate",
        "schema": {"type": "object",
                   "properties": {
                       "tasks": {"type": "array",
                                 "items": {"type": "string"}}},
                   "required": ["tasks"],
                   "additionalProperties": False},
    }],
}


MISSION_MANIFEST = {
    "name": "core.mission",
    "version": "1.0.0",
    "tools": [{
        "name": "run_mission",
        "op": "run_mission",
        "permission": "tasks.delegate",
        "schema": {"type": "object",
                   "properties": {
                       "goal": {"type": "string", "minLength": 1},
                       "max_steps": {"type": "integer",
                                     "minimum": 1, "maximum": 25}},
                   "required": ["goal"],
                   "additionalProperties": False},
    }],
}


BUILDS_MANIFEST = {
    "name": "core.builds",
    "version": "1.0.0",
    "tools": [{
        "name": "build_task",
        "op": "build_task",
        "permission": "tasks.delegate",
        "schema": {"type": "object",
                   "properties": {
                       "goal": {"type": "string", "minLength": 1},
                       "mode": {"type": "string",
                                "enum": ["planning", "fast"]},
                       "workspace": {"type": "string"}},
                   "required": ["goal"],
                   "additionalProperties": False},
    }],
}


USER_MODEL_MANIFEST = {
    "name": "core.user_model",
    "version": "1.0.0",
    "tools": [{
        "name": "note_insight",
        "op": "note_insight",
        "permission": "memory.write",
        "schema": {"type": "object",
                   "properties": {
                       "dimension": {"type": "string"},
                       "claim": {"type": "string", "minLength": 1},
                       "evidence": {"type": "string"},
                       "relation": {"type": "string"}},
                   "required": ["claim"],
                   "additionalProperties": False},
    }],
}


MAIL_MANIFEST = {
    "name": "core.mail",
    "version": "1.0.0",
    "tools": [{
        "name": "email_send",
        "op": "email_send",
        "permission": "notify.send",
        "schema": {"type": "object",
                   "properties": {
                       "to": {"type": "string", "minLength": 3},
                       "subject": {"type": "string", "minLength": 1},
                       "body": {"type": "string", "minLength": 1},
                       "account": {"type": "string"}},
                   "required": ["to", "subject", "body"],
                   "additionalProperties": False},
    }, {
        "name": "email_draft",
        "op": "email_draft",
        "permission": "notify.send",
        "schema": {"type": "object",
                   "properties": {
                       "to": {"type": "string", "minLength": 3},
                       "subject": {"type": "string", "minLength": 1},
                       "body": {"type": "string", "minLength": 1},
                       "account": {"type": "string"}},
                   "required": ["to", "subject", "body"],
                   "additionalProperties": False},
    }, {
        "name": "accounts_status",
        "op": "accounts_status",
        "permission": "system.diagnose",
        "schema": {"type": "object",
                   "properties": {"channel": {"type": "string"}},
                   "additionalProperties": False},
    }],
}


def _mail_ops(secrets, data_dir):
    """Email from the owner's own accounts (jarvis/accounts.py +
    jarvis/mailer.py). Sending rotates across eligible accounts
    and counts against each account's daily cap — the book, not
    the model, keeps that discipline. Drafts file into the
    account's own Drafts folder."""
    from .accounts import AccountBook
    from . import mailer

    def _choose(account: str):
        book = AccountBook(data_dir)
        if account:
            row = book.get(str(account)) or next(
                (r for r in book.list("email")
                 if r["label"] == account
                 or r["address"] == account), None)
            if row is None:
                return book, None, "woh email khaata mila nahi, sir"
            if not book.eligible(row):
                return book, None, (
                    f"khaata '{row['label']}' abhi nahi chal "
                    f"sakta (cap/rest: {row.get('rest_reason', '')}"
                    "), sir")
            return book, row, ""
        row = book.pick("email")
        if row is None:
            if not book.list("email"):
                return book, None, ("koi email khaata juda hi "
                                    "nahi hai, sir — Settings ke "
                                    "ACCOUNTS hisse se jodo")
            return book, None, ("saare email khaate aaj ki had "
                                "par hain ya rest par hain, sir")
        return book, row, ""

    def _password(row):
        try:
            return secrets.get(f"account-{row['id']}")
        except Exception:
            return None

    def email_send(to: str, subject: str, body: str,
                   account: str = ""):
        book, row, error = _choose(account)
        if row is None:
            return {"ok": False, "error": error}
        password = _password(row)
        if not password:
            return {"ok": False,
                    "error": f"khaate '{row['label']}' ka "
                             "app-password Settings me nahi "
                             "dala gaya, sir"}
        try:
            mailer.send_email(row["address"], str(password),
                              to, subject, body)
        except Exception as exc:
            return {"ok": False,
                    "error": f"email nahi gaya: "
                             f"{type(exc).__name__}: "
                             f"{str(exc)[:120]}"}
        book.record_send(row["id"])
        return {"ok": True, "account": row["label"], "to": to,
                "sent_today": row["sent_count"] + 1,
                "cap": row["cap"]}

    def email_draft(to: str, subject: str, body: str,
                    account: str = ""):
        book, row, error = _choose(account)
        if row is None:
            return {"ok": False, "error": error}
        password = _password(row)
        if not password:
            return {"ok": False,
                    "error": f"khaate '{row['label']}' ka "
                             "app-password Settings me nahi "
                             "dala gaya, sir"}
        try:
            out = mailer.draft_email(row["address"],
                                     str(password), to,
                                     subject, body)
        except Exception as exc:
            return {"ok": False,
                    "error": f"draft nahi bana: "
                             f"{type(exc).__name__}: "
                             f"{str(exc)[:120]}"}
        return {"ok": True, "account": row["label"],
                "draft_in": out["draft_in"]}

    def accounts_status(channel: str = ""):
        book = AccountBook(data_dir)
        return {"ok": True,
                "accounts": book.overview()
                if not channel else
                [r for r in book.overview()
                 if r["channel"] == channel]}

    return {"email_send": email_send, "email_draft": email_draft,
            "accounts_status": accounts_status}


FORGE_MANIFEST = {
    "name": "core.forge",
    "version": "1.0.0",
    "tools": [{
        "name": "build_feature",
        "op": "build_feature",
        "permission": "self.upgrade",
        "schema": {"type": "object",
                   "properties": {
                       "name": {"type": "string", "minLength": 1},
                       "brief": {"type": "string", "minLength": 5}},
                   "required": ["name", "brief"],
                   "additionalProperties": False},
    }],
}


def _app_root(parts) -> Path:
    source = Path(str(getattr(parts, "rules_source", "") or ""))
    if source.exists():
        return source.parent
    return Path(__file__).resolve().parent.parent


AUTOMATION_MANIFEST = {
    "name": "core.automations",
    "version": "1.0.0",
    "tools": [
        {"name": "create_automation",
         "op": "create_automation",
         "permission": "tasks.delegate",
         "schema": {"type": "object",
                    "properties": {
                        "name": {"type": "string", "minLength": 1},
                        "title": {"type": "string"},
                        "steps": {"type": "array"},
                        "params": {"type": "array"},
                        "triggers": {"type": "array"}},
                    "required": ["name", "steps"],
                    "additionalProperties": False}},
        {"name": "list_automations",
         "op": "list_automations",
         "permission": "tasks.delegate",
         "schema": {"type": "object", "properties": {},
                    "additionalProperties": False}},
        {"name": "run_automation",
         "op": "run_automation",
         "permission": "tasks.delegate",
         "schema": {"type": "object",
                    "properties": {
                        "name": {"type": "string", "minLength": 1},
                        "params": {"type": "object"},
                        "start_step": {"type": "integer",
                                       "minimum": 0}},
                    "required": ["name"],
                    "additionalProperties": False}},
        {"name": "delete_automation",
         "op": "delete_automation",
         "permission": "tasks.delegate",
         "destructive": True,
         "schema": {"type": "object",
                    "properties": {
                        "name": {"type": "string", "minLength": 1}},
                    "required": ["name"],
                    "additionalProperties": False}},
    ],
}

_META_TOOLS = ("create_automation", "run_automation",
               "delete_automation")


def _automation_ops(holder, data_dir):
    """Saved routes for repeated work (jarvis/automations.py):
    the brain composes a route once — steps of real tools,
    {{params}} for the parts that change — and later runs it
    on command. Steps dispatch normally, so a destructive
    step files its confirmation card and the route pauses
    there instead of sneaking past the gate."""

    def _book():
        from .automations import AutomationBook
        return AutomationBook(data_dir)

    def create_automation(name: str, title: str = "",
                          steps=None, params=None, triggers=None):
        parts = holder.get("parts")
        if parts is None:
            return {"ok": False,
                    "error": "automations abhi taiyaar nahi, sir"}
        from .automations import validate_steps
        known = {t["name"] for t in
                 parts.registry.advertised_tools(parts.allowances)}
        known -= set(_META_TOOLS)
        err = validate_steps(steps, known)
        if err:
            return {"ok": False, "error": err}
        row, err = _book().create(name, title or name,
                                  steps, params or [],
                                  triggers=triggers)
        if err:
            return {"ok": False, "error": err}
        return {"ok": True, "automation": row["name"],
                "title": row["title"], "steps": len(steps),
                "params": row["params"],
                "message": f"Route ban gaya, sir — agli baar "
                           "ye kaam bolo ge to JARVIS khud "
                           "pehchan kar chala dega."}

    def list_automations():
        return {"ok": True, "automations": _book().list()}

    def run_automation(name: str, params=None, start_step: int = 0):
        parts = holder.get("parts")
        if parts is None:
            return {"ok": False,
                    "error": "automations abhi taiyaar nahi, sir"}
        book = _book()
        auto = book.get(name)
        if auto is None:
            return {"ok": False,
                    "error": f"{name!r} naam ka automation nahi "
                             "mila — list_automations me dekho, "
                             "sir"}
        for step in auto.get("steps") or []:
            if step.get("tool") in _META_TOOLS:
                return {"ok": False,
                        "error": "is route me automation wala "
                                 "tool hai — route ke andar route "
                                 "nahi chalega, sir"}
        from .automations import run_steps
        report = run_steps(parts.dispatch, auto, params or {},
                           start_step or 0)
        book.note_run(auto["name"], str(report.get("status", "")))
        return report

    def delete_automation(name: str):
        if _book().remove(name):
            return {"ok": True, "deleted": name}
        return {"ok": False,
                "error": f"{name!r} naam ka automation nahi "
                         "mila, sir"}

    return {"create_automation": create_automation,
            "list_automations": list_automations,
            "run_automation": run_automation,
            "delete_automation": delete_automation}


def _forge_ops(holder, data_dir):
    """build_feature (jarvis/forge.py): hukum par JARVIS khud
    naya feature banata hai — pehle research (saboot ke saath),
    phir alag jagah me code + uska apna test, sandbox me
    verify, pass ho to hi live — aur live test fail ho to
    khud rollback. Malik ke khade hukum ke tehat promote
    automatic hai; quality gate hi iski suraksha hai."""
    def build_feature(name: str, brief: str):
        parts = holder.get("parts")
        if parts is None:
            return {"ok": False,
                    "error": "forge abhi taiyaar nahi hai, sir"}
        if _is_stopped(data_dir):
            return {"ok": False,
                    "error": "Band hoon, sir — /resume ke baad "
                             "feature banega"}
        task_id = uuid.uuid4().hex[:10]

        def _run():
            from . import forge
            root = _app_root(parts)
            research = _delegated_summary(parts, (
                "Research how to build this feature for a "
                "stdlib-only Python assistant. Use github_search"
                " / web_search / web_fetch. Feature: "
                f"{brief}\nAnswer with: the pattern to use, "
                "the source names you took it from, and any "
                "pitfall — short."))
            builder = _team_brain(parts, with_tools=False)
            reply = builder.respond([], (
                "You are the BUILDER. Design this feature for "
                "JARVIS (stdlib-only Python; modules live in "
                "jarvis/). Research notes:\n" + research
                + "\nFeature: " + brief
                + "\nReply with ONLY JSON: {\"files\": "
                "{\"jarvis/<module>.py\": \"<full code>\"}, "
                "\"test\": \"<pytest code importing the new "
                "module and proving it works>\", \"source\": "
                "\"<where the pattern came from>\"}. Files "
                "only under jarvis/, tests/ or knowledge/. "
                "Keep it small and self-contained.")).reply
            plan = _json_from_reply(reply)
            if not isinstance(plan, dict) \
                    or not isinstance(plan.get("files"), dict):
                _notify(data_dir, "Feature nahi bana",
                        "Builder ka plan samajh nahi aaya, sir — "
                        "kuch live nahi badla.", kind="task")
                return
            staged = forge.stage_feature(
                data_dir, root, name, plan["files"],
                str(plan.get("test", "")),
                str(plan.get("source", "")))
            if not staged.get("ok"):
                _notify(data_dir, "Feature nahi bana",
                        "Stage fail, sir: "
                        + str(staged.get("error")), kind="task")
                return
            entry = Path(staged["dir"])
            verdict = forge.verify_staged(root, entry)
            if not verdict.get("ok"):
                _notify(data_dir, "Feature ruk gaya",
                        f"'{name}' sandbox me fail hua "
                        f"({verdict.get('stage')}): "
                        + str(verdict.get("detail"))[:250]
                        + "\nLive tree ko haath nahi laga.",
                        kind="task")
                _task_log(data_dir, {
                    "id": task_id, "kind": "forge",
                    "status": "verify-failed", "task": name})
                return
            landed = forge.promote(root, entry)
            ok = bool(landed.get("ok"))
            _task_log(data_dir, {
                "id": task_id, "kind": "forge",
                "status": "promoted" if ok else "rolled-back",
                "task": name})
            if ok:
                _notify(data_dir, "Naya feature live hai",
                        f"'{name}' ban kar live ho gaya, sir.\n"
                        "Files: " + ", ".join(landed["files"])
                        + f"\nPasand na aaye to: /forge "
                          f"rollback {staged['slug']}",
                        kind="task")
            else:
                _notify(data_dir, "Feature wapas liya gaya",
                        f"'{name}' live test me fail hua — "
                        "khud rollback ho gaya, sir. Wajah: "
                        + str(landed.get("detail"))[:250],
                        kind="task")
            try:
                parts.journal.append(
                    "forge.promoted" if ok else "forge.rolled_back",
                    {"slug": staged["slug"], "name": name})
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True,
                         name=f"jarvis-forge-{task_id}").start()
        return {"ok": True, "task_id": task_id,
                "note": "feature ban raha hai, sir — research, "
                        "code, sandbox test; pass hua to live, "
                        "warna khabar milegi ki kyun ruka"}

    return {"build_feature": build_feature}


TRIGGERS_MANIFEST = {
    "name": "core.triggers",
    "version": "1.0.0",
    "tools": [{
        "name": "set_trigger",
        "op": "set_trigger",
        "permission": "tasks.delegate",
        "schema": {"type": "object",
                   "properties": {
                       "name": {"type": "string", "minLength": 1},
                       "event": {"type": "string", "minLength": 1},
                       "match": {"type": "object"},
                       "action": {"type": "string", "minLength": 3}},
                   "required": ["name", "event", "action"],
                   "additionalProperties": False},
    }, {
        "name": "list_triggers",
        "op": "list_triggers",
        "permission": "tasks.delegate",
        "schema": {"type": "object", "properties": {},
                   "additionalProperties": False},
    }],
}


def _trigger_ops(data_dir):
    """Triggered tasks (jarvis/triggers.py): the owner defines
    event + filter + work ONCE — banane ka hukum hi uski
    manzoori hai — phir JARVIS har minute dekhta hai aur
    ghatna ghatne par woh kaam ek hi baar chalata hai."""
    def set_trigger(name: str, event: str, action: str,
                    match: dict = None):
        from .triggers import TriggerBook
        try:
            row = TriggerBook(data_dir).add(
                name, event, match or {}, action)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "trigger_id": row["id"],
                "note": "trigger tayyaar hai, sir — ghatna "
                        "ghat te hi kaam ek baar chalega aur "
                        "khabar milegi"}

    def list_triggers():
        from .triggers import TriggerBook
        rows = TriggerBook(data_dir).list()
        return {"ok": True, "triggers": [
            {"id": r["id"], "name": r["name"],
             "event": r["event"], "state": r["state"],
             "fired": len(r.get("fired") or {})}
            for r in rows]}

    return {"set_trigger": set_trigger,
            "list_triggers": list_triggers}


def _maybe_nudge(app) -> None:
    """The heartbeat nudge: ONCE a day, if things are waiting
    that the owner acts on (follow-ups due, rule proposals,
    undecided calibrations), one quiet notification — never a
    daily spam, never when nothing waits."""
    import json as _json
    from pathlib import Path as _Path
    meta_path = _Path(app.data_dir) / "meta.json"
    today = time.strftime("%Y-%m-%d")
    try:
        meta = _json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            meta = {}
    except Exception:
        meta = {}
    if meta.get("nudge_day") == today:
        return
    bits = []
    try:
        from .tracker import due_followups
        due = due_followups(app.data_dir)
        if due:
            bits.append(f"{len(due)} follow-up due")
    except Exception:
        pass
    try:
        from .candidates import pending as _pend
        waiting = _pend(app.data_dir)
        if waiting:
            bits.append(f"{len(waiting)} rule salah baaki")
    except Exception:
        pass
    try:
        open_decisions = app.calibrator.pending(limit=50)
        if open_decisions:
            bits.append(f"{len(open_decisions)} faisle nateeje "
                        "ke intezaar me")
    except Exception:
        pass
    if not bits:
        return
    meta["nudge_day"] = today
    try:
        meta_path.write_text(_json.dumps(meta), encoding="utf-8")
    except Exception:
        return
    _notify(app.data_dir, "Aaj ki khabar",
            "Sir, kuch cheezein aapke faisle ki raah dekh "
            "rahi hain: " + "; ".join(bits) + ". /brief me "
            "tafseel hai.", kind="nudge")


TEAM_MANIFEST = {
    "name": "core.team",
    "version": "1.0.0",
    "tools": [{
        "name": "delegate_team",
        "op": "delegate_team",
        "permission": "tasks.delegate",
        "schema": {"type": "object",
                   "properties": {
                       "task": {"type": "string", "minLength": 1},
                       "target": {"type": "string"}},
                   "required": ["task"],
                   "additionalProperties": False},
    }],
}


def _team_brain(parts, with_tools: bool):
    from .brain import Brain
    from .rules import chat_protocol
    schemas = (parts.registry.advertised_tools(parts.allowances)
               if with_tools else [])
    return Brain(parts.chain, schemas, parts.dispatch,
                 parts.journal, parts.memory, audit=False,
                 **({"protocol": chat_protocol(parts.rules_text)}
                    if getattr(parts, "rules_text", "") else {}))


def _json_from_reply(text: str):
    """A model's JSON, fenced or bare; None when it isn't any."""
    cleaned = str(text).strip()
    if "```" in cleaned:
        cleaned = cleaned.split("```")[1].strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        return json.loads(cleaned)
    except Exception:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(cleaned[start:end + 1])
            except Exception:
                return None
    return None


def _team_ops(holder, data_dir):
    """delegate_team (jarvis/team.py): a coding task split by a
    planner into file-owned subtasks, worked in parallel
    isolated workspaces, judged by a separate critic, repaired
    at most twice, merged and verified by execution. Runs on a
    background thread; the owner hears the outcome by
    notification."""
    def delegate_team(task: str, target: str = ""):
        parts = holder.get("parts")
        if parts is None:
            return {"ok": False,
                    "error": "team abhi taiyaar nahi hai, sir"}
        if _is_stopped(data_dir):
            return {"ok": False,
                    "error": "Band hoon, sir — /resume ke baad "
                             "team chalega"}
        task = str(task).strip()
        if not task:
            return {"ok": False, "error": "task khaali hai, sir"}
        task_id = uuid.uuid4().hex[:10]

        def planner(task_text: str):
            brain = _team_brain(parts, with_tools=False)
            reply = brain.respond([], (
                "You are the COORDINATOR. Split this coding task "
                "into at most 5 subtasks with EXPLICIT file "
                "ownership — each file belongs to exactly ONE "
                "subtask. Reply with ONLY JSON: {\"subtasks\": "
                "[{\"title\": \"...\", \"files\": [\"name.py\"], "
                "\"brief\": \"what to build\"}]}\nTask: "
                + task_text)).reply
            return _json_from_reply(reply)

        def worker(task_text, subtask, workspace, issues):
            order = (
                f"Team task: {task_text}\nTumhara hissa: "
                f"{subtask['title']} — {subtask['brief']}\n"
                f"Sirf ye files tumhari hain: "
                f"{', '.join(subtask['files'])}\nHar file ko "
                f"write_file se is folder me banao: {workspace}\n"
                "Doosri files ko haath mat lagana. Aakhir me "
                "summary do ki kya banaya.")
            if issues:
                order += ("\nCritic ne ye khaamiyan nikali hain "
                          "— inhe theek karo:\n" + issues)
            return _delegated_summary(parts, order)

        def critic(task_text, subtask, content, summary):
            brain = _team_brain(parts, with_tools=False)
            reply = brain.respond([], (
                "You are the CRITIC — you did NOT write this "
                "code; judge it cold. Subtask: "
                f"{subtask['title']} — {subtask['brief']}\n"
                f"Worker ki summary: {summary}\nFiles:\n"
                f"{content}\nReply with ONLY JSON: "
                "{\"ok\": true} or {\"ok\": false, \"issues\": "
                "\"kya kharab hai, saaf shabdon me\"}")).reply
            verdict = _json_from_reply(reply)
            if not isinstance(verdict, dict) \
                    or "ok" not in verdict:
                return {"ok": False,
                        "issues": "critic ka jawab samajh nahi "
                                  "aaya — dobara koshish karo"}
            return verdict

        def _run():
            from .team import run_team
            report = run_team(
                data_dir, task, planner, worker, critic,
                target_dir=(Path(target) if str(target).strip()
                            else None))
            ok = bool(report.get("ok"))
            _task_log(data_dir, {
                "id": task_id, "kind": "team",
                "status": "done" if ok else "failed",
                "task": task[:200], "report": report})
            headline = "ho gaya" if ok else "fail hua"
            lines = [f"Team kaam {headline}, sir: {task[:80]}"]
            for sub in report.get("subtasks", []):
                mark = "✓" if sub.get("ok") else "✗"
                lines.append(f"{mark} {sub.get('title')} "
                             f"({sub.get('rounds', 1)} rounds)")
                if not sub.get("ok") and sub.get("issues"):
                    lines.append("   khaami: "
                                 + sub["issues"][:200])
            ver = report.get("verification") or {}
            if ver:
                lines.append(f"Jaanch ({ver.get('stage')}): "
                             f"{ver.get('detail', '')[:200]}")
            lines.append(f"Output: {report.get('output_dir', '')}")
            _notify(data_dir, "Team report", "\n".join(lines),
                    kind="task")
            try:
                parts.journal.append(
                    "team.done" if ok else "team.failed",
                    {"id": task_id, "run_id": report.get("run_id")})
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True,
                         name=f"jarvis-team-{task_id}").start()
        return {"ok": True, "task_id": task_id,
                "note": "team kaam par lag gaya hai, sir — planner "
                        "hisse banayega, workers apne-apne folder "
                        "me banayenge, critic jaanchega; report "
                        "notification me aayega"}

    return {"delegate_team": delegate_team}


TELEGRAM_MANIFEST = {
    "name": "core.telegram",
    "version": "1.0.0",
    "tools": [{
        "name": "telegram_send",
        "op": "telegram_send",
        "permission": "notify.send",
        "schema": {"type": "object",
                   "properties": {"text": {"type": "string",
                                           "minLength": 1}},
                   "required": ["text"],
                   "additionalProperties": False},
    }],
}


def _telegram_ops(secrets, data_dir):
    """telegram_send: a message to the owner's bound phone chat.
    Honest errors until the token is filed and the phone paired
    (/telegram token, /telegram pair)."""
    def telegram_send(text: str):
        from .telegram import TelegramBot, load_state
        token = None
        try:
            token = secrets.get("telegram_bot_token")
        except Exception:
            token = None
        if not token:
            return {"ok": False,
                    "error": "Telegram token nahi hai, sir — "
                             "pehle /telegram token <token> se "
                             "set karo"}
        state = load_state(data_dir)
        chat_id = state.get("chat_id")
        if not chat_id:
            return {"ok": False,
                    "error": "Phone abhi juda nahi hai, sir — "
                             "/telegram pair se jodo"}
        try:
            TelegramBot(str(token)).send_message(
                chat_id, str(text))
        except Exception as exc:
            return {"ok": False,
                    "error": f"telegram send failed: "
                             f"{type(exc).__name__}"}
        return {"ok": True}

    return {"telegram_send": telegram_send}


def _telegram_handle(app, text: str) -> str:
    """What a phone message runs: slash commands go to the same
    brain-free command path as the UI; everything else is a
    normal turn — same brain, same tools, same gate."""
    stripped = text.strip()
    if stripped.startswith("/"):
        return app.handle_command(stripped)
    return str(app.turn(stripped))


def _active_pair_code(app):
    pair = getattr(app, "_telegram_pair", None)
    if pair and pair[1] > time.time():
        return pair[0]
    return None


def start_telegram(app) -> None:
    """The Telegram polling thread: long-polls the Bot API when a
    token is filed and the channel is enabled; sleeps quietly
    otherwise. Started by the app entry points, never build_app."""
    if getattr(app, "_telegram_started", False):
        return
    app._telegram_started = True

    def _loop():
        while True:
            try:
                from .telegram import (TelegramBot, TelegramChannel,
                                       load_state)
                token = app.secrets.get("telegram_bot_token")
                state = load_state(app.data_dir)
                if not token or not state.get("enabled", True) \
                        or _is_stopped(app.data_dir):
                    time.sleep(10)
                    continue
                channel = TelegramChannel(
                    TelegramBot(str(token)), app.data_dir,
                    handle=lambda t: _telegram_handle(app, t),
                    pair_code=lambda: _active_pair_code(app),
                    notify_owner=lambda t, b: _notify(
                        app.data_dir, t, b, kind="telegram"),
                    journal=app.journal)
                while True:
                    if _is_stopped(app.data_dir):
                        break
                    if not load_state(app.data_dir).get(
                            "enabled", True):
                        break
                    if str(app.secrets.get(
                            "telegram_bot_token") or "") \
                            != str(token):
                        break    # token changed — rebuild
                    if channel.poll_once() == 0:
                        time.sleep(1)
            except Exception:
                time.sleep(10)

    threading.Thread(target=_loop, daemon=True,
                     name="jarvis-telegram").start()


CALIBRATION_MANIFEST = {
    "name": "core.calibration",
    "version": "1.0.0",
    "tools": [{
        "name": "record_decision",
        "op": "record_decision",
        "permission": "memory.write",
        "schema": {"type": "object",
                   "properties": {
                       "domain": {"type": "string", "minLength": 1},
                       "claim": {"type": "string", "minLength": 1},
                       "confidence": {"type": "number",
                                      "minimum": 0, "maximum": 1}},
                   "required": ["domain", "claim", "confidence"],
                   "additionalProperties": False},
    }, {
        "name": "resolve_decision",
        "op": "resolve_decision",
        "permission": "memory.write",
        "schema": {"type": "object",
                   "properties": {
                       "decision_id": {"type": "string",
                                       "minLength": 1},
                       "right": {"type": "boolean"}},
                   "required": ["decision_id", "right"],
                   "additionalProperties": False},
    }, {
        "name": "check_confidence",
        "op": "check_confidence",
        "permission": "memory.read",
        "schema": {"type": "object",
                   "properties": {
                       "domain": {"type": "string", "minLength": 1},
                       "confidence": {"type": "number",
                                      "minimum": 0, "maximum": 1}},
                   "required": ["domain", "confidence"],
                   "additionalProperties": False},
    }],
}


def _calibration_ops(calibrator):
    """The RLCD loop's hands (jarvis/calibration.py): record a
    decision with its confidence, resolve it when the outcome
    lands (the log score is computed in code, never by the
    model), and ask the calibration map before acting."""
    def record_decision(domain: str, claim: str,
                        confidence: float):
        row = calibrator.record(domain, claim, confidence)
        return {"ok": True, "decision_id": row["id"],
                "note": "nateeja aane par resolve_decision se "
                        "band karna — score khud lagega"}

    def resolve_decision(decision_id: str, right: bool):
        row = calibrator.resolve(decision_id, bool(right))
        if row is None:
            return {"ok": False,
                    "error": "woh decision mili nahi ya pehle se "
                             "resolved hai, sir"}
        return {"ok": True, "decision_id": row["id"],
                "score": row["score"]}

    def check_confidence(domain: str, confidence: float):
        return {"ok": True,
                **calibrator.policy(domain, confidence)}

    return {"record_decision": record_decision,
            "resolve_decision": resolve_decision,
            "check_confidence": check_confidence}


CANDIDATES_MANIFEST = {
    "name": "core.candidates",
    "version": "1.0.0",
    "tools": [{
        "name": "propose_rule",
        "op": "propose_rule",
        "permission": "memory.write",
        "schema": {"type": "object",
                   "properties": {
                       "text": {"type": "string", "minLength": 3},
                       "why": {"type": "string"}},
                   "required": ["text"],
                   "additionalProperties": False},
    }],
}


def _candidate_ops(data_dir):
    """propose_rule: a lasting rule JARVIS suggests from what
    it has learned — a PROPOSAL only (jarvis/candidates.py).
    The owner accepts with /rules accept; nothing a model
    proposes ever imposes itself."""
    def propose_rule(text: str, why: str = ""):
        from . import candidates
        try:
            row = candidates.propose(data_dir, text, why)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "candidate_id": row["id"],
                "note": "ye sirf ek salah hai, sir — lagegi "
                        "tabhi jab aap /rules accept karoge"}

    return {"propose_rule": propose_rule}


TRACKER_MANIFEST = {
    "name": "core.tracker",
    "version": "1.0.0",
    "tools": [{
        "name": "track_add",
        "op": "track_add",
        "permission": "memory.write",
        "schema": {"type": "object",
                   "properties": {
                       "table": {"type": "string", "minLength": 1},
                       "record": {"type": "object"}},
                   "required": ["table", "record"],
                   "additionalProperties": False},
    }, {
        "name": "track_list",
        "op": "track_list",
        "permission": "memory.read",
        "schema": {"type": "object",
                   "properties": {
                       "table": {"type": "string", "minLength": 1},
                       "status": {"type": "string"},
                       "limit": {"type": "integer"}},
                   "required": ["table"],
                   "additionalProperties": False},
    }, {
        "name": "track_update",
        "op": "track_update",
        "permission": "memory.write",
        "schema": {"type": "object",
                   "properties": {
                       "table": {"type": "string", "minLength": 1},
                       "row_id": {"type": "string", "minLength": 1},
                       "status": {"type": "string", "minLength": 1}},
                   "required": ["table", "row_id", "status"],
                   "additionalProperties": False},
    }, {
        "name": "followups_due",
        "op": "followups_due",
        "permission": "memory.read",
        "schema": {"type": "object", "properties": {},
                   "additionalProperties": False},
    }],
}


def _tracker_ops(data_dir):
    """The tracker tools (jarvis/tracker.py): named tables of
    records a long task keeps between sessions — leads today,
    anything tomorrow."""
    from . import tracker as _tracker

    def track_add(table: str, record):
        return _tracker.add(data_dir, table,
                            record if isinstance(record, dict)
                            else {})

    def track_list(table: str, status: str = "", limit: int = 20):
        return {"ok": True, "table": str(table),
                "rows": _tracker.list_rows(data_dir, table, status,
                                           limit)}

    def track_update(table: str, row_id: str, status: str):
        row = _tracker.set_status(data_dir, table, row_id, status)
        if row is None:
            return {"ok": False,
                    "error": f"'{table}' me id {row_id} nahi mili, "
                             "sir"}
        return {"ok": True, "row": row}

    def followups_due():
        return {"ok": True,
                "due": _tracker.due_followups(data_dir)}

    return {"track_add": track_add, "track_list": track_list,
            "track_update": track_update,
            "followups_due": followups_due}


SCHEDULER_MANIFEST = {
    "name": "core.scheduler",
    "version": "1.0.0",
    "tools": [{
        "name": "set_routine",
        "op": "set_routine",
        "permission": "tasks.delegate",
        "schema": {"type": "object",
                   "properties": {
                       "name": {"type": "string", "minLength": 1},
                       "steps": {"type": "array"},
                       "schedule_type": {"type": "string"},
                       "at": {"type": "string"},
                       "hours": {"type": "number"},
                       "local": {"type": "boolean"}},
                   "required": ["name", "steps"],
                   "additionalProperties": False},
    }, {
        "name": "set_goal",
        "op": "set_goal",
        "permission": "tasks.delegate",
        "schema": {"type": "object",
                   "properties": {
                       "name": {"type": "string", "minLength": 1},
                       "prompt": {"type": "string", "minLength": 1},
                       "schedule_type": {"type": "string"},
                       "at": {"type": "string"},
                       "hours": {"type": "number"},
                       "local": {"type": "boolean"}},
                   "required": ["name", "prompt"],
                   "additionalProperties": False},
    }],
}


def _scheduler_ops(data_dir):
    """set_routine / set_goal: named work + a schedule, stored.
    The scheduler thread (start_scheduler) fires what falls due
    through the delegated runner and notifies the owner."""
    from .scheduler import add_item

    def _schedule(schedule_type, at, hours):
        if schedule_type == "interval":
            return {"type": "interval",
                    "hours": float(hours or 24)}
        if schedule_type == "once":
            try:
                return {"type": "once", "at": float(at)}
            except (TypeError, ValueError):
                return {"type": "daily", "at": "09:00"}
        return {"type": "daily", "at": str(at or "09:00")}

    def set_routine(name: str, steps, schedule_type: str = "daily",
                    at: str = "", hours: float = 0,
                    local: bool = False):
        steps = [str(s).strip() for s in (steps or [])
                 if str(s).strip()]
        if not str(name).strip() or not steps:
            return {"ok": False,
                    "error": "routine ke liye naam aur kam se kam "
                             "ek step chahiye, sir"}
        return add_item(data_dir, str(name).strip(),
                        "routines.json", steps=steps,
                        schedule=_schedule(schedule_type, at, hours),
                        enabled=True, local=bool(local))

    def set_goal(name: str, prompt: str,
                 schedule_type: str = "daily", at: str = "",
                 hours: float = 0, local: bool = False):
        if not str(name).strip() or not str(prompt).strip():
            return {"ok": False,
                    "error": "goal ke liye naam aur kaam dono "
                             "chahiye, sir"}
        return add_item(data_dir, str(name).strip(),
                        "scheduled_goals.json",
                        prompt=str(prompt).strip(),
                        schedule=_schedule(schedule_type, at, hours),
                        enabled=True, status="active",
                        local=bool(local))

    return {"set_routine": set_routine, "set_goal": set_goal}


def start_scheduler(app) -> None:
    """The scheduler thread: every minute, fire due routines and
    scheduled goals through the delegated runner, and notify the
    owner. Started by the app entry points (UI/CLI), never by
    build_app — tests and one-shot processes stay thread-free."""
    if getattr(app, "_scheduler_started", False):
        return
    app._scheduler_started = True

    def _loop():
        from .scheduler import tick
        from .triggers import tick as triggers_tick
        while True:
            time.sleep(60)
            try:
                if _is_stopped(app.data_dir):
                    continue
                runner = lambda work: _delegated_summary(  # noqa: E731
                    app.parts, work)
                tick(app.data_dir, runner=runner,
                     notify=lambda t, b: _notify(
                         app.data_dir, t, b, kind="schedule"),
                     journal=app.journal,
                     local_runner=lambda work: _local_summary(
                         app.parts, work))
                triggers_tick(
                    app.data_dir, runner,
                    notify=lambda t, b: _notify(
                        app.data_dir, t, b, kind="trigger"),
                    journal=app.journal)
                _maybe_nudge(app)
            except Exception as exc:
                from .swallow import note as _swallow
                _swallow(app.data_dir, "scheduler.loop", exc)

    threading.Thread(target=_loop, daemon=True,
                     name="jarvis-scheduler").start()


VISION_MANIFEST = {
    "name": "core.vision",
    "version": "1.0.0",
    "tools": [{
        "name": "see_screen",
        "op": "see_screen",
        "permission": "pc.control",
        "schema": {"type": "object",
                   "properties": {"question": {"type": "string"}},
                   "additionalProperties": False},
    }, {
        "name": "see_page",
        "op": "see_page",
        "permission": "browser.control",
        "schema": {"type": "object",
                   "properties": {"question": {"type": "string"}},
                   "additionalProperties": False},
    }, {
        "name": "visual_recall",
        "op": "visual_recall",
        "permission": "memory.read",
        "schema": {"type": "object",
                   "properties": {"query": {"type": "string"}},
                   "additionalProperties": False},
    }],
}


GRAPH_MANIFEST = {
    "name": "core.graph",
    "version": "1.0.0",
    "tools": [{
        "name": "graph_lookup",
        "op": "graph_lookup",
        "permission": "memory.read",
        "schema": {"type": "object",
                   "properties": {"entity": {"type": "string",
                                              "minLength": 1}},
                   "required": ["entity"],
                   "additionalProperties": False},
    }],
}


READER_MANIFEST = {
    "name": "core.reader",
    "version": "1.0.0",
    "tools": [{
        "name": "read_fields",
        "op": "read_fields",
        "permission": "net.outbound",
        "schema": {"type": "object",
                   "properties": {
                       "url": {"type": "string", "minLength": 1},
                       "fields": {"type": "array", "minItems": 1}},
                   "required": ["url", "fields"],
                   "additionalProperties": False},
    }],
}


WATCH_MANIFEST = {
    "name": "core.watchers",
    "version": "1.0.0",
    "tools": [{
        "name": "watch_url",
        "op": "watch_url",
        "permission": "net.outbound",
        "schema": {"type": "object",
                   "properties": {
                       "url": {"type": "string", "minLength": 1},
                       "name": {"type": "string"},
                       "condition": {"type": "string"}},
                   "required": ["url"],
                   "additionalProperties": False},
    }],
}


BRIEF_MANIFEST = {
    "name": "core.brief",
    "version": "1.0.0",
    "tools": [{
        "name": "daily_brief",
        "op": "daily_brief",
        "permission": "memory.read",
        "schema": {"type": "object", "properties": {},
                   "additionalProperties": False},
    }],
}


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
                   "additionalProperties": False}},
        {"name": "search_all",
         "op": "search_all",
         "permission": "net.outbound",
         "schema": {"type": "object",
                    "properties": {"query": {"type": "string",
                                             "minLength": 1}},
                    "required": ["query"],
                    "additionalProperties": False}},
    ],
}


def _task_log(data_dir, row: dict) -> None:
    try:
        with (Path(data_dir) / "tasks.jsonl").open(
                "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _delegated_summary(parts, task_text: str) -> str:
    """Run one autonomous work order on a fresh Brain over the
    shared chain/dispatch/journal/memory and return its summary.
    The one worker every background mechanism rides: delegated
    tasks, the scheduler, routines and goals."""
    from .brain import Brain
    from .rules import chat_protocol
    schemas = parts.registry.advertised_tools(parts.allowances)
    from .router import route_schemas
    subset = route_schemas(task_text, schemas)
    brain = Brain(parts.chain, subset or schemas, parts.dispatch,
                  parts.journal, parts.memory,
                  **({"protocol": chat_protocol(parts.rules_text)}
                     if getattr(parts, "rules_text", "") else {}))
    if subset:
        try:
            parts.journal.append("brain.route", {
                "shown": len(subset), "total": len(schemas),
                "where": "delegated"})
        except Exception:
            pass
    prompt = (
        "Background task from the owner (work autonomously "
        "with your tools; do not ask questions — nobody is "
        "watching live): " + task_text +
        "\nWhen finished, end with a concise summary of what "
        "was done and the outcome.")
    result = brain.respond([], prompt)
    return str(result.reply)[:900]


def _local_summary(parts, task_text: str) -> str:
    """The same worker, but on the LOCAL brain only (Ollama) —
    the job scheduler's cheap road: a routine marked `local`
    never spends a cloud call. When Ollama is down the summary
    says so honestly; there is no silent cloud fallback."""
    from .brain import Brain
    from .providers import FallbackChain, OllamaProvider
    from .rules import chat_protocol
    cfg = parts.config
    chain = FallbackChain([OllamaProvider(
        base_url=str(cfg.get("ollama", "base_url",
                             "http://localhost:11434")),
        model=str(cfg.get("ollama", "model",
                          "qwen2.5:7b-instruct-q4_K_M")),
        timeout=float(cfg.get("ollama", "timeout", 120.0)))])
    schemas = parts.registry.advertised_tools(parts.allowances)
    brain = Brain(chain, schemas, parts.dispatch,
                  parts.journal, parts.memory,
                  **({"protocol": chat_protocol(parts.rules_text)}
                     if getattr(parts, "rules_text", "") else {}))
    prompt = ("Background task from the owner (LOCAL brain "
              "only; work autonomously with your tools): "
              + task_text
              + "\nWhen finished, end with a concise summary.")
    result = brain.respond([], prompt)
    return str(result.reply)[:900]


def _run_delegated(parts, data_dir, task_id: str, task: str) -> None:
    """The Spark pattern's worker: _delegated_summary on a
    background thread; the result reaches the owner as a
    notification — the conversation that delegated it never
    blocks and never has to ask."""
    try:
        summary = _delegated_summary(parts, task)
        status = "done"
    except Exception as exc:
        summary = f"Task failed: {type(exc).__name__}: {exc}"
        status = "failed"
    _task_log(data_dir, {"id": task_id, "task": task[:500],
                         "status": status, "ts": time.time(),
                         "result": summary[:500]})
    _notify(data_dir, f"Background task {status}", summary,
            kind="task")
    try:
        parts.journal.append(f"task.delegated_{status}",
                             {"id": task_id})
    except Exception:
        pass


def _fleet_for(holder: dict, parts):
    """The app's shared Fleet (many-hands engine), built lazily
    from config fleet.workers (default 32, ceiling 300)."""
    fleet = holder.get("fleet")
    if fleet is None:
        from .fleet import Fleet
        workers = 32
        cfg = getattr(parts, "config", None)
        if cfg is not None:
            try:
                workers = int(cfg.get("fleet", "workers", 32))
            except Exception:
                workers = 32
        fleet = Fleet(workers)
        holder["fleet"] = fleet
    return fleet


def _run_batch(parts, data_dir, batch_id: str, tasks, fleet):
    """A jhund of work orders through the fleet: every job runs
    _delegated_summary quietly (NO per-job notification — the
    owner asked not to be pestered), each lands in the task
    log, and ONE summary notification closes the batch."""
    results = fleet.run_many(
        tasks, lambda t: _delegated_summary(parts, t))
    ok_count = 0
    failures = []
    for i, (task, res) in enumerate(zip(tasks, results)):
        if res.get("ok"):
            ok_count += 1
            summary = str(res.get("result", ""))[:400]
            status = "done"
        else:
            summary = str(res.get("error", ""))[:400]
            status = "failed"
            failures.append(f"{task[:60]} — {summary[:80]}")
        _task_log(data_dir, {"id": f"{batch_id}-{i}",
                             "task": task[:500], "status": status,
                             "ts": time.time(),
                             "result": summary})
    lines = [f"Batch khatm: {ok_count}/{len(tasks)} kaam safal."]
    lines += [f"✗ {f}" for f in failures[:3]]
    if len(tasks) > 1:
        lines.append(f"(fleet peak concurrency: "
                     f"{fleet.peak})")
    _notify(data_dir, "Batch report", "\n".join(lines),
            kind="batch")
    try:
        parts.journal.append("task.batch_done",
                             {"id": batch_id, "ok": ok_count,
                              "total": len(tasks)})
    except Exception:
        pass


def _delegate_tasks_op(holder: dict, data_dir: Path):
    def delegate_tasks(tasks):
        parts = holder.get("parts")
        if parts is None:
            return {"ok": False,
                    "error": "delegation is not wired yet, sir."}
        if _is_stopped(data_dir):
            return {"ok": False,
                    "error": "Band hoon, sir — pehle /resume "
                             "kahiye."}
        clean = [str(t).strip() for t in (tasks or [])
                 if str(t).strip()]
        if not clean:
            return {"ok": False, "error": "Khali jhund, sir."}
        clean = clean[:300]
        batch_id = uuid.uuid4().hex[:8]
        fleet = _fleet_for(holder, parts)
        threading.Thread(target=_run_batch,
                         args=(parts, data_dir, batch_id, clean,
                               fleet),
                         daemon=True,
                         name=f"jarvis-batch-{batch_id}").start()
        return {"ok": True, "batch_id": batch_id,
                "count": len(clean),
                "note": "Poora jhund peeche chal raha hai, sir — "
                        "sirf ant me EK report milegi."}
    return delegate_tasks


def _mission_check(parts, goal: str, log) -> dict:
    """The mission's judge: a no-tools Brain reads the goal and
    the step log and rules done / continue / stuck / owner with
    a stated confidence (which the RLCD calibrator then scores
    the mission against). Any failure defaults to continue at
    0.5 — the loop's own budget is the backstop."""
    from .brain import Brain
    steps = "\n".join(f"{s['step']}. {s['summary']}"
                      for s in log[-6:])
    prompt = (
        f"MISSION GOAL: {goal}\nAb tak ke kadam:\n{steps}\n"
        "Judge strictly: goal pura hua to done; kaam baki hai "
        "to continue; raasta band ya asambhav to stuck; faisla "
        "sirf OWNER kar sakta ho to owner.\n"
        'Reply ONLY with JSON: {"state": "done|continue|stuck|'
        'owner", "confidence": 0.0, "note": "ek chhoti line"}')
    try:
        brain = Brain(parts.chain, [], parts.dispatch,
                      parts.journal, parts.memory, audit=False)
        out = brain.respond([], prompt)
        parsed = _json_from_reply(str(out.reply))
        if isinstance(parsed, dict) and parsed.get("state") in (
                "done", "continue", "stuck", "owner"):
            conf = parsed.get("confidence", 0.5)
            try:
                conf = max(0.0, min(1.0, float(conf)))
            except (TypeError, ValueError):
                conf = 0.5
            return {"state": parsed["state"], "confidence": conf,
                    "note": str(parsed.get("note", ""))[:300]}
    except Exception:
        pass
    return {"state": "continue", "confidence": 0.5, "note": ""}


def _mission_repair(parts, goal: str, log, failure: str) -> dict:
    """The mission's doctor — the self-healing law: when a
    step fails, do not just retry blindly and do not give up.
    A fresh no-tools Brain performs the root-cause analysis:
    WHAT the error is, WHERE it comes from, WHY it really
    happened — then prescribes retry (with a concrete
    instruction for what to do differently), skip, or abort.
    Any failure of the doctor itself defaults to one honest
    retry, so a sick doctor never kills a mission either."""
    from .brain import Brain
    steps = "\n".join(f"{s['step']}. {s['summary']}"
                      for s in log[-6:])
    prompt = (
        f"MISSION GOAL: {goal}\nAb tak ke kadam:\n{steps}\n"
        f"NAKAMI: {failure}\n"
        "Tum doctor ho, judge nahi. Is nakami ki teh tak jao: "
        "error kya hai, woh kahan se aa raha hai (origin), aur "
        "uski ASAL root cause kya hai. Phir ilaaj chuno: "
        "retry (dobara koshish — instruction me saaf likho is "
        "baar kya ALAG karna hai: dusra tool, dusra raasta, "
        "theek args), skip (ye kadam chhod kar aage badhna "
        "behtar hai), abort (raasta asal me band hai).\n"
        'Reply ONLY with JSON: {"error": "...", "origin": '
        '"...", "root_cause": "...", "strategy": '
        '"retry|skip|abort", "instruction": "..."}')
    try:
        brain = Brain(parts.chain, [], parts.dispatch,
                      parts.journal, parts.memory, audit=False)
        out = brain.respond([], prompt)
        parsed = _json_from_reply(str(out.reply))
        if isinstance(parsed, dict) and parsed.get("strategy") in (
                "retry", "skip", "abort"):
            return {"error": str(parsed.get("error", ""))[:200],
                    "origin": str(parsed.get("origin", ""))[:200],
                    "root_cause": str(
                        parsed.get("root_cause", ""))[:240],
                    "strategy": parsed["strategy"],
                    "instruction": str(
                        parsed.get("instruction", ""))[:240]}
    except Exception:
        pass
    return {"error": failure[:200], "origin": "",
            "root_cause": failure[:240], "strategy": "retry",
            "instruction": "wahi kadam ek baar aur, dhyaan se — "
                           "pichhli galti dohrana mat"}


def _run_mission_app(parts, data_dir, mission_id: str,
                     goal: str, max_steps: int) -> None:
    """Autopilot, app-side: worker = the delegated brain,
    judge = _mission_check, conscience = the RLCD calibrator
    (every 'aage badho' decision is recorded with the judge's
    confidence, gated by the calibration policy, and resolved
    when the mission lands). ONE notification at the end —
    beech me koi 'yeh hua woh hua' nahi."""
    from .autopilot import run_mission
    decision_ids: list = []
    cal = getattr(parts, "calibrator", None)

    def step_fn(g, log, n):
        ctx = "; ".join(s["summary"] for s in log[-3:])
        # Manus's todo recitation: the plan is re-read at the
        # END of every step prompt, where the model's eyes
        # finish — long missions drift without it.
        todo = "\n".join(
            [f"[x] kadam {s['step']}: {s['summary'][:80]}"
             for s in log]
            + [f"[ ] kadam {n}: (ye wala — abhi karna hai)"])
        work = (f"MISSION kadam {n}. GOAL: {g}"
                + (f"\nAb tak: {ctx}" if ctx else "")
                + "\nSirf AGla ek asal kadam lo aur use pura "
                  "karo. Ant me us kadam ka saar ek-do line me "
                  "batao."
                + f"\n\nTODO:\n{todo}")
        return _delegated_summary(parts, work)

    def on_step(n, verdict, entry):
        if cal is None:
            return
        conf = verdict.get("confidence", 0.5)
        try:
            row = cal.record(
                "mission",
                f"mission '{goal[:60]}' kadam {n} ke baad aage "
                "badhna sahi hai", conf)
            decision_ids.append(
                row["id"] if isinstance(row, dict) else row)
            if cal.policy("mission", conf)["action"] == "ask" \
                    and verdict.get("state") == "continue":
                verdict["state"] = "owner"
                verdict["note"] = (
                    "calibration kehta hai is raaste par "
                    "bharosa kam hai — owner ka faisla chahiye")
        except Exception:
            pass

    try:
        outcome = run_mission(
            goal, step_fn,
            lambda g, log: _mission_check(parts, g, log),
            max_steps=max_steps,
            should_stop=lambda: _is_stopped(data_dir),
            on_step=on_step,
            repair_fn=lambda g, log, f: _mission_repair(
                parts, g, log, f))
    except Exception as exc:
        outcome = {"goal": goal, "state": "stuck",
                   "note": f"mission crash: "
                           f"{type(exc).__name__}: {exc}",
                   "steps": [], "steps_taken": 0}
    if cal is not None and decision_ids:
        try:
            if outcome["state"] == "done":
                for did in decision_ids:
                    cal.resolve(did, True)
            elif outcome["state"] == "stuck":
                for did in decision_ids:
                    cal.resolve(did, False)
        except Exception:
            pass
    _task_log(data_dir, {"id": mission_id, "kind": "mission",
                         "task": goal[:500],
                         "status": outcome["state"],
                         "ts": time.time(),
                         "result": (f"{outcome['steps_taken']} "
                                    f"kadam — "
                                    f"{outcome['note']}")[:500],
                         "steps": outcome["steps"]})
    icons = {"done": "✅", "stuck": "⛔", "owner": "🙋",
             "budget": "⏳", "stopped": "⏸"}
    repair_note = ""
    if outcome.get("repairs"):
        causes = "; ".join(
            r["root_cause"] for r in outcome["repairs"][:3])
        repair_note = (f"\nRepairs: {outcome['repairs_made']} "
                       f"(root cause: {causes})")
    _notify(data_dir, f"Mission {outcome['state']}",
            f"{icons.get(outcome['state'], '•')} Goal: "
            f"{goal[:120]}\nKadam: {outcome['steps_taken']} — "
            f"{outcome['note']}{repair_note}", kind="mission")
    try:
        parts.journal.append("mission.done",
                             {"id": mission_id,
                              "state": outcome["state"],
                              "steps": outcome["steps_taken"]})
    except Exception:
        pass


def _mission_op(holder: dict, data_dir: Path):
    def run_mission_tool(goal: str, max_steps: int = 10):
        parts = holder.get("parts")
        if parts is None:
            return {"ok": False,
                    "error": "mission engine wired nahi hai, "
                             "sir."}
        if _is_stopped(data_dir):
            return {"ok": False,
                    "error": "Band hoon, sir — pehle /resume "
                             "kahiye."}
        goal = str(goal).strip()
        if not goal:
            return {"ok": False, "error": "Khali goal, sir."}
        mission_id = uuid.uuid4().hex[:8]
        _task_log(data_dir, {"id": mission_id, "kind": "mission",
                             "task": goal[:500],
                             "status": "running",
                             "ts": time.time()})
        threading.Thread(target=_run_mission_app,
                         args=(parts, data_dir, mission_id, goal,
                               int(max_steps)),
                         daemon=True,
                         name=f"jarvis-mission-{mission_id}").start()
        return {"ok": True, "mission_id": mission_id,
                "note": "Mission chal raha hai, sir — JARVIS "
                        "khud kadam-dar-kadam goal tak jayega. "
                        "Beech me tang nahi karunga; sirf ant me "
                        "ya asal rukawat par khabar dunga."}
    return run_mission_tool


def _plan_for_build(parts, goal: str, workspace: str):
    """The planner brain: Antigravity's plan schema as JSON —
    changes tagged [NEW]/[MODIFY]/[DELETE], steps, and a
    Verification Plan the builder commits to BEFORE building.
    One self-repair round: a plan the schema refuses is sent
    back with its problems named."""
    from .brain import Brain
    from .builds import validate_plan
    prompt = (
        f"BUILD GOAL: {goal}\n"
        f"WORKSPACE: {workspace or '(build ki apni folder)'}\n"
        "Tum planner ho. Is kaam ka implementation plan banao "
        "— abhi code NAHI likhna, sirf plan.\n"
        'Reply ONLY with JSON: {"review_notes": ["khatre ya '
        'faisle jo owner ko dikhne chahiye"], "changes": '
        '[{"path": "relative/path", "action": '
        '"NEW|MODIFY|DELETE", "what": "kya hoga"}], "steps": '
        '["kadam 1", "..."], "verification": [{"kind": '
        '"command|check", "detail": "command items me woh '
        'shell command jo safalta saabit kare; check items me '
        'woh jaanch jo ek parakhne wala kare"}]}\n'
        "Verification plan lazmi hai — safalta KAISE saabit "
        "hogi, pehle se tay karo.")
    problems = ["plan JSON hi nahi mila"]
    for _attempt in range(2):
        try:
            brain = Brain(parts.chain, [], parts.dispatch,
                          parts.journal, parts.memory,
                          audit=False)
            out = brain.respond([], prompt)
            parsed = _json_from_reply(str(out.reply))
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            plan = {"goal": goal,
                    "review_notes":
                        parsed.get("review_notes") or [],
                    "changes": parsed.get("changes") or [],
                    "steps": parsed.get("steps") or [],
                    "verification":
                        parsed.get("verification") or []}
            problems = validate_plan(plan)
            if not problems:
                return plan, []
            prompt += ("\nPichhle plan me ye khamiyan theen, "
                       "sudhaar kar do: " + "; ".join(problems))
    return None, problems


def _build_check(parts, goal: str, item: dict, context: str):
    """The critic for a verification 'check' item: a fresh
    no-tools Brain judges ONE declared check against the
    real build context (steps + diff). Fail-closed: a check
    that cannot be judged counts as not passed, with the
    reason named."""
    from .brain import Brain
    prompt = (
        f"BUILD GOAL: {goal}\nJAANCH: {item.get('detail')}\n"
        f"ASAL HAAL:\n{context}\n"
        "Sirf upar diye asal haal ki bina par faisla do — "
        "andaza nahi.\n"
        'Reply ONLY with JSON: {"ok": true|false, "note": '
        '"ek line saboot ke saath"}')
    try:
        brain = Brain(parts.chain, [], parts.dispatch,
                      parts.journal, parts.memory, audit=False)
        out = brain.respond([], prompt)
        parsed = _json_from_reply(str(out.reply))
        if isinstance(parsed, dict) and "ok" in parsed:
            return {"ok": bool(parsed["ok"]),
                    "note": str(parsed.get("note", ""))[:240]}
    except Exception:
        pass
    return {"ok": False, "note": "jaanch chal nahi payi"}


def _run_build_app(parts, data_dir, build_id: str) -> None:
    """The build executor: steps through the gated dispatch,
    the live checklist on disk, the plan's Verification Plan
    actually RUN, the plan bound to the real tree diff, and
    ONE walkthrough at the end — batched, Antigravity tarz,
    no drip-feed."""
    from .builds import (BuildStore, binding_report,
                         computed_confidence, diff_trees,
                         snapshot_tree)
    store = BuildStore(data_dir)
    state = store.load(build_id)
    if not state:
        return
    plan = state["plan"]
    workspace = Path(state["workspace"]) if state.get(
        "workspace") else store._dir(build_id) / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state["status"] = "executing"
    store.save(state)
    baseline = snapshot_tree(workspace)
    summaries = []
    changes_txt = "; ".join(
        f"[{c.get('action')}] {c.get('path')}"
        for c in plan.get("changes") or [])
    for i, step in enumerate(plan.get("steps") or []):
        if _is_stopped(data_dir):
            state["step_states"][i] = "failed"
            state["note"] = "owner ne rok diya"
            break
        state["step_states"][i] = "running"
        store.save(state)
        work = (f"BUILD TASK. GOAL: {plan['goal']}\n"
                f"WORKSPACE: {workspace}\n"
                f"Plan ke badlav: {changes_txt}\n"
                f"Sirf ye kadam pura karo: {step}\n"
                "Files isi workspace ke andar banao/badlo "
                "(create_project, run_code, file tools). Ant "
                "me ek-do line me batao kya hua.")
        try:
            summary = _delegated_summary(parts, work)
            state["step_states"][i] = "done"
        except Exception as exc:
            summary = (f"kadam crash: {type(exc).__name__}: "
                       f"{exc}")
            state["step_states"][i] = "failed"
        summaries.append(str(summary)[:300])
        store.save(state)
    diff = diff_trees(baseline, snapshot_tree(workspace))
    binding = binding_report(plan, diff)
    state["status"] = "verifying"
    store.save(state)
    verify_results = []
    for item in plan.get("verification") or []:
        if item.get("kind") == "command":
            code = (
                "import subprocess, json\n"
                f"_p = subprocess.run({item['detail']!r}, "
                "shell=True, capture_output=True, text=True, "
                "timeout=110)\n"
                "print(json.dumps({'exit': _p.returncode, "
                "'out': (_p.stdout + _p.stderr)[-800:]}))")
            evidence = ""
            ok = False
            try:
                res = parts.dispatch("run_code", {
                    "code": code, "timeout": 120,
                    "cwd": str(workspace)})
                raw = str(res.get("stdout") or "")
                for line in reversed(raw.splitlines()):
                    try:
                        got = json.loads(line)
                        ok = got.get("exit") == 0
                        evidence = str(got.get("out", ""))[:300]
                        break
                    except (ValueError, AttributeError):
                        continue
            except Exception as exc:
                evidence = f"{type(exc).__name__}: {exc}"
            verify_results.append(
                {"kind": "command", "detail": item["detail"],
                 "ok": ok, "evidence": evidence})
        else:
            context = ("Kadam: " + " | ".join(summaries)
                       + "\nBadlav: created="
                       + ", ".join(diff["created"])
                       + "; modified="
                       + ", ".join(diff["modified"])
                       + "; deleted="
                       + ", ".join(diff["deleted"]))
            got = _build_check(parts, plan["goal"], item,
                                context)
            verify_results.append(
                {"kind": "check", "detail": item["detail"],
                 "ok": got["ok"], "evidence": got["note"]})
    confidence, justification = computed_confidence(
        verify_results, binding)
    all_ok = all(r["ok"] for r in verify_results) \
        and not binding["missing"]
    state["status"] = "done" if all_ok else "failed"
    state["confidence"] = confidence
    state["finished"] = time.time()
    if not all_ok:
        bits = []
        if binding["missing"]:
            bits.append("plan ke badlav adhoore: "
                        + ", ".join(binding["missing"]))
        bad = [r["detail"] for r in verify_results
               if not r["ok"]]
        if bad:
            bits.append("verification fail: " + "; ".join(bad))
        state["note"] = "; ".join(bits)[:300]
    lines = [f"# Walkthrough — {plan['goal']}", "",
             f"**Confidence: {confidence}** — {justification}",
             "", "## Kya hua (kadam-dar-kadam)"]
    lines += [f"{i + 1}. [{state['step_states'][i]}] "
              f"{plan['steps'][i]} — {summaries[i]}"
              for i in range(len(summaries))]
    lines += ["", "## Plan bandish (plan vs asal)",
              f"- Hue: {', '.join(binding['done']) or '—'}",
              f"- NAHI hue: "
              f"{', '.join(binding['missing']) or '—'}",
              f"- Plan se bahar ke badlav: "
              f"{', '.join(binding['extras']) or '—'}",
              "", "## Verification (plan ka apna waada)"]
    lines += [f"- [{'x' if r['ok'] else '!'}] ({r['kind']}) "
              f"{r['detail']} — {r['evidence'][:160]}"
              for r in verify_results]
    if state["note"]:
        lines += ["", f"## Note\n{state['note']}"]
    store.write_walkthrough(build_id, "\n".join(lines) + "\n")
    store.save(state)
    icon = "✅" if state["status"] == "done" else "⛔"
    _notify(data_dir, f"Build {state['status']}",
            f"{icon} Goal: {plan['goal'][:110]}\n"
            f"Confidence: {confidence} — {justification}\n"
            f"Walkthrough: data/builds/{build_id}/"
            "walkthrough.md", kind="build")
    try:
        parts.journal.append("build.done", {
            "id": build_id, "status": state["status"],
            "confidence": confidence})
    except Exception:
        pass


def _builds_op(holder: dict, data_dir: Path):
    def build_task(goal: str, mode: str = "planning",
                   workspace: str = ""):
        parts = holder.get("parts")
        if parts is None:
            return {"ok": False,
                    "error": "build engine wired nahi hai, sir."}
        if _is_stopped(data_dir):
            return {"ok": False,
                    "error": "Band hoon, sir — pehle /resume "
                             "kahiye."}
        goal = str(goal).strip()
        if not goal:
            return {"ok": False, "error": "Khali goal, sir."}
        plan, problems = _plan_for_build(
            parts, goal, str(workspace).strip())
        if plan is None:
            return {"ok": False,
                    "error": "plan nahi ban paya — "
                             + "; ".join(problems)}
        from .builds import BuildStore
        build_id = uuid.uuid4().hex[:8]
        store = BuildStore(data_dir)
        state = store.create(
            build_id, goal, plan,
            mode if mode in ("planning", "fast") else "planning",
            str(workspace).strip())
        changes = [f"[{c.get('action')}] {c.get('path')}"
                   for c in plan["changes"]]
        if state["status"] == "awaiting_proceed":
            _notify(data_dir, "Build plan taiyaar",
                    f"Goal: {goal[:110]}\nBadlav: "
                    f"{len(changes)} | Kadam: "
                    f"{len(plan['steps'])}\nProceed karne ke "
                    f"liye: /proceed {build_id}",
                    kind="build")
            return {"ok": True, "build_id": build_id,
                    "status": "awaiting_proceed",
                    "changes": changes,
                    "note": "Plan ban gaya hai, sir — "
                            "implementation_plan.md me pura "
                            "plan hai. Owner ke Proceed ke "
                            "baad hi kaam shuru hoga."}
        threading.Thread(target=_run_build_app,
                         args=(parts, data_dir, build_id),
                         daemon=True,
                         name=f"jarvis-build-{build_id}").start()
        return {"ok": True, "build_id": build_id,
                "status": "executing",
                "note": "Fast mode — plan ke saath build chal "
                        "raha hai, sir. Ant me walkthrough "
                        "milega."}
    return build_task


def _delegate_op(holder: dict, data_dir: Path):
    """The delegate_task tool op. `holder` receives the AppParts
    after build_app assembles them (the op is registered before
    the dispatch it must share exists)."""
    def delegate_task(task: str):
        parts = holder.get("parts")
        if parts is None:
            return {"ok": False,
                    "error": "delegation is not wired yet, sir."}
        if _is_stopped(data_dir):
            return {"ok": False,
                    "error": "Band hoon, sir — pehle /resume "
                             "kahiye."}
        task = str(task).strip()
        if not task:
            return {"ok": False, "error": "Khali kaam, sir."}
        task_id = uuid.uuid4().hex[:8]
        _task_log(data_dir, {"id": task_id, "task": task[:500],
                             "status": "running", "ts": time.time()})
        threading.Thread(target=_run_delegated,
                         args=(parts, data_dir, task_id, task),
                         daemon=True,
                         name=f"jarvis-task-{task_id}").start()
        return {"ok": True, "task_id": task_id,
                "note": "Peeche chal raha hai, sir — ho jane par "
                        "notification milega."}
    return delegate_task


def _insight_op(user_model):
    """The note_insight tool op: an observation about the owner
    enters the dialectic user model (jarvis/user_model.py) —
    confidence moves, disputes become questions."""
    def note_insight(claim: str, dimension: str = "general",
                     evidence: str = "", relation: str = "new"):
        insight = user_model.observe(
            dimension or "general", claim, evidence,
            relation=relation if relation in (
                "new", "support", "contradict") else "new")
        return {"ok": True, "insight_id": insight["id"],
                "confidence": insight["confidence"],
                "status": insight["status"]}
    return note_insight


def _vision_ops(secrets, data_dir=None):
    """The vision tools (jarvis/vision.py): capture in code,
    ONE Gemini vision call, and the answer returns labelled an
    OBSERVATION — vision output never becomes a memory fact on
    its own (the vision_gate discipline). Every look also files
    a line in the visual memory (words only, never the image),
    so 'kal screen pe kya tha' has an answer."""
    def _key() -> str:
        try:
            keys = _load_keys_module().get_keys(secrets, "gemini")
            return str(keys[0]) if keys else ""
        except Exception:
            return ""

    def _logged(kind, question, result):
        if data_dir is not None and result.get("ok"):
            from .vision import log_visual
            log_visual(data_dir, kind, question,
                       str(result.get("observation", "")))
        return result

    def see_screen(question: str = ""):
        import base64 as _b64
        from .vision import analyze_image, capture_desktop_png
        png, err = capture_desktop_png()
        if png is None:
            return {"ok": False, "error": err}
        return _logged("screen", question,
                       analyze_image(_key(), png, question))

    def see_page(question: str = ""):
        import base64 as _b64
        from .browser_cdp import op_browser_screenshot
        from .vision import analyze_image
        shot = op_browser_screenshot()
        if shot.get("error"):
            return {"ok": False, "error": shot["error"]}
        try:
            png = _b64.b64decode(shot["png_b64"])
        except Exception:
            return {"ok": False,
                    "error": "page ki tasveer kharab aayi, sir"}
        return _logged("page", question,
                       analyze_image(_key(), png, question))

    def visual_recall(query: str = ""):
        if data_dir is None:
            return {"ok": False,
                    "error": "visual memory band hai, sir"}
        from .vision import recall_visual
        return {"ok": True, "looks": recall_visual(data_dir, query)}

    return {"see_screen": see_screen, "see_page": see_page,
            "visual_recall": visual_recall}


def _graph_op(memory):
    """The graph_lookup tool op: one hop through the knowledge
    graph — the edges around an entity plus the current facts
    behind them."""
    def graph_lookup(entity: str):
        current = memory.facts.current_facts()
        current_ids = {f["id"] for f in current}
        found = memory.graph.about(str(entity), current_ids)
        by_id = {f["id"]: f["claim"] for f in current}
        facts = []
        for edge in found["edges"]:
            claim = by_id.get(edge["fact_id"])
            if claim and claim not in facts:
                facts.append(claim)
        return {"ok": True, "entity": entity,
                "edges": [{"subject": e["subject"],
                           "predicate": e["predicate"],
                           "object": e["object"]}
                          for e in found["edges"]],
                "neighbours": found["neighbours"],
                "facts": facts[:6]}
    return graph_lookup


def _reader_op(chain_getter, spotlight_key: bytes = b""):
    """The read_fields tool op: fetch the page in code, extract
    with ONE tool-less model call, validate every value in code
    (jarvis/reader.py). The page never holds the pen. The chain is
    fetched lazily — at registration time it is still the raw
    provider chain; the goodput adapter replaces it later in
    build_app, and the getter sees the final value."""
    def read_fields(url: str, fields: list):
        from . import builtin_skills as bs
        from .reader import read_fields as _read
        fetched = bs.op_web_fetch(str(url))
        if fetched.get("error"):
            return {"ok": False, "error": fetched["error"]}
        document = str(fetched.get("content")
                       or fetched.get("text") or "")
        if not document:
            return {"ok": False,
                    "error": "the page gave no readable text, sir."}
        return _read(chain_getter().complete, document, fields,
                     spotlight_key=spotlight_key or None)
    return read_fields


def _watch_op(data_dir: Path):
    """The watch_url tool op (jarvis/watchers.py): create a watcher
    with its baseline snapshot."""
    def watch_url(url: str, name: str = "", condition: str = ""):
        from .watchers import add_watcher
        result = add_watcher(data_dir, url, name=name,
                             condition=condition or "noteworthy")
        if result.get("ok"):
            result["note"] = ("Watcher ban gaya, sir — page badlega "
                               "to khabar dunga.")
        return result
    return watch_url


def _brief_op(data_dir: Path, memory):
    """The daily_brief tool op: the assembled brief (jarvis/brief.py)
    over the app's own reminders, notifications and current facts."""
    def daily_brief():
        from .brief import build_brief
        brief = build_brief(
            data_dir, facts=memory.facts.current_facts())
        return {"ok": True, "brief": brief["text"],
                "top_of_mind": brief["top_of_mind"],
                "fyi": brief["fyi"],
                "looking_ahead": brief["looking_ahead"]}
    return daily_brief


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
        # Upgraded engine (the original was serial): every
        # angle searches AT ONCE and every page is read AT
        # ONCE — same tool, same contract, combined power.
        from .fanout import run_parallel
        angle_jobs = {f"angle{i}": (lambda q=q:
                                    bs.op_web_search(q, limit=5))
                      for i, q in enumerate(angles)}
        oks, fails = run_parallel(angle_jobs, timeout=45)
        seen, sources = set(), []
        search_errors = [fails[k] for k in sorted(fails)]
        for i in range(len(angles)):
            for hit in oks.get(f"angle{i}", {}).get("results", []):
                if hit["url"] not in seen:
                    seen.add(hit["url"])
                    sources.append(hit)
        if not sources and search_errors:
            return {"error": "deep_research failed: search hi nahi "
                             f"ho paya — {search_errors[0]}",
                    "sources_found": 0, "fetch_failed": 0}
        page_cap = 4 if int(depth) <= 1 else 8
        chosen = sources[:page_cap]
        fetch_jobs = {src["url"]: (lambda u=src["url"]:
                                   bs.op_web_fetch(u, max_chars=6000))
                      for src in chosen}
        pages, fetch_fails = run_parallel(fetch_jobs, timeout=45)
        read, failed = [], len(fetch_fails)
        for src in chosen:
            page = pages.get(src["url"])
            if not page:
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


def _search_all_op():
    """The fan-out tool: EVERY search route fires at once —
    web search, GitHub, Wikipedia, and the real browser's own
    view — and the findings merge into one digest. A route
    that fails is named in routes_failed and never sinks the
    others (ye nahi to woh — at full scale, all at once)."""
    def search_all(query: str):
        from . import builtin_skills as bs
        from .fanout import merge_hits, run_parallel
        q = str(query).strip()

        def github():
            data = bs.op_github_search(q, limit=5)
            if data.get("error"):
                return data
            return {"results": [{
                "title": r.get("name", ""), "url": r.get("url", ""),
                "snippet": ((r.get("description") or "")
                            + (f" (stars: {r['stars']})"
                               if r.get("stars") else ""))}
                for r in data.get("repos", [])]}

        def wiki():
            data = bs.op_wikipedia(q)
            if data.get("error") or data.get("ok") is False:
                return {"error": data.get(
                    "error", "wikipedia par kuch nahi mila")}
            return {"results": [{
                "title": data.get("title", ""), "url": data.get("url", ""),
                "snippet": (data.get("extract") or "")[:300]}]}

        def browser():
            page = bs.op_search_browser(q)
            if page.get("error"):
                return page
            return {"excerpt": str(page.get("text")
                                   or page.get("content")
                                   or "")[:1500]}

        oks, failures = run_parallel({
            "web_search": lambda: bs.op_web_search(q, limit=6),
            "github": github,
            "wikipedia": wiki,
            "real_browser": browser,
        }, timeout=30)
        route_hits = [(name, oks[name].get("results", []))
                      for name in ("web_search", "github", "wikipedia")
                      if name in oks]
        out = {"ok": False, "query": q,
               "routes_used": sorted(oks),
               "routes_failed": [{"route": n, "error": e}
                                 for n, e in sorted(failures.items())],
               "results": merge_hits(route_hits, cap=12)}
        if "real_browser" in oks and oks["real_browser"].get("excerpt"):
            out["browser_view"] = oks["real_browser"]["excerpt"]
        out["ok"] = bool(out["results"] or out.get("browser_view"))
        if not out["ok"]:
            out["error"] = ("search_all: saare raaste fail — "
                            + "; ".join(f"{n}: {e}" for n, e in
                                        sorted(failures.items())))[:300]
        return out

    return search_all


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


def _is_stopped(data_dir) -> bool:
    """The Stop-Everything flag (Sentient's kill switch): a
    persisted, model-free halt. When set, turns refuse, voice will
    not start, and background cycles stand down — across restarts,
    until the owner resumes. Commands still work: /resume is how
    the owner comes back, so it must never be gated by the brain."""
    try:
        meta = json.loads((Path(data_dir) / "meta.json").read_text(
            encoding="utf-8"))
        return bool(meta.get("stopped"))
    except Exception:
        return False


def _set_stopped(data_dir, stopped: bool) -> None:
    path = Path(data_dir) / "meta.json"
    meta = {}
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        meta = {}
    meta["stopped"] = bool(stopped)
    try:
        path.write_text(json.dumps(meta, ensure_ascii=False),
                        encoding="utf-8")
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


def _config_json(data_dir) -> dict:
    path = Path(data_dir) / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_config_json(data_dir, cfg: dict) -> None:
    (Path(data_dir) / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=1),
        encoding="utf-8")


def provider_settings(secrets, data_dir) -> dict:
    """What the Settings page shows: preset providers with their
    filed-key counts, and the owner's custom providers (name,
    base URL, model — never a key value)."""
    keys_mod = _load_keys_module()
    filed = {}
    presets = []
    if keys_mod is not None:
        try:
            filed = keys_mod.list_keys(secrets)
        except Exception:
            filed = {}
        for name in sorted(getattr(keys_mod, "PRESETS", {}) or {}):
            presets.append({"name": name,
                            "keys": int(filed.get(name, 0))})
    custom = []
    for entry in _config_json(data_dir).get("providers") or []:
        if not isinstance(entry, dict):
            continue
        secret = str(entry.get("secret")
                     or entry.get("name") or "")
        has_key = False
        try:
            has_key = bool(secrets.get(secret))
        except Exception:
            has_key = False
        custom.append({"name": str(entry.get("name", "")),
                       "base_url": str(entry.get("base_url", "")),
                       "model": str(entry.get("model", "")),
                       "has_key": has_key})
    return {"presets": presets, "custom": custom}


def set_provider(secrets, data_dir, name: str, key: str,
                 base_url: str = "", model: str = "") -> dict:
    """The Settings page's OK button. Two shapes:
    - a PRESET name (gemini, openrouter, ...) -> the key files
      through jarvis.keys, exactly like /key;
    - a NEW name + base URL + model -> a custom provider: the key
      goes to the secret store under its own name, and the
      provider joins config["providers"], which build_chain
      already reads. Either way the reply never contains the key,
      and the chain picks the provider up on the next start (the
      chain is built at startup — the same rule /key follows)."""
    name = str(name or "").strip().lower()
    key = str(key or "").strip()
    if not name or not key:
        return {"ok": False,
                "error": "naam aur key dono chahiye, sir"}
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", name):
        return {"ok": False,
                "error": "naam sirf chhote akshar, number, - ya _ "
                         "ho sakta hai, sir"}
    keys_mod = _load_keys_module()
    if keys_mod is None:
        return {"ok": False, "error": "keys module nahi mila, sir"}
    presets = getattr(keys_mod, "PRESETS", {}) or {}
    if name in presets:
        try:
            keys_mod.add_key(secrets, key, provider=name)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "kind": "preset", "name": name,
                "note": "key set ho gayi, sir — JARVIS restart ke "
                        "baad ye brain chain me kaam karegi"}
    base_url = str(base_url or "").strip()
    model = str(model or "").strip()
    if not base_url.startswith(("http://", "https://")) \
            or not model:
        return {"ok": False,
                "error": "naye provider ke liye base URL (http se "
                         "shuru) aur model ka naam chahiye, sir"}
    secret_name = f"provider-{name}"
    try:
        secrets.set(secret_name, key)
    except Exception as exc:
        return {"ok": False,
                "error": f"key rakh nahi paya: "
                         f"{type(exc).__name__}"}
    cfg = _config_json(data_dir)
    entries = [e for e in (cfg.get("providers") or [])
               if isinstance(e, dict)
               and str(e.get("name", "")).lower() != name]
    entries.append({"name": name, "base_url": base_url,
                    "model": model, "secret": secret_name})
    cfg["providers"] = entries
    try:
        _save_config_json(data_dir, cfg)
    except Exception as exc:
        return {"ok": False,
                "error": f"config likh nahi paya: "
                         f"{type(exc).__name__}"}
    return {"ok": True, "kind": "custom", "name": name,
            "note": "provider set ho gaya, sir — JARVIS restart "
                    "ke baad ye brain chain me aa jayega"}


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


def _run_guarded(ledger, session_id, name, args, execute, journal):
    """Run one destructive effect through the idempotency ledger.

    Returns (result_dict, replayed). The Dodaios audit found v4's
    ledger built but wired to nothing; this is the wiring. Semantics:
    - a COMMITTED key returns its recorded result WITHOUT
      re-executing (a duplicate effect is refused by design);
    - a FAILED key refuses a blind repeat of the identical action;
    - a key left PENDING (the app stopped mid-effect) is an UNKNOWN
      OUTCOME: never auto-retried — the owner is told it may already
      have happened and must decide;
    - exceptions from the effect propagate with the key left PENDING,
      exactly the state a real crash would leave.
    """
    payload = {"tool": name, "args": args}
    try:
        key = _idem_business_key(session_id or "session", payload)
        prior = ledger.lookup(key, payload)
    except IdempotencyConflictError as exc:
        return {"ok": False, "error": str(exc)}, False
    if prior is not None:
        if prior["status"] == "committed":
            return prior["result"], True
        if prior["status"] == "failed":
            return {"ok": False,
                    "error": f"{name} already failed once with these "
                             f"exact details ({prior.get('error')}); "
                             f"I won't repeat it blindly, sir."}, False
        return {"ok": False, "needs_decision": True,
                "error": f"{name} was started earlier but its outcome "
                         f"is unknown — the app stopped mid-way, sir. "
                         f"It may already have happened; ask me to "
                         f"run it again only if you want it "
                         f"repeated."}, False
    ledger.begin(key, payload)
    result = execute()
    try:
        stored = json.loads(json.dumps(result, default=str))
    except Exception:
        stored = {"ok": bool(_result_ok(result)),
                  "result": "result could not be recorded verbatim"}
    try:
        ledger.commit(key, stored)
    except Exception as exc:  # never lose the effect's real result
        journal.append("idem.commit_failed", {
            "tool": name,
            "error": f"{type(exc).__name__}: {exc}"})
    return result, False


def make_dispatch(gate, registry, journal, allowances,
                  pass_decision=False, refused_exc=(), ledger=None,
                  session_id="", approval_sink=None):
    """The gate-mediated dispatch every tool call flows through.

    Journals tool.call (with the gate verdict) and tool.result — the
    journal is how behaviour is OBSERVED (the eval harness scores
    these events, never the model's claims). When a ledger is given,
    destructive-class tools run through it (see _run_guarded), so the
    same side effect can never happen twice in one session.
    """
    guarded = set()
    if ledger is not None:
        try:
            guarded = {t.get("name") for t in
                       registry.advertised_tools(allowances)
                       if t.get("destructive")}
        except Exception:
            guarded = set()

    def _dispatch_once(name, args=None, origin="owner",
                       approved_decision=None):
        args = dict(args or {})
        if approved_decision is not None:
            # The owner approved this exact call on a card: the
            # fresh decision (with its one-time token) rides in;
            # the registry verifies it before anything runs.
            decision = approved_decision
        else:
            decision = gate.evaluate(name, args, origin,
                                     allowances)
        verdict = getattr(decision, "verdict", "deny")
        journal.append("tool.call", {
            "tool": name, "args": args, "gate_verdict": verdict,
            "origin": origin,
            "via_card": approved_decision is not None})
        replayed = False
        executable = verdict in ("allow", "allow_with_undo") or (
            verdict == "require_approval"
            and approved_decision is not None)
        if executable:

            def _execute():
                try:
                    if pass_decision:
                        return registry.dispatch(name, args,
                                                 decision=decision)
                    return registry.dispatch(name, args)
                except refused_exc as exc:
                    return {"ok": False, "error": str(exc)}

            if ledger is not None and name in guarded:
                result, replayed = _run_guarded(
                    ledger, session_id, name, args, _execute, journal)
            else:
                result = _execute()
        elif verdict == "require_approval":
            result = {"ok": False, "needs_approval": True,
                      "error": f"{name} needs your approval before I "
                               f"run it, sir."}
            if approval_sink is not None:
                try:
                    card = approval_sink(name, args)
                    if card:
                        result["card"] = {
                            "id": card.get("id"),
                            "summary": card.get("summary"),
                            "note": "Owner: HAAN ke liye /confirm "
                                    f"{card.get('id')}, NAHI ke "
                                    "liye /deny "
                                    f"{card.get('id')} — ya app me "
                                    "card par tap karein."}
                except Exception:
                    pass
        else:
            result = {"ok": False, "gate_denied": True,
                      "error": f"gate denied {name}: "
                               f"{getattr(decision, 'reason', '')}"}
        if not isinstance(result, dict):
            result = {"ok": True, "result": result}
        entry = {"tool": name, "ok": _result_ok(result),
                 "needs_approval": bool(result.get("needs_approval"))}
        if replayed:
            entry["idempotent_replay"] = True
        journal.append("tool.result", entry)
        return result

    def dispatch(name, args=None, origin="owner",
                 approved_decision=None, _depth=0):
        """One call, with the owner's route doctrine on top:
        the latest route runs first; on an honest failure the
        preserved older/alternate route is TRIED — never
        silently (the result carries served_by/fallback_from,
        and both attempts are journaled). Gate denials and
        approval pauses never fall back: a refusal is a
        decision, not a failure. Depth-capped, so a chain
        (scrape -> fetch -> tor) resolves in at most 3 hops."""
        result = _dispatch_once(name, args, origin,
                                approved_decision)
        if _depth >= 2 or approved_decision is not None:
            return result
        if not isinstance(result, dict) or _result_ok(result):
            return result
        if result.get("needs_approval") or result.get("gate_denied"):
            return result
        fallback = FALLBACK_ROUTES.get(name)
        if not fallback:
            return result
        try:
            alt = dispatch(fallback, args, origin=origin,
                           _depth=_depth + 1)
        except Exception:
            return result
        if isinstance(alt, dict) and _result_ok(alt):
            alt = dict(alt)
            # In a chain (scrape -> fetch -> tor) the stamp
            # names the route that ULTIMATELY served, and the
            # tool the caller originally asked for.
            alt.setdefault("served_by", fallback)
            alt["fallback_from"] = name
            alt["primary_error"] = str(
                result.get("error", ""))[:200]
            journal.append("tool.fallback", {
                "from": name, "to": fallback, "ok": True})
            return alt
        merged = dict(result)
        merged["fallback_tried"] = fallback
        merged["fallback_error"] = str(
            (alt or {}).get("error", ""))[:200] \
            if isinstance(alt, dict) else ""
        journal.append("tool.fallback", {
            "from": name, "to": fallback, "ok": False})
        return merged

    return dispatch


# The route doctrine, as data: latest -> preserved alternate.
# Same-argument pairs only (a fallback must be able to carry
# the very same request). New upgrades ADD a row here; no row
# is ever deleted — that is the owner's standing order.
FALLBACK_ROUTES = {
    "web_scrape": "web_fetch",
    "web_fetch": "web_fetch_tor",
    "web_search": "search_browser",
    "open_app": "open_app_legacy",
}


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
        except Exception as exc:
            # Persistence must never break the conversation —
            # but the failure is COUNTED now (jarvis/swallow.py),
            # never silent.
            from .swallow import note as _swallow
            _swallow(self.data_dir, "learned.persist", exc)

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

    def __init__(self, memory, learned: LearnedStore,
                 user_model=None, calibrator=None):
        self._memory = memory
        self._learned = learned
        self._user_model = user_model
        self._calibrator = calibrator

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
        lines = []
        if self._user_model is not None:
            # Dialectic insights ride along as UNDERSTANDING, in
            # their own framing — never dressed as facts.
            lines.extend(self._user_model.context_lines(query, k=2))
        if self._calibrator is not None:
            # RLCD's return path: measured miscalibration rides
            # into the brain's context, so the next confidence it
            # states starts corrected.
            try:
                lines.extend(self._calibrator.feedback_lines())
            except Exception:
                pass
        lines.extend(self._memory.excerpts(query, k=k))
        seen = set(lines)
        for hit in self._learned.search(query, k=k):
            line = f"[{hit['file']}] {hit['text']}"
            if line not in seen:
                lines.append(line)
                seen.add(line)
            if len(lines) >= 8:
                break
        return lines[:8]


class Activity:
    """What JARVIS is ACTUALLY doing right now — the UI's state word
    reads this, never a script. The owner caught the first UI cycling
    Listening/Thinking/Creating on a timer while nothing of the sort
    was happening (2026-10-21) and ordered: jo dikhe, wahi ho raha ho.
    States: standby (waiting), thinking (brain generating), researching
    (a knowledge/net tool is executing), creating (a making tool is
    executing), working (any other tool). Listening/Speaking do not
    exist until the voice feature does — no fake states, ever."""

    def __init__(self):
        self.state = "standby"
        self.tool = None

    def set(self, state, tool=None):
        self.state = state
        self.tool = tool


TOOL_STATES = {
    "web_search": "researching", "web_fetch": "researching",
    "web_scrape": "researching", "search_browser": "researching",
    "web_fetch_tor": "researching", "learn_url": "researching",
    "deep_research": "researching", "knowledge_search": "researching",
    "search_all": "researching",
    "build_task": "creating",
    "watch_for_upgrades": "researching", "upgrade_report": "researching",
    "create_project": "creating", "run_code": "creating",
    "write_file": "creating", "learn_text": "creating",
    "propose_skill": "creating",
    "email_send": "sending", "email_draft": "writing",
    "telegram_send": "sending", "delegate_team": "creating",
    "delegate_task": "creating",
    "delegate_tasks": "creating",
    "run_mission": "creating",
    "open_app_legacy": "working",
    "create_automation": "creating",
    "list_automations": "working",
    "run_automation": "working",
    "delete_automation": "working",
    "pc_info": "working",
    "process_list": "working",
    "window_list": "working",
    "disk_usage": "working",
    "battery_status": "working",
    "clipboard_read": "working",
    "clipboard_write": "working",
    "volume_get": "working",
    "volume_set": "working",
    "process_kill": "working",
    "empty_recycle_bin": "working",
    "lock_pc": "working",
}


def state_for_tool(name) -> str:
    return TOOL_STATES.get(str(name), "working")


def _activity_dispatch(dispatch, activity):
    """Wrap the gate-mediated dispatch so the activity tells the truth
    while a tool executes, then returns to thinking (the brain is
    generating again) when it finishes."""

    def traced(name, args=None, origin="owner", **kwargs):
        activity.set(state_for_tool(name), tool=str(name))
        try:
            return dispatch(name, args, origin=origin, **kwargs)
        finally:
            activity.set("thinking")

    return traced


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
    activity: object = None
    user_model: object = None
    rules_text: str = ""
    rules_source: str = ""
    calibrator: object = None


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


def _process_gaps(learned: LearnedStore, data_dir,
                  limit: int = 2) -> int:
    """Consume queued knowledge gaps (Nova's curiosity engine):
    the oldest gaps are deep-researched; a successful study lands
    in the inbox and is auto-learned, so the question JARVIS could
    not answer today is one it can answer tomorrow. A failed
    research keeps its gap queued for the next cycle."""
    path = Path(data_dir) / "gaps.jsonl"
    if not path.exists():
        return 0
    try:
        rows = [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
    except Exception:
        return 0
    if not rows:
        return 0
    research = _research_op(learned)
    done, remaining = 0, []
    for idx, gap in enumerate(rows):
        if idx < limit:
            try:
                result = research(topic=str(gap.get("query", "")))
            except Exception:
                result = {"error": "research raised"}
            if "error" not in result:
                done += 1
                continue            # consumed: drop from the queue
        remaining.append(gap)
    try:
        path.write_text(
            "".join(json.dumps(g, ensure_ascii=False) + "\n"
                    for g in remaining), encoding="utf-8")
    except Exception:
        pass
    return done


def _watcher_feedback(data_dir) -> None:
    """Watcher thresholds learn from the notifications themselves:
    a watcher notification the owner READ counts as an approval
    (+1); one still unread after 48 hours counts as a dismissal
    (-1). Each notification is counted exactly once."""
    try:
        from .thresholds import LearnedThresholds
        from .watchers import load_watchers
        thresholds = LearnedThresholds(data_dir)
        by_url = {w["url"]: w["id"]
                  for w in load_watchers(data_dir)}
        if not by_url:
            return
        now = time.time()
        for row in _read_notifications(data_dir, limit=500):
            if row.get("kind") != "watcher":
                continue
            body = str(row.get("body", ""))
            watcher_id = next(
                (wid for url, wid in by_url.items() if url in body),
                None)
            note_id = str(row.get("id", ""))
            if watcher_id is None or not note_id or \
                    thresholds.is_counted(watcher_id, note_id):
                continue
            if row.get("read"):
                thresholds.feedback(watcher_id, approved=True)
                thresholds.mark_counted(watcher_id, note_id)
            elif now - float(row.get("ts", now)) > 48 * 3600:
                thresholds.feedback(watcher_id, approved=False)
                thresholds.mark_counted(watcher_id, note_id)
    except Exception:
        pass


def _maybe_auto_watch(app) -> None:
    """The standing order, running itself: when JARVIS starts (CLI
    or UI) and the last market watch is over ~20 hours old, one watch
    cycle runs quietly in the background. Never blocks startup,
    never raises."""
    try:
        if _is_stopped(app.data_dir):
            return
        from .watchers import any_watcher_due, check_watchers
        if any_watcher_due(app.data_dir):
            from .thresholds import LearnedThresholds
            _watcher_feedback(app.data_dir)

            def _judge(old: str, new: str):
                prompt = (
                    "A watched page changed.\nOLD:\n" + old +
                    "\nNEW:\n" + new +
                    "\nHow worth telling the owner is this change, "
                    "from 0.0 to 1.0? Worth a high score: an item "
                    "was added or removed, or a price, date, status "
                    "or result they watch changed. Worth a low "
                    "score: only a counter, clock, last-updated "
                    "time, ad, ordering, spacing or wording "
                    "changed. Answer with ONLY the number.")
                reply = app.chain.complete(
                    [{"role": "user", "content": prompt}], [])
                content = reply.get("content") if isinstance(
                    reply, dict) else getattr(reply, "content", "")
                match = re.search(r"\d+(?:\.\d+)?",
                                  str(content or ""))
                if not match:
                    return None
                return max(0.0, min(1.0, float(match.group(0))))

            threading.Thread(
                target=check_watchers, args=(app.data_dir,),
                kwargs={"judge": _judge,
                        "thresholds": LearnedThresholds(app.data_dir),
                        "notify": lambda t, b: _notify(
                            app.data_dir, t, b, kind="watcher")},
                daemon=True).start()
        rows = _read_upgrade_log(app.data_dir)
        last = max((r.get("ts", 0) for r in rows
                    if r.get("type") == "watch_run"), default=0)
        if time.time() - last < 20 * 3600:
            return
        learned = app.parts.learned
        if learned is None:
            return

        def _cycle():
            _watch_cycle(learned, app.data_dir)
            _process_gaps(learned, app.data_dir)
            try:
                from .dreaming import dream

                def _merge(a: str, b: str):
                    reply = app.chain.complete(
                        [{"role": "user", "content":
                          "Merge these two memory facts into ONE "
                          "fact that keeps every detail from both. "
                          "Answer with only the merged fact:\n"
                          + a + "\n" + b}], [])
                    content = reply.get("content") if isinstance(
                        reply, dict) else getattr(reply, "content",
                                                  "")
                    return str(content or "").strip() or None

                dream(app.data_dir, app.memory.facts,
                      merge_text=_merge, journal=app.journal)
            except Exception:
                pass

        threading.Thread(target=_cycle, daemon=True).start()
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
    ledger = IdempotencyLedger(journal)
    session_id = "s-" + uuid.uuid4().hex[:12]
    try:
        _idem_unknowns = ledger.unknown_outcomes()
    except Exception:
        _idem_unknowns = []
    if _idem_unknowns:
        journal.append("chat.idem_unknown",
                       {"count": len(_idem_unknowns)})
        _notify(data_dir, "An earlier action's outcome is unknown",
                f"{len(_idem_unknowns)} action(s) were interrupted "
                f"mid-way earlier and may or may not have completed. "
                f"I have not repeated them.", kind="warning")
    config = _resolve_module(modules, "config").load(data_dir)
    secrets = _resolve_module(modules, "secrets").SecretStore(data_dir)

    gate_mod = _resolve_module(modules, "gate")
    gate_key = secrets.get("gate_key")
    if not gate_key:
        gate_key = uuid.uuid4().hex
        secrets.set("gate_key", gate_key)
    spotlight_key = secrets.get("spotlight_key")
    if not spotlight_key:
        spotlight_key = uuid.uuid4().hex
        secrets.set("spotlight_key", spotlight_key)
    if gate_mod is _defaults:
        gate = gate_mod.Gate(journal, gate_key)
    else:
        gate = gate_mod.Gate(journal, str(gate_key).encode("utf-8"))

    memory = Memory(data_dir, journal=journal)
    learned = LearnedStore(data_dir, journal=journal)
    from .user_model import UserModel
    user_model = UserModel(data_dir)
    from .calibration import Calibrator
    calibrator = Calibrator(data_dir)
    memory = _LearnedMemory(memory, learned, user_model=user_model,
                            calibrator=calibrator)
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
                                   _research_op(learned),
                                   "search_all":
                                   _search_all_op()})
            registry.approve(RESEARCH_MANIFEST["name"])
        except Exception as exc:  # a sibling got there first, etc.
            journal.append("chat.research_skill_skipped",
                           {"error": f"{type(exc).__name__}: {exc}"})
    if hasattr(registry, "register") and hasattr(registry, "approve"):
        try:
            registry.register(BRIEF_MANIFEST,
                              ops={"daily_brief":
                                   _brief_op(data_dir, memory)})
            registry.approve(BRIEF_MANIFEST["name"])
        except Exception as exc:  # a sibling got there first, etc.
            journal.append("chat.brief_skill_skipped",
                           {"error": f"{type(exc).__name__}: {exc}"})
    parts_holder: dict = {}
    if hasattr(registry, "register") and hasattr(registry, "approve"):
        try:
            registry.register(DELEGATE_MANIFEST,
                              ops={"delegate_task":
                                   _delegate_op(parts_holder,
                                                data_dir),
                                   "delegate_tasks":
                                   _delegate_tasks_op(
                                       parts_holder, data_dir)})
            registry.approve(DELEGATE_MANIFEST["name"])
        except Exception as exc:  # a sibling got there first, etc.
            journal.append("chat.delegate_skill_skipped",
                           {"error": f"{type(exc).__name__}: {exc}"})
    if hasattr(registry, "register") and hasattr(registry, "approve"):
        try:
            registry.register(MISSION_MANIFEST,
                              ops={"run_mission":
                                   _mission_op(parts_holder,
                                               data_dir)})
            registry.approve(MISSION_MANIFEST["name"])
        except Exception as exc:
            journal.append("chat.mission_skill_skipped",
                           {"error": f"{type(exc).__name__}: {exc}"})
    if hasattr(registry, "register") and hasattr(registry, "approve"):
        try:
            registry.register(BUILDS_MANIFEST,
                              ops={"build_task":
                                   _builds_op(parts_holder,
                                              data_dir)})
            registry.approve(BUILDS_MANIFEST["name"])
        except Exception as exc:
            journal.append("chat.builds_skill_skipped",
                           {"error": f"{type(exc).__name__}: {exc}"})
    if hasattr(registry, "register") and hasattr(registry, "approve"):
        try:
            registry.register(WATCH_MANIFEST,
                              ops={"watch_url": _watch_op(data_dir)})
            registry.approve(WATCH_MANIFEST["name"])
        except Exception as exc:  # a sibling got there first, etc.
            journal.append("chat.watch_skill_skipped",
                           {"error": f"{type(exc).__name__}: {exc}"})
    if hasattr(registry, "register") and hasattr(registry, "approve"):
        try:
            registry.register(READER_MANIFEST,
                              ops={"read_fields": _reader_op(
                                  lambda: chain, str(spotlight_key)
                                  .encode("utf-8"))})
            registry.approve(READER_MANIFEST["name"])
        except Exception as exc:  # a sibling got there first, etc.
            journal.append("chat.reader_skill_skipped",
                           {"error": f"{type(exc).__name__}: {exc}"})
    if hasattr(registry, "register") and hasattr(registry, "approve"):
        try:
            registry.register(USER_MODEL_MANIFEST,
                              ops={"note_insight":
                                   _insight_op(user_model)})
            registry.approve(USER_MODEL_MANIFEST["name"])
        except Exception as exc:  # a sibling got there first, etc.
            journal.append("chat.user_model_skill_skipped",
                           {"error": f"{type(exc).__name__}: {exc}"})
    if hasattr(registry, "register") and hasattr(registry, "approve"):
        try:
            registry.register(GRAPH_MANIFEST,
                              ops={"graph_lookup":
                                   _graph_op(memory)})
            registry.approve(GRAPH_MANIFEST["name"])
        except Exception as exc:  # a sibling got there first, etc.
            journal.append("chat.graph_skill_skipped",
                           {"error": f"{type(exc).__name__}: {exc}"})
    try:
        memory.graph.build_from_facts(memory.facts.current_facts())
    except Exception as exc:
        journal.append("chat.graph_backfill_skipped",
                       {"error": f"{type(exc).__name__}: {exc}"})
    if hasattr(registry, "register") and hasattr(registry, "approve"):
        try:
            registry.register(VISION_MANIFEST,
                              ops=_vision_ops(secrets, data_dir))
            registry.approve(VISION_MANIFEST["name"])
        except Exception as exc:  # a sibling got there first, etc.
            journal.append("chat.vision_skill_skipped",
                           {"error": f"{type(exc).__name__}: {exc}"})
    if hasattr(registry, "register") and hasattr(registry, "approve"):
        try:
            registry.register(SCHEDULER_MANIFEST,
                              ops=_scheduler_ops(data_dir))
            registry.approve(SCHEDULER_MANIFEST["name"])
        except Exception as exc:  # a sibling got there first, etc.
            journal.append("chat.scheduler_skill_skipped",
                           {"error": f"{type(exc).__name__}: {exc}"})
    if hasattr(registry, "register") and hasattr(registry, "approve"):
        try:
            registry.register(CANDIDATES_MANIFEST,
                              ops=_candidate_ops(data_dir))
            registry.approve(CANDIDATES_MANIFEST["name"])
        except Exception as exc:  # a sibling got there first, etc.
            journal.append("chat.candidates_skill_skipped",
                           {"error": f"{type(exc).__name__}: {exc}"})
    if hasattr(registry, "register") and hasattr(registry, "approve"):
        try:
            registry.register(TRACKER_MANIFEST,
                              ops=_tracker_ops(data_dir))
            registry.approve(TRACKER_MANIFEST["name"])
        except Exception as exc:  # a sibling got there first, etc.
            journal.append("chat.tracker_skill_skipped",
                           {"error": f"{type(exc).__name__}: {exc}"})
    if hasattr(registry, "register") and hasattr(registry, "approve"):
        try:
            registry.register(CALIBRATION_MANIFEST,
                              ops=_calibration_ops(calibrator))
            registry.approve(CALIBRATION_MANIFEST["name"])
        except Exception as exc:  # a sibling got there first, etc.
            journal.append("chat.calibration_skill_skipped",
                           {"error": f"{type(exc).__name__}: {exc}"})
    if hasattr(registry, "register") and hasattr(registry, "approve"):
        try:
            registry.register(TELEGRAM_MANIFEST,
                              ops=_telegram_ops(secrets, data_dir))
            registry.approve(TELEGRAM_MANIFEST["name"])
        except Exception as exc:  # a sibling got there first, etc.
            journal.append("chat.telegram_skill_skipped",
                           {"error": f"{type(exc).__name__}: {exc}"})
    if hasattr(registry, "register") and hasattr(registry, "approve"):
        try:
            registry.register(TEAM_MANIFEST,
                              ops=_team_ops(parts_holder, data_dir))
            registry.approve(TEAM_MANIFEST["name"])
        except Exception as exc:  # a sibling got there first, etc.
            journal.append("chat.team_skill_skipped",
                           {"error": f"{type(exc).__name__}: {exc}"})
    if hasattr(registry, "register") and hasattr(registry, "approve"):
        try:
            registry.register(TRIGGERS_MANIFEST,
                              ops=_trigger_ops(data_dir))
            registry.approve(TRIGGERS_MANIFEST["name"])
        except Exception as exc:  # a sibling got there first, etc.
            journal.append("chat.triggers_skill_skipped",
                           {"error": f"{type(exc).__name__}: {exc}"})
    if hasattr(registry, "register") and hasattr(registry, "approve"):
        try:
            registry.register(FORGE_MANIFEST,
                              ops=_forge_ops(parts_holder, data_dir))
            registry.approve(FORGE_MANIFEST["name"])
        except Exception as exc:  # a sibling got there first, etc.
            journal.append("chat.forge_skill_skipped",
                           {"error": f"{type(exc).__name__}: {exc}"})
    if hasattr(registry, "register") and hasattr(registry, "approve"):
        try:
            registry.register(AUTOMATION_MANIFEST,
                              ops=_automation_ops(parts_holder,
                                                  data_dir))
            registry.approve(AUTOMATION_MANIFEST["name"])
        except Exception as exc:  # a sibling got there first, etc.
            journal.append("chat.automation_skill_skipped",
                           {"error": f"{type(exc).__name__}: {exc}"})
    if hasattr(registry, "register") and hasattr(registry, "approve"):
        try:
            registry.register(MAIL_MANIFEST,
                              ops=_mail_ops(secrets, data_dir))
            registry.approve(MAIL_MANIFEST["name"])
        except Exception as exc:  # a sibling got there first, etc.
            journal.append("chat.mail_skill_skipped",
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
    activity = Activity()
    from .confirmations import ConfirmationBook
    _card_book = ConfirmationBook(data_dir)
    dispatch = _activity_dispatch(
        make_dispatch(gate, registry, journal, allowances,
                      pass_decision=pass_decision,
                      refused_exc=refused_exc, ledger=ledger,
                      session_id=session_id,
                      approval_sink=_card_book.add),
        activity)
    tool_schemas = registry.advertised_tools(allowances)
    # The ONE rules file (the owner's standing order): the brain's
    # protocol is built from JARVIS-RULES.txt at the repo root —
    # restrictions and protocols live there, nowhere else.
    from .rules import chat_protocol, load_rules
    _rules_text, _rules_source = load_rules(
        Path(__file__).resolve().parent.parent)
    brain = _resolve_module(modules, "brain").Brain(
        chain, tool_schemas, dispatch, journal, memory,
        protocol=chat_protocol(_rules_text))
    try:
        brain.spotlight_key = str(spotlight_key).encode("utf-8")
    except Exception as exc:
        journal.append("chat.spotlight_skipped",
                       {"error": f"{type(exc).__name__}: {exc}"})

    parts = AppParts(journal=journal, config=config, secrets=secrets,
                     goodput=goodput, chain=chain, registry=registry,
                     gate=gate, memory=memory, brain=brain,
                     dispatch=dispatch, allowances=allowances,
                     learned=learned, activity=activity,
                     user_model=user_model,
                     calibrator=calibrator,
                     rules_text=_rules_text,
                     rules_source=_rules_source)
    parts_holder["parts"] = parts
    return parts


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
# Atomic app launches (the Dodaios fast-path case): a bare imperative
# naming one app should not pay a full LLM round trip. Anchored and
# narrow on purpose — anything with a conjunction, a question, or a
# target longer than three words falls through to the brain.
_OPEN_PREFIX_RE = re.compile(
    r"^(?:open|launch|start|run|kholo|khol do|chalu karo|"
    r"start karo)\s+(.+?)\s*$", re.I)
_OPEN_SUFFIX_RE = re.compile(
    r"^(.+?)\s+(?:kholo|khol do|chalu karo)\s*$", re.I)
_OPEN_FILLER_RE = re.compile(
    r"\b(please|now|sir|karo|kar do|bhai|yaar)\b", re.I)
_OPEN_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$", re.I)
_OPEN_NOT_APPS = {"timer", "stopwatch", "the timer", "a timer"}
# Hedged / admitted-ignorance replies (Nova's curiosity trigger):
# a reply that confesses uncertainty queues a knowledge gap the
# watch cycle later researches and learns — the assistant gets
# less ignorant while the owner sleeps.
_HEDGE_RE = re.compile(
    r"(i'?m not (sure|certain)|i am not (sure|certain)|"
    r"not entirely sure|i don'?t know|i do not know|"
    r"can'?t say for certain|not certain|"
    r"mujhe (pakka )?nahi pata|mujhe nahi pata|pakka nahi hai|"
    r"theek se nahi pata)", re.I)


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
        self.activity = parts.activity or Activity()
        self.user_model = parts.user_model
        self.rules_text = parts.rules_text
        self.rules_source = parts.rules_source
        self.calibrator = parts.calibrator
        self.voice_manager = None
        self.history: list = []
        self.incognito = False
        self._incognito_history: list = []
        self.last_provider = "none"

    # -- tool execution (the same gate-mediated path the brain uses) --

    def execute_tool(self, name: str, args: Optional[dict] = None,
                     origin: str = "owner") -> dict:
        return self.parts.dispatch(name, args or {}, origin=origin)

    # -- voice (Gemini Live — the Mark-LVI design) ----------------------

    def start_voice(self) -> str:
        if _is_stopped(self.data_dir):
            return ("Band hoon, sir — pehle /resume kahiye, phir "
                    "voice chalu karunga.")
        if self.voice_manager is not None \
                and self.voice_manager.active:
            return "Voice is already on, sir."
        from .voice_manager import VoiceManager
        cfg = {
            "voice_name": self.config.get("voice", "voice_name",
                                          "Charon"),
            "wake_word": bool(self.config.get("voice", "wake_word",
                                              False)),
            "proactive": bool(self.config.get("voice", "proactive",
                                              True)),
            "input_device": self.config.get("voice", "input_device"),
            "output_device": self.config.get("voice", "output_device"),
            "engine": self.config.get("voice", "engine", "live"),
            "whisper_model": self.config.get("voice", "whisper_model",
                                             "base"),
            "thinking_level": self.config.get(
                "voice", "thinking_level"),
        }
        models = self.config.get("voice", "live_models")
        if isinstance(models, list) and models:
            cfg["live_models"] = [str(m) for m in models]
        self._voice_cfg = cfg
        manager = VoiceManager(
            session_factory=self._make_voice_session,
            activity=self.activity,
            on_transcript=self._voice_transcript,
            notify=lambda text: _notify(self.data_dir, "Voice", text,
                                        kind="voice"),
            config=cfg)
        ok, message = manager.start()
        if ok:
            self.voice_manager = manager
            self.journal.append("voice.start",
                                {"voice": cfg["voice_name"]})
        return message

    def stop_voice(self) -> str:
        if self.voice_manager is None \
                or not self.voice_manager.active:
            return "Voice is not on, sir."
        self.voice_manager.stop()
        self.journal.append("voice.stop", {})
        return "Voice is off, sir."

    def voice_status(self) -> dict:
        if self.voice_manager is None:
            return {"active": False, "connected": False,
                    "speaking": False, "muted": False,
                    "awake": True, "wake_word": False,
                    "voice": "Charon"}
        return self.voice_manager.status()

    def _make_voice_session(self, events):
        """The Live session factory the VoiceManager calls: the
        owner's filed Gemini key, the same gated tools the chat
        brain advertises, and a system instruction assembled the
        Mark-LVI way (time, identity, memory, persona)."""
        cfg0 = getattr(self, "_voice_cfg", {}) or {}
        if str(cfg0.get("engine", "live")).lower() == "local":
            # The offline engine: faster-whisper hears, the chat
            # brain answers, the OS voice speaks. No key needed.
            from .local_voice import (LocalPipelineSession, SystemTTS,
                                      WhisperSTT)
            return LocalPipelineSession(
                events,
                stt=WhisperSTT(
                    model=str(cfg0.get("whisper_model", "base"))),
                tts=SystemTTS(),
                respond=self.turn,
                ack_delay=float(cfg0.get("ack_delay_s", 0.6)))
        from .prompt import PERSONA
        from .voice import LIVE_MODELS, LiveSession
        keys_mod = _load_keys_module()
        values = []
        if keys_mod is not None:
            try:
                values = keys_mod.get_keys(self.secrets, "gemini")
            except Exception:
                values = []
        if not values:
            raise RuntimeError(
                "Voice needs a Gemini key, sir — file one with /key "
                "first, then start voice.")
        cfg = getattr(self, "_voice_cfg", {}) or {}
        now = datetime.now()
        parts = [
            "[CURRENT DATE & TIME]\nRight now it is: "
            f"{now:%A, %B %d, %Y — %I:%M %p}\nUse this to calculate "
            "exact times for reminders.",
            "[IDENTITY]\nYour name is JARVIS. Always refer to "
            "yourself as JARVIS. Address the user as 'sir'.",
        ]
        try:
            facts = self.memory.facts.current_facts()
        except Exception:
            facts = []
        if facts:
            lines = []
            for fact in facts[:20]:
                text = fact.get("text") if isinstance(fact, dict) \
                    else str(fact)
                if text:
                    lines.append(f"- {text}")
            if lines:
                parts.append("[MEMORY]\n" + "\n".join(lines))
        parts.append(PERSONA)
        try:
            from .rules import voice_rules
            parts.append("[RULES — JARVIS-RULES.txt]\n"
                         + voice_rules(self.rules_text))
        except Exception:
            pass
        parts.append(
            "[SPEAKING]\nYou are speaking aloud, not writing. Keep "
            "answers short, natural and conversational. When a tool "
            "is needed, call it — do not describe calling it. "
            "Before a longer task, give a short spoken heads-up "
            "first (a few words, like \"On it, sir\") so the work "
            "never starts in silence; skip the heads-up for short "
            "factual answers. Never run more than two tool calls "
            "in a row without saying something to the user.")
        return LiveSession(
            api_key=values[0],
            dispatch=lambda name, args: self.execute_tool(name, args),
            events=events,
            system_instruction="\n\n".join(parts),
            tools=self.registry.advertised_tools(self.allowances),
            voice=cfg.get("voice_name", "Charon"),
            models=cfg.get("live_models") or LIVE_MODELS,
            proactive=cfg.get("proactive", True),
            thinking_level=cfg.get("thinking_level"),
            destructive_tools=frozenset(
                t["name"] for t in self.registry.advertised_tools(
                    self.allowances) if t.get("destructive")),
            on_session_end=self._voice_session_end)

    def _voice_transcript(self, role: str, text: str):
        """Voice turns join the same history the chat brain reads —
        one conversation, two mouths. In incognito they join only
        the throwaway session history and are journaled redacted."""
        if self.incognito:
            self._incognito_history.append(
                {"role": role, "content": text})
            self.journal.append("voice.turn",
                                {"role": role, "text": "[incognito]"})
            return
        self.history.append({"role": role, "content": text})
        self.journal.append("voice.turn",
                            {"role": role, "text": text[:500]})
        try:
            self.memory.note_turn(role, text)
        except Exception:
            pass

    def _voice_session_end(self, turns: list):
        """Mark-LVI's closing ritual: a session of >= 3 turns is
        summarised into long-term memory, so tomorrow remembers
        that the conversation happened."""
        if self.incognito:
            return            # an incognito session is never summarised
        try:
            convo = "\n".join(f"{r}: {t}" for r, t in turns[-40:])
            reply = self.chain.complete(
                [{"role": "user",
                  "content": "Summarize this voice conversation in "
                             "one or two sentences for long-term "
                             f"memory:\n{convo}"}], [])
            summary = getattr(reply, "content", "") or ""
            if summary and not getattr(reply, "error", None):
                self.memory.learn(f"Voice session: {summary.strip()}")
        except Exception:
            pass

    # -- commands ---------------------------------------------------------

    # -- Push-to-talk (config voice.push_to_talk) -------------------
    def push_to_talk(self) -> bool:
        try:
            return bool(self.config.get("voice", "push_to_talk",
                                        False))
        except Exception:
            return False

    def set_push_to_talk(self, enabled: bool) -> dict:
        try:
            self.config.set("voice", "push_to_talk", bool(enabled))
        except Exception as exc:
            return {"ok": False,
                    "error": f"setting rakh nahi paya: "
                             f"{type(exc).__name__}"}
        return {"ok": True, "push_to_talk": bool(enabled)}

    # -- Telegram: one set of methods, two doors (the Settings
    # panel is the owner's chosen door; /telegram stays as the
    # chat fallback over the very same methods) ------------------
    def telegram_status(self) -> dict:
        from .telegram import load_state
        state = load_state(self.data_dir)
        token_set = False
        try:
            token_set = bool(self.secrets.get("telegram_bot_token"))
        except Exception:
            token_set = False
        return {"token_set": token_set,
                "bound": bool(state.get("chat_id")),
                "bound_name": state.get("bound_name", ""),
                "enabled": bool(state.get("enabled", True))}

    def set_telegram_token(self, token: str) -> dict:
        token = str(token or "").strip()
        if not token:
            return {"ok": False, "error": "token khaali hai, sir"}
        try:
            self.secrets.set("telegram_bot_token", token)
        except Exception as exc:
            return {"ok": False,
                    "error": f"token rakh nahi paya: "
                             f"{type(exc).__name__}"}
        try:
            self.journal.append("telegram.token_set", {})
        except Exception:
            pass
        return {"ok": True,
                "note": "Telegram token set ho gaya, sir — wapas "
                        "kabhi nahi dikhega"}

    def telegram_pair_code(self) -> dict:
        import uuid as _uuid
        code = str(_uuid.uuid4().int % 1000000).zfill(6)
        self._telegram_pair = (code, time.time() + 600)
        return {"ok": True, "code": code,
                "note": "Phone se JARVIS bot ko yehi code bhejo — "
                        "10 minute valid hai"}

    def set_telegram_enabled(self, enabled: bool) -> dict:
        from .telegram import load_state, save_state
        state = load_state(self.data_dir)
        state["enabled"] = bool(enabled)
        save_state(self.data_dir, state)
        return {"ok": True, "enabled": bool(enabled)}

    def _telegram_command(self, text: str) -> str:
        arg = text[len("/telegram"):].strip()
        if arg.startswith("token "):
            out = self.set_telegram_token(arg[len("token "):])
            return out.get("note") or out.get("error", "?")
        if arg == "pair":
            out = self.telegram_pair_code()
            return (f"Pair code: {out['code']}\nApne phone se "
                    "JARVIS bot ko yehi code bhej do — bas 10 "
                    "minute valid hai. Jis phone se sahi code "
                    "aaya, wahi jud jayega.")
        if arg in ("off", "on"):
            self.set_telegram_enabled(arg == "on")
            return (f"Telegram {'chalu' if arg == 'on' else 'band'}"
                    " kar diya, sir.")
        status = self.telegram_status()
        return ("Telegram, sir: token "
                + ("set hai" if status["token_set"] else "nahi hai")
                + "; "
                + (f"phone juda hai ({status['bound_name']})"
                   if status["bound"] else "phone abhi juda nahi")
                + "; "
                + ("chalu hai" if status["enabled"] else "band hai")
                + ".\nAsli setting ⚙ KEYS wale panel me hai, sir — "
                  "wahi se token aur pair hota hai.")

    # -- Accounts (Settings door) ----------------------------------
    def accounts_overview(self) -> list:
        from .accounts import AccountBook
        return AccountBook(self.data_dir).overview()

    def add_account(self, channel: str, label: str,
                    address: str, secret: str = "",
                    cap: int = 0) -> dict:
        from .accounts import AccountBook
        try:
            row = AccountBook(self.data_dir).add(
                channel, label, address, cap)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if secret:
            try:
                self.secrets.set(f"account-{row['id']}",
                                 str(secret))
            except Exception as exc:
                return {"ok": False,
                        "error": f"password rakh nahi paya: "
                                 f"{type(exc).__name__}",
                        "account": row}
        try:
            self.journal.append("account.added",
                                {"id": row["id"],
                                 "channel": row["channel"]})
        except Exception:
            pass
        return {"ok": True, "account": row,
                "note": "khaata jud gaya, sir — password wapas "
                        "kabhi nahi dikhega"}

    def set_account_enabled(self, account_id: str,
                            enabled: bool) -> dict:
        from .accounts import AccountBook
        ok = AccountBook(self.data_dir).set_enabled(
            account_id, enabled)
        return {"ok": ok}

    def remove_account(self, account_id: str) -> dict:
        from .accounts import AccountBook
        return {"ok": AccountBook(self.data_dir).remove(account_id)}

    def provider_settings(self) -> dict:
        return provider_settings(self.secrets, self.data_dir)

    def set_provider(self, name: str, key: str, base_url: str = "",
                     model: str = "") -> dict:
        out = set_provider(self.secrets, self.data_dir, name, key,
                           base_url=base_url, model=model)
        try:
            self.journal.append(
                "settings.provider_set",
                {"name": name, "ok": bool(out.get("ok")),
                 "kind": out.get("kind", "")})
        except Exception:
            pass
        return out

    def _schedule_listing(self, filename: str, label: str) -> str:
        from .scheduler import load_goals, load_routines
        rows = (load_goals(self.data_dir)
                if filename == "scheduled_goals.json"
                else load_routines(self.data_dir))
        if not rows:
            return f"Koi {label} nahi hai, sir."
        lines = []
        for r in rows:
            nxt = r.get("next_run")
            when = (time.strftime("%d %b %I:%M %p",
                                  time.localtime(nxt))
                    if nxt else "—")
            if not r.get("enabled", True):
                state = "band"
            else:
                state = r.get("status") or "chalu"
            lines.append(
                f"• {r.get('name', '')} [{state}] — agla run: "
                f"{when} (ab tak {r.get('runs', 0)} runs)")
        return "\n".join(lines)

    def _routines_command(self, text: str) -> str:
        arg = text[len("/routines"):].strip()
        if arg.startswith("run "):
            from .scheduler import fire_row, load_routines
            name = arg[4:].strip().lower()
            for row in load_routines(self.data_dir):
                if name and name in str(
                        row.get("name", "")).lower():
                    threading.Thread(
                        target=lambda row=row: fire_row(
                            self.data_dir, "routines.json",
                            row["id"],
                            runner=lambda w: _delegated_summary(
                                self.parts, w),
                            notify=lambda t, b: _notify(
                                self.data_dir, t, b,
                                kind="schedule"),
                            journal=self.journal,
                            local_runner=lambda w: _local_summary(
                                self.parts, w)),
                        daemon=True,
                        name="jarvis-routine-manual").start()
                    return (f"Routine '{row.get('name')}' chala "
                            "diya, sir — report notification me "
                            "aayega.")
            return f"'{name}' naam ki routine nahi mili, sir."
        return self._schedule_listing("routines.json", "routine")

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
        if text == "/stop":
            _set_stopped(self.data_dir, True)
            self.journal.append("app.stopped", {})
            return ("Sab band kar diya, sir. Main ab koi kaam "
                    "nahi karunga — na jawab dunga, na peeche koi "
                    "cycle chalega. Dobara chalane ke liye "
                    "/resume kahiye.")
        if text == "/resume":
            _set_stopped(self.data_dir, False)
            self.journal.append("app.resumed", {})
            return "Phir se hazir hoon, sir — sab chalu hai."
        if text == "/incognito" or text.startswith("/incognito "):
            arg = text[len("/incognito"):].strip().lower()
            if arg == "off":
                self.incognito = False
                self._incognito_history = []
                return ("Incognito off, sir — ab ki baatein yaad "
                        "rakhunga.")
            if arg in ("", "on"):
                if not self.incognito:
                    self.incognito = True
                    self._incognito_history = []
                return ("Incognito on, sir — is session ki baatein "
                        "na history me rahengi, na memory me. Band "
                        "karne ke liye /incognito off.")
            return ("Samajh nahi aaya, sir — /incognito ya "
                    "/incognito off kahiye.")
        if text == "/model" or text.startswith("/model "):
            return self._model_command(text)
        if text == "/dream":
            from .dreaming import dream
            report = dream(self.data_dir, self.memory.facts,
                           journal=self.journal)
            return (f"Dreaming ho gaya, sir — merged: "
                    f"{report['merged']}, settled: "
                    f"{report['settled']}, alag rakhe gaye: "
                    f"{report['kept_apart']}.")
        if text == "/watchers":
            from .watchers import load_watchers
            rows = load_watchers(self.data_dir)
            if not rows:
                return "Koi watcher nahi hai, sir."
            return "\n".join(
                f"👁 {w.get('name', w['url'])} "
                f"[{w.get('condition', 'noteworthy')}] — {w['url']}"
                for w in rows[-10:])
        if text == "/tasks":
            path = self.data_dir / "tasks.jsonl"
            rows = []
            if path.exists():
                try:
                    rows = [json.loads(x) for x in path.read_text(
                        encoding="utf-8").splitlines() if x.strip()]
                except Exception:
                    rows = []
            latest = {}
            for row in rows:            # last status per task wins
                latest[row.get("id", "")] = row
            if not latest:
                return "Koi background task nahi hai, sir."
            return "\n".join(
                f"🧵 {r.get('id', '?')} [{r.get('status', '?')}] "
                f"{str(r.get('task', ''))[:80]}"
                for r in list(latest.values())[-8:])
        if text == "/rules candidates":
            from . import candidates as _cand
            waiting = _cand.pending(self.data_dir)
            if not waiting:
                return "Koi rule-salah baaki nahi hai, sir."
            lines = ["Rule salahen (sirf salah — lagengi tab "
                     "jab aap accept karoge), sir:"]
            for row in waiting:
                lines.append(f"• {row['id']} — {row['text']}")
            lines.append("Hukum: /rules accept <id> · "
                         "/rules drop <id>")
            return "\n".join(lines)
        if text.startswith("/rules accept ") or \
                text.startswith("/rules drop "):
            from . import candidates as _cand
            accepting = text.startswith("/rules accept ")
            cid = text.rsplit(" ", 1)[-1].strip()
            status = "accepted" if accepting else "dropped"
            row = _cand.decide(self.data_dir, cid, status)
            if row is None:
                return "Woh salah mili nahi, sir."
            if not accepting:
                return f"Salah {cid} hata di, sir."
            source = Path(str(self.rules_source))
            if not source.exists():
                return ("Salah accept ho gayi, sir, lekin "
                        "JARVIS-RULES.txt mili nahi — file "
                        "banane par ye line haath se dalni hogi: "
                        + row["text"])
            body = source.read_text(encoding="utf-8")
            line = f"- {row['text']}\n"
            marker = "## AWAAZ"
            if marker in body:
                body = body.replace(marker, line + "\n"
                                    + marker, 1)
            else:
                body = body.rstrip() + "\n" + line
            source.write_text(body, encoding="utf-8")
            return ("Rule lag gaya, sir — JARVIS-RULES.txt ke "
                    "TAREEKA hisse me jud gaya. Naya asar agle "
                    "start se hoga (rules shuru me padhe jate "
                    "hain).")
        if text == "/confirm" or text.startswith("/confirm "):
            parts_cmd = text.split(None, 1)
            return self.confirm_card(
                parts_cmd[1].strip() if len(parts_cmd) > 1 else "",
                approve=True)
        if text == "/deny" or text.startswith("/deny "):
            parts_cmd = text.split(None, 1)
            return self.confirm_card(
                parts_cmd[1].strip() if len(parts_cmd) > 1 else "",
                approve=False)
        if text == "/automations":
            from .automations import AutomationBook
            rows = AutomationBook(self.data_dir).list()
            if not rows:
                return ("Koi automation nahi bana abhi, sir — "
                        "jo kaam baar-baar hota hai, mujhe use "
                        "route bana kar rakhne ko kahiye.")
            lines = ["Saved automations, sir:"]
            for r in rows:
                lines.append(
                    f"🤖 {r['name']} — {r['title']} "
                    f"({r['steps']} kadam"
                    + (f", params: {', '.join(r['params'])}"
                       if r["params"] else "")
                    + f", {r['runs']} baar chala)")
            return "\n".join(lines)
        if text == "/cards":
            pending = self.pending_confirmations()
            if not pending:
                return "Koi pending confirmation card nahi hai, sir."
            lines = ["Pending cards, sir:"]
            for c in pending:
                lines.append(f"🪪 {c['id']} — {c['summary']} "
                             f"(/confirm {c['id']} | "
                             f"/deny {c['id']})")
            return "\n".join(lines)
        if text == "/builds":
            rows = self.list_builds()
            if not rows:
                return "Abhi tak koi build nahi hui, sir."
            return "\n".join(
                f"🔨 {r['id']} — {r['status']} — "
                f"{str(r['goal'])[:60]}"
                for r in rows)
        if text.startswith("/proceed "):
            out = self.proceed_build(
                text[len("/proceed "):].strip())
            return ("Build shuru kar di, sir — ant me "
                    "walkthrough milega."
                    if out.get("ok")
                    else f"Proceed nahi hua: {out.get('error')}")
        if text.startswith("/builddeny "):
            out = self.proceed_build(
                text[len("/builddeny "):].strip(),
                approve=False)
            return ("Build rok di gayi, sir."
                    if out.get("ok")
                    else f"Deny nahi hua: {out.get('error')}")
        if text.startswith("/buildplan "):
            return self.build_plan_text(
                text[len("/buildplan "):].strip())
        if text == "/rules":
            return ("Ye rahe mere saare rules, sir — ek hi file "
                    f"se ({self.rules_source}):\n\n"
                    + self.rules_text)
        if text == "/calibration":
            return self.calibrator.report()
        if text == "/forge":
            from .forge import list_entries
            rows = list_entries(self.data_dir)
            if not rows:
                return "Forge khaali hai, sir — koi feature " \
                       "abhi khud nahi bana."
            lines = ["Forge, sir (khud banaye features):"]
            for row in rows:
                lines.append(f"• {row['slug']} — {row['name']} "
                             f"[{row['status']}]")
            return "\n".join(lines)
        if text.startswith("/forge rollback "):
            from .forge import rollback as _forge_rollback
            slug = text[len("/forge rollback "):].strip()
            out = _forge_rollback(_app_root(self.parts),
                                  self.data_dir, slug)
            if out.get("ok"):
                return (f"Feature {slug} wapas liya gaya, sir "
                        "— purani haalat bahal. Asar agle start "
                        "se dikhega.")
            return "Rollback nahi hua, sir: " + str(
                out.get("error"))
        if text == "/triggers":
            from .triggers import TriggerBook
            rows = TriggerBook(self.data_dir).list()
            if not rows:
                return "Koi trigger nahi hai, sir."
            lines = ["Triggers, sir:"]
            for row in rows:
                lines.append(
                    f"• {row['name']} [{row['event']}] — "
                    f"{row['state']}, "
                    f"{len(row.get('fired') or {})} baar chala")
            return "\n".join(lines)
        if text == "/usage":
            report = _diagnose_op(self.data_dir, self.secrets)()
            lines = ["Usage, sir — apne records se:"]
            for name, slot in sorted(
                    report["providers"].items()):
                lines.append(f"- {name}: {slot['answered']}/"
                             f"{slot['attempts']} jawab")
            stats = self.calibrator.stats()
            if stats["resolved"]:
                lines.append(f"- Faisle: {stats['resolved']} "
                             "band hue, sahi "
                             f"{int(stats['accuracy'] * 100)}%")
            rates = self.registry.rate_stats()
            if rates:
                lines.append("- Is minute ke tool calls: "
                             + ", ".join(f"{k}×{v}" for k, v in
                                         rates.items()))
            lines.append("- Note: zyada tar providers free keys "
                         "par hain; paisa-kharcha alag se track "
                         "nahi hota, sir — ye istemal ka hisaab "
                         "hai.")
            return "\n".join(lines)
        if text == "/telegram" or text.startswith("/telegram "):
            return self._telegram_command(text)
        if text.startswith("/decided "):
            bits = text.split()
            if len(bits) == 3 and bits[2].lower() in (
                    "sahi", "right", "galat", "wrong"):
                row = self.calibrator.resolve(
                    bits[1], bits[2].lower() in ("sahi", "right"))
                if row is not None:
                    return (f"Decision {bits[1]} band ho gaya, "
                            f"sir — score {row['score']}.")
                return "Woh decision mili nahi, sir."
            return ("Tareeka: /decided <id> sahi|galat, sir — "
                    "khule faisle /calibration me dikhte hain.")
        if text == "/routines" or text.startswith("/routines "):
            return self._routines_command(text)
        if text == "/goals":
            return self._schedule_listing("scheduled_goals.json",
                                          "scheduled goal")
        if text == "/brief":
            from .brief import build_brief
            return build_brief(
                self.data_dir,
                facts=self.memory.facts.current_facts())["text"]
        if text == "/diagnose":
            # Brain-free on purpose: when every brain is down, the
            # owner still needs the diagnosis — this runs on records
            # alone and names each provider's last recorded error.
            report = _diagnose_op(self.data_dir, self.secrets)()
            lines = ["Diagnosis, sir — apne records se:"]
            lines += [f"- {f}" for f in report["findings"]]
            for name, slot in sorted(report["providers"].items()):
                line = (f"- {name}: {slot['answered']}/"
                        f"{slot['attempts']} jawab")
                if slot.get("last_error"):
                    line += f"; aakhri galti: {slot['last_error']}"
                lines.append(line)
            for item in report["checks_failed"][:3]:
                lines.append(f"- Check fail: {item['check']} — "
                             f"{item['detail']}")
            # The monitor tier: live rate-limit traffic and the
            # swallowed-failure ledger — the numbers that used to
            # be invisible (Wave 4.6/5).
            try:
                rates = self.registry.rate_stats()
                if rates:
                    top = ", ".join(
                        f"{k}×{v}" for k, v in
                        sorted(rates.items(),
                               key=lambda kv: -kv[1])[:4])
                    lines.append(f"- Rate (is minute): {top}")
                from .swallow import count as _sw_count
                from .swallow import recent as _sw_recent
                total = _sw_count(self.data_dir)
                if total:
                    lines.append(
                        f"- Khamosh fail kul: {total} "
                        "(jo daba diye gaye, ginati ke saath)")
                    for row in _sw_recent(self.data_dir, 2):
                        lines.append(f"  · {row.get('where')}: "
                                     f"{row.get('error')}")
            except Exception:
                pass
            return "\n".join(lines)
        if text == "/backup":
            from .backup import export_profile
            out = export_profile(self.data_dir)
            return (f"Profile ka backup ban gaya, sir — "
                    f"{out['files']} files: {out['path']}\n"
                    "Note: keys isme NAHI hain (suraksha ke "
                    "liye) — restore ke baad keys Settings se "
                    "dobara dalni hongi.")
        if text.startswith("/restore "):
            from .backup import restore_profile
            name = text[len("/restore "):].strip()
            target = (self.data_dir / "backups" / name)
            if not target.exists() or "/" in name or "\\" in name:
                return ("Woh backup mila nahi, sir — backups "
                        "folder me jo zip hai usi ka naam do.")
            out = restore_profile(self.data_dir, target)
            return (f"Restore ho gaya, sir — {out['restored']} "
                    "files wapas aa gayin. Purani yaadein aur "
                    "records ab is install me hain; keys Settings "
                    "se dobara dal do.")
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
        if text == "/voice" or text.startswith("/voice "):
            arg = text[len("/voice"):].strip().lower()
            if arg in ("off", "stop"):
                return self.stop_voice()
            if arg == "mute":
                if self.voice_manager is None \
                        or not self.voice_manager.active:
                    return "Voice is not on, sir."
                muted = self.voice_manager.toggle_mute()
                return ("Microphone muted, sir." if muted
                        else "Microphone live, sir.")
            if arg == "interrupt":
                if self.voice_manager is not None:
                    self.voice_manager.interrupt()
                return "Interrupted, sir."
            return self.start_voice()
        return f"Unknown command, sir: {text.split()[0] if text else ''}"

    # -- the turn -----------------------------------------------------------

    # -- build tasks (the Antigravity planning gate) ----------

    def list_builds(self):
        from .builds import BuildStore
        return BuildStore(self.data_dir).list_builds()

    def build_plan_text(self, build_id: str) -> str:
        path = (self.data_dir / "builds" / build_id
                / "implementation_plan.md")
        if not path.exists():
            return "Aisi koi build nahi hai, sir."
        return path.read_text(encoding="utf-8")

    def proceed_build(self, build_id: str,
                      approve: bool = True) -> dict:
        """The owner's Proceed on a build plan — the gate
        Antigravity puts between planning and execution.
        Approval starts the executor thread on the plan
        exactly as filed; denial retires the build."""
        from .builds import BuildStore
        store = BuildStore(self.data_dir)
        state = store.load(build_id)
        if not state:
            return {"ok": False, "error": "build nahi mili"}
        if state["status"] != "awaiting_proceed":
            return {"ok": False,
                    "error": f"build '{state['status']}' haal "
                             "me hai — Proceed sirf plan par "
                             "hota hai"}
        if not approve:
            state["status"] = "denied"
            store.save(state)
            return {"ok": True, "status": "denied"}
        state["status"] = "executing"
        store.save(state)
        threading.Thread(target=_run_build_app,
                         args=(self.parts, self.data_dir,
                               build_id),
                         daemon=True,
                         name=f"jarvis-build-{build_id}").start()
        return {"ok": True, "status": "executing"}

    # -- confirmation cards (the owner's approval door) --------

    def pending_confirmations(self):
        from .confirmations import ConfirmationBook
        return ConfirmationBook(self.data_dir).pending()

    def confirm_card(self, card_id: str = "",
                     approve: bool = True) -> str:
        """The owner's verdict on a card. Approval re-evaluates
        the gate for a FRESH one-time token and runs the tool
        exactly once through the guarded dispatch; denial just
        retires the card. Either way the card can never be
        used again."""
        from .confirmations import ConfirmationBook
        book = ConfirmationBook(self.data_dir)
        if not card_id:
            pending = book.pending()
            if not pending:
                return "Koi pending card nahi hai, sir."
            card_id = pending[-1]["id"]
        if not approve:
            if book.deny(card_id):
                self.journal.append("confirm.denied",
                                    {"id": card_id})
                return (f"Card {card_id} radd kar diya, sir — "
                        "woh kaam nahi chalega.")
            return f"Card {card_id} pending nahi mila, sir."
        card = book.take(card_id)
        if card is None:
            return (f"Card {card_id} nahi chala, sir — ya woh "
                    "pehle hi tay ho chuka hai, ya uski miyad "
                    "(1 ghanta) khatm ho gayi. Hukum dobara "
                    "dijiye, naya card ban jayega.")
        decision = self.gate.evaluate(
            card["tool"], card["args"], ifc.OWNER_ORIGIN,
            self.allowances)
        if getattr(decision, "verdict", "") != "require_approval":
            return (f"Ab is kaam ko approval ki zaroorat nahi "
                    f"rahi ({getattr(decision, 'verdict', '?')}) "
                    "— kuch nahi chalaya, sir.")
        result = self.parts.dispatch(
            card["tool"], card["args"],
            approved_decision=decision)
        ok = _result_ok(result)
        self.journal.append("confirm.executed",
                            {"id": card_id, "tool": card["tool"],
                             "ok": ok})
        if ok:
            detail = result.get("result", result)
            return (f"Ho gaya, sir — {card['tool']} chal gaya "
                    f"(card {card_id}).\n"
                    f"{str(detail)[:400]}")
        return (f"Card manzoor tha, par {card['tool']} chal "
                f"nahi paya, sir: "
                f"{str(result.get('error', result))[:300]}")

    # -- automations: JARVIS uses them itself -----------------

    def _automation_pre_brain(self, text: str):
        """Before the brain: does a saved route own this job?
        The owner's standing order — no commands, JARVIS
        recognizes and runs it itself. Exactly one match with
        no params runs mechanically and its report IS the
        reply; a card-pause also completes the turn (the card
        is the owner's to approve); anything else becomes a
        hint inside the brain's turn, and a failed mechanical
        run falls through to the brain — mechanical route
        first, thinking route as its fallback."""
        try:
            from .automations import (AutomationBook,
                                      match_automations,
                                      run_steps)
            book = AutomationBook(self.data_dir)
            rows = book.all()
            if not rows:
                return None, None
            low = " " + str(text).lower() + " "
            if any(w in low for w in (" mat ", "ruk jao",
                                      "band kar do")):
                return None, None
            hits = match_automations(rows, text)
            if not hits:
                return None, None
            creating = any(w in low for w in ("banao", "bana do",
                                              "create", "save kar"))
            row, trigger = hits[0]
            if len(hits) == 1 and not creating \
                    and not (row.get("params") or []):
                report = run_steps(self.parts.dispatch, row, {})
                book.note_run(row["name"],
                              str(report.get("status", "")))
                self.journal.append("automation.auto_run", {
                    "name": row["name"], "trigger": trigger,
                    "status": report.get("status")})
                if report.get("status") == "done":
                    return (f"🤖 Automation '{row['name']}' khud "
                            f"chala diya, sir — "
                            f"{len(report.get('completed') or [])}"
                            " kadam poore hue.", None)
                if report.get("status") == "waiting_approval":
                    card = report.get("card") or {}
                    return (f"🤖 Automation '{row['name']}' "
                            f"chalaya, par kadam "
                            f"{report.get('stopped_at_step')} par "
                            f"confirmation card ban gaya hai "
                            f"({card.get('summary', '')}). "
                            "Manzoor kar dein to route wahi se "
                            "aage badhega — /cards dekhein, sir.",
                            None)
                return None, (f"automation '{row['name']}' "
                              "chalane ki koshish nakam rahi: "
                              f"{report.get('error')} — ab tum "
                              "khud auzaaron se ye kaam karo.")
            hint_rows = hits[:2]
            desc = "; ".join(
                f"'{r['name']}' (params: "
                f"{', '.join(r.get('params') or []) or 'koi nahi'})"
                for r, _ in hint_rows)
            return None, (f"in saved automations me se is kaam "
                          f"ke liye bana hai: {desc} — "
                          "run_automation se chalao, params is "
                          "sandesh se bhar kar. Naya route tabhi "
                          "banao jab koi mail na khaye.")
        except Exception:
            return None, None

    def _automation_auto_create(self, text: str, result):
        """After a brain turn: a job that took >= 3 successful
        tool calls has shown its shape. The SECOND time the
        same shape shows up, it has earned a route — save it
        (owner's words lifted into {{params}}), and say so in
        one line. One-off jobs never become routes."""
        try:
            records = [r for r in
                       (getattr(result, "tool_calls_made", None)
                        or [])
                       if getattr(r, "ok", False)]
            if len(records) < 3:
                return None
            banned = {"delegate_task", "delegate_tasks",
                      "run_mission", "delegate_team",
                      "build_feature"} | set(_META_TOOLS)
            if any(r.tool in banned for r in records):
                return None
            from .automations import (AutomationBook,
                                      parameterize_steps,
                                      signature_of)
            steps = [{"tool": r.tool, "args": dict(r.args or {})}
                     for r in records]
            steps, params = parameterize_steps(steps, text)
            sig = signature_of(steps)
            book = AutomationBook(self.data_dir)
            for row in book.all():
                if row.get("signature") == sig:
                    return None      # the route already exists
            if book.note_seen(sig) < 2:
                return None          # pehli dafa — sirf gin lo
            trigger = " ".join(str(text).lower().split())[:80]
            if len(trigger) < 10:
                return None
            base = records[0].tool.split("_")[0]
            name = f"auto-{base}-{sig[:4]}"
            row, err = book.create(
                name, f"Khud bana route: {trigger[:50]}",
                steps, params, triggers=[trigger],
                origin="auto")
            if err:
                return None
            self.journal.append("automation.auto_created",
                                {"name": row["name"],
                                 "signature": sig})
            return (f"🤖 Ye kaam doosri dafa hua, isliye iska "
                    f"route bana kar rakh diya hai "
                    f"('{row['name']}') — agli baar ye bolne par "
                    "ye khud chal jayega. /automations me "
                    "dikhega, sir.")
        except Exception:
            return None

    def _brain_for(self, text: str):
        """Route-then-specialize for the main turn: a cached
        sibling brain that sees only the domains this message
        calls for (plus the always-on core). No signal → the
        full brain. The choice is journaled, never silent."""
        from .router import route_schemas, routed_domains
        try:
            schemas = self.registry.advertised_tools(
                self.allowances)
            subset = route_schemas(text, schemas)
        except Exception:
            return self.brain
        if not subset:
            return self.brain
        cache = getattr(self, "_routed_brains", None)
        if cache is None:
            cache = self._routed_brains = {}
        key = frozenset(t["name"] for t in subset)
        if key not in cache:
            if len(cache) >= 24:      # bounded, like everything
                cache.clear()
            cache[key] = self.brain.with_schemas(subset)
            try:
                self.journal.append("brain.route", {
                    "domains": routed_domains(text),
                    "shown": len(subset), "total": len(schemas),
                    "where": "turn"})
            except Exception:
                pass
        clone = cache[key]
        # The clone froze the chain it was born with; the app's
        # chain can be swapped under it (fault injection in the
        # eval harness, a model switch in production). Sync it
        # every turn — the view is the clone's, the chain is
        # always the app's current one.
        clone.chain = self.brain.chain
        return clone

    def turn(self, user_text: str) -> str:
        text = str(user_text)
        if _is_stopped(self.data_dir):
            self.journal.append("app.turn_refused_stopped", {})
            return ("Band hoon, sir — /resume kahiye jab dobara "
                    "chalana ho.")
        # Automatic learning: anything new in data/inbox is ingested
        # before this turn is answered — no command, no ceremony.
        try:
            self.memory.scan_learned()
        except Exception:
            pass
        labeled = ifc.label_content(text, origin=ifc.OWNER_ORIGIN)
        self.journal.append("user.message", {
            "text": "[incognito]" if self.incognito else text,
            "origin": labeled.origin,
            "label": labeled.label})
        if not self.incognito:
            self.memory.note_turn("user", text)

        correction = None if self.incognito \
            else self._check_correction(text)
        reply = None
        stopped_reason = "completed"
        if correction is None:
            reply = self._fast_path(text, incognito=self.incognito)
        auto_hint = None
        auto_ran = False
        if reply is None and correction is None \
                and not self.incognito:
            reply, auto_hint = self._automation_pre_brain(text)
            auto_ran = reply is not None
        if reply is not None:
            via = "automation" if auto_ran else "fast_path"
            self.last_provider = "automation" if auto_ran \
                else "fast-path"
        else:
            rows_before = len(self.goodput.to_rows())
            brain_text = text if not auto_hint else (
                text + "\n[Automation note: " + auto_hint + "]")
            result = self._brain_for(text).respond(
                self._brain_history(), brain_text,
                correction=bool(correction))
            reply = str(result.reply)
            stopped_reason = str(
                getattr(result, "stopped_reason", "completed"))
            via = "brain"
            if not self.incognito:
                note = self._automation_auto_create(text, result)
                if note:
                    reply = reply + "\n\n" + note
            rows = self.goodput.to_rows()
            if len(rows) > rows_before:
                self.last_provider = str(rows[-1]["provider"])
            else:
                self.last_provider = (
                    getattr(self.chain, "last_provider", None)
                    or getattr(self.chain, "name", None) or "unknown")
        if correction is not None:
            self._store_lesson(correction, evidence_text=text)
        if not self.incognito:
            self._note_turn_signals(text, reply, stopped_reason)

        # Reminders whose time has come surface as notifications.
        try:
            _due_reminder_notifications(self.data_dir)
        except Exception:
            pass
        self.journal.append("assistant.reply", {
            "text": "[incognito]" if self.incognito else reply,
            "via": via, "provider": self.last_provider})
        # The goodput ledger is the diagnostic record (which brain
        # answered, which failed, why). It used to be loaded at startup
        # but NEVER saved, so data/goodput.jsonl simply never existed
        # on the owner's PC — persist it after every turn, silently:
        # a failed save must never break the conversation.
        try:
            self.goodput.save_jsonl(self.data_dir / "goodput.jsonl")
        except Exception as exc:
            from .swallow import note as _swallow
            _swallow(self.data_dir, "goodput.save", exc)
        if self.incognito:
            # The session converses normally — it is simply never
            # written to the permanent record (Sesame's incognito:
            # read memory all you like, write nothing).
            self._incognito_history.append(
                {"role": "user", "content": text})
            self._incognito_history.append(
                {"role": "assistant", "content": reply})
            return reply
        self.memory.note_turn("assistant", reply)
        self.history.append({"role": "user", "content": text})
        self.history.append({"role": "assistant", "content": reply})
        return reply

    def _brain_history(self) -> list:
        """What the brain may see this turn: the real history, plus
        the incognito session's own turns while incognito is on
        (they live apart and are discarded when it ends)."""
        if self.incognito:
            return self.history + self._incognito_history
        return self.history

    def _model_command(self, text: str) -> str:
        """The dialectic user model, owner-facing: list insights,
        add one in the owner's own words (never auto-changed
        afterwards), or read the questions disputes have queued."""
        model = self.user_model
        if model is None:
            return "User model is build me nahi hai, sir."
        rest = text[len("/model"):].strip()
        if rest == "questions":
            rows = model.pending_questions()
            if not rows:
                return "Koi pending sawaal nahi hai, sir."
            return "\n".join(f"❓ {r['question']}" for r in rows[-5:])
        if rest.startswith("add "):
            body = rest[4:].strip()
            dimension, _, claim = body.partition(":")
            if not claim:
                return ("Format: /model add <dimension>: <baat>, "
                        "sir — jaise /model add khana: mujhe "
                        "filter coffee pasand hai.")
            insight = model.observe(dimension.strip(),
                                    claim.strip(),
                                    evidence="owner /model add",
                                    relation="new", origin="owner")
            return (f"Samajh me jod liya, sir [{insight['dimension']}"
                    f"]: {insight['claim']}")
        rows = model.insights()
        if not rows:
            return ("Abhi koi insight nahi hai, sir. "
                    "/model add <dimension>: <baat> se pehli "
                    "jod sakte hain.")
        lines = []
        for ins in rows[-10:]:
            mark = "⚠ disputed" if ins["status"] == "disputed" \
                else f"{ins['confidence']:.0%}"
            lines.append(f"🧠 [{ins['dimension']}] {ins['claim']} "
                         f"({mark})")
        return "\n".join(lines)

    # -- corrections (Nova's learn-from-being-wrong, wired) -------------

    def _check_correction(self, text: str) -> Optional[dict]:
        """Regex pre-filter, then one structured model call — see
        jarvis/corrections.py. Returns {wrong, correct, lesson} for a
        confirmed correction, else None. Any failure means 'not a
        correction': a detector problem must never break or hijack
        a conversation turn."""
        try:
            if not looks_like_correction(text):
                return None
            prev_reply = ""
            for msg in reversed(self.history):
                if msg.get("role") == "assistant" \
                        and str(msg.get("content") or "").strip():
                    prev_reply = str(msg["content"])
                    break
            if not prev_reply:
                return None
            found = extract_correction(self.chain.complete, text,
                                       prev_reply)
            if found:
                self.journal.append("correction.detected", {
                    "wrong": found["wrong"],
                    "correct": found["correct"],
                    "lesson": found["lesson"]})
            return found
        except Exception:
            return None

    def _note_turn_signals(self, user_text: str, reply: str,
                           stopped_reason: str) -> None:
        """Nova's reflexion + curiosity, wired: an honest stop
        stores a retrievable WARNING (a similar future query's
        recall surfaces it), and a hedged reply queues a knowledge
        GAP that the watch cycle researches and learns later.
        Signals never break a turn."""
        try:
            if stopped_reason and stopped_reason != "completed":
                reason = stopped_reason.split(":", 1)[-1]
                self.memory.learn(
                    f"Warning: a similar request ran into trouble "
                    f"before ({reason}). The user had asked: "
                    f"{str(user_text)[:120]}",
                    source="inference")
                self.journal.append("reflexion.warning",
                                    {"reason": reason})
        except Exception:
            pass
        try:
            if _HEDGE_RE.search(str(reply or "")):
                gap = {"query": str(user_text)[:300],
                       "ts": time.time()}
                with (self.data_dir / "gaps.jsonl").open(
                        "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(gap, ensure_ascii=False)
                             + "\n")
                self.journal.append(
                    "curiosity.gap", {"query": gap["query"][:120]})
        except Exception:
            pass

    def _store_lesson(self, correction: dict,
                      evidence_text: str = "") -> None:
        try:
            self.memory.learn_lesson(correction["wrong"],
                                     correction["correct"],
                                     correction["lesson"],
                                     evidence=evidence_text or None)
        except Exception as exc:
            self.journal.append("correction.lesson_failed", {
                "error": f"{type(exc).__name__}: {exc}"})

    # -- fast path ------------------------------------------------------------

    def _fast_path(self, text: str,
                   incognito: bool = False) -> Optional[str]:
        low = text.strip()
        if _TIME_RE.search(low) or _DATE_RE.search(low):
            result = self.execute_tool("time_now", {})
            return self._format_now(result)
        m = _LEARN_PREFIX_RE.match(low) or _LEARN_SUFFIX_RE.match(low)
        if m and not low.rstrip().endswith("?"):
            statement = m.group(1).strip(" .")
            if statement:
                if incognito:
                    return ("Sir, incognito chal raha hai — ye "
                            "baat yaad nahi rakhunga. Yaad rakhna "
                            "hai to pehle /incognito off kar dijiye.")
                self.memory.learn(statement, source="owner",
                                  evidence=low)
                return f"Yaad rakh liya, sir: {statement}"
        if _RECALL_RE.search(low):
            hits = self.memory.recall(low, k=1)
            if hits:
                return f"Ji sir, yaad hai: {hits[0]['text']}"
        target = self._launch_target(low)
        if target is not None:
            result = self.execute_tool("open_app", {"name": target})
            if result.get("error"):
                return f"Nahi khul paaya, sir — {result['error']}"
            return f"Opening {target}, sir."
        return None

    @staticmethod
    def _launch_target(low: str) -> Optional[str]:
        """The app name in a bare launch imperative, else None.

        Conservative by design: no question marks, no conjunctions
        (a compound request belongs to the brain), the target is
        1-3 plain tokens after filler is stripped."""
        text = low.strip().rstrip(".").lower()
        if "?" in text or " and " in text or " aur " in text:
            return None
        m = _OPEN_PREFIX_RE.match(text) or _OPEN_SUFFIX_RE.match(text)
        if not m:
            return None
        target = _OPEN_FILLER_RE.sub(" ", m.group(1))
        target = re.sub(r"\s+", " ", target).strip(" .,")
        if target.startswith("the "):
            target = target[4:]
        tokens = target.split()
        if not (1 <= len(tokens) <= 3):
            return None
        if target in _OPEN_NOT_APPS:
            return None
        if "/" in target or "\\" in target:
            return None
        if not all(_OPEN_TOKEN_RE.match(tok) for tok in tokens):
            return None
        return target

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
