"""THE CONTRACT TEST — the two laws, enforced not commented.

LAW 1: a tool may be advertised ONLY if a permission for it exists in
       the turn's allowances. (v2 defect: web_search advertised, never
       permitted, gate denied it mid-turn — evals/BASELINE.md.)
LAW 2: a tool may be advertised ONLY if its executor op exists.
       (v2 defect: pc.launch_app permitted and called, but the
       executor had no such op — evals/BASELINE.md.)

For the built-in registry these tests assert
    advertised ⊆ permitted ∩ implemented
and that a deliberately broken fixture skill FAILS the contract check
with both defects named.
"""

from jarvis.builtin_skills import ALL_PERMISSIONS, build_default_registry
from jarvis.gate import Gate
from jarvis.journal import Journal
from jarvis.skills import SkillRegistry


def _rig(tmp_path):
    journal = Journal(str(tmp_path / "j.jsonl"))
    gate = Gate(journal, b"contract-key")
    registry = build_default_registry(gate=gate, journal=journal)
    return gate, registry


def _implemented_ops(registry):
    """Tool names whose op callable actually exists in the registry."""
    names = set()
    for skill in registry._skills.values():
        for tool in skill.tools:
            if callable(skill.ops.get(tool.op_name)):
                names.add(tool.name)
    return names


def _permitted_tools(registry, allowances):
    names = set()
    for skill in registry._skills.values():
        for tool in skill.tools:
            if tool.permission in allowances:
                names.add(tool.name)
    return names


def test_builtin_registry_honours_both_laws(tmp_path):
    _, registry = _rig(tmp_path)
    advertised = {t["name"] for t in registry.advertised_tools(ALL_PERMISSIONS)}
    assert advertised  # the builtins do advertise under full allowances
    assert advertised <= _permitted_tools(registry, ALL_PERMISSIONS)
    assert advertised <= _implemented_ops(registry)
    assert registry.contract_defects(ALL_PERMISSIONS) == []


def test_law1_no_permission_no_advertisement(tmp_path):
    _, registry = _rig(tmp_path)
    without_net = set(ALL_PERMISSIONS) - {"net.outbound"}
    advertised = {t["name"] for t in registry.advertised_tools(without_net)}
    # the exact v2 defect, now impossible: web_search is not advertised
    assert "web_search" not in advertised
    # and the audit names the defect instead of hiding it
    defects = registry.contract_defects(without_net)
    assert any(d["law"] == 1 and d["tool"] == "web_search" for d in defects)


BROKEN_MANIFEST = {
    "name": "test.broken",
    "version": "0.0.1",
    "tools": [
        {   # LAW 2 defect: manifest promises an op that does not exist
            "name": "launch_app",
            "op": "launch_app",
            "permission": "pc.control",
            "schema": {"type": "object",
                       "properties": {"name": {"type": "string"}},
                       "required": ["name"]},
        },
        {   # LAW 1 defect: a permission no sane turn ever grants
            "name": "teleport",
            "op": "teleport",
            "permission": "teleport.use",
            "schema": {"type": "object", "properties": {}},
        },
    ],
}


def test_broken_fixture_fails_contract_with_both_defects_named(tmp_path):
    journal = Journal(str(tmp_path / "j.jsonl"))
    gate = Gate(journal, b"contract-key")
    registry = SkillRegistry(gate=gate, journal=journal)
    # 'teleport' has an op; 'launch_app' deliberately does not
    registry.register(BROKEN_MANIFEST, ops={"teleport": lambda: "poof"})
    registry.approve("test.broken")

    defects = registry.contract_defects(ALL_PERMISSIONS)
    laws_named = {d["law"] for d in defects}
    assert laws_named == {1, 2}
    by_tool = {d["tool"]: d for d in defects}
    assert by_tool["teleport"]["law"] == 1
    assert "teleport.use" in by_tool["teleport"]["problem"]
    assert by_tool["launch_app"]["law"] == 2
    assert "launch_app" in by_tool["launch_app"]["problem"]

    # and enforcement: neither broken tool is ever advertised
    advertised = {t["name"]
                  for t in registry.advertised_tools(ALL_PERMISSIONS)}
    assert "teleport" not in advertised
    assert "launch_app" not in advertised


def test_every_builtin_tool_dispatches_to_a_real_op(tmp_path):
    """LAW 2's positive side: what is advertised can actually run."""
    _, registry = _rig(tmp_path)
    assert registry.dispatch("time_now", {})["iso"]
    registry.dispatch("remember_fact", {"text": "mera naam Owais hai"})
    out = registry.dispatch("recall_memory", {"query": "naam"})
    assert any("Owais" in fact for fact in out["facts"])
    # open_app's op exists and answers honestly off-Windows (this host)
    result = registry.dispatch("open_app", {"name": "notepad"})
    assert isinstance(result, dict) and result  # a result, not a crash
