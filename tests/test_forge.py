"""Wave 5b — the Forge: a self-built feature is staged,
sandbox-verified, promoted with a backup, and rolls back —
by itself on a live failure, or by the owner's hand."""

from pathlib import Path

from jarvis import forge
from jarvis.chat import ChatApp

GOOD_CODE = "def jodo(a, b):\n    return a + b\n"
GOOD_TEST = ("from jarvis.hisaab import jodo\n\n\n"
             "def test_jodo():\n    assert jodo(2, 3) == 5\n")


def _root(tmp_path):
    root = tmp_path / "app"
    (root / "jarvis").mkdir(parents=True)
    (root / "jarvis" / "__init__.py").write_text("")
    (root / "jarvis" / "chat.py").write_text("# app\n")
    (root / "tests").mkdir()
    return root


def test_stage_verify_promote_rollback(tmp_path):
    root = _root(tmp_path)
    data = tmp_path / "data"
    staged = forge.stage_feature(
        data, root, "hisaab jodo",
        {"jarvis/hisaab.py": GOOD_CODE}, GOOD_TEST,
        source="test se")
    assert staged["ok"] is True
    entry = Path(staged["dir"])
    assert forge.verify_staged(root, entry)["ok"] is True
    landed = forge.promote(root, entry)
    assert landed["ok"] is True
    assert (root / "jarvis" / "hisaab.py").exists()
    # The test path rides the entry's UNIQUE slug (audit
    # 2026-10-22) — read it from the manifest, never guess
    # it from the feature name.
    import json as _json
    test_rel = _json.loads(
        (entry / "MANIFEST.json").read_text())["test_rel"]
    assert test_rel.startswith("tests/test_forge_hisaab_jodo_")
    assert (root / test_rel).exists()
    back = forge.rollback(root, data, staged["slug"])
    assert back["ok"] is True
    assert not (root / "jarvis" / "hisaab.py").exists()
    assert not (root / test_rel).exists()


def test_bad_paths_and_failing_tests_never_land(tmp_path):
    root = _root(tmp_path)
    data = tmp_path / "data"
    evil = forge.stage_feature(
        data, root, "bura", {"../evil.py": "x = 1"}, GOOD_TEST)
    assert evil["ok"] is False and "mana" in evil["error"]
    staged = forge.stage_feature(
        data, root, "galat test",
        {"jarvis/hisaab.py": GOOD_CODE},
        "def test_fail():\n    assert False\n")
    entry = Path(staged["dir"])
    verdict = forge.verify_staged(root, entry)
    assert verdict["ok"] is False
    refused = forge.promote(root, entry)
    assert refused["ok"] is False
    assert not (root / "jarvis" / "hisaab.py").exists()


def test_live_failure_rolls_back_by_itself(tmp_path):
    root = _root(tmp_path)
    data = tmp_path / "data"
    staged = forge.stage_feature(
        data, root, "hisaab jodo",
        {"jarvis/hisaab.py": GOOD_CODE}, GOOD_TEST)
    entry = Path(staged["dir"])
    assert forge.verify_staged(root, entry)["ok"] is True

    def live_fails(cwd, test_rel):
        return False, "live me toot gaya"
    out = forge.promote(root, entry, runner=live_fails)
    assert out["ok"] is False and "wapas" in out["note"]
    assert not (root / "jarvis" / "hisaab.py").exists()
    entries = forge.list_entries(data)
    assert entries[0]["status"] == "rolled-back"


def test_app_advertises_build_feature(tmp_path):
    app = ChatApp(tmp_path)
    names = {t["name"] for t in
             app.registry.advertised_tools(app.allowances)}
    assert "build_feature" in names
    reply = app.handle_command("/forge")
    assert "Forge" in reply
