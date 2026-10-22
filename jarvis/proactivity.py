"""Proactivity chain — JARVIS v4 (Sentient install, Spec 2).

Four small roles, one pipeline, judgment where v4 used to
have only a deterministic nudge:

    trigger -> [prefilter: code] -> Formulator -> Searcher
    -> Reasoner -> near-duplicate drop -> Standardizer
    -> ONE notification (+ a confirmation card when the
    proposed action is destructive/send-class)

- **Formulator** (one tool-free call): the trigger -> at
  most 4 plain questions naming what context would decide
  the matter (availability, related open work, the
  document, the person).
- **Searcher** (bounded read-only loop, built by the
  caller): answers the questions from memory / knowledge /
  web / tracker / reminders and returns one compact
  report. It may never send, write or delete — its tool
  set is read-class only, enforced where it is built.
- **Reasoner** (one tool-free call): trigger + report +
  user-model insights + per-type feedback scores + the
  last 24h of suggestions -> decision JSON. Silence is
  the default and the normal outcome; newsletters,
  receipts and almost-FYI items are almost never worth it.
- **Standardizer** (one tool-free call): the Reasoner's
  type hint -> a canonical suggestion type (an existing
  one, or a new snake_case type code stores), so feedback
  scores accumulate per KIND of suggestion.

The owner's hard rules stay in code, not prompts: the
deterministic prefilter drops bulk mail before any model
is consulted; quiet hours and the once-a-day heartbeat
live in the scheduler wiring; a suggestion whose
confidence falls below the calibrator's verify band for
the `proactivity` domain is dropped silently (journaled);
feedback (owner's /suggest ok|no, or a confirmed/denied
action card) is scored per type and resolves the
Reasoner's calibration decision. Nothing the chain
produces executes by itself — a destructive action rides
a confirmation card, like everything else in v4.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path

_LOCKS: dict = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path) -> threading.Lock:
    key = str(Path(path).resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


_BULK_RE = re.compile(
    r"(newsletter|no-?reply|noreply|receipt|invoice|"
    r"otp|verification code|unsubscribe|"
    r"mailer-daemon|postmaster|notifications?@|"
    r"promotional|sale is live|%\s*off\b)", re.IGNORECASE)

_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def prefilter(trigger: dict) -> bool:
    """Deterministic, model-free: is this trigger even a
    candidate? Bulk / automated matter is dropped here —
    no model is ever consulted about it. Heartbeat
    triggers are assembled in code from the owner's own
    waiting items and always pass."""
    if not isinstance(trigger, dict):
        return False
    if str(trigger.get("kind", "")) == "heartbeat":
        return True
    text = str(trigger.get("text", ""))
    if not text.strip():
        return False
    return not _BULK_RE.search(text)


def _tokens(text: str) -> set:
    return set(_TOKEN_RE.findall(str(text).lower()))


def _overlap(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, min(len(ta), len(tb)))


# --------------------------------------------------------------------------- #
# The stores: suggestions (append-only, fold-on-read) and
# per-type feedback scores.
# --------------------------------------------------------------------------- #

class SuggestionStore:
    def __init__(self, data_dir):
        self.dir = Path(data_dir)
        self.rows_path = self.dir / "suggestions.jsonl"
        self.types_path = self.dir / "suggestion_types.json"
        self._lock = _lock_for(self.rows_path)

    # -- suggestion rows ---------------------------------- #

    def _rows(self) -> list:
        rows = []
        if self.rows_path.exists():
            try:
                for line in self.rows_path.read_text(
                        encoding="utf-8").splitlines():
                    if line.strip():
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            continue
            except Exception:
                return []
        folded = {}
        order = []
        for row in rows:
            rid = row.get("id")
            if not rid:
                continue
            if rid not in folded:
                order.append(rid)
                folded[rid] = dict(row)
            else:
                folded[rid].update(row)
        return [folded[rid] for rid in order]

    def add(self, row: dict) -> dict:
        row = {"id": uuid.uuid4().hex[:8],
               "ts": time.time(), "status": "shown", **row}
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            with self.rows_path.open("a",
                                     encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False)
                         + "\n")
        return row

    def update(self, suggestion_id: str, **fields) -> None:
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            with self.rows_path.open("a",
                                     encoding="utf-8") as fh:
                fh.write(json.dumps(
                    {"id": suggestion_id, **fields},
                    ensure_ascii=False) + "\n")

    def get(self, suggestion_id: str):
        for row in self._rows():
            if row.get("id") == suggestion_id:
                return row
        return None

    def recent(self, hours: float = 24.0) -> list:
        horizon = time.time() - hours * 3600
        return [r for r in self._rows()
                if float(r.get("ts", 0)) >= horizon]

    def all(self) -> list:
        return self._rows()

    # -- canonical types + feedback scores ----------------- #

    def _types(self) -> dict:
        try:
            data = json.loads(self.types_path.read_text(
                encoding="utf-8"))
            if isinstance(data, dict) \
                    and isinstance(data.get("types"), dict):
                return data["types"]
        except Exception:
            pass
        return {}

    def _save_types(self, types: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.types_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"types": types}, ensure_ascii=False),
            encoding="utf-8")
        tmp.replace(self.types_path)

    def known_types(self) -> list:
        return sorted(self._types().keys())

    def ensure_type(self, name: str) -> str:
        with self._lock:
            types = self._types()
            if name not in types:
                types[name] = {"pos": 0, "neg": 0}
                self._save_types(types)
        return name

    def score(self, name: str) -> float:
        """Laplace-smoothed usefulness of one type: an
        unseen type starts neutral (0.5)."""
        row = self._types().get(name) or {}
        pos, neg = int(row.get("pos", 0)), int(row.get("neg", 0))
        return (pos + 1) / (pos + neg + 2)

    def scores(self) -> dict:
        return {name: round(self.score(name), 2)
                for name in self.known_types()}

    def record_feedback(self, name: str, useful: bool) -> None:
        with self._lock:
            types = self._types()
            row = types.setdefault(name, {"pos": 0, "neg": 0})
            row["pos" if useful else "neg"] = \
                int(row.get("pos" if useful else "neg", 0)) + 1
            self._save_types(types)


# --------------------------------------------------------------------------- #
# The roles
# --------------------------------------------------------------------------- #

def _json_from_reply(text: str):
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


def _ask(brain_factory, prompt: str):
    try:
        brain = brain_factory()
        out = brain.respond([], prompt)
        return _json_from_reply(str(out.reply))
    except Exception:
        return None


def formulate(brain_factory, trigger: dict) -> list:
    """At most 4 context questions for one trigger."""
    prompt = (
        "Tum Query Formulator ho. Ek ghatna (trigger) "
        "aayi hai; faisla karne ke liye kaunsi jaankari "
        "chahiye, woh zyada se zyada 4 sawaal likho — "
        "saaf, chhote, aur sirf wohi jo asal me faisla "
        "badle (uplabdhata, juda khula kaam, woh dastavez, "
        "woh insaan).\n"
        f"TRIGGER: {str(trigger.get('text', ''))[:600]}\n"
        'Reply ONLY with JSON: {"questions": ["...", '
        '"..."]}')
    parsed = _ask(brain_factory, prompt)
    if not isinstance(parsed, dict) \
            or not isinstance(parsed.get("questions"), list):
        return []
    return [str(q).strip() for q in parsed["questions"]
            if str(q).strip()][:4]


def reason(brain_factory, *, trigger: dict, report: str,
           insights, type_scores: dict, recent) -> dict | None:
    """The Reasoner's verdict, or None when it cannot
    answer (silence — the default — either way)."""
    insight_lines = "\n".join(f"- {i}" for i in
                              (insights or [])[:5]) or "-"
    score_lines = "\n".join(
        f"- {name}: {score}" for name, score in
        list((type_scores or {}).items())[:10]) or "-"
    recent_lines = "\n".join(
        f"- {r.get('suggestion', '')[:100]}"
        for r in (recent or [])[-6:]) or "-"
    prompt = (
        "Tum Proactive Reasoner ho. Faisla karo: kya ye "
        "baat owner ko ab batani chahiye?\n"
        f"TRIGGER: {str(trigger.get('text', ''))[:500]}\n"
        f"SEARCH REPORT:\n{str(report)[:1800]}\n"
        f"OWNER KE BAARE ME SAMAJH:\n{insight_lines}\n"
        f"TYPE SCORES (kis kisam ki salah kaam aayi):\n"
        f"{score_lines}\n"
        f"PICHHLE 24 GHANTE KI SALAHEN:\n{recent_lines}\n"
        "Usool: KHAMOSHI hi aam haal hai — worth_it false "
        "hi zyadatar sahi jawab hai. Newsletter, raseed, "
        "sirf-jaankari wali baatein lagbhag kabhi salah ke "
        "layak nahi. Salah sirf tab jab owner ko kuch "
        "karna, rokna, ya waqt par jaanna asal me zaroori "
        "ho. Jo salah pehle di ja chuki hai use mat "
        "dohrana.\n"
        'Reply ONLY with JSON: {"worth_it": true|false, '
        '"confidence": 0.0, "reason": "kyon", '
        '"suggestion": "owner ke liye ek saaf salah", '
        '"type_hint": "kis kisam ki salah hai", "action": '
        '{"tool": "tool_name", "args": {...}} ya null}')
    parsed = _ask(brain_factory, prompt)
    if not isinstance(parsed, dict) \
            or parsed.get("worth_it") is not True:
        return None
    try:
        confidence = max(0.0, min(1.0, float(
            parsed.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    suggestion = str(parsed.get("suggestion", "")).strip()
    if not suggestion:
        return None
    action = parsed.get("action")
    if not isinstance(action, dict) \
            or not action.get("tool"):
        action = None
    return {"confidence": confidence,
            "reason": str(parsed.get("reason", ""))[:240],
            "suggestion": suggestion[:400],
            "type_hint": str(
                parsed.get("type_hint", ""))[:60],
            "action": action}


def standardize(brain_factory, type_hint: str,
                known_types) -> str:
    """Map a hint to a canonical type. The model's answer
    is accepted only when it names a known type or a
    clean new snake_case token; otherwise the hint itself
    is sanitized, and 'general' is the floor."""
    known = list(known_types or [])
    raw = ""
    try:
        brain = brain_factory()
        out = brain.respond([], (
            "Salah ki canonical type chuno. Maujood types: "
            + (", ".join(known) if known else "(koi nahi)")
            + f"\nHINT: {str(type_hint)[:60]}\n"
            "Ek maujood type bilkul waisa hi likho, ya ek "
            "naya snake_case naam banao. Jawab me SIRF type "
            "ka naam — ek shabd, aur kuch nahi."))
        raw = str(out.reply)
    except Exception:
        raw = ""
    candidate = raw.strip().split()[0] if raw.strip() else ""
    if candidate in known:
        return candidate
    if _TYPE_RE.match(candidate):
        return candidate
    cleaned = re.sub(r"[^a-z0-9]+", "_",
                     str(type_hint).lower()).strip("_")
    if _TYPE_RE.match(cleaned):
        return cleaned
    return "general"


# --------------------------------------------------------------------------- #
# The chain
# --------------------------------------------------------------------------- #

def run_chain(*, data_dir, trigger: dict,
              formulate_factory, searcher, reason_factory,
              standardize_factory, calibrator=None,
              notify=None, journal=None, card_sink=None,
              insights=(), action_is_risky=None):
    """One full pass. Returns the stored suggestion row,
    or None when the chain stayed silent (the normal
    outcome). Every outcome is journaled; nothing here
    executes an action — a risky action rides a card."""
    def _journal(kind, payload):
        if journal is not None:
            try:
                journal.append(kind, payload)
            except Exception:
                pass

    store = SuggestionStore(data_dir)
    if not prefilter(trigger):
        _journal("proactivity.dropped",
                 {"reason": "prefilter",
                  "trigger": str(trigger.get("text", ""))[:120]})
        return None
    questions = formulate(formulate_factory, trigger)
    try:
        report = str(searcher(questions) or "")[:2500]
    except Exception:
        report = ""
    recent = store.recent(24)
    verdict = reason(reason_factory, trigger=trigger,
                     report=report, insights=insights,
                     type_scores=store.scores(),
                     recent=recent)
    if verdict is None:
        _journal("proactivity.silent",
                 {"trigger": str(trigger.get("text", ""))[:120]})
        return None
    for old in recent:
        if old.get("status") in ("shown", "accepted") \
                and _overlap(verdict["suggestion"],
                             str(old.get("suggestion", ""))) \
                >= 0.7:
            _journal("proactivity.dropped",
                     {"reason": "near-duplicate",
                      "of": old.get("id")})
            return None
    confidence = verdict["confidence"]
    decision_id = None
    band = "act"
    if calibrator is not None:
        try:
            policy = calibrator.policy("proactivity",
                                       confidence)
            band = str(policy.get("action", "act"))
            if band == "ask":
                # Below the verify band: dropped silently
                # (journaled) — the owner never sees it and
                # no claim is recorded against the domain.
                _journal("proactivity.dropped",
                         {"reason": "confidence-band",
                          "confidence": confidence})
                return None
            row = calibrator.record(
                "proactivity",
                f"salah sahi hai: "
                f"{verdict['suggestion'][:60]}", confidence)
            decision_id = row["id"] if isinstance(row, dict) \
                else row
        except Exception:
            band = "act"
    canonical = standardize(standardize_factory,
                            verdict["type_hint"],
                            store.known_types())
    store.ensure_type(canonical)
    action = verdict["action"]
    card_id = None
    if action and band == "act" and card_sink is not None \
            and action_is_risky is not None \
            and action_is_risky(str(action.get("tool", ""))):
        try:
            card = card_sink(str(action["tool"]),
                             action.get("args") or {})
            if card:
                card_id = card.get("id")
        except Exception:
            card_id = None
    row = store.add({
        "type": canonical,
        "trigger": str(trigger.get("text", ""))[:300],
        "suggestion": verdict["suggestion"],
        "confidence": confidence,
        "reason": verdict["reason"],
        "decision_id": decision_id,
        "action": action, "card_id": card_id})
    if notify is not None:
        body = verdict["suggestion"]
        if card_id:
            body += (f"\nIs par card bana hai — /confirm "
                     f"{card_id} se chalega, /deny {card_id} "
                     "se radd.")
        body += (f"\nSalah kaam ki lagi? /suggest ok "
                 f"{row['id']} · nahi? /suggest no "
                 f"{row['id']}")
        try:
            notify("Ek salah hai, sir", body)
        except Exception:
            pass
    _journal("proactivity.suggested",
             {"id": row["id"], "type": canonical,
              "confidence": confidence,
              "card": card_id})
    return row


def feedback(data_dir, suggestion_id: str, useful: bool,
             calibrator=None):
    """The owner's verdict on one suggestion: per-type
    score + the Reasoner's calibration decision resolved.
    Returns the row, or None."""
    store = SuggestionStore(data_dir)
    row = store.get(suggestion_id)
    if row is None or row.get("status") != "shown":
        return None
    store.update(suggestion_id,
                 status="accepted" if useful else "dismissed")
    store.record_feedback(str(row.get("type", "general")),
                          useful)
    if calibrator is not None and row.get("decision_id"):
        try:
            calibrator.resolve(row["decision_id"], useful)
        except Exception:
            pass
    row["status"] = "accepted" if useful else "dismissed"
    return row


def settle_card_outcomes(data_dir, card_status,
                         calibrator=None) -> int:
    """Cards decide too: a suggestion whose action card
    was CONFIRMED counts accepted; a denied/expired card
    counts dismissed. `card_status(card_id)` returns the
    card's status string ("pending" while undecided)."""
    store = SuggestionStore(data_dir)
    settled = 0
    for row in store.all():
        if row.get("status") != "shown" \
                or not row.get("card_id"):
            continue
        try:
            status = card_status(row["card_id"])
        except Exception:
            continue
        if status == "pending" or status is None:
            continue
        useful = status == "used"
        store.update(row["id"],
                     status="accepted" if useful
                     else "dismissed")
        store.record_feedback(
            str(row.get("type", "general")), useful)
        if calibrator is not None and row.get("decision_id"):
            try:
                calibrator.resolve(row["decision_id"], useful)
            except Exception:
                pass
        settled += 1
    return settled
