"""The prompt-budget floor (2026-10-22): schemas are REAL now
(the view reads the advertised 'schema' key), the ladder diets
them before touching memory, and when everything is spent it
degrades to names-only + a folded summary instead of the old
silence. Refusing a turn is the last resort, not the third."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.prompt import (PromptBuilder, compact_schema,
                           tool_view, tool_views)


def test_view_reads_the_advertised_schema_key():
    v = tool_view({"name": "email_send", "skill": "core.mail",
                   "schema": {"type": "object",
                              "properties": {
                                  "to": {"type": "string"}},
                              "required": ["to"]}})
    out = v.openai(None)
    props = out["function"]["parameters"]["properties"]
    assert props["to"]["type"] == "string"


def test_compact_schema_keeps_shape_drops_trimmings():
    fat = {"type": "object",
           "properties": {"q": {"type": "string", "minLength": 3,
                                "description": "lambi tafseel"},
                          "n": {"type": "integer", "minimum": 0}},
           "required": ["q"], "additionalProperties": False}
    slim = compact_schema(fat)
    assert slim["properties"]["q"] == {"type": "string"}
    assert slim["properties"]["n"] == {"type": "integer"}
    assert slim["required"] == ["q"]
    assert "additionalProperties" not in slim


def _fat_tools(n=60):
    return [{"name": f"tool_{i}", "skill": "core.web",
             "schema": {"type": "object",
                        "properties": {
                            "query": {"type": "string",
                                      "minLength": 1,
                                      "description": "x" * 60},
                            "count": {"type": "integer"}},
                        "required": ["query"]}}
            for i in range(n)]


def test_builder_degrades_instead_of_refusing():
    views = tool_views(_fat_tools())
    builder = PromptBuilder(views)
    history = []
    for i in range(10):
        history.append({"role": "user",
                        "content": f"purani baat {i} " + "lafz " * 120})
        history.append({"role": "assistant",
                        "content": f"jawab {i} " + "lafz " * 120})
    out = builder.build(history, "aakhri sawal", [])
    assert out.estimate <= 4000
    assert any("names-only" in a or "compacted" in a
               for a in out.ladder_actions)


# ---------------------------------------------------------------------------
# The floor with the ECC rules install: the new (bigger) core
# protocol plus a rules annex — the largest shipped card, and
# the legal-maximum 1,600-char annex — must still land inside
# the hard budget, annex dropped before history.
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent


def _real_builder(tmp_path):
    from jarvis.chat import ChatApp
    from jarvis.rules import chat_protocol
    app = ChatApp(tmp_path)
    schemas = app.registry.advertised_tools(app.allowances)
    builder = PromptBuilder(
        tool_views(schemas),
        protocol=chat_protocol(app.rules_text))
    return app, builder


def _heavy_history():
    # Six exchanges: heavy enough that the FULL ladder engages
    # on the real tool set (measured), light enough that the
    # names-only floor can still land inside the hard budget.
    history = []
    for i in range(6):
        history.append({"role": "user",
                        "content": f"purani baat {i} " + "lafz " * 40})
        history.append({"role": "assistant",
                        "content": f"jawab {i} " + "lafz " * 40})
    return history


def test_floor_with_new_core_and_largest_card(tmp_path):
    import jarvis.rules as rules
    _, builder = _real_builder(tmp_path)
    biggest = max(rules.list_rule_packs(ROOT),
                  key=lambda n: len(rules.load_rule_pack(ROOT, n)))
    annex = rules.load_rule_packs(ROOT, [biggest])
    assert annex
    out = builder.build(_heavy_history(), "aakhri sawal", [],
                        rules_annex=annex)
    assert out.estimate <= 4000
    assert out.rules_annex_included is False


def test_floor_with_max_annex(tmp_path):
    _, builder = _real_builder(tmp_path)
    head = "## LANGUAGE PACKS (is kaam ke liye chune hue)\n\n"
    annex = head + "x" * (1600 - len(head))
    assert len(annex) == 1600
    out = builder.build(_heavy_history(), "aakhri sawal", [],
                        rules_annex=annex)
    assert out.estimate <= 4000
    # Whatever the ladder dropped, the annex went BEFORE any
    # history trimming.
    annex_at = next((i for i, a in enumerate(out.ladder_actions)
                     if "dropped rules annex" in a), None)
    trim_at = next((i for i, a in enumerate(out.ladder_actions)
                    if "trimmed" in a), None)
    assert annex_at is not None
    if trim_at is not None:
        assert annex_at < trim_at
