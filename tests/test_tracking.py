"""Step 04 tests: Hungarian, Kalman, ByteTrack two-pass association."""

from jarvis.tracking import (
    Detection,
    KalmanBoxFilter,
    Tracker,
    assignment_cost,
    hungarian,
    iou,
)

# ---------------------------------------------------------------------------
# Hungarian assignment
# ---------------------------------------------------------------------------


def test_hungarian_finds_known_optimum():
    cost = [[4, 1, 3], [2, 0, 5], [3, 2, 2]]
    pairs = hungarian(cost)
    assert set(pairs) == {(0, 1), (1, 0), (2, 2)}
    assert assignment_cost(cost, pairs) == 5


def test_hungarian_rectangular_more_rows_than_cols():
    cost = [[10, 1], [1, 10], [5, 5]]
    pairs = hungarian(cost)
    assert len(pairs) == 2  # only two columns exist
    assert assignment_cost(cost, pairs) == 2


def test_hungarian_empty():
    assert hungarian([]) == []


# ---------------------------------------------------------------------------
# IoU and Kalman filter
# ---------------------------------------------------------------------------


def test_iou_basics():
    assert iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
    assert iou((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0
    assert iou((0, 0, 10, 10), (5, 0, 15, 10)) == 1 / 3


def test_kalman_learns_constant_velocity():
    kf = KalmanBoxFilter((0, 0, 20, 20))  # centre (10, 10)
    kf.predict()
    kf.update((4, 0, 24, 20))  # centre (14, 10)
    kf.predict()
    kf.update((8, 0, 28, 20))  # centre (18, 10)
    predicted = kf.predict()
    cx = (predicted[0] + predicted[2]) / 2
    assert cx > 18.0  # velocity carries it past the last measurement


def test_kalman_stationary_stays_put():
    kf = KalmanBoxFilter((0, 0, 20, 20))
    for _ in range(3):
        kf.predict()
        kf.update((0, 0, 20, 20))
    predicted = kf.predict()
    cx = (predicted[0] + predicted[2]) / 2
    assert abs(cx - 10.0) < 1.0


# ---------------------------------------------------------------------------
# ByteTrack behaviour
# ---------------------------------------------------------------------------


def test_second_pass_survives_low_confidence_occlusion():
    """The headline ByteTrack case: a faded element (score below the
    high band) for one frame must NOT break the track's identity."""
    tr = Tracker()
    t1 = tr.update([Detection((100, 100, 140, 140), 0.9, gt_id="ok-btn")])
    assert len(t1) == 1
    tid = t1[0].track_id

    # Occluded/faded frame: only a low-confidence sighting.
    t2 = tr.update([Detection((103, 100, 143, 140), 0.3, gt_id="ok-btn")])
    assert [t.track_id for t in t2] == [tid]
    assert t2[0].hits == 2

    # Fully visible again: still the same identity, no switch.
    t3 = tr.update([Detection((106, 100, 146, 140), 0.95, gt_id="ok-btn")])
    assert [t.track_id for t in t3] == [tid]
    assert tr.id_switches == 0


def test_low_confidence_detections_never_birth_tracks():
    tr = Tracker()
    for _ in range(3):
        out = tr.update([Detection((10, 10, 50, 50), 0.3, gt_id="ghost")])
    assert out == []
    assert tr.id_switches == 0


def test_coasting_survives_brief_dropout():
    tr = Tracker(max_age=5)
    t1 = tr.update([Detection((0, 0, 40, 40), 0.9, gt_id="toast")])
    tid = t1[0].track_id
    tr.update([])  # gone for two frames
    tr.update([])
    t4 = tr.update([Detection((2, 0, 42, 40), 0.9, gt_id="toast")])
    assert [t.track_id for t in t4] == [tid]
    assert tr.id_switches == 0


def test_death_and_rebirth_counts_an_id_switch():
    tr = Tracker(max_age=1)
    t1 = tr.update([Detection((0, 0, 40, 40), 0.9, gt_id="popup")])
    first_id = t1[0].track_id
    tr.update([])  # age 1
    tr.update([])  # age 2 > max_age -> track dies
    assert tr.update([]) == []
    t5 = tr.update([Detection((0, 0, 40, 40), 0.9, gt_id="popup")])
    assert len(t5) == 1
    assert t5[0].track_id != first_id
    assert tr.id_switches == 1


def test_two_elements_keep_distinct_ids():
    tr = Tracker()
    tr.update(
        [
            Detection((0, 0, 30, 30), 0.9, gt_id="a"),
            Detection((200, 200, 240, 240), 0.9, gt_id="b"),
        ]
    )
    out = tr.update(
        [
            Detection((3, 0, 33, 30), 0.9, gt_id="a"),
            Detection((197, 200, 237, 240), 0.88, gt_id="b"),
        ]
    )
    by_gt = {t.gt_id: t.track_id for t in out}
    assert by_gt["a"] == 1 and by_gt["b"] == 2
    assert tr.id_switches == 0


def test_below_low_threshold_is_ignored_entirely():
    tr = Tracker()
    out = tr.update([Detection((0, 0, 10, 10), 0.05)])
    assert out == []
