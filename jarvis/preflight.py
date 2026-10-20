"""Route-aware preflight for JARVIS v4 (Step 00 — Setup & Tooling).

Pattern: ai-engineering-from-scratch, Phase 00 Lesson 01 (`verify.py`).
Every check is REQUIRED or OPTIONAL, every failure carries an exact
``next_fix`` — the action the owner takes, in plain language, so he is
never left in the dark. The exit code is non-zero ONLY when a required
check fails; optional extras (offline brain, voice packs) never block
the start.

Run it:
    python -m jarvis.preflight            # short checklist
    python -m jarvis.preflight --explain  # plain-language explanation
    python -m jarvis.preflight --json     # machine-readable results

No third-party imports; safe to run before anything else exists.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import sys
import urllib.request
from pathlib import Path

MIN_PYTHON = (3, 11)
OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"
OLLAMA_TIMEOUT_S = 1.5

LEVEL_REQUIRED = "required"
LEVEL_OPTIONAL = "optional"

# ---------------------------------------------------------------------------
# Fix texts — exact owner actions, GUI-first (the owner never uses a terminal).
# ---------------------------------------------------------------------------
FIX_PYTHON = (
    "Install Python 3.11 or newer from python.org (tick 'Add Python to PATH' "
    "during setup), then double-click JARVIS.bat again."
)
FIX_PACKAGE = (
    "This folder looks incomplete. Download the JARVIS v4 ZIP again, extract "
    "ALL of it into one fresh folder, then double-click JARVIS.bat there."
)
FIX_DATA_MISSING = (
    "Double-click setup.bat once — it creates the data folder JARVIS needs "
    "for its memory and settings."
)
FIX_DATA_UNWRITABLE = (
    "Move the whole JARVIS folder into your Documents folder (not inside "
    "Program Files), then double-click JARVIS.bat again."
)
FIX_OLLAMA = (
    "Optional — for the free offline brain: install Ollama from ollama.com "
    "and open it once so it runs in the background. JARVIS still starts "
    "without it."
)
FIX_MODELS = (
    "Optional — for the hands-free wake word: put the wake-word model files "
    "in a 'models' folder next to JARVIS.bat."
)
FIX_WHISPER = (
    "Optional — for voice typing: double-click setup.bat and let it install "
    "the voice pack (faster-whisper)."
)
FIX_KOKORO = (
    "Optional — for the Kokoro voice: double-click setup.bat and let it "
    "install the voice pack (kokoro)."
)


def _result(name, level, ok, detail, why, next_fix=""):
    return {
        "name": name,
        "level": level,
        "ok": bool(ok),
        "detail": detail,
        "why": why,
        "next_fix": next_fix,
    }


# ---------------------------------------------------------------------------
# Individual checks. Each returns one result dict and NEVER raises.
# ---------------------------------------------------------------------------
def check_python_version(version=None):
    """Required: Python >= MIN_PYTHON. `version` is injectable for tests."""
    if version is None:
        version = sys.version_info[:3]
    found = ".".join(str(p) for p in version[:3])
    need = ".".join(str(p) for p in MIN_PYTHON)
    ok = tuple(version[:2]) >= MIN_PYTHON
    detail = f"found Python {found}; JARVIS v4 needs {need} or newer"
    return _result(
        "Python version",
        LEVEL_REQUIRED,
        ok,
        detail,
        "Python is the engine JARVIS runs on — too old and JARVIS cannot start.",
        "" if ok else FIX_PYTHON,
    )


def check_package(root):
    """Required: the jarvis package folder is present and complete."""
    pkg = Path(root) / "jarvis"
    ok = pkg.is_dir() and (pkg / "__init__.py").is_file()
    detail = (
        f"program folder found at {pkg}"
        if ok
        else f"program folder missing or incomplete at {pkg}"
    )
    return _result(
        "JARVIS program files",
        LEVEL_REQUIRED,
        ok,
        detail,
        "These are JARVIS's own program files — without them there is nothing to run.",
        "" if ok else FIX_PACKAGE,
    )


def _probe_writable(folder):
    """True if we can create + delete a scratch file inside `folder`."""
    probe = Path(folder) / ".preflight-write-probe.tmp"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def check_data_dir(data_dir):
    """Required: data folder exists AND is writable (memory/settings live there)."""
    data_dir = Path(data_dir)
    why = (
        "The data folder holds JARVIS's memory, settings and keys. "
        "If JARVIS cannot write there, it forgets everything between runs."
    )
    if not data_dir.is_dir():
        return _result(
            "Data folder",
            LEVEL_REQUIRED,
            False,
            f"data folder not found at {data_dir}",
            why,
            FIX_DATA_MISSING,
        )
    if not _probe_writable(data_dir):
        return _result(
            "Data folder",
            LEVEL_REQUIRED,
            False,
            f"data folder at {data_dir} is not writable",
            why,
            FIX_DATA_UNWRITABLE,
        )
    return _result(
        "Data folder",
        LEVEL_REQUIRED,
        True,
        f"data folder ready and writable at {data_dir}",
        why,
    )


def _default_ollama_probe():
    """(ok, detail) — asks the local Ollama service if it is alive."""
    try:
        req = urllib.request.Request(OLLAMA_TAGS_URL, method="GET")
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT_S) as resp:
            if resp.status == 200:
                return True, "Ollama is running (offline brain available)"
            return False, f"Ollama answered with HTTP {resp.status}"
    except Exception as exc:  # refused / timeout / no service — all just "not there"
        return False, f"Ollama is not answering ({type(exc).__name__})"


def check_ollama(probe=None):
    """Optional: local Ollama service for the free offline brain."""
    if probe is None:
        probe = _default_ollama_probe
    try:
        ok, detail = probe()
    except Exception as exc:  # a broken probe must never crash preflight
        ok, detail = False, f"Ollama check failed ({type(exc).__name__})"
    return _result(
        "Ollama (offline brain)",
        LEVEL_OPTIONAL,
        ok,
        detail,
        "Ollama lets JARVIS think on your own PC for free, with no internet. "
        "Nice to have — JARVIS can also use its other brains.",
        "" if ok else FIX_OLLAMA,
    )


def check_models_dir(models_dir):
    """Optional: models folder (wake-word .onnx files live there)."""
    models_dir = Path(models_dir)
    ok = models_dir.is_dir()
    detail = (
        f"models folder found at {models_dir}"
        if ok
        else f"models folder not found at {models_dir}"
    )
    return _result(
        "Wake-word models folder",
        LEVEL_OPTIONAL,
        ok,
        detail,
        "The wake-word model lives here so JARVIS can hear 'Jarvis' "
        "hands-free. Without it you start JARVIS by clicking.",
        "" if ok else FIX_MODELS,
    )


def _default_spec_finder(module_name):
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


def check_optional_package(name, module, why, fix, spec_finder=None):
    """Optional: a third-party package a later step wants (voice packs)."""
    if spec_finder is None:
        spec_finder = _default_spec_finder
    try:
        ok = bool(spec_finder(module))
    except Exception:
        ok = False
    detail = f"{module} is installed" if ok else f"{module} is not installed"
    return _result(name, LEVEL_OPTIONAL, ok, detail, why, "" if ok else fix)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def default_root():
    """Repo root = the folder that contains the `jarvis` package."""
    return Path(__file__).resolve().parent.parent


def run_checks(root=None, data_dir=None, models_dir=None, *,
               ollama_probe=None, spec_finder=None, python_version=None):
    """Run every check and return a list of result dicts. Never raises."""
    root = Path(root) if root is not None else default_root()
    data_dir = Path(data_dir) if data_dir is not None else root / "data"
    models_dir = Path(models_dir) if models_dir is not None else root / "models"
    return [
        check_python_version(python_version),
        check_package(root),
        check_data_dir(data_dir),
        check_ollama(ollama_probe),
        check_models_dir(models_dir),
        check_optional_package(
            "Voice typing pack (faster-whisper)",
            "faster_whisper",
            "faster-whisper turns your voice into text on your own PC. "
            "Without it, JARVIS is text-only for now.",
            FIX_WHISPER,
            spec_finder,
        ),
        check_optional_package(
            "Kokoro voice pack",
            "kokoro",
            "Kokoro is JARVIS's natural speaking voice. Without it, JARVIS "
            "can still reply in text (and other voices arrive in later steps).",
            FIX_KOKORO,
            spec_finder,
        ),
    ]


def exit_code(results):
    """0 when every REQUIRED check passed, 1 otherwise. Optionals never block."""
    return 1 if any(
        r["level"] == LEVEL_REQUIRED and not r["ok"] for r in results
    ) else 0


def summarize(results):
    required_failed = [r for r in results
                       if r["level"] == LEVEL_REQUIRED and not r["ok"]]
    optional_missing = [r for r in results
                        if r["level"] == LEVEL_OPTIONAL and not r["ok"]]
    return {
        "ready": not required_failed,
        "required_failed": len(required_failed),
        "optional_missing": len(optional_missing),
        "total": len(results),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render_checklist(results):
    lines = ["JARVIS v4 — preflight check", "-" * 40]
    for r in results:
        if r["ok"]:
            tag = "OK      "
        elif r["level"] == LEVEL_REQUIRED:
            tag = "MISSING "
        else:
            tag = "OPTIONAL"
        lines.append(f"[{tag}] {r['name']} — {r['detail']}")
        if not r["ok"] and r["next_fix"]:
            lines.append(f"    Next: {r['next_fix']}")
    s = summarize(results)
    lines.append("-" * 40)
    if s["ready"]:
        extra = (f" ({s['optional_missing']} optional extra(s) not available — "
                 "JARVIS starts anyway)") if s["optional_missing"] else ""
        lines.append(f"Result: READY{extra}")
    else:
        lines.append(
            f"Result: NOT READY — {s['required_failed']} required item(s) "
            "missing. Follow the 'Next:' step(s) above, then start JARVIS again."
        )
    return "\n".join(lines)


def render_explain(results):
    lines = [
        "JARVIS v4 — what I checked, in plain words",
        "=" * 44,
        "",
        "Before JARVIS starts, it checks that everything it needs is in",
        "place. 'Required' items must be there. 'Optional' items are extra",
        "powers — JARVIS starts fine without them.",
        "",
    ]
    for r in results:
        status = "working" if r["ok"] else "not there yet"
        lines.append(f"* {r['name']} ({r['level']}) — {status}")
        lines.append(f"  Why it matters: {r['why']}")
        lines.append(f"  What I found: {r['detail']}")
        if not r["ok"] and r["next_fix"]:
            lines.append(f"  What to do: {r['next_fix']}")
        lines.append("")
    s = summarize(results)
    if s["ready"]:
        lines.append("Good news: everything JARVIS needs is in place. "
                     "You can start JARVIS now.")
    else:
        lines.append("JARVIS cannot start yet. Do the 'What to do' step(s) "
                     "above — each one is the exact fix — then start again.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="jarvis.preflight",
        description="Check that everything JARVIS v4 needs is in place.",
    )
    parser.add_argument("--explain", action="store_true",
                        help="plain-language explanation of every check")
    parser.add_argument("--json", action="store_true",
                        help="print results as JSON")
    parser.add_argument("--root", type=Path, default=None,
                        help="JARVIS folder (default: this repo)")
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="data folder (default: <root>/data)")
    parser.add_argument("--models-dir", type=Path, default=None,
                        help="models folder (default: <root>/models)")
    args = parser.parse_args(argv)

    results = run_checks(root=args.root, data_dir=args.data_dir,
                         models_dir=args.models_dir)
    if args.json:
        payload = {"checks": results, "summary": summarize(results),
                   "python": platform.python_version()}
        print(json.dumps(payload, indent=2))
    elif args.explain:
        print(render_explain(results))
    else:
        print(render_checklist(results))
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
