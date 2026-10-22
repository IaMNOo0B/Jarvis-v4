"""Wake-word gate — Mark-LVI's semantics over openWakeWord.

- OFF by default (the repo's default, and the owner's machine is
  his to open): with the gate off, every mic block streams.
- ON and asleep: mic audio NEVER leaves the machine — blocks go
  only to the local detector. Detection wakes for one conversation;
  120 s without user speech puts it back to sleep.
- The detector's inference runs on its OWN daemon thread (a queue
  between the audio callback and the model) — the callback thread
  must never wait on inference (its one-time import alone cost the
  repo a measured 2.1 s).
- openWakeWord is an opt-in install (setup offers it); enabling the
  gate without it answers honestly instead of pretending to listen.
"""

from __future__ import annotations

import importlib.util
import queue
import threading
import time
from pathlib import Path
from typing import Callable, Optional

WAKE_MODEL = "hey_jarvis"
DEFAULT_THRESHOLD = 0.5
SLEEP_TIMEOUT_S = 120.0


class WakeUnavailable(Exception):
    pass


# ---------------------------------------------------------------------------
# Wake-model resolution — ONE search, used by the detector AND
# by preflight/diagnose. The old preflight check tested only
# <install>/models — a folder the detector never looked in —
# so a fresh ZIP extraction failed the check while the model
# sat bundled in the openwakeword package all along (live
# /diagnose, 2026-10-22). The search order:
#   1. an explicit .onnx path (the owner's trained model)
#   2. the app models dir (<install>/models)
#   3. the openwakeword package's own resources/models
# A bundled name matches "<name>.onnx" or a versioned
# "<name>_v*.onnx" in any of those dirs.
# ---------------------------------------------------------------------------

def _app_models_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "models"


def _package_models_dir() -> Optional[Path]:
    try:
        spec = importlib.util.find_spec("openwakeword")
    except Exception:
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(list(spec.submodule_search_locations)[0]) \
        / "resources" / "models"


def _search_dirs(models_dir=None):
    dirs = []
    if models_dir:
        dirs.append(Path(models_dir))
    dirs.append(_app_models_dir())
    pkg = _package_models_dir()
    if pkg is not None:
        dirs.append(pkg)
    return dirs


def resolve_wake_model(model: str = "",
                       models_dir=None) -> Optional[str]:
    """The .onnx path for a wake model name/path, or None."""
    requested = str(model or "").strip()
    if requested and Path(requested).is_file():
        return requested
    stem = Path(requested).stem if requested else WAKE_MODEL
    for folder in _search_dirs(models_dir):
        exact = folder / f"{stem}.onnx"
        if exact.is_file():
            return str(exact)
        try:
            versioned = sorted(folder.glob(f"{stem}*.onnx"))
        except OSError:
            versioned = []
        if versioned:
            return str(versioned[0])
    return None


def wake_model_status(model: str = "",
                      models_dir=None) -> dict:
    """Where the wake model was found (or everywhere that was
    searched) — preflight and /diagnose report from THIS, so
    the check can never disagree with the detector again."""
    found = resolve_wake_model(model, models_dir)
    return {"found": found is not None, "path": found,
            "searched": [str(d) for d in _search_dirs(models_dir)]}


_download_attempted = False


def ensure_wake_model(model: str = "",
                      models_dir=None) -> Optional[str]:
    """Resolve, and on a miss make ONE guarded attempt to
    download the bundled models (first voice start). Offline
    or package-less: returns None and the caller degrades
    honestly — never a crash, never a pretend success."""
    global _download_attempted
    found = resolve_wake_model(model, models_dir)
    if found is not None or _download_attempted:
        return found
    _download_attempted = True
    try:
        from openwakeword.utils import download_models
    except Exception:
        return None
    target = Path(models_dir) if models_dir else _app_models_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    stem = Path(str(model or "").strip()).stem or WAKE_MODEL
    try:
        try:
            download_models(model_names=[stem],
                            target_directory=str(target))
        except TypeError:
            download_models()
    except Exception:
        return None
    return resolve_wake_model(model, models_dir)


class WakeGate:
    def __init__(self, enabled: bool = False, detector=None,
                 clock: Callable[[], float] = time.monotonic,
                 on_wake: Optional[Callable[[], None]] = None):
        self.enabled = bool(enabled)
        self._detector = detector
        self._clock = clock
        self._on_wake = on_wake
        self.awake = not self.enabled
        self._last_speech = 0.0

    def feed(self, pcm: bytes) -> bool:
        """One mic block. True = stream it to the session;
        False = it stays on this machine (detector food only)."""
        if not self.enabled:
            return True
        if not self.awake:
            if self._detector is not None:
                self._detector.feed(pcm)
            return False
        return True

    def stop(self):
        """Stop the detector behind the gate (voice shutdown):
        its inference thread joins instead of leaking."""
        detector = self._detector
        if detector is not None and hasattr(detector, "stop"):
            try:
                detector.stop()
            except Exception:
                pass

    def note_user_speech(self):
        self._last_speech = self._clock()

    def wake(self):
        if not self.awake:
            self.awake = True
            self._last_speech = self._clock()
            if self._on_wake:
                self._on_wake()

    def sleep(self):
        if self.enabled:
            self.awake = False

    def check_sleep(self) -> bool:
        """Auto-sleep after SLEEP_TIMEOUT_S without user speech.
        Returns True when this call put the gate to sleep."""
        if self.enabled and self.awake and self._last_speech \
                and self._clock() - self._last_speech > SLEEP_TIMEOUT_S:
            self.awake = False
            return True
        return False


class OpenWakeWordDetector:
    """The real detector: openWakeWord's hey_jarvis ONNX model,
    inference on a daemon thread fed by a bounded queue."""

    def __init__(self, on_detect: Callable[[], None],
                 threshold: float = DEFAULT_THRESHOLD,
                 model: str = ""):
        try:
            from openwakeword.model import Model
        except Exception as e:
            raise WakeUnavailable(
                "openwakeword is not installed — the voice pack in "
                f"setup.bat installs it ({e})") from e
        # A custom wake phrase is a trained model: config
        # voice.wake_model names a bundled model ("hey_jarvis",
        # "alexa", ...) or a path to the owner's own .onnx —
        # his 'hello jarvis' plan rides exactly here. The name
        # is resolved to a real .onnx PATH first (app models
        # dir, then the package's own resources, one download
        # attempt on a miss) — passing a bare name into a fresh
        # install found nothing and the wake word was dead.
        requested = str(model).strip()
        resolved = ensure_wake_model(requested)
        try:
            self._model = Model(
                wakeword_models=[resolved or requested
                                 or WAKE_MODEL],
                inference_framework="onnx")
        except Exception as e:
            raise WakeUnavailable(
                f"wake model '{requested or WAKE_MODEL}' mili "
                f"nahi / load nahi hui ({e}) — models folder ya "
                "openwakeword ke bundled models me .onnx chahiye"
            ) from e
        self._threshold = float(threshold)
        self._on_detect = on_detect
        self._queue: queue.Queue = queue.Queue(maxsize=50)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop,
                                        daemon=True,
                                        name="jarvis-wakeword")
        self._thread.start()

    def stop(self):
        """End the inference thread (audit 2026-10-22: the
        loop was `while True` with no exit — every voice
        restart leaked one immortal thread holding an ONNX
        model)."""
        self._stop.set()
        if getattr(self, "_thread", None) is not None:
            self._thread.join(timeout=2.0)

    def feed(self, pcm: bytes):
        try:
            self._queue.put_nowait(pcm)
        except queue.Full:
            pass    # frames drop when backed up — never block audio

    def _loop(self):
        import numpy as np
        while not self._stop.is_set():
            try:
                pcm = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                frame = np.frombuffer(pcm, dtype=np.int16)
                scores = self._model.predict(frame)
            except Exception:
                continue
            if any("jarvis" in str(k).lower()
                   and float(v) >= self._threshold
                   for k, v in scores.items()):
                # Drain so one utterance cannot double-fire.
                while not self._queue.empty():
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        break
                self._on_detect()
