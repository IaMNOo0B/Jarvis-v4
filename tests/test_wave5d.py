"""Wave 5, fourth slice: local-brain routines spend no cloud,
the scrub gate stops secret shapes at the transcript door,
and the UI's network reach is the owner's config choice."""

import queue

from jarvis import scheduler
from jarvis.chat import ChatApp
from jarvis.scrub import find_secret_shapes
from jarvis.ui import ui_bind_host
from jarvis.voice_manager import VoiceManager


def test_local_routine_runs_on_the_local_runner(tmp_path):
    scheduler.add_item(tmp_path, "sasta kaam", "routines.json",
                       steps=["hisaab lagao"],
                       schedule={"type": "interval", "hours": 1},
                       enabled=True, local=True)
    scheduler.add_item(tmp_path, "bada kaam", "routines.json",
                       steps=["gehri research"],
                       schedule={"type": "interval", "hours": 1},
                       enabled=True)
    ran = {"cloud": [], "local": []}
    scheduler.tick(
        tmp_path,
        runner=lambda w: ran["cloud"].append(w) or "cloud",
        local_runner=lambda w: ran["local"].append(w) or "local",
        now=10**12)
    assert len(ran["local"]) == 1 and len(ran["cloud"]) == 1


def test_scrub_shapes_are_deterministic():
    assert find_secret_shapes("mera card 4111 1111 1111 1111 "
                              "hai") == ["card"]
    assert find_secret_shapes("order id 1234567890123") == []
    assert find_secret_shapes(
        "key sk-abcdefgh12345678 suno") == ["api-key"]
    assert find_secret_shapes("aam baat cheet") == []


def test_scrub_gate_redacts_before_history():
    mgr = VoiceManager.__new__(VoiceManager)
    mgr.gate = None
    mgr._play_q = queue.Queue()
    seen, notes = [], []
    mgr._on_transcript = lambda role, text: seen.append(text)
    mgr._notify = lambda msg: notes.append(msg)
    mgr.on_transcript("user", "sun, card 4111111111111111")
    assert seen and "4111" not in seen[0]
    assert notes and "Suraksha" in notes[0]
    mgr.on_transcript("user", "aam si baat hai")
    assert seen[-1] == "aam si baat hai"


def test_ui_bind_host_defaults_to_loopback(tmp_path):
    app = ChatApp(tmp_path)
    assert ui_bind_host(app) == "127.0.0.1"
    app.config.set("ui", "bind_host", "100.64.0.9")
    assert ui_bind_host(app) == "100.64.0.9"
