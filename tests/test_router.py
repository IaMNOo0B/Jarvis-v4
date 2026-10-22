"""Route-then-specialize (the Kodee lesson): the brain sees
the domain it needs, not the whole registry — measured in the
actual system prompt a turn sends. No signal = the full set:
routing may focus the brain, never starve it."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.chat import ChatApp
from jarvis.router import route_rule_packs, route_schemas, routed_domains


class CaptureChain:
    name = "capture"

    def __init__(self):
        self.system = ""

    def complete(self, messages, tools=None):
        for m in messages:
            if m.get("role") == "system":
                self.system = str(m.get("content", ""))
                break
        return {"content": "theek hai, sir"}


def test_router_picks_domains_and_keeps_core():
    schemas = [
        {"name": "browser_open", "skill": "core.browser"},
        {"name": "email_send", "skill": "core.mail"},
        {"name": "time_now", "skill": "core.time"},
        {"name": "recall", "skill": "core.memory"},
    ]
    subset = route_schemas("browser me site khol do", schemas)
    names = {s["name"] for s in subset}
    assert "browser_open" in names          # the called domain
    assert "time_now" in names and "recall" in names  # core rides
    assert "email_send" not in names         # the rest is masked


def test_router_silence_means_full_set():
    schemas = [{"name": "email_send", "skill": "core.mail"}]
    assert route_schemas("kuch acha sa batao", schemas) is None
    assert routed_domains("kuch acha sa batao") == []


def test_turn_prompt_is_specialized(tmp_path):
    chain = CaptureChain()
    app = ChatApp(tmp_path, chain=chain)
    app.turn("browser me ja kar website khol do please")
    assert "browser_open" in chain.system
    assert "email_send" not in chain.system
    assert "time_now" in chain.system       # core always rides


def test_vague_turn_gets_everything(tmp_path):
    chain = CaptureChain()
    app = ChatApp(tmp_path, chain=chain)
    app.turn("kuch batao apne baare me")
    assert "email_send" in chain.system
    assert "browser_open" in chain.system


# ---------------------------------------------------------------------------
# Rule-pack routing (ECC rules install): two keys — a
# coding/build domain AND a language signal — then chains.
# ---------------------------------------------------------------------------

def test_route_rule_packs_selection_table():
    code = ["core.code"]
    assert route_rule_packs(
        "python me ek script likho jo CSV padhe",
        domains=code) == ["python"]
    assert route_rule_packs(
        "react component banao",
        domains=code) == ["react", "typescript"]
    assert route_rule_packs(
        "nuxt app ka login page banao",
        domains=["core.builds"]) == ["nuxt", "vue", "typescript"]
    assert route_rule_packs(
        "ek landing page design karo",
        domains=["core.builds"]) == ["web"]
    # No coding/build domain -> no pack, language or not.
    assert route_rule_packs(
        "python ek language hai, kuch batao") == []
    assert route_rule_packs("khaana khaya?") == []
    # Path signals when the text names no language.
    assert route_rule_packs(
        "kuch banao", domains=code,
        paths=["src/main.rs"]) == ["rust"]
    assert route_rule_packs(
        "kuch banao", domains=code,
        paths=["api/main_api.py"]) == ["python"]


def test_route_rule_packs_two_key_and_dedupe():
    # A language with a NON-coding domain loads nothing.
    assert route_rule_packs(
        "python seekhna hai mujhe", domains=["core.web"]) == []
    # work=True is the internal caller's key.
    assert route_rule_packs("python script", work=True) == ["python"]
    # Duplicate signals collapse into one entry.
    assert route_rule_packs(
        "python python pytest python",
        domains=["core.code"]) == ["python"]
    # Boundary safety: javascript is typescript, not java.
    assert route_rule_packs(
        "javascript me code likho",
        domains=["core.code"]) == ["typescript"]


def _journal(app, kind):
    return [r for r in app.journal.replay()
            if r.get("kind") == kind]


def test_coding_turn_carries_python_card(tmp_path):
    from jarvis.prompt_stability import DEFAULT_BOUNDARY
    chain = CaptureChain()
    app = ChatApp(tmp_path, chain=chain)
    app.turn("python me ek chhota script likho jo file padhe")
    tail = chain.system.split(DEFAULT_BOUNDARY, 1)[1]
    assert "PEP 8" in tail            # the card, below the boundary
    selected = _journal(app, "rules.pack_selected")
    assert selected
    assert selected[-1]["payload"]["packs"] == ["python"]
    turns = _journal(app, "brain.turn")
    assert turns[-1]["payload"]["rules_annex_included"] is True
    assert app._last_rule_packs == ["python"]


def test_non_coding_turn_has_no_pack(tmp_path):
    chain = CaptureChain()
    app = ChatApp(tmp_path, chain=chain)
    app.turn("aaj mausam kaisa hai")
    assert "PACK:" not in chain.system
    assert _journal(app, "rules.pack_selected") == []


def test_delegated_python_task_carries_card(tmp_path):
    from jarvis.chat import _delegated_run, _team_brain
    chain = CaptureChain()
    app = ChatApp(tmp_path, chain=chain)
    _delegated_run(app.parts,
                   "python me script likho jo file count kare")
    assert "PEP 8" in chain.system
    # The internal JSON brains keep their audit=False setting —
    # the annex changed their dynamic tail, nothing else.
    assert _team_brain(app.parts,
                       with_tools=False)._audit_enabled is False


def test_rules_pack_surfaces(tmp_path):
    app = ChatApp(tmp_path, chain=CaptureChain())
    shown = app.handle_command("/rules")
    assert "BANDISHEIN" in shown
    assert "Language packs" in shown and "python" in shown
    index = app.handle_command("/rules packs")
    assert "python" in index and "card" in index
    card = app.handle_command("/rules card python")
    assert "PEP 8" in card
    full = app.handle_command("/rules pack python")
    # FastAPI lives in the full ECC text but NOT in the card —
    # the two surfaces really are different layers.
    assert "FastAPI" in full and "FastAPI" not in card
    refused = app.handle_command("/rules pack ../python")
    assert "mila nahi" in refused
