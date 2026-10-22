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
