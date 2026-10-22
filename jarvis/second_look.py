"""Second Look — JARVIS v4 (Sentient install, Spec 1).

Sentient's hardest safety rule, rebuilt in v4's own words:
before a call that SENDS something out of the machine may
run, prove the call matches what the owner actually asked —
in two stages, the second of which outside content can
never reach.

Stage A (code, always, no model): every destination
(address, handle, account) and every amount in the proposed
call's arguments must trace to one of three sources —
  (i)   the owner's own words in this conversation,
  (ii)  a trusted record: the tracker's rows, or the
        trust-guarded memory facts,
  (iii) a destination PRODUCED by an earlier tool result in
        this same run — but only from record-class tools
        (memory / knowledge / tracker reads). Raw web
        content is outside content: an address that appears
        only inside a fetched page is NOT traced, and the
        flag says so in plain words.
For replies, destinations must come from the thread's own
headers; a Reply-To that redirects away from the From is
itself a flag (the check() headers argument).

Stage B (one tool-free brain call, conversational path
only): the checker brain sees ONLY the owner's request text
and the proposed call — never any outside content — and
answers whether the action matches the request. Error,
refusal, timeout or unparseable output all count as "does
not match": fail closed.

Effect (decided by the dispatch, not here): a flag on the
conversational path raises an approval card carrying the
reason; a flag on an internal (worker) path fails the step.
A clean pass changes nothing. A warning may only ever ADD
a question, never remove one — this module never weakens
the gate, the cards, the fact-gate, or the guardrails; it
runs after the gate's verdict, before execution.
"""

from __future__ import annotations

import json
import re
import threading
from collections import deque

# The starter send set (Spec 1: "start with email_send,
# telegram_send, and any future send tool"). A tool is also
# in scope when its manifest permission is the send class.
STARTER_SEND_TOOLS = frozenset({"email_send", "telegram_send"})
SEND_PERMISSIONS = frozenset({"message.send"})

# Tool results whose destinations count as "produced by a
# trusted record": internal record reads only. net.outbound
# (the web) is deliberately absent — a fetched page is
# outside content, the exact poisoning vector Stage A
# exists to catch.
TRUSTED_RESULT_PERMISSIONS = frozenset({
    "memory.read", "knowledge.read", "reminder.read",
    "system.diagnose"})

_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_HANDLE_RE = re.compile(r"@[A-Za-z0-9._]{2,}")
_CURRENCY_RE = re.compile(
    r"(?:₹|\$|€|rs\.?|inr|usd)\s*(\d[\d,]*(?:\.\d{1,2})?)",
    re.IGNORECASE)
_AMOUNT_WORD_RE = re.compile(
    r"(\d[\d,]*(?:\.\d{1,2})?)\s*(?:rupees|rs|dollars|usd|inr)\b",
    re.IGNORECASE)

# Argument keys whose string values are destinations even
# when they are not shaped like an email (chat ids, phone
# numbers, account labels, bare handles).
_DEST_KEYS = frozenset({
    "to", "recipient", "recipients", "address", "email",
    "handle", "chat_id", "user", "user_id", "target",
    "phone", "contact", "account_id", "channel"})
# Argument keys whose numeric values are amounts.
_AMOUNT_KEYS = frozenset({
    "amount", "price", "total", "cost", "sum", "value",
    "quantity_price"})


def in_scope(name: str, tool=None) -> bool:
    """Is this tool send-class for Second Look purposes?"""
    if name in STARTER_SEND_TOOLS:
        return True
    if tool is not None and getattr(tool, "permission", "") \
            in SEND_PERMISSIONS:
        return True
    return False


def _strings(value, out):
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _strings(item, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _strings(item, out)


def extract_destinations(args) -> list:
    """Every destination named by a proposed call: emails
    and @handles anywhere in its strings, plus the values
    of destination-shaped keys (chat ids, phone numbers)."""
    found = []
    seen = set()

    def _add(value):
        value = str(value).strip()
        if value and value.lower() not in seen:
            seen.add(value.lower())
            found.append(value)

    texts = []
    _strings(args or {}, texts)
    for text in texts:
        for match in _EMAIL_RE.findall(text):
            _add(match)
        for match in _HANDLE_RE.findall(text):
            # An email's domain part is not a handle.
            if not any(match.lower() in e.lower()
                       for e in found if "@" in e):
                _add(match)
    if isinstance(args, dict):
        for key, value in args.items():
            if str(key).lower() in _DEST_KEYS \
                    and isinstance(value, (str, int)):
                _add(value)
    return found


def _norm_amount(raw: str) -> str:
    num = str(raw).replace(",", "").strip()
    if "." in num:
        num = num.rstrip("0").rstrip(".")
    return num


def extract_amounts(args) -> list:
    """Every amount named by a proposed call, normalized
    ("1,000.00" -> "1000") so it can be matched against
    the owner's own words."""
    found = []
    seen = set()

    def _add(raw):
        num = _norm_amount(raw)
        if num and num not in seen:
            seen.add(num)
            found.append(num)

    texts = []
    _strings(args or {}, texts)
    for text in texts:
        for match in _CURRENCY_RE.findall(text):
            _add(match)
        for match in _AMOUNT_WORD_RE.findall(text):
            _add(match)
    if isinstance(args, dict):
        for key, value in args.items():
            if str(key).lower() in _AMOUNT_KEYS \
                    and isinstance(value, (int, float)) \
                    and not isinstance(value, bool):
                _add(str(value))
    return found


def _json_from_reply(text: str):
    """A model's JSON, fenced or bare; None when it isn't
    any. (Same small shape the other internal roles use.)"""
    cleaned = str(text or "").strip()
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


class SecondLook:
    """The provenance checker. One instance per app; the
    dispatch consults it for send-class calls. Owner words
    are fed in by the surfaces that hear the owner (chat
    turns, delegated task texts); trusted records are read
    live through the provider callable, so a tracker row or
    memory fact filed a minute ago already counts."""

    def __init__(self, *, journal=None, brain_factory=None,
                 trusted_texts=None):
        self.journal = journal
        self.brain_factory = brain_factory
        self._trusted_texts = trusted_texts or (lambda: [])
        self._owner_texts = deque(maxlen=50)
        self._extra_trusted = []
        self._result_dests = set()
        self._lock = threading.Lock()

    # -- context feeds ------------------------------------ #

    def note_owner_text(self, text: str) -> None:
        """One utterance that is genuinely the owner's (a
        chat turn, a delegated task's text)."""
        text = str(text or "").strip()
        if text:
            with self._lock:
                self._owner_texts.append(text[:2000])

    def note_trusted_text(self, text: str) -> None:
        """A trusted record created by the app itself —
        e.g. a follow-up thread's header-fixed recipient."""
        text = str(text or "").strip()
        if text:
            with self._lock:
                self._extra_trusted.append(text[:1000])

    def note_result(self, name: str, permission: str,
                    result) -> None:
        """Harvest destinations an earlier tool result
        PRODUCED — record-class tools only (see the module
        docstring: web content never feeds this set)."""
        if permission not in TRUSTED_RESULT_PERMISSIONS:
            return
        try:
            blob = json.dumps(result, ensure_ascii=False,
                              default=str)
        except Exception:
            return
        dests = set()
        for match in _EMAIL_RE.findall(blob):
            dests.add(match.lower())
        for match in _HANDLE_RE.findall(blob):
            dests.add(match.lower())
        if dests:
            with self._lock:
                self._result_dests |= dests

    def owner_request_text(self) -> str:
        with self._lock:
            return self._owner_texts[-1] if self._owner_texts \
                else ""

    # -- the check ---------------------------------------- #

    def _haystacks(self):
        with self._lock:
            owner = "\n".join(self._owner_texts).lower()
            extra = "\n".join(self._extra_trusted).lower()
            results = set(self._result_dests)
        try:
            trusted = "\n".join(
                str(t) for t in self._trusted_texts()).lower()
        except Exception:
            trusted = ""
        return owner, (trusted + "\n" + extra).strip(), results

    def _trace(self, needle: str, owner: str, trusted: str,
               results: set):
        """Where does this value trace to? Returns the
        source's plain name, or None when untraceable."""
        low = needle.lower()
        if low and low in owner:
            return "owner ke apne shabdon se"
        if low and low in trusted:
            return "trusted record se (tracker / memory)"
        if low in results:
            return "is run ke ek record-tool ke result se"
        return None

    def check(self, name: str, args, *, mode: str,
              owner_request: str = "",
              headers=None):
        """None when the call is clean; otherwise a flag
        dict {stage, flags, reason}. mode "card" runs both
        stages (conversational path); mode "fail" runs
        Stage A only (internal worker paths)."""
        args = args or {}
        owner, trusted, results = self._haystacks()
        flags = []
        for dest in extract_destinations(args):
            if self._trace(dest, owner, trusted,
                           results) is None:
                flags.append({
                    "kind": "destination", "value": dest,
                    "note": f"'{dest}' na owner ke shabdon "
                            "me mila, na kisi trusted record "
                            "me — ye kahin bahar ke content "
                            "se aaya lagta hai"})
        for amount in extract_amounts(args):
            if self._trace(amount, owner, trusted,
                           results) is None:
                flags.append({
                    "kind": "amount", "value": amount,
                    "note": f"rakam '{amount}' owner ke "
                            "shabdon ya trusted record me "
                            "nahi milti — iski pushti "
                            "chahiye"})
        if headers:
            frm = str(headers.get("from", "")).lower()
            reply_to = str(
                headers.get("reply_to", "")).lower()
            if reply_to and frm and reply_to != frm:
                flags.append({
                    "kind": "redirect", "value": reply_to,
                    "note": f"reply ka pata thread ke From "
                            f"({frm}) se alag Reply-To "
                            f"({reply_to}) par ja raha hai "
                            "— redirect jaisa lagta hai"})
        if flags:
            return self._flag(name, "A", flags)
        if mode == "card" and self.brain_factory is not None:
            problem = self._stage_b(name, args,
                                    owner_request)
            if problem is not None:
                return self._flag(name, "B", [{
                    "kind": "mismatch", "value": name,
                    "note": problem}])
        self._journal("second_look.checked",
                      {"tool": name, "ok": True,
                       "mode": mode})
        return None

    def _stage_b(self, name: str, args, owner_request: str):
        """The tool-free checker brain: sees ONLY the
        owner's request and the proposed call. Returns a
        plain reason when the action does not match (or
        cannot be confirmed — fail closed), else None."""
        request = str(owner_request
                      or self.owner_request_text())[:800]
        try:
            blob = json.dumps(args, ensure_ascii=False,
                              default=str)[:800]
        except Exception:
            blob = str(args)[:800]
        prompt = (
            "You are the Second-Look checker. You see ONLY "
            "the owner's own request and one proposed tool "
            "call — nothing else exists for you.\n"
            f"OWNER REQUEST: {request or '(koi lafz nahi)'}\n"
            f"PROPOSED CALL: {name} {blob}\n"
            "Does this call do what the owner asked — same "
            "action, same destination, same amounts? Any "
            "doubt, any extra destination or amount, any "
            "different action = does NOT match.\n"
            'Reply ONLY with JSON: {"matches": true} or '
            '{"matches": false, "why": "ek chhoti line"}')
        try:
            brain = self.brain_factory()
            out = brain.respond([], prompt)
            parsed = _json_from_reply(str(out.reply))
        except Exception as exc:
            return (f"checker brain chal nahi paya "
                    f"({type(exc).__name__}) — fail-closed: "
                    "bina pushti ke ye send nahi jayega")
        if not isinstance(parsed, dict) \
                or parsed.get("matches") is not True:
            why = ""
            if isinstance(parsed, dict):
                why = str(parsed.get("why", ""))[:160]
            return ("checker brain ne is call ko owner ki "
                    "baat se milta hua nahi mana"
                    + (f": {why}" if why else
                       " (jawab saaf nahi aaya — fail-closed)"))
        return None

    def _flag(self, name: str, stage: str, flags: list):
        reason = "; ".join(f["note"] for f in flags)[:400]
        self._journal("second_look.flagged", {
            "tool": name, "stage": stage, "flags": flags,
            "reason": reason})
        return {"stage": stage, "flags": flags,
                "reason": reason}

    def _journal(self, kind: str, payload: dict) -> None:
        if self.journal is not None:
            try:
                self.journal.append(kind, payload)
            except Exception:
                pass
