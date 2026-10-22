"""The ONE rules file: it drives the chat brain's protocol AND
the voice rules; the voice-only section never leaks into chat;
a missing file falls back to the same rules, never to none."""

from pathlib import Path

import jarvis.rules as rules
from jarvis.chat import ChatApp

ROOT = Path(__file__).resolve().parent.parent


def test_shipped_file_loads_and_splits():
    text, source = rules.load_rules(ROOT)
    assert source.endswith("JARVIS-RULES.txt")
    chat = rules.chat_protocol(text)
    assert "BANDISHEIN" in chat and "TAREEKA" in chat
    assert "AWAAZ" not in chat            # voice-only stays out
    assert "[outside content CODE]" in chat
    assert "You HAVE internet" in chat
    voice = rules.voice_rules(text)
    assert "AWAAZ" in voice and "BANDISHEIN" in voice


def test_missing_file_falls_back_to_the_same_rules(tmp_path):
    text, source = rules.load_rules(tmp_path)
    assert source == "built-in"
    assert "BANDISHEIN" in rules.chat_protocol(text)


def test_app_brain_carries_the_file_protocol(tmp_path):
    app = ChatApp(tmp_path)
    assert app.rules_source.endswith("JARVIS-RULES.txt")
    protocol = app.brain._builder.protocol
    assert "malik ke rules" in protocol
    assert "/rules" not in app.handle_command("/rules")[:0]
    shown = app.handle_command("/rules")
    assert "BANDISHEIN" in shown
