"""Tests for jarvis.data_dr (Step 00 — data-dir disaster discipline)."""

import hashlib
import json

import pytest

from jarvis.data_dr import (
    MANIFEST_NAME,
    make_backup,
    restore_backup,
    snapshot_manifest,
    verify_manifest,
)

SECRET = "sk-TEST-SECRET-do-not-leak-1234567890"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture()
def data_dir(tmp_path):
    d = tmp_path / "data"
    (d / "memory").mkdir(parents=True)
    (d / "settings.json").write_text('{"lang": "hinglish"}', encoding="utf-8")
    (d / "memory" / "facts.txt").write_text("owner likes chai\n", encoding="utf-8")
    (d / "secrets.local.yaml").write_text(f"openrouter: {SECRET}\n", encoding="utf-8")
    return d


class TestSnapshot:
    def test_manifest_contents(self, data_dir):
        m = snapshot_manifest(data_dir)
        assert m["total_files"] == 3
        assert m["total_bytes"] > 0
        facts = m["files"]["memory/facts.txt"]
        assert facts["size"] == len("owner likes chai\n")
        assert facts["sha256"] == _sha(b"owner likes chai\n")
        assert facts["hashed"] is True
        assert m["unreadable"] == []

    def test_secret_content_never_in_manifest(self, data_dir):
        m = snapshot_manifest(data_dir)
        blob = json.dumps(m)
        assert SECRET not in blob
        # ...but its fingerprint IS there, so identity is still provable
        entry = m["files"]["secrets.local.yaml"]
        assert entry["sha256"] == _sha(f"openrouter: {SECRET}\n".encode())

    def test_large_file_is_size_only(self, data_dir):
        m = snapshot_manifest(data_dir, hash_limit=4)
        for entry in m["files"].values():
            assert entry["hashed"] is False
            assert entry["sha256"] is None
            assert entry["size"] > 0

    def test_missing_dir_raises(self, tmp_path):
        with pytest.raises(ValueError):
            snapshot_manifest(tmp_path / "nope")

    def test_empty_dir(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        m = snapshot_manifest(d)
        assert m["total_files"] == 0
        assert verify_manifest(d, m)["ok"] is True


class TestVerify:
    def test_clean_tree_ok(self, data_dir):
        m = snapshot_manifest(data_dir)
        report = verify_manifest(data_dir, m)
        assert report["ok"] is True
        assert report["checked"] == 3
        assert report["missing"] == []
        assert report["changed"] == []

    def test_tampered_file_detected_by_hash(self, data_dir):
        m = snapshot_manifest(data_dir)
        (data_dir / "memory" / "facts.txt").write_text("tampered!\n", encoding="utf-8")
        # same length trick: also try a same-size edit
        report = verify_manifest(data_dir, m)
        assert report["ok"] is False
        paths = [c["path"] for c in report["changed"]]
        assert "memory/facts.txt" in paths

    def test_same_size_edit_detected(self, data_dir):
        m = snapshot_manifest(data_dir)
        p = data_dir / "memory" / "facts.txt"
        p.write_text("owner likes coff\n", encoding="utf-8")  # same byte count, new bytes
        report = verify_manifest(data_dir, m)
        assert report["ok"] is False
        assert {"path": "memory/facts.txt", "reason": "sha256"} in report["changed"]

    def test_missing_file_detected(self, data_dir):
        m = snapshot_manifest(data_dir)
        (data_dir / "settings.json").unlink()
        report = verify_manifest(data_dir, m)
        assert report["ok"] is False
        assert report["missing"] == ["settings.json"]

    def test_extra_file_reported_but_not_fatal(self, data_dir):
        m = snapshot_manifest(data_dir)
        (data_dir / "new_note.txt").write_text("hi", encoding="utf-8")
        report = verify_manifest(data_dir, m)
        assert report["extra"] == ["new_note.txt"]
        assert report["ok"] is True  # growth is fine; loss/tamper is not

    def test_missing_data_dir(self, tmp_path, data_dir):
        m = snapshot_manifest(data_dir)
        report = verify_manifest(tmp_path / "gone", m)
        assert report["ok"] is False
        assert report["missing"] == sorted(m["files"])


class TestBackupRestoreRoundTrip:
    def test_round_trip(self, data_dir, tmp_path):
        backup_dir = tmp_path / "backup1"
        result = make_backup(data_dir, backup_dir)
        assert result["files"] == 3
        assert result["verification"]["ok"] is True
        assert (backup_dir / MANIFEST_NAME).is_file()
        assert (backup_dir / "memory" / "facts.txt").read_text(
            encoding="utf-8") == "owner likes chai\n"

        # disaster: wipe the original, then restore
        for p in sorted(data_dir.rglob("*"), reverse=True):
            if p.is_file():
                p.unlink()
        report = restore_backup(backup_dir, data_dir)
        assert report["ok"] is True
        assert (data_dir / "settings.json").is_file()
        assert SECRET in (data_dir / "secrets.local.yaml").read_text(encoding="utf-8")

        # and the restored tree still verifies against a fresh snapshot cycle
        m2 = snapshot_manifest(data_dir)
        assert verify_manifest(data_dir, m2)["ok"] is True

    def test_restore_into_fresh_dir(self, data_dir, tmp_path):
        backup_dir = tmp_path / "b"
        make_backup(data_dir, backup_dir)
        fresh = tmp_path / "fresh" / "data"
        report = restore_backup(backup_dir, fresh)
        assert report["ok"] is True
        assert (fresh / "memory" / "facts.txt").is_file()

    def test_backup_refuses_nested_dirs(self, data_dir, tmp_path):
        with pytest.raises(ValueError):
            make_backup(data_dir, data_dir / "inside")
        with pytest.raises(ValueError):
            make_backup(data_dir, data_dir)

    def test_backup_missing_source_raises(self, tmp_path):
        with pytest.raises(ValueError):
            make_backup(tmp_path / "nope", tmp_path / "b")

    def test_restore_without_manifest_raises(self, tmp_path):
        with pytest.raises(ValueError):
            restore_backup(tmp_path, tmp_path / "data")

    def test_backup_manifest_on_disk_has_no_secret_content(self, data_dir, tmp_path):
        backup_dir = tmp_path / "b"
        make_backup(data_dir, backup_dir)
        blob = (backup_dir / MANIFEST_NAME).read_text(encoding="utf-8")
        assert SECRET not in blob


class TestCli:
    def test_cli_snapshot_and_verify(self, data_dir, tmp_path, capsys):
        from jarvis.data_dr import main

        assert main(["snapshot", str(data_dir)]) == 0
        manifest = json.loads(capsys.readouterr().out)
        mf = tmp_path / "m.json"
        mf.write_text(json.dumps(manifest), encoding="utf-8")
        assert main(["verify", str(data_dir), str(mf)]) == 0
        (data_dir / "settings.json").unlink()
        assert main(["verify", str(data_dir), str(mf)]) == 1

    def test_cli_backup_restore(self, data_dir, tmp_path):
        from jarvis.data_dr import main

        backup_dir = tmp_path / "b"
        assert main(["backup", str(data_dir), str(backup_dir)]) == 0
        target = tmp_path / "restored"
        assert main(["restore", str(backup_dir), str(target)]) == 0
        assert (target / "settings.json").is_file()
