"""Vision — JARVIS v4, Wave 4 (Nova's ask, combined with the
frame's own vision_gate discipline).

Two eyes, one rule:
- `see_page` photographs the controlled browser (CDP screenshot);
- `see_screen` photographs the owner's desktop (Windows capture
  through PowerShell/.NET — the owner's machine, the owner's
  screen, his standing full-access doctrine).

The rule (vision_gate's CMER spirit): a vision answer is an
OBSERVATION, never a fact. It is returned labelled as one, it is
never auto-learned into memory, and when there is no Gemini key
or the capture cannot happen, the tools say so honestly instead
of describing a screen they never saw.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import tempfile
from typing import Optional, Tuple

_DEFAULT_QUESTION = ("Describe what is on this screen clearly and "
                     "briefly: the main window or page, what the "
                     "user seems to be doing, and anything that "
                     "looks like an error or a problem.")


def capture_desktop_png() -> Tuple[Optional[bytes], str]:
    """(png_bytes, '') on success, (None, honest reason) otherwise.
    Windows only — the capture rides System.Drawing through
    PowerShell, no terminal for the owner, ever."""
    if sys.platform != "win32":
        return None, ("screen capture abhi sirf Windows par hai, "
                      "sir — ye machine Windows nahi hai")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
        "$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height;"
        "$g = [System.Drawing.Graphics]::FromImage($bmp);"
        "$g.CopyFromScreen($b.Location, "
        "[System.Drawing.Point]::Empty, $b.Size);"
        f"$bmp.Save('{tempfile.gettempdir()}\\\\jarvis-screen.png', "
        "[System.Drawing.Imaging.ImageFormat]::Png);"
        "$g.Dispose(); $bmp.Dispose()")
    path = os.path.join(tempfile.gettempdir(), "jarvis-screen.png")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", script],
                       timeout=20, capture_output=True)
        with open(path, "rb") as fh:
            data = fh.read()
        os.unlink(path)
        if not data:
            return None, "screen capture khaali aaya, sir"
        return data, ""
    except Exception as exc:
        return None, f"screen capture failed: {type(exc).__name__}"


def analyze_image(api_key: str, png_bytes: bytes,
                  question: str = "") -> dict:
    """One Gemini vision call over the PNG. Returns the shaped
    observation or an honest error."""
    if not api_key:
        return {"ok": False,
                "error": "vision ke liye Gemini key chahiye, sir — "
                         "/key se file kar dijiye"}
    from .providers import GeminiProvider
    payload = {"contents": [{"parts": [
        {"text": question.strip() or _DEFAULT_QUESTION},
        {"inlineData": {"mimeType": "image/png",
                        "data": base64.b64encode(
                            png_bytes).decode("ascii")}}]}]}
    try:
        data = GeminiProvider(api_key).generate_content(payload)
    except Exception as exc:
        return {"ok": False,
                "error": f"vision call failed: "
                         f"{type(exc).__name__}: {exc}"}
    candidates = data.get("candidates") or []
    if not candidates:
        return {"ok": False,
                "error": "vision model ne koi jawab nahi diya, sir"}
    parts = ((candidates[0].get("content") or {}).get("parts") or [])
    text = "\n".join(str(p["text"]) for p in parts
                     if isinstance(p, dict) and p.get("text"))
    if not text:
        return {"ok": False,
                "error": "vision model ka jawab khaali tha, sir"}
    return {"ok": True, "observation": text,
            "note": "yeh vision ki observation hai — pakki baat "
                    "nahi; zaroorat ho to doosre tareeke se pakka "
                    "kar lena"}


# ---------------------------------------------------------------------------
# Visual memory (Astra's rolling log, at text level)
# ---------------------------------------------------------------------------

def log_visual(data_dir, kind: str, question: str,
               answer: str) -> None:
    """One line per look: when, what kind, what was asked, what
    was seen (a snippet). The screen itself is never stored —
    only the words about it. Never raises."""
    import json as _json
    import time as _time
    from pathlib import Path as _Path
    try:
        path = _Path(data_dir) / "visual_memory.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps({
                "ts": _time.time(), "kind": str(kind)[:20],
                "question": str(question)[:120],
                "answer": str(answer)[:400]}) + "\n")
    except Exception as exc:
        from .swallow import note
        note(data_dir, "visual.log", exc)


def recall_visual(data_dir, query: str = "", k: int = 5) -> list:
    """The most recent looks, newest first; a query filters by
    substring over question + answer."""
    import json as _json
    from pathlib import Path as _Path
    path = _Path(data_dir) / "visual_memory.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(_json.loads(line))
        except Exception:
            continue
    needle = str(query).strip().lower()
    if needle:
        rows = [r for r in rows
                if needle in (str(r.get("question", "")) + " "
                              + str(r.get("answer", ""))).lower()]
    return list(reversed(rows))[:max(1, k)]
