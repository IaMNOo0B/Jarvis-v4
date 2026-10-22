"""SkillBook (Sentient Spec 6): the learned-playbook
store — create/patch preserving usage evidence, the
usage counter, context lines that bring a matching
playbook back into the brain, and self-checks as a
contract (file checks gate proposals; run checks run
scrubbed and flag, never silently pass)."""

from jarvis.skillbook import (SkillBook, file_checks_pass,
                              named_tools, run_checks)

SKILL = {"name": "morning_brief_prep",
         "description": "Subah ke brief ki taiyaari",
         "when_to_use": "jab subah ka brief banana ho",
         "procedure": ["memory_recall se kal ki baatein lo",
                       "web_search se khabrein lo"],
         "pitfalls": ["purani khabar mat dohrayo"],
         "verification": ["brief me tareekh check karo"],
         "checks": []}


def test_add_get_and_patch_preserves_stats(tmp_path):
    book = SkillBook(tmp_path)
    book.add(SKILL)
    book.record_use("morning_brief_prep", True)
    book.record_use("morning_brief_prep", False)
    patched = dict(SKILL)
    patched["procedure"] = ["memory_recall pehle",
                            "phir web_search"]
    book.add(patched)
    row = book.get("morning_brief_prep")
    assert row["uses"] == 2 and row["successes"] == 1
    assert row["failures"] == 1
    assert row["procedure"][0] == "memory_recall pehle"
    assert book.remove("morning_brief_prep") is True
    assert book.list() == []


def test_context_lines_match_when_to_use(tmp_path):
    book = SkillBook(tmp_path)
    book.add(SKILL)
    lines = book.context_lines("subah ka brief banao")
    assert len(lines) == 1
    assert "morning_brief_prep" in lines[0]
    assert "memory_recall" in lines[0]
    assert book.context_lines("cricket score batao") == []


def test_named_tools_from_procedure(tmp_path):
    named = named_tools(SKILL, {"memory_recall", "web_search",
                                "email_send"})
    assert named == {"memory_recall", "web_search"}


def test_file_checks_are_a_real_gate(tmp_path):
    target = tmp_path / "proof.txt"
    target.write_text("saboot yahan hai", encoding="utf-8")
    assert file_checks_pass(
        [{"file": str(target), "expect": "saboot"}]) is True
    assert file_checks_pass(
        [{"file": str(target), "expect": "gayab"}]) is False
    # No file checks declared -> nothing to gate.
    assert file_checks_pass(
        [{"run": "echo ok", "expect": "ok"}]) is True


def test_run_checks_execute_scrubbed(tmp_path):
    results = run_checks(
        [{"run": "echo hello-check", "expect": "hello-check"},
         {"run": "echo bye", "expect": "gayab"}])
    assert results[0]["ok"] is True
    assert results[1]["ok"] is False
