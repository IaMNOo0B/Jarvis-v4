"""Step 04 - Computer Vision: ByteTrack-style tracking of UI elements.

Treat UI elements in successive screenshots as *tracks*: predict each
track with a constant-velocity Kalman filter, associate predictions to
detections with the Hungarian algorithm on cost ``1 - IoU``, and manage
the track lifecycle.  The ByteTrack insight (study Part B, tracking
lesson) is implemented faithfully:

* Detections are split into high-confidence and low-confidence bands.
* Pass 1 associates tracks to high-confidence detections.
* Pass 2 re-matches the still-unmatched tracks against the
  low-confidence detections at a looser IoU gate - a faded or briefly
  occluded element is re-found instead of being declared gone.
* Low-confidence detections NEVER birth a track.
* Unmatched tracks coast on their Kalman prediction for ``max_age``
  frames before dying.

Identity quality is measurable: when a detection carries a ground-truth
id (``gt_id``, supplied by an evaluation harness), an *ID switch* is
counted whenever that ground-truth identity becomes bound to a
different track id than before - the standard MOT definition.  See
``Tracker.id_switches``.

The Kalman filter uses the classical state ``(cx, cy, w, h, vx, vy, vw,
vh)`` and the matrix math (multiply, transpose, Gauss-Jordan inverse)
is implemented by hand on lists - no numpy, no scipy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

BIG_COST = 1e6  # "forbidden pair" cost fed to the Hungarian solver


# ---------------------------------------------------------------------------
# Small dense-matrix helpers (lists of lists of floats)
# ---------------------------------------------------------------------------


def _mat_mul(a, b):
    n, m, p = len(a), len(b), len(b[0])
    out = [[0.0] * p for _ in range(n)]
    for i in range(n):
        for k in range(m):
            aik = a[i][k]
            if aik == 0.0:
                continue
            bk = b[k]
            oi = out[i]
            for j in range(p):
                oi[j] += aik * bk[j]
    return out


def _mat_vec_mul(a, v):
    return [sum(row[k] * v[k] for k in range(len(v))) for row in a]


def _transpose(a):
    return [list(col) for col in zip(*a)]


def _mat_add(a, b):
    return [[x + y for x, y in zip(ra, rb)] for ra, rb in zip(a, b)]


def _mat_sub(a, b):
    return [[x - y for x, y in zip(ra, rb)] for ra, rb in zip(a, b)]


def _identity(n):
    return [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]


def _mat_inv(a):
    """Gauss-Jordan inverse with partial pivoting (small dense matrices)."""
    n = len(a)
    aug = [list(a[i]) + _identity(n)[i] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[piv][col]) < 1e-12:
            raise ValueError("singular matrix")
        aug[col], aug[piv] = aug[piv], aug[col]
        inv_piv = 1.0 / aug[col][col]
        aug[col] = [v * inv_piv for v in aug[col]]
        for r in range(n):
            if r != col and aug[r][col] != 0.0:
                factor = aug[r][col]
                aug[r] = [v - factor * w for v, w in zip(aug[r], aug[col])]
    return [row[n:] for row in aug]


# ---------------------------------------------------------------------------
# Hungarian assignment (min-cost, rectangular), O(n^2 * m) potentials method
# ---------------------------------------------------------------------------


def hungarian(cost):
    """Solve the assignment problem for a rectangular cost matrix.

    ``cost`` is a list of ``n`` rows of ``m`` numbers.  Returns a list of
    ``(row, col)`` pairs minimising total cost; every row is assigned
    when ``n <= m`` (if ``n > m`` the matrix is transposed internally
    and every column gets a row).  Implemented from scratch (the
    potentials / shortest-augmenting-path formulation).
    """
    n = len(cost)
    m = len(cost[0]) if n else 0
    if n == 0 or m == 0:
        return []
    transposed = False
    a = cost
    if n > m:
        a = _transpose(cost)
        n, m = m, n
        transposed = True
    # 1-indexed potentials algorithm; a[i][j], i in 1..n, j in 1..m.
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)  # p[j] = row assigned to column j (0 = none)
    way = [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [float("inf")] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = float("inf")
            j1 = 0
            row = a[i0 - 1]
            for j in range(1, m + 1):
                if used[j]:
                    continue
                cur = row[j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    pairs = []
    for j in range(1, m + 1):
        if p[j]:
            r, c = p[j] - 1, j - 1
            pairs.append((c, r) if transposed else (r, c))
    return pairs


def assignment_cost(cost, pairs) -> float:
    return sum(cost[r][c] for r, c in pairs)


# ---------------------------------------------------------------------------
# Kalman filter: constant velocity over (cx, cy, w, h)
# ---------------------------------------------------------------------------


class KalmanBoxFilter:
    """8-state constant-velocity Kalman filter for one bounding box.

    State: ``[cx, cy, w, h, vx, vy, vw, vh]``; measurement: ``[cx, cy,
    w, h]`` derived from a detection box.  Noise values are modest and
    fixed: the filter's job here is smooth prediction through brief
    dropouts, not sensor fusion.
    """

    def __init__(self, box):
        cx, cy, w, h = _box_to_cxcywh(box)
        self.x = [cx, cy, w, h, 0.0, 0.0, 0.0, 0.0]
        # P0: positions/sizes fairly sure, velocities unknown.
        self.P = [[0.0] * 8 for _ in range(8)]
        for i in range(4):
            self.P[i][i] = 10.0
            self.P[i + 4][i + 4] = 1000.0
        self._F = _identity(8)
        for i in range(4):
            self._F[i][i + 4] = 1.0  # dt = 1 frame
        self._Q = [[0.0] * 8 for _ in range(8)]
        for i in range(4):
            self._Q[i][i] = 1.0
            self._Q[i + 4][i + 4] = 0.05
        self._R = [[1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]

    def predict(self):
        self.x = _mat_vec_mul(self._F, self.x)
        self.P = _mat_add(_mat_mul(_mat_mul(self._F, self.P), _transpose(self._F)), self._Q)
        return self.box()

    def update(self, box):
        z = list(_box_to_cxcywh(box))
        # y = z - Hx  (H picks the first four state components)
        y = [z[i] - self.x[i] for i in range(4)]
        p_ht = [[self.P[i][j] for j in range(4)] for i in range(8)]  # P H^T (8x4)
        s = _mat_add([[self.P[i][j] for j in range(4)] for i in range(4)], self._R)
        k = _mat_mul(p_ht, _mat_inv(s))  # Kalman gain, 8x4
        for i in range(8):
            self.x[i] += sum(k[i][j] * y[j] for j in range(4))
        # P = (I - K H) P ; K H is 8x8 with only the first 4 columns of K used.
        kh = [[0.0] * 8 for _ in range(8)]
        for i in range(8):
            for j in range(4):
                kh[i][j] = k[i][j]
        self.P = _mat_mul(_mat_sub(_identity(8), kh), self.P)
        return self.box()

    def box(self):
        cx, cy, w, h = self.x[:4]
        return (cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)


def _box_to_cxcywh(box):
    x0, y0, x1, y1 = (float(v) for v in box)
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0, x1 - x0, y1 - y0)


def iou(a, b) -> float:
    ax0, ay0, ax1, ay1 = (float(v) for v in a)
    bx0, by0, bx1, by1 = (float(v) for v in b)
    ix = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    iy = max(0.0, min(ay1, by1) - max(ay0, by0))
    inter = ix * iy
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    union = area_a + area_b - inter
    return inter / union if union > 0.0 else 0.0


# ---------------------------------------------------------------------------
# Detections, tracks, and the ByteTrack-style tracker
# ---------------------------------------------------------------------------


@dataclass
class Detection:
    box: tuple  # (x0, y0, x1, y1) floats
    score: float
    label: str = ""
    gt_id: object = None  # ground-truth identity, set only by eval harnesses


@dataclass
class Track:
    track_id: int
    kalman: KalmanBoxFilter
    box: tuple
    score: float = 0.0
    label: str = ""
    hits: int = 1
    age: int = 0  # frames since last successful association
    gt_id: object = None

    def predict(self):
        self.box = self.kalman.predict()
        return self.box


class Tracker:
    """ByteTrack-style two-pass associator for screenshot elements.

    Parameters mirror the paper/lesson: ``high_thresh`` splits the
    detection bands, ``low_thresh`` is the floor below which a
    detection is ignored entirely, ``match_iou`` gates pass 1 and
    ``second_match_iou`` (looser) gates pass 2, ``max_age`` is how many
    consecutive unmatched frames a track may coast before it dies.
    """

    def __init__(
        self,
        high_thresh: float = 0.5,
        low_thresh: float = 0.1,
        match_iou: float = 0.3,
        second_match_iou: float = 0.2,
        max_age: int = 5,
    ):
        self.high_thresh = high_thresh
        self.low_thresh = low_thresh
        self.match_iou = match_iou
        self.second_match_iou = second_match_iou
        self.max_age = max_age
        self.tracks: list[Track] = []
        self._next_id = 1
        self.id_switches = 0
        self._gt_binding: dict = {}

    # -- identity bookkeeping -------------------------------------------------

    def _bind_gt(self, track: Track, det: Detection):
        if det.gt_id is None:
            return
        prev = self._gt_binding.get(det.gt_id)
        if prev is not None and prev != track.track_id:
            self.id_switches += 1
        self._gt_binding[det.gt_id] = track.track_id
        track.gt_id = det.gt_id

    # -- association ----------------------------------------------------------

    def _associate(self, tracks, dets, iou_gate):
        """Hungarian-match ``tracks`` to ``dets`` on cost 1 - IoU.

        Pairs whose IoU falls below ``iou_gate`` are forbidden (given
        BIG_COST) and filtered out of the result.
        """
        if not tracks or not dets:
            return [], list(range(len(tracks))), list(range(len(dets)))
        cost = []
        for t in tracks:
            row = []
            for d in dets:
                overlap = iou(t.box, d.box)
                row.append(1.0 - overlap if overlap >= iou_gate else BIG_COST)
            cost.append(row)
        matches = []
        matched_t, matched_d = set(), set()
        for r, c in hungarian(cost):
            if cost[r][c] < BIG_COST / 2:
                matches.append((r, c))
                matched_t.add(r)
                matched_d.add(c)
        free_t = [i for i in range(len(tracks)) if i not in matched_t]
        free_d = [j for j in range(len(dets)) if j not in matched_d]
        return matches, free_t, free_d

    # -- main entry -----------------------------------------------------------

    def update(self, detections) -> list:
        """Advance one frame.  Returns the live tracks after the update."""
        detections = list(detections)
        for t in self.tracks:
            t.predict()

        high = [d for d in detections if d.score >= self.high_thresh]
        low = [d for d in detections if self.low_thresh <= d.score < self.high_thresh]

        matched_track_ids = set()

        # Pass 1: all tracks vs high-confidence detections.
        matches, free_t, free_high = self._associate(self.tracks, high, self.match_iou)
        for ti, di in matches:
            track, det = self.tracks[ti], high[di]
            track.box = track.kalman.update(det.box)
            track.score, track.label = det.score, det.label
            track.hits += 1
            track.age = 0
            matched_track_ids.add(track.track_id)
            self._bind_gt(track, det)

        # Pass 2 (ByteTrack): leftover tracks vs low-confidence detections.
        leftover = [self.tracks[i] for i in free_t]
        matches2, free_t2, _free_low = self._associate(leftover, low, self.second_match_iou)
        for ti, di in matches2:
            track, det = leftover[ti], low[di]
            track.box = track.kalman.update(det.box)
            track.score, track.label = det.score, det.label
            track.hits += 1
            track.age = 0
            matched_track_ids.add(track.track_id)
            self._bind_gt(track, det)

        # Age unmatched tracks; kill the ones that coasted too long.
        survivors = []
        for t in self.tracks:
            if t.track_id not in matched_track_ids:
                t.age += 1
            if t.age <= self.max_age:
                survivors.append(t)
        self.tracks = survivors

        # Births: only from still-unmatched HIGH-confidence detections.
        for di in free_high:
            det = high[di]
            track = Track(
                track_id=self._next_id,
                kalman=KalmanBoxFilter(det.box),
                box=tuple(float(v) for v in det.box),
                score=det.score,
                label=det.label,
            )
            self._next_id += 1
            self._bind_gt(track, det)
            self.tracks.append(track)

        return sorted(self.tracks, key=lambda t: t.track_id)


__all__ = [
    "Detection",
    "Track",
    "Tracker",
    "KalmanBoxFilter",
    "hungarian",
    "assignment_cost",
    "iou",
]
