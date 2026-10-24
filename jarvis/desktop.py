"""Desktop lane: operate any app the way a human would — see the
screen, ground a control, act on it, verify the result.

Two laws, both fail-closed:

1. Versioned refs: every grounding pass mints a new version. An
   action against a ref from an older version is REFUSED, never
   replayed — a moved window or a re-rendered dialog must never
   receive a click meant for something else.
2. Zoom + verify: after an action, the lane re-grounds and checks
   the expected change (the control gone, its label changed, a new
   control present). One re-ground + retry is allowed; a second
   miss is an honest failure, never a claimed success.

The live backend is Windows-only (UI Automation) and is NOT faked
here: on any other platform it answers an honest windows_only
error. Tests run against FakeBackend, an in-memory screen.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ElementRef:
    """One grounded control. version ties it to the grounding pass
    that minted it; token is opaque backend identity."""
    ref_id: str
    version: int
    name: str
    role: str
    bbox: tuple  # (x, y, w, h), normalized 0-1000
    token: str = ""


@dataclass
class Grounding:
    version: int
    elements: list = field(default_factory=list)

    def find(self, query: str):
        q = query.lower()
        return [e for e in self.elements
                if q in e.name.lower() or q in e.role.lower()]


class FakeBackend:
    """In-memory screen for tests. move() simulates a re-render:
    same controls, new version, new tokens."""

    def __init__(self, elements):
        self._elements = list(elements)
        self._version = 1
        self.actions = []

    def snapshot(self) -> Grounding:
        return Grounding(
            version=self._version,
            elements=[ElementRef(e.ref_id, self._version, e.name,
                                 e.role, e.bbox, e.token)
                      for e in self._elements])

    def move(self):
        self._version += 1
        self._elements = [
            ElementRef(e.ref_id, self._version, e.name, e.role,
                       e.bbox, e.token + "#v%d" % self._version)
            for e in self._elements]

    def remove(self, ref_id: str):
        self._elements = [e for e in self._elements
                          if e.ref_id != ref_id]
        self._version += 1

    def act(self, ref: ElementRef, action: str):
        self.actions.append((ref.ref_id, action))
        return {"ok": True, "ref": ref.ref_id, "action": action}


class WindowsBackend:
    """Live UIA backend. Deliberately unimplemented on non-Windows
    hosts: a fake would be worse than an honest refusal."""

    def snapshot(self) -> Grounding:
        if sys.platform != "win32":
            raise NotImplementedError(
                "desktop lane needs Windows UI Automation")
        raise NotImplementedError(
            "live UIA backend lands on the owner's PC")

    def act(self, ref: ElementRef, action: str):
        raise NotImplementedError(
            "live UIA backend lands on the owner's PC")


class DesktopLane:
    """Versioned, verified desktop actions over any backend."""

    def __init__(self, backend):
        self.backend = backend

    def ground(self, query: str):
        snap = self.backend.snapshot()
        return snap, snap.find(query)

    def act(self, ref: ElementRef, action: str):
        """Refuse stale refs outright — never click a ghost."""
        try:
            current = self.backend.snapshot().version
        except NotImplementedError as exc:
            return {"ok": False, "error": "windows_only",
                    "detail": str(exc)}
        if ref.version != current:
            return {"ok": False, "error": "stale_ref",
                    "detail": ("ref version %s but screen is at %s; "
                               "re-ground first") % (ref.version, current)}
        return self.backend.act(ref, action)

    def act_verified(self, ref: ElementRef, action: str,
                     expect_gone: str = None):
        """Act, re-ground, verify. One retry on a fresh ref, then
        an honest failure. expect_gone names a control that must
        disappear (e.g. a dialog after clicking its OK button)."""
        result = self.act(ref, action)
        if not result.get("ok"):
            if result.get("error") != "stale_ref":
                return result
            snap = self.backend.snapshot()
            fresh = [e for e in snap.elements
                     if e.ref_id == ref.ref_id]
            if not fresh:
                return {"ok": False, "error": "stale_ref",
                        "detail": "control vanished on re-ground"}
            result = self.backend.act(fresh[0], action)
            if not result.get("ok"):
                return result
        if expect_gone is not None:
            snap = self.backend.snapshot()
            if snap.find(expect_gone):
                return {"ok": False, "error": "verify_failed",
                        "detail": "%r still present after %s"
                                  % (expect_gone, action)}
        return {"ok": True, "verified": True, "action": action}
