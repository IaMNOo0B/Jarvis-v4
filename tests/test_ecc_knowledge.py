"""ECC Wave A — the installed Everything Claude Code library must be
reachable through the real knowledge_search op: an ECC skill found by
a distinctive phrase from its body, an ECC agent found by name, and
the ecc-index map itself present in the corpus."""
from jarvis import builtin_skills as bs


def _sources(out):
    return [r["source"] for r in out.get("results", [])]


def test_ecc_skill_found_by_distinctive_body_phrase():
    # "fact-forcing" is GateGuard vocabulary from the skill's body.
    out = bs.op_knowledge_search("GateGuard fact-forcing gate", limit=6)
    assert "error" not in out
    assert "ecc/skills/gateguard/SKILL.md" in _sources(out)


def test_ecc_agent_found_by_name():
    out = bs.op_knowledge_search("gan-evaluator", limit=10)
    assert "error" not in out
    assert "ecc/agents/gan-evaluator.md" in _sources(out)


def test_ecc_index_map_is_searchable():
    out = bs.op_knowledge_search("ECC Library Everything Claude Code "
                                 "installed as knowledge", limit=6)
    assert "error" not in out
    assert "ecc-index.md" in _sources(out)


def test_ecc_corpus_counts_on_disk():
    root = bs._knowledge_dir()
    skills = list((root / "ecc" / "skills").glob("*/SKILL.md"))
    agents = list((root / "ecc" / "agents").glob("*.md"))
    rules = [p for p in (root / "ecc" / "rules").rglob("*")
             if p.is_file()]
    commands = list(
        (root / "ecc" / "reference" / "commands").glob("*.md"))
    assert len(skills) == 311
    assert len(agents) == 71
    assert len(rules) == 122
    assert len(commands) == 97
    assert (root / "ecc" / "LICENSE-ECC.txt").exists()
