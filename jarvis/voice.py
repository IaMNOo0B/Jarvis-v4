"""Gemini Live voice — the Mark-LVI architecture, rebuilt for v4.

What was taken from FatihMakes/Mark-LVI (studied file-by-file at the
owner's order, 2026-10-21), and how it maps onto this codebase:

- One long-lived Live WebSocket session owns the conversation; the
  mic streams raw PCM16 16 kHz (mime audio/pcm) as realtimeInput, the
  model answers in 24 kHz PCM, sliced into ~50 ms pieces BEFORE
  queueing so an interrupt can bite within one slice.
- The server VAD decides turn boundaries (no activity_start/end is
  ever sent); tuning fields exist but ride OFF by default, exactly
  like the repo — clipped turns made its replies arrive a turn late.
- The system prompt travels as systemInstruction; the SAME gated
  tools the chat brain uses are declared as functionDeclarations and
  executed through the app's dispatch, results returned as plain
  strings in toolResponse.
- Interrupt is a hard drain: incoming audio is discarded until the
  turn completes, and NOTHING of the cut answer is logged or kept.
- Transcriptions are buffered per turn (with the repo's repeat-tail
  guard), flushed on turnComplete, and kept in RAM; at session end
  (>= 3 turns) the owner-approved summary hook may persist one line.
- The Live ladder has two rungs and steps down ONLY on quota (429,
  rest 300 s) or a gone model (404/403/not-found, rest 6 h) — a bad
  key or a network wobble hits the same wall on every rung, so those
  never move the ladder.
- Resumption handles live in RAM only, never on disk — a fresh
  launch must never silently continue yesterday's conversation.
- A stall watchdog (30 s, armed by sends, cleared by ANY server
  traffic) reconnects a half-dead socket; rejected config sheds its
  tuning/proactive flags one at a time instead of dying.

The model names are the repo's own claims and are NOT independently
verified — they are config-overridable (voice.live_models) so a name
can be corrected without a code change.
"""

from __future__ import annotations

import base64
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from .live_ws import WSClosed, WSConnection, WSError
from .providers import _gemini_payload

LIVE_MODELS = (
    "models/gemini-3.1-flash-live-preview",
    "models/gemini-2.5-flash-native-audio-preview-12-2025",
)
AVAILABLE_VOICES = ("Charon", "Puck", "Kore", "Fenrir", "Aoede")
DEFAULT_VOICE = "Charon"
SEND_SAMPLE_RATE = 16000
RECEIVE_SAMPLE_RATE = 24000
AUDIO_SLICE_BYTES = 2400          # ~50 ms of 24 kHz int16 mono
STALL_LIMIT_S = 30.0

_COOLDOWNS: dict = {}
_COOLDOWN_LOCK = threading.Lock()


def valid_voice(name) -> str:
    """A stored voice the API does not know must never reach it."""
    return name if name in AVAILABLE_VOICES else DEFAULT_VOICE


def note_live_failure(model: str, err) -> bool:
    """Classify a Live failure. True = the ladder stepped down (the
    rung is resting); False = stay — the wall is the same everywhere
    (bad key, network) or transient (503: back off, do not step)."""
    text = str(err).lower()
    now = time.monotonic()
    with _COOLDOWN_LOCK:
        if "429" in text or "resource_exhausted" in text:
            _COOLDOWNS[model] = now + 300.0
            return True
        if any(k in text for k in ("404", "not found",
                                   "is not supported", "permission",
                                   "403")):
            _COOLDOWNS[model] = now + 6 * 3600.0
            return True
    return False


def live_model(models=LIVE_MODELS) -> str:
    """First rung not resting; if all rest, the top anyway —
    refusing to connect at all is never the better answer."""
    now = time.monotonic()
    with _COOLDOWN_LOCK:
        for model in models:
            if _COOLDOWNS.get(model, 0.0) <= now:
                return model
    return models[0]


def build_setup(*, model: str, system_instruction: str, tools=None,
                voice: str = DEFAULT_VOICE, resume_handle=None,
                proactive: bool = False, tuning: Optional[dict] = None,
                thinking: bool = False,
                thinking_level: Optional[str] = None) -> dict:
    """The setup message, mirroring Mark-LVI's _build_config —
    tuning rides separately so a rejected preview field can be shed
    wholesale on reconnect."""
    decls = _gemini_payload([], tools).get("tools", [])
    setup: dict = {
        "model": model,
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {
                "voiceName": valid_voice(voice)}}},
        },
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "inputAudioTranscription": {},
        "outputAudioTranscription": {},
        "sessionResumption": {"handle": resume_handle},
        "contextWindowCompression": {"slidingWindow": {}},
    }
    if decls:
        setup["tools"] = decls
    if proactive:
        setup["proactivity"] = {"proactiveAudio": True}
    if thinking_level in ("low", "medium", "high"):
        # The thinking variant's routing knob (Google's Live
        # guidance): fast rung thinks low, hard turns think high.
        setup["thinkingConfig"] = {"thinkingLevel": thinking_level}
    elif thinking:
        setup["thinkingConfig"] = {"thinkingBudget": -1}
    if tuning:
        detect = {}
        if tuning.get("silence_ms"):
            detect["silenceDurationMs"] = int(tuning["silence_ms"])
        if tuning.get("prefix_ms"):
            detect["prefixPaddingMs"] = int(tuning["prefix_ms"])
        if tuning.get("end_sensitivity") == "high":
            detect["endOfSpeechSensitivity"] = "END_SENSITIVITY_HIGH"
        elif tuning.get("end_sensitivity") == "low":
            detect["endOfSpeechSensitivity"] = "END_SENSITIVITY_LOW"
        if tuning.get("start_sensitivity") == "high":
            detect["startOfSpeechSensitivity"] = \
                "START_SENSITIVITY_HIGH"
        elif tuning.get("start_sensitivity") == "low":
            detect["startOfSpeechSensitivity"] = \
                "START_SENSITIVITY_LOW"
        if detect:
            setup["realtimeInputConfig"] = {
                "automaticActivityDetection": detect}
    return {"setup": setup}


class VoiceEvents:
    """No-op sink — the voice manager overrides what it needs."""

    def on_audio(self, pcm: bytes):
        pass

    def on_turn_audio_end(self):
        pass

    def on_transcript(self, role: str, text: str):
        pass

    def on_session(self, connected: bool, detail: str = ""):
        pass

    def on_interrupted(self):
        pass


class _TranscriptBuffer:
    """Per-turn transcript with the repo's repeat-tail guard: the
    API re-sends the transcript tail across a tool-using turn's
    several completions, and a naive join prints it twice."""

    def __init__(self):
        self.text = ""
        self._last = ""

    def add(self, chunk: str):
        if not chunk:
            return
        if chunk == self._last or self.text.endswith(chunk):
            return
        self._last = chunk
        self.text += chunk

    def flush(self) -> str:
        out, self.text, self._last = self.text.strip(), "", ""
        return out

    def clear(self):
        self.text, self._last = "", ""

    def heard(self, fraction) -> str:
        """The heard prefix of the buffered text: `fraction` of the
        characters, cut back to a word boundary. No measurement
        (None) or a zero fraction keeps nothing; >= 1 keeps all."""
        text = self.text.strip()
        self.clear()
        if not text or fraction is None:
            return ""
        if fraction >= 1.0:
            return text
        if fraction <= 0.0:
            return ""
        piece = text[:int(len(text) * fraction)]
        if " " in piece:
            piece = piece[:piece.rfind(" ")].rstrip(" ,;:")
        return piece.strip()


class _Reconnect(Exception):
    def __init__(self, keep_context: bool = True):
        super().__init__("reconnect")
        self.keep_context = keep_context


_RETRYABLE_HINTS = ("rate", "429", "timeout", "timed out", "503",
                    "502", "unavailable", "temporarily", "try again")


def _retryable(message: str) -> bool:
    low = str(message).lower()
    return any(hint in low for hint in _RETRYABLE_HINTS)


class LiveSession:
    """One voice conversation. Threads: a run thread owns connect /
    receive / reconnect; send_audio / send_text / interrupt are safe
    to call from any thread (the mic callback, the UI, plugins)."""

    def __init__(self, *, api_key: str,
                 dispatch: Callable[[str, dict], object],
                 events: Optional[VoiceEvents] = None,
                 system_instruction: str = "",
                 tools=None, voice: str = DEFAULT_VOICE,
                 models=LIVE_MODELS, proactive: bool = True,
                 tuning: Optional[dict] = None,
                 thinking_level: Optional[str] = None,
                 destructive_tools=frozenset(),
                 ws_factory: Optional[Callable[[str], object]] = None,
                 on_session_end: Optional[Callable[[list], None]] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.api_key = api_key
        self.dispatch = dispatch
        self.events = events or VoiceEvents()
        self.system_instruction = system_instruction
        self.tools = tools
        self.voice = valid_voice(voice)
        self.models = tuple(models)
        self.tuning = tuning
        self.thinking_level = thinking_level
        self._destructive_tools = frozenset(destructive_tools)
        self._ws_factory = ws_factory
        self._on_session_end = on_session_end
        self._clock = clock

        self._enhanced = bool(proactive)   # one-way downgrade flags,
        self._tuned = tuning is not None   # exactly like the repo
        self._api_version = "v1alpha" if proactive else "v1beta"

        self._ws = None
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._resume_handle = None
        self._resumed_with = None
        self._interrupted = False
        self._pending_said = False   # a finished turn whose
                                     # transcript is NOT yet
                                     # committed — playback may
                                     # still be behind
        self._in_buf = _TranscriptBuffer()
        self._out_buf = _TranscriptBuffer()
        self._turns: list = []
        self._rx_deadline: Optional[float] = None
        self.connected = False
        self.last_user_speech = 0.0

    # -- public control ---------------------------------------------------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="jarvis-live")
        self._thread.start()

    def stop(self):
        # Commit whatever of the pending reply was actually
        # HEARD before the session closes — no more, no less.
        if self._pending_said:
            self._commit_said(self._probe_heard())
        self._stop.set()
        self._close_ws()
        if self._on_session_end and len(self._turns) >= 3:
            try:
                self._on_session_end(list(self._turns))
            except Exception:
                pass

    def send_audio(self, pcm: bytes):
        """One mic block of raw PCM16 16 kHz. Gating (speaking, echo
        tail, wake, mute) is the caller's job — this only speaks."""
        self._send({"realtimeInput": {"audio": {
            "data": base64.b64encode(pcm).decode(),
            "mimeType": "audio/pcm;rate=16000"}}})
        # Audio sends arm the stall watchdog too (audit
        # 2026-10-22: only send_text did — a half-dead socket
        # after speech never tripped the deadline, so the UI
        # showed "connected" while JARVIS was deaf).
        self._arm_stall()

    def send_text(self, text: str):
        self._send({"clientContent": {
            "turns": [{"role": "user", "parts": [{"text": text}]}],
            "turnComplete": True}})
        self._arm_stall()

    def interrupt(self, heard_fraction=None):
        """Hard drain: what is queued stops, what still arrives is
        discarded. The cut answer is remembered only up to what was
        actually HEARD — OpenAI's truncate rule, and the owner's
        own doctrine (record what was spoken, not what was
        generated): when the playback side reports a heard fraction
        (played bytes / sent bytes), that prefix of the transcript
        is logged and the unheard tail dropped; with no measurement
        nothing is logged (the repo's original rule)."""
        self._interrupted = True
        self._in_buf.clear()
        if heard_fraction is None:
            heard_fraction = self._probe_heard()
        self._commit_said(heard_fraction)
        self.events.on_interrupted()

    def _probe_heard(self):
        probe = getattr(self.events, "heard_fraction", None)
        if callable(probe):
            try:
                return probe()
            except Exception:
                return None
        return None

    def _commit_said(self, fraction):
        """The ONLY door by which a spoken reply reaches
        history/memory: the prefix that was actually HEARD
        (the owner's doctrine — record what was spoken, not
        what was generated)."""
        self._pending_said = False
        said = self._out_buf.heard(fraction)
        if said:
            self._turns.append(("assistant", said))
            self.events.on_transcript("assistant", said)

    def on_playback_drained(self):
        """Manager signal: the play queue emptied — everything
        generated for this turn was heard, so the pending
        transcript commits WHOLE. Until this fires, the
        transcript waits: an interrupt after turnComplete can
        still cut it to the played prefix (audit 2026-10-22:
        turnComplete used to flush the full text instantly,
        so seconds of unplayed audio were already 'history'
        and an interrupt found nothing to truncate)."""
        if self._pending_said:
            self._commit_said(1.0)

    # -- plumbing -----------------------------------------------------------

    def _close_ws(self):
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _send(self, payload: dict):
        ws = self._ws
        if ws is None or self._stop.is_set():
            return
        with self._send_lock:
            try:
                ws.send_text(json.dumps(payload))
            except WSError:
                pass

    def _arm_stall(self):
        if self._rx_deadline is None:
            self._rx_deadline = self._clock() + STALL_LIMIT_S

    def _url(self) -> str:
        return ("wss://generativelanguage.googleapis.com/ws/"
                "google.ai.generativelanguage."
                f"{self._api_version}.GenerativeService."
                f"BidiGenerateContent?key={self.api_key}")

    # -- run loop -----------------------------------------------------------

    def _run(self):
        backoff = 0.0
        while not self._stop.is_set():
            model = live_model(self.models)
            try:
                self._connect_and_serve(model)
                backoff = 0.0
            except _Reconnect as r:
                if not r.keep_context:
                    self._resume_handle = None
                backoff = 0.0
            except (WSError, OSError) as e:
                backoff = self._classify_failure(model, str(e), backoff)
            except Exception as e:  # a voice loop must not die quietly
                backoff = self._classify_failure(model, str(e), backoff)
            finally:
                self.connected = False
                self._close_ws()
            if self._stop.is_set():
                break
            if backoff:
                self._stop.wait(backoff)

    def _classify_failure(self, model: str, err: str,
                          backoff: float) -> float:
        """Mark-LVI's error ladder, in its order."""
        low = err.lower()
        if self._resumed_with and any(
                k in low for k in ("resum", "handle",
                                   "invalid_argument", "not_found")):
            # The feature meant to survive a reconnect must not be
            # the thing preventing one: drop the handle, retry fresh.
            self._resume_handle = None
            self._resumed_with = None
            return 0.0
        if note_live_failure(model, err):
            return 0.0 if live_model(self.models) != model else 15.0
        if self._tuned and ("invalid_argument" in low
                            or "unknown name" in low
                            or "realtime_input" in low):
            self._tuned = False
            return 0.0
        if self._enhanced and ("invalid_argument" in low
                               or "proactiv" in low
                               or "unknown name" in low):
            self._enhanced = False
            self._api_version = "v1beta"
            return 0.0
        if "api key not valid" in low \
                or "api_key_invalid" in low \
                or "invalid_api_key" in low \
                or "api key invalid" in low \
                or "invalid api key" in low \
                or ("key" in low and ("400" in low
                                      or "403" in low)):
            # A rejected key never heals by retrying (audit
            # 2026-10-22: only the one exact phrase was
            # recognized; every other auth shape retried
            # forever against a dead key).
            self.events.on_session(False, "bad key")
            self._stop.set()
            return 0.0
        # Network class: 3 s first, doubling to a 60 s ceiling.
        return 3.0 if backoff <= 0 else min(backoff * 2, 60.0)

    def _connect_and_serve(self, model: str):
        if self._ws_factory is not None:
            ws = self._ws_factory(self._url())
        else:
            ws = WSConnection.connect(self._url(), timeout=20.0)
        ws.set_recv_timeout(2.0)
        self._ws = ws
        self._resumed_with = self._resume_handle
        setup = build_setup(
            model=model, system_instruction=self.system_instruction,
            tools=self.tools, voice=self.voice,
            resume_handle=self._resume_handle,
            proactive=self._enhanced,
            tuning=self.tuning if self._tuned else None,
            thinking_level=self.thinking_level)
        ws.send_text(json.dumps(setup))
        while not self._stop.is_set():
            try:
                _op, raw = ws.recv()
            except socket.timeout:
                if self._rx_deadline is not None \
                        and self._clock() > self._rx_deadline:
                    raise _Reconnect(keep_context=True)
                continue
            self._rx_deadline = None     # any traffic = path alive
            try:
                msg = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            self._handle(msg)

    # -- message handling ---------------------------------------------------

    def _handle(self, msg: dict):
        if "setupComplete" in msg:
            self.connected = True
            self.events.on_session(True, self.voice)
        update = msg.get("sessionResumptionUpdate")
        if isinstance(update, dict) and update.get("resumable") \
                and update.get("newHandle"):
            self._resume_handle = update["newHandle"]
        content = msg.get("serverContent")
        if isinstance(content, dict):
            self._handle_server_content(content)
        call = msg.get("toolCall")
        if isinstance(call, dict):
            self._handle_tool_call(call)
        if "goAway" in msg:
            raise _Reconnect(keep_context=True)

    def _handle_server_content(self, content: dict):
        if content.get("interrupted"):
            self.interrupt()
        turn = content.get("modelTurn") or {}
        for part in turn.get("parts") or []:
            data = (part.get("inlineData") or {}).get("data")
            if data and not self._interrupted:
                pcm = base64.b64decode(data)
                for i in range(0, len(pcm), AUDIO_SLICE_BYTES):
                    self.events.on_audio(pcm[i:i + AUDIO_SLICE_BYTES])
        in_tr = content.get("inputTranscription") or {}
        if in_tr.get("text"):
            # A NEW user turn ends any interrupt episode: the
            # flag must not eat the next turn's audio (audit
            # 2026-10-22: it only cleared at turnComplete).
            if self._interrupted:
                self._interrupted = False
            if self._pending_said:
                # The owner spoke over the tail of the last
                # reply — commit only what was heard by now.
                self._commit_said(self._probe_heard())
            self._in_buf.add(str(in_tr["text"]))
            self.last_user_speech = self._clock()
        out_tr = content.get("outputTranscription") or {}
        if out_tr.get("text"):
            self._out_buf.add(str(out_tr["text"]))
        if content.get("turnComplete"):
            self.events.on_turn_audio_end()
            if self._interrupted:
                self._interrupted = False
                self._in_buf.clear()
                self._out_buf.clear()
                self._pending_said = False
                return
            heard = self._in_buf.flush()
            if heard:
                self._turns.append(("user", heard))
                self.events.on_transcript("user", heard)
            # The assistant's words are NOT committed yet:
            # they wait in the buffer until playback drains
            # (on_playback_drained) or an interrupt cuts them
            # to the heard prefix.
            if self._out_buf.text.strip():
                self._pending_said = True

    def _run_one_tool(self, fn: dict) -> dict:
        """One tool call -> one STRUCTURED function response
        (Google's developer-guide shape): status / retryable /
        message alongside the result. An empty or bare response
        invites the model to repeat the call; a structured one
        tells it exactly what happened."""
        name = str(fn.get("name") or "")
        args = fn.get("args") or {}
        try:
            result = self.dispatch(name, args)
        except Exception as e:  # a tool must never kill the call
            result = {"error": f"Tool '{name}' failed: {e}"}
        if isinstance(result, dict) and result.get("error"):
            message = str(result["error"])
            payload = {"result": message, "status": "error",
                       "retryable": _retryable(message),
                       "message": message}
        else:
            text = result if isinstance(result, str) \
                else json.dumps(result, ensure_ascii=False,
                                default=str)
            payload = {"result": text, "status": "ok",
                       "retryable": False, "message": "done"}
        return {"id": fn.get("id"), "name": name,
                "response": payload}

    def _handle_tool_call(self, call: dict):
        fns = call.get("functionCalls") or []
        # Parallel execution (the GPT-Realtime/Gemini pattern):
        # independent calls in one model turn run together and
        # their responses return in ONE toolResponse. A batch
        # containing a destructive tool stays sequential — order
        # and the idempotency guard matter more than speed there.
        if len(fns) > 1 and not any(
                str(fn.get("name") or "") in self._destructive_tools
                for fn in fns):
            with ThreadPoolExecutor(
                    max_workers=min(4, len(fns))) as pool:
                responses = list(pool.map(self._run_one_tool, fns))
        else:
            responses = [self._run_one_tool(fn) for fn in fns]
        if responses:
            self._send({"toolResponse": {"functionResponses":
                                        responses}})
            self._arm_stall()
