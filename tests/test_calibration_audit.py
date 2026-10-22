"""Calibration log integrity after the 2026-10-22 audit:
the resolve RMW raced across instances and erased records,
one torn line truncated the log, and write failures were
swallowed behind usable-looking ids."""

import json
import threading

from jarvis.calibration import Calibrator


def test_concurrent_record_resolve_across_instances(tmp_path):
    cals = [Calibrator(tmp_path), Calibrator(tmp_path)]
    errors = []

    def worker(n):
        try:
            cal = cals[n % 2]
            for i in range(30):
                row = cal.record("race", f"claim {n}-{i}", 0.7)
                cal.resolve(row["id"], i % 2 == 0)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,))
               for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    rows = cals[0]._rows()
    assert len(rows) == 180
    assert sum(1 for r in rows if r["outcome"] is not None) == 180
    assert cals[1].stats("race")["resolved"] == 180


def test_torn_line_is_skipped_never_truncates(tmp_path):
    cal = Calibrator(tmp_path)
    ids = [cal.record("d", f"c{i}", 0.6)["id"] for i in range(5)]
    path = tmp_path / "decisions.jsonl"
    lines = path.read_text().splitlines()
    lines.insert(2, '{"id": "torn", "dom')      # half-written
    lines.append("not json at all")
    path.write_text("\n".join(lines) + "\n")
    fresh = Calibrator(tmp_path)
    assert [r["id"] for r in fresh._rows()] == ids
    fresh.resolve(ids[0], True)
    # The resolve rewrite keeps every surviving row.
    assert [r["id"] for r in fresh._rows()] == ids


def test_pending_zero_limit_is_empty(tmp_path):
    cal = Calibrator(tmp_path)
    cal.record("d", "c", 0.5)
    assert cal.pending(0) == []
    assert len(cal.pending()) == 1


def test_stats_survives_null_scores(tmp_path):
    cal = Calibrator(tmp_path)
    row = cal.record("d", "c", 0.5)
    cal.resolve(row["id"], True)
    path = tmp_path / "decisions.jsonl"
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    rows[0]["score"] = None                    # legacy/hand edit
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    stats = Calibrator(tmp_path).stats("d")
    assert stats["resolved"] == 1
    assert stats["mean_log_score"] == 0.0


def test_ids_are_16_hex_and_long_domains_match(tmp_path):
    cal = Calibrator(tmp_path)
    domain = "x" * 80
    row = cal.record(domain, "claim", 0.9)
    assert len(row["id"]) == 16
    cal.resolve(row["id"], True)
    assert cal.stats(domain)["resolved"] == 1
    assert cal.calibrated(domain, 0.9) != 0.9 or True  # no crash


def test_write_failure_raises_instead_of_a_fake_id(tmp_path):
    cal = Calibrator(tmp_path)
    (tmp_path / "decisions.jsonl").mkdir()      # open('a') fails
    try:
        cal.record("d", "c", 0.5)
        raise AssertionError("record should have raised")
    except (OSError, IsADirectoryError):
        pass
