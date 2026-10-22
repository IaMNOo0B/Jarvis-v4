"""Wave 5, first slice: swallowed failures are COUNTED, every
look lands in the visual memory, and the profile backs up and
restores — with secrets never travelling."""

import zipfile

from jarvis import swallow
from jarvis.backup import export_profile, restore_profile
from jarvis.chat import ChatApp
from jarvis.vision import log_visual, recall_visual


def test_swallow_counts_what_it_hides(tmp_path):
    assert swallow.count(tmp_path) == 0
    swallow.note(tmp_path, "goodput.save", ValueError("disk bhari"))
    swallow.note(tmp_path, "learned.persist", OSError("band"))
    assert swallow.count(tmp_path) == 2
    rows = swallow.recent(tmp_path)
    assert rows[-1]["where"] == "learned.persist"
    assert "ValueError" in rows[0]["error"]


def test_visual_memory_keeps_words_not_images(tmp_path):
    log_visual(tmp_path, "screen", "error kya hai",
               "ek laal error dialog dikh raha hai")
    log_visual(tmp_path, "page", "price", "shoes 499 me hain")
    hits = recall_visual(tmp_path, "error")
    assert len(hits) == 1 and "dialog" in hits[0]["answer"]
    assert len(recall_visual(tmp_path)) == 2


def test_backup_excludes_secrets_and_restore_roundtrips(tmp_path):
    data = tmp_path / "data"
    (data / "inbox").mkdir(parents=True)
    (data / "memory.db").write_bytes(b"yaadein")
    (data / "secrets.json").write_text('{"gemini": "RAAZ"}')
    (data / "config.json").write_text("{}")
    out = export_profile(data)
    names = zipfile.ZipFile(out["path"]).namelist()
    assert "memory.db" in names
    assert "secrets.json" not in names     # raaz safar nahi karta
    fresh = tmp_path / "fresh"
    back = restore_profile(fresh, out["path"])
    assert back["restored"] >= 2
    assert (fresh / "memory.db").read_bytes() == b"yaadein"
    assert not (fresh / "secrets.json").exists()


def test_app_backup_command_and_visual_tool(tmp_path):
    app = ChatApp(tmp_path)
    names = {t["name"] for t in
             app.registry.advertised_tools(app.allowances)}
    assert "visual_recall" in names
    out = app.execute_tool("visual_recall", {})
    assert out["ok"] is True and out["looks"] == []
    reply = app.handle_command("/backup")
    assert "backup ban gaya" in reply and "keys" in reply
    diag = app.handle_command("/diagnose")
    assert "Diagnosis" in diag
