"""The ONE rules file: it drives the chat brain's protocol AND
the voice rules; the voice-only section never leaks into chat;
a missing file falls back to the same rules, never to none."""

from pathlib import Path

import pytest

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


# ---------------------------------------------------------------------------
# ECC rules install (2026-10-22): the distilled core lines and
# the rules-packs loader. The owner's rules outrank ECC
# everywhere; a bad card is refused, never truncated.
# ---------------------------------------------------------------------------

EXPECTED_PACKS = [
    "angular", "arkts", "cpp", "csharp", "dart", "fsharp",
    "golang", "java", "kotlin", "nuxt", "perl", "php", "python",
    "react", "react-native", "ruby", "rust", "swift",
    "typescript", "vue", "web",
]


def test_rules_file_within_ceiling():
    text = (ROOT / "JARVIS-RULES.txt").read_text(encoding="utf-8")
    assert len(text) <= 4050
    tareeka = text.split("## AWAAZ")[0]
    for bullet in ("Coding: tests first (RED-GREEN-refactor)",
                   "Functions <50 lines",
                   "Validate boundary inputs",
                   "Commits conventional"):
        assert bullet in tareeka


def test_fallback_matches_file_body():
    text = (ROOT / "JARVIS-RULES.txt").read_text(encoding="utf-8")
    body = text[text.index("## BANDISHEIN"):]
    assert body.strip() == rules.FALLBACK_RULES.strip()


def test_core_bullets_are_shared_but_cards_are_not_voice():
    text, _ = rules.load_rules(ROOT)
    chat = rules.chat_protocol(text)
    assert "RED-GREEN-refactor" in chat and "push" in chat
    voice = rules.voice_rules(text)
    # The distilled bullets live in shared TAREEKA, so voice
    # hears them too — but pack CARDS are a chat/build-brain
    # mechanism and never enter the voice protocol.
    assert "RED-GREEN-refactor" in voice
    assert "BANDISHEIN" in voice and "AWAAZ" in voice
    assert "PACK:" not in voice


def test_list_and_load_rule_packs():
    assert rules.list_rule_packs(ROOT) == EXPECTED_PACKS
    card = rules.load_rule_pack(ROOT, "python")
    assert "PEP 8" in card
    assert len(card) <= rules.MAX_PACK_CHARS
    assert rules.load_rule_pack(ROOT, "cobol") == ""


def _write_card(root, name, text):
    folder = Path(root) / rules.PACKS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.txt").write_text(text, encoding="utf-8")


def test_pack_loader_rejects_bad_cards(tmp_path):
    _write_card(tmp_path, "big",
                "## PACK: BIG\n" + "- ek line\n" * 200)
    assert rules.load_rule_pack(tmp_path, "big") == ""
    assert rules.pack_card_problem(tmp_path, "big") == "over-cap"
    _write_card(tmp_path, "claudey",
                "## PACK: C\n- config ~/.claude me rakho")
    assert rules.load_rule_pack(tmp_path, "claudey") == ""
    _write_card(tmp_path, "autoacc",
                "## PACK: A\n- permissions auto-accept karo")
    assert rules.load_rule_pack(tmp_path, "autoacc") == ""
    assert rules.pack_card_problem(tmp_path, "autoacc") == "banned"
    # A path-like name never reads outside rules-packs/.
    (Path(tmp_path) / "python.txt").write_text(
        "BAHAR KA TEXT", encoding="utf-8")
    assert rules.load_rule_pack(tmp_path, "../python") == ""
    assert rules.load_rule_pack(tmp_path, "..\\python") == ""
    with pytest.raises(ValueError):
        rules.rules_pack_path(tmp_path, "../python")


def test_load_rule_packs_cap_and_order(tmp_path):
    _write_card(tmp_path, "aaa", "## PACK: AAA\n" + "a" * 560)
    _write_card(tmp_path, "bbb", "## PACK: BBB\n" + "b" * 560)
    _write_card(tmp_path, "ccc", "## PACK: CCC\n" + "c" * 560)
    annex = rules.load_rule_packs(tmp_path, ["aaa", "bbb", "ccc"])
    assert annex.startswith(rules.ANNEX_HEADING)
    assert len(annex) <= rules.MAX_ANNEX_CHARS
    # Priority order kept; the lowest-priority card drops
    # WHOLE — never truncated mid-text.
    assert "PACK: AAA" in annex and "PACK: BBB" in annex
    assert "PACK: CCC" not in annex
    assert annex.index("PACK: AAA") < annex.index("PACK: BBB")
    deduped = rules.load_rule_packs(tmp_path, ["bbb", "bbb", "aaa"])
    assert deduped.count("PACK: BBB") == 1
    assert deduped.index("PACK: BBB") < deduped.index("PACK: AAA")
    assert rules.load_rule_packs(tmp_path, []) == ""
    assert rules.load_rule_packs(tmp_path, ["nope"]) == ""


def test_real_chain_annex_respects_cap():
    # nuxt chains vue + typescript; the shipped cards are big
    # enough that typescript (lowest priority) drops whole.
    annex = rules.load_rule_packs(ROOT, ["nuxt", "vue",
                                         "typescript"])
    assert "PACK: NUXT" in annex and "PACK: VUE" in annex
    assert "PACK: TYPESCRIPT" not in annex
    assert len(annex) <= rules.MAX_ANNEX_CHARS


def test_shipped_cards_are_clean():
    for name in EXPECTED_PACKS:
        card = rules.load_rule_pack(ROOT, name)
        assert card, f"{name}: loader ne card refuse kar diya"
        assert len(card) <= rules.MAX_PACK_CHARS
        assert not rules._pack_banned(card), name
        assert card.startswith(f"## PACK: {name.upper()}")
        assert 'knowledge_search "ecc rules' in card
