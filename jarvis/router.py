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

import re
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
    "core.automations": ("automation", "automate", "form bhar",
                         "form fill", "scrape kar", "data scrape",
                         "route bana", "roz wala kaam"),
    "core.mission": ("mission", "goal pura", "maqsad",
                     "khud pura kar", "end tak"),
    "core.builds": ("build task", "app banao", "app bana do",
                    "project banao", "software banao",
                    "tool banao", "plan karke banao",
                    "coding task"),
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


def _top_domains(text: str) -> List[str]:
    """The score-ranked top domains for this text — the ONE
    selection both the schema router and the journal use
    (audit 2026-10-22: routed_domains sorted alphabetically
    and the journal named domains the router never chose)."""
    low = " " + str(text or "").lower() + " "
    scored: Dict[str, int] = {}
    for skill, stems in DOMAIN_STEMS.items():
        hits = sum(1 for stem in stems if stem in low)
        if hits:
            scored[skill] = hits
    ranked = sorted(scored, key=lambda s: (-scored[s], s))
    return ranked[:MAX_DOMAINS]


def route_schemas(text: str,
                  schemas: Sequence[dict]) -> Optional[List[dict]]:
    """The specialized subset for this text, or None when no
    domain speaks up (the caller then uses the full set)."""
    top = _top_domains(text)
    if not top:
        return None
    chosen = set(top) | set(ALWAYS_SKILLS)
    subset = [s for s in schemas if s.get("skill") in chosen]
    if not subset or len(subset) >= len(schemas):
        # An empty subset is NOT a routing decision — it
        # would leave the brain tool-less. None = full set.
        return None
    return subset


def routed_domains(text: str) -> List[str]:
    """Which domains the router heard (for the journal) —
    the same ranked top-3 route_schemas picks."""
    return _top_domains(text)


# ---------------------------------------------------------------------------
# Rule-pack routing (ECC rules install, 2026-10-22)
#
# Separate from tool-schema routing above: this picks which
# distilled language cards (rules-packs/*.txt) ride in the
# prompt's dynamic tail for a coding turn. TWO keys must turn —
# (1) work: a coding/build domain was routed, or the caller
# already knows its input is a coding task (work=True); and
# (2) a language signal: the text names the language/framework,
# or known file paths match the pack's extensions. A language
# mentioned in passing ("Java is a place") loads nothing.
# ---------------------------------------------------------------------------

CODING_DOMAINS = frozenset({
    "core.code", "core.builds", "core.team", "core.forge",
})

# Specific stems: distinctive enough to name the pack on their
# own. Matched on word boundaries, so "java" never fires inside
# "javascript" and "rails" never inside "guardrails".
PACK_STEMS: Dict[str, Sequence[str]] = {
    "python": ("python", "pytest", "pip", "fastapi", "flask",
               "django"),
    "typescript": ("typescript", "javascript", "node.js", "nodejs",
                   "node", "npm", "deno", "bun"),
    "react": ("react", "next.js", "nextjs", "jsx", "tsx"),
    "react-native": ("react native", "react-native", "expo"),
    "vue": ("vue", "pinia"),
    "nuxt": ("nuxt", "nitro"),
    "angular": ("angular", "rxjs", "ngrx"),
    "web": ("html", "css", "website design", "landing page",
            "ui design"),
    "golang": ("golang", "go mod", "go test"),
    "java": ("java", "spring", "maven", "gradle"),
    "kotlin": ("kotlin", "android", "compose"),
    "rust": ("rust", "cargo"),
    "cpp": ("c++", "cpp", "cmake"),
    "csharp": ("c#", "csharp", ".net", "dotnet"),
    "fsharp": ("f#", "fsharp"),
    "swift": ("swift", "swiftui", "ios"),
    "dart": ("dart", "flutter"),
    "php": ("php", "laravel", "wordpress"),
    "ruby": ("ruby", "rails"),
    "perl": ("perl",),
    "arkts": ("arkts", "harmonyos", "arkui"),
}

# Generic stems: real signals, but too weak to outrank a
# specific match for another pack — consulted only when no
# specific stem fired ("ek component banao" -> react).
GENERIC_PACK_STEMS: Dict[str, Sequence[str]] = {
    "react": ("component",),
    "react-native": ("mobile app",),
}

# A framework card rarely travels alone: its base language
# card follows it (budget permitting — the annex loader drops
# whole cards from the end when the cap bites).
PACK_CHAINS: Dict[str, Sequence[str]] = {
    "react": ("typescript",),
    "react-native": ("typescript",),
    "nuxt": ("vue", "typescript"),
    "angular": ("typescript",),
}

EXTENSION_PACKS: Dict[str, str] = {
    ".py": "python",
    ".ts": "typescript", ".js": "typescript",
    ".tsx": "react", ".jsx": "react",
    ".vue": "vue",
    ".go": "golang",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin", ".kts": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".pl": "perl", ".pm": "perl",
    ".swift": "swift",
    ".dart": "dart",
    ".cs": "csharp",
    ".fs": "fsharp", ".fsx": "fsharp",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".h": "cpp",
    ".ets": "arkts",
    ".html": "web", ".css": "web",
    ".csproj": "csharp", ".fsproj": "fsharp",
}

# Whole-file signals — basenames (or glob prefixes) that name a
# pack even without a language extension.
FILENAME_PACKS: Dict[str, str] = {
    "pom.xml": "java",
    "go.mod": "golang",
    "cargo.toml": "rust",
    "gemfile": "ruby",
    "composer.json": "php",
    "pubspec.yaml": "dart",
    "cmakelists.txt": "cpp",
    "cpanfile": "perl",
    "package.swift": "swift",
    "angular.json": "angular",
}


def _stem_re(stem: str) -> "re.Pattern[str]":
    return re.compile(r"(?<![a-z0-9])" + re.escape(stem)
                      + r"(?![a-z0-9])")


def _stem_hits(text: str,
               table: Dict[str, Sequence[str]]) -> List[str]:
    """Packs named by a stem table, best first: earliest match
    position in the text, table order breaking ties."""
    low = " " + str(text or "").lower() + " "
    hits: List = []
    for order, (pack, stems) in enumerate(table.items()):
        positions = [m.start() for stem in stems
                     for m in _stem_re(stem).finditer(low)]
        if positions:
            hits.append((min(positions), order, pack))
    hits.sort()
    return [pack for _, _, pack in hits]


def _path_pack(path: str) -> Optional[str]:
    name = str(path or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if not name:
        return None
    if name.startswith("nuxt.config."):
        return "nuxt"
    if name in FILENAME_PACKS:
        return FILENAME_PACKS[name]
    dot = name.rfind(".")
    if dot > 0:
        return EXTENSION_PACKS.get(name[dot:])
    return None


def _expand_chains(packs: Sequence[str]) -> List[str]:
    out: List[str] = []
    for pack in packs:
        if pack not in out:
            out.append(pack)
        for chained in PACK_CHAINS.get(pack, ()):
            if chained not in out:
                out.append(chained)
    return out


def route_rule_packs(text: str,
                     domains: Sequence[str] = (),
                     paths: Sequence[str] = (),
                     work: bool = False) -> List[str]:
    """Priority-ordered pack names for this work input.

    [] unless `work` is true or a coding/build domain is
    present — the two-key rule. Explicit text signals outrank
    path/extension signals, which outrank generic stems.
    Chains follow their framework pack; duplicates appear
    once. The annex loader enforces the size cap."""
    if not work and not (set(domains or ()) & CODING_DOMAINS):
        return []
    chosen = _stem_hits(text, PACK_STEMS)
    if not chosen:
        for path in paths or ():
            pack = _path_pack(path)
            if pack and pack not in chosen:
                chosen.append(pack)
    if not chosen:
        chosen = _stem_hits(text, GENERIC_PACK_STEMS)
    return _expand_chains(chosen)
