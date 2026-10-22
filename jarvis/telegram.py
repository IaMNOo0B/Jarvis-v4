"""Telegram channel — JARVIS v4, Wave 4 (Thoth's messaging ask,
built on the v2 Phase-1 design).

The phone reaches the PC, never the other way round: the app
long-polls Telegram's Bot API outward, so no port forwarding and
no public endpoint exist. Three safety shapes, all inherited:

1. The bot token lives in the SecretStore, filed through the
   in-app /telegram command — never in a config file, never
   echoed back.
2. Binding takes a PAIR CODE: /telegram pair shows a short-lived
   code in the app; only a phone that sends that exact code
   becomes the owner's chat. A stranger who finds the bot's
   username gets silence.
3. Only the bound chat is served. Its messages run the same
   ChatApp turn as the UI — same brain, same tools, same gate —
   and the reply goes back to that chat, chunked to Telegram's
   message limit.

v1 scope, stated plainly: text messages. Voice notes get an
honest reply, not a guess.
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

_API = "https://api.telegram.org/bot{token}/{method}"
_CHUNK = 3900


def _default_transport(token: str, method: str,
                       params: dict) -> dict:
    url = _API.format(token=token, method=method)
    body = json.dumps(params).encode("utf-8")
    request = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=35) as response:
        return json.loads(response.read().decode("utf-8"))


class TelegramBot:
    """The Bot API, transport injectable for tests."""

    def __init__(self, token: str, transport=None):
        self.token = token
        self._transport = transport or _default_transport

    def call(self, method: str, **params) -> dict:
        return self._transport(self.token, method, params)

    def get_updates(self, offset: int = 0,
                    timeout: int = 25) -> list:
        out = self.call("getUpdates", offset=offset,
                        timeout=timeout,
                        allowed_updates=["message"])
        return out.get("result") or []

    def send_message(self, chat_id, text: str) -> None:
        for i in range(0, max(1, len(text)), _CHUNK):
            self.call("sendMessage", chat_id=chat_id,
                      text=text[i:i + _CHUNK])


# ---------------------------------------------------------------------------
# Binding state (which chat is the owner's)
# ---------------------------------------------------------------------------

def _state_path(data_dir) -> Path:
    return Path(data_dir) / "telegram.json"


def load_state(data_dir) -> dict:
    path = _state_path(data_dir)
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            return state if isinstance(state, dict) else {}
        except Exception:
            return {}
    return {}


def save_state(data_dir, state: dict) -> None:
    try:
        _state_path(data_dir).write_text(
            json.dumps(state), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# The channel: pure update processing (thread lives in chat.py)
# ---------------------------------------------------------------------------

class TelegramChannel:
    """Processes Bot API updates against a handle(text) -> reply
    callable. Pairing codes are supplied by the app (RAM-only,
    expiring); this class just checks them."""

    def __init__(self, bot: TelegramBot, data_dir,
                 handle: Callable[[str], str],
                 pair_code: Callable[[], Optional[str]],
                 notify_owner: Optional[Callable] = None,
                 journal=None):
        self.bot = bot
        self.data_dir = data_dir
        self.handle = handle
        self.pair_code = pair_code
        self.notify_owner = notify_owner
        self.journal = journal
        self.offset = 0

    def _log(self, event: str, payload: dict) -> None:
        if self.journal is not None:
            try:
                self.journal.append(event, payload)
            except Exception:
                pass

    def poll_once(self) -> int:
        """Fetch and process one batch; returns how many messages
        were handled. Never raises."""
        try:
            updates = self.bot.get_updates(offset=self.offset)
        except Exception:
            return 0
        handled = 0
        for update in updates:
            self.offset = max(self.offset,
                              int(update.get("update_id", 0)) + 1)
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            text = message.get("text")
            if chat_id is None:
                continue
            if text is None:
                if load_state(self.data_dir).get("chat_id") \
                        == chat_id:
                    self._safe_send(
                        chat_id,
                        "Sir, phone se abhi sirf text samajhta "
                        "hoon — voice note ka jawab dena abhi "
                        "nahi aata.")
                continue
            handled += self._serve(chat_id, str(text),
                                   chat.get("first_name", ""))
        return handled

    def _safe_send(self, chat_id, text: str) -> None:
        try:
            self.bot.send_message(chat_id, text)
        except Exception:
            pass

    def _serve(self, chat_id, text: str, name: str) -> int:
        state = load_state(self.data_dir)
        bound = state.get("chat_id")
        if bound is None:
            code = self.pair_code()
            if code and text.strip() == code:
                state["chat_id"] = chat_id
                state["bound_name"] = name
                save_state(self.data_dir, state)
                self._log("telegram.bound", {"chat_id": chat_id})
                if self.notify_owner is not None:
                    self.notify_owner(
                        "Telegram jud gaya",
                        f"{name or 'Owner'} ka phone ab JARVIS se "
                        "Telegram par baat kar sakta hai.")
                self._safe_send(
                    chat_id,
                    "Jud gaya hoon, sir. Ab aap yahan se kuch bhi "
                    "bhej dijiye — main wahi JARVIS hoon jo PC par "
                    "hai.")
                return 1
            return 0    # unbound stranger: silence
        if bound != chat_id:
            return 0    # not the owner's chat: silence
        try:
            reply = self.handle(text)
        except Exception as exc:
            reply = (f"Sir, kuch gadbad ho gayi: "
                     f"{type(exc).__name__}")
        self._safe_send(chat_id, str(reply))
        self._log("telegram.message", {"chars": len(text)})
        return 1
