"""System-prompt byte-stability checker — Step 17 (Infra & Production).

Prefix-cache discipline (ai-eng-study part-I 17-06 / part-E 11-15): a
provider's prefix cache reuses everything up to the first byte that
differs. **One differing token inside the system prompt kills the cache
for everything after it** — so the persona + protocol PREFIX of JARVIS's
system prompt must be byte-identical across every turn, and anything
dynamic (timestamps, memory excerpts, the live clock) must live AFTER a
declared stable boundary.

This module is the checker for that discipline. It takes the sequence of
system prompts a session actually sent (one per turn) plus the boundary
marker, and reports:

- the first divergence offset when a turn's stable prefix differs from
  the first turn's, and WHICH section contains that offset (so the fix
  is "your PROTOCOL section moved", not "something changed somewhere");
- dynamic content found INSIDE the stable region (ISO dates/timestamps,
  epoch seconds) — dynamic data that escaped above the boundary;
- a missing boundary marker in any turn.

Fail-closed house style: an empty sequence proves nothing and is NOT ok.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

DEFAULT_BOUNDARY = "<!-- DYNAMIC -->"

# Content that is dynamic by nature and must never sit inside the stable
# prefix. (Kept narrow on purpose: these shapes are never legitimately
# part of a fixed persona/protocol text.)
_DYNAMIC_PATTERNS = (
    ("iso_datetime", re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?\b")),
    ("iso_date", re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),
    ("epoch_seconds", re.compile(r"\b1[6-9]\d{8}\b")),
)

_HEADER_MD = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
_HEADER_LABEL = re.compile(r"^([A-Z][A-Z0-9 _/\-]{2,}):\s*$")


def split_stable(prompt: str, boundary: str = DEFAULT_BOUNDARY):
    """(stable_prefix, dynamic_tail) or None when the boundary is absent.

    The boundary marker itself belongs to neither side's content: the
    stable prefix is everything BEFORE the marker.
    """
    idx = prompt.find(boundary)
    if idx < 0:
        return None
    return prompt[:idx], prompt[idx + len(boundary):]


def common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _section_at(text: str, offset: int) -> Optional[str]:
    """The section header in force at ``offset`` (last header at/before it)."""
    current: Optional[str] = None
    pos = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        m = _HEADER_MD.match(stripped)
        if m:
            current = m.group(2)
        else:
            m2 = _HEADER_LABEL.match(stripped)
            if m2:
                current = m2.group(1)
        if pos + len(line) > offset:
            break
        pos += len(line)
    return current


@dataclass
class Violation:
    kind: str            # prefix_diverged | dynamic_in_stable_prefix | boundary_missing | no_prompts
    prompt_index: int    # which turn (0-based); -1 for sequence-level problems
    offset: Optional[int] = None
    section: Optional[str] = None
    detail: str = ""


@dataclass
class StabilityReport:
    ok: bool
    boundary: str
    checked: int                       # number of prompts examined
    reference_len: int                 # chars in turn 0's stable prefix
    violations: List[Violation] = field(default_factory=list)

    @property
    def first_divergence_offset(self) -> Optional[int]:
        for v in self.violations:
            if v.kind == "prefix_diverged":
                return v.offset
        return None

    def summary(self) -> str:
        if self.ok:
            return (f"stable: {self.checked} prompt(s), prefix of "
                    f"{self.reference_len} chars byte-identical")
        bits = []
        for v in self.violations[:5]:
            where = f"turn {v.prompt_index}" if v.prompt_index >= 0 else "sequence"
            bits.append(f"{v.kind} at {where}"
                        + (f" offset {v.offset}" if v.offset is not None else "")
                        + (f" in section {v.section!r}" if v.section else "")
                        + (f" — {v.detail}" if v.detail else ""))
        return "UNSTABLE: " + "; ".join(bits)


def check_stability(prompts: Sequence[str],
                    boundary: str = DEFAULT_BOUNDARY) -> StabilityReport:
    """Verify the stable prefix is byte-identical across all turns."""
    prompts = list(prompts)
    violations: List[Violation] = []
    if not prompts:
        violations.append(Violation(
            kind="no_prompts", prompt_index=-1,
            detail="an empty prompt sequence proves nothing"))
        return StabilityReport(ok=False, boundary=boundary, checked=0,
                               reference_len=0, violations=violations)

    stables: List[str] = []
    for i, prompt in enumerate(prompts):
        parts = split_stable(prompt, boundary)
        if parts is None:
            violations.append(Violation(
                kind="boundary_missing", prompt_index=i,
                detail=f"boundary marker {boundary!r} not found; the whole "
                       "prompt is treated as stable prefix"))
            stables.append(prompt)
        else:
            stables.append(parts[0])

    reference = stables[0]
    for i in range(1, len(stables)):
        if stables[i] != reference:
            off = common_prefix_len(reference, stables[i])
            violations.append(Violation(
                kind="prefix_diverged", prompt_index=i, offset=off,
                section=_section_at(reference, off),
                detail=(f"expected {reference[off:off + 24]!r}…, "
                        f"got {stables[i][off:off + 24]!r}…")))

    for i, stable in enumerate(stables):
        found = 0
        for name, pattern in _DYNAMIC_PATTERNS:
            for m in pattern.finditer(stable):
                if found >= 3:
                    break
                violations.append(Violation(
                    kind="dynamic_in_stable_prefix", prompt_index=i,
                    offset=m.start(), section=_section_at(stable, m.start()),
                    detail=f"{name} {m.group(0)!r} sits inside the stable "
                           "prefix; move it below the boundary"))
                found += 1

    return StabilityReport(ok=not violations, boundary=boundary,
                           checked=len(prompts),
                           reference_len=len(reference),
                           violations=violations)


def assert_stable(prompts: Sequence[str],
                  boundary: str = DEFAULT_BOUNDARY) -> StabilityReport:
    """check_stability that raises ValueError on any violation."""
    report = check_stability(prompts, boundary)
    if not report.ok:
        raise ValueError(report.summary())
    return report


__all__ = ["DEFAULT_BOUNDARY", "Violation", "StabilityReport",
           "split_stable", "common_prefix_len", "check_stability",
           "assert_stable"]
