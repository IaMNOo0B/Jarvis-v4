"""Route-then-specialize — the Kodee lesson, JARVIS-shaped.

Kodee never shows its brain 500 actions: a classifier routes
the request to one domain, and only that domain's handlers
load. v4 showed its brain all ~67 tools on every turn — a fat
prompt and a distracted chooser. This router is deterministic
(keyword stems, English + Roman Hinglish): the always-on core
(time, memory, knowledge, calibration, identity) rides every
turn; domain tools load when the words call for them; when
nothing matches, the FULL set loads — routing can only focus
the brain, never starve it. Dispatch is untouched: an unshown
tool is masked, not removed (the Manus rule), and the gate
still governs every call.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

ALWAYS_SKILLS = frozenset({
    "core.time", "core.memory", "core.knowledge",
    "core.calibration", "core.user_model", "core.graph",
    "core.notify", "core.brief",
    # Delegation is the universal escape hatch: a specialized
    # turn that hits its own edge can hand the work to a
    # background brain, which re-routes on the task text.
    "core.tasks", "core.mission",
})

DOMAIN_STEMS: Dict[str, Sequence[str]] = {
    "core.browser": ("browser", "chrome", "website", "web page",
                     "page khol", "site par", "click kar",
                     "tab khol", "link khol"),
    "core.web": ("search", "google", "internet", "online",
                 "web par", "khabar", "news", "wikipedia",
                 "github", "url", "website se", "dhoondh",
                 "dhundh", "pata kar", "mausam", "weather"),
    "core.files": ("file", "folder", "document", "docx", "pdf",
                   "spreadsheet", "excel", "csv", "note bana",
                   "likh kar save"),
    "core.pc": ("app khol", "kholo", "khol do", "band kar",
                "notepad", "calculator", "volume", "awaaz",
                "desktop", "window", "program khol", "pc par",
                "computer par", "screenshot le"),
    "core.vision": ("screen dekho", "screen par kya", "photo",
                    "tasveer", "image dekh", "ye kya dikh",
                    "screenshot dekh"),
    "core.mail": ("email", "mail", "gmail", "inbox", "draft",
                  "bhej do", "bhejna hai"),
    "core.telegram": ("telegram", "phone par bhej",
                      "mobile par"),
    "core.tracker": ("lead", "tracker", "follow-up",
                     "followup", "follow up", "client"),
    "core.tasks": ("background me", "peeche chal", "delegate",
                   "kaam de do"),
    "core.mission": ("mission", "goal pura", "maqsad",
                     "khud pura kar", "end tak"),
    "core.scheduler": ("routine", "roz ", "har din", "schedule",
                       "baje roz", "daily"),
    "core.triggers": ("jab bhi", "jaise hi", "trigger",
                       "nazar rakh", "watch kar"),
    "core.team": ("team bana", "kai agent", "mil kar banao",
                  "project banao poora"),
    "core.forge": ("feature banao", "khud me jodo",
                   "apne andar bana", "naya feature"),
    "core.code": ("code", "script", "program likh", "python",
                  "bug", "error theek"),
    "core.research": ("research", "gehri study", "deep study",
                      "tafteesh", "compare kar"),
    "core.reader": ("padh kar", "article", "ye padho",
                    "summarize kar", "sar batao"),
    "core.reminders": ("yaad dila", "reminder", "bhoolna mat"),
    "core.candidates": ("rule banao", "niyam", "hamesha ke "
                        "liye"),
    "core.diagnose": ("diagnose", "kya kharab", "health",
                      "masla kya hai", "check kar sab"),
    "core.watchers": ("file badle", "nazar rakh", "watcher"),
    "core.upgrade": ("update kar khud", "naya seekh",
                     "upgrade"),
}

MAX_DOMAINS = 3


def route_schemas(text: str,
                  schemas: Sequence[dict]) -> Optional[List[dict]]:
    """The specialized subset for this text, or None when no
    domain speaks up (the caller then uses the full set)."""
    low = " " + str(text or "").lower() + " "
    scored: Dict[str, int] = {}
    for skill, stems in DOMAIN_STEMS.items():
        hits = sum(1 for stem in stems if stem in low)
        if hits:
            scored[skill] = hits
    if not scored:
        return None
    top = sorted(scored, key=lambda s: (-scored[s], s))
    chosen = set(top[:MAX_DOMAINS]) | set(ALWAYS_SKILLS)
    subset = [s for s in schemas if s.get("skill") in chosen]
    if len(subset) >= len(schemas):
        return None
    return subset


def routed_domains(text: str) -> List[str]:
    """Which domains the router heard (for the journal)."""
    low = " " + str(text or "").lower() + " "
    hits = [skill for skill, stems in DOMAIN_STEMS.items()
            if any(stem in low for stem in stems)]
    return sorted(hits)[:MAX_DOMAINS]
