"""Setup -> Cameras -> Calibrate: fit a hub's one-ball size from a drop test.

The colour counter's count depends steeply on the one-ball pixel area it is given: on the 2026-10-10
recording, half the right area counted 302 against 112 and double counted 46, and measuring lone balls
mid-match gave 150-230 px against the right 693 (the event's ~9x overcount was a wrong area). So the
area is not measured or guessed: someone drops a known number of balls into the hub while vision
records every frame's yellow blobs near the outline, and the counter is replayed over those blobs at
log-spaced areas. The area kept is the middle of the run of areas whose count is nearest the number
dropped. No cv2 (the blobs come from the camera thread), so tests can run it.
"""
from __future__ import annotations

import math

from hubcounter import hubcount as HC

AREA_LO, AREA_HI = 30.0, 30000.0
COARSE, FINE = 25, 17        # 33% apart over the whole range, then 4% apart around the best
RECORD_BOX_AREA = 4000.0     # record a generous box (the area isn't known yet), as hubcount.measure does
MAX_FRAMES = 60 * 60 * 3     # 3 minutes at 60 fps
BLUR = HC.DEFAULT_BLUR       # what the live counter uses (vision/hubcounter/fms_counter.py)


def record_box(poly, w, h):
    return HC.region_of(poly, RECORD_BOX_AREA, w, h)


def replay(poly, frames, area, w, h, blur=BLUR) -> int:
    """The count the live counter would have reported over these frames at this ball area. Blobs are
    kept only inside the box the live counter would search at that area."""
    x0, y0, x1, y1 = HC.region_of(poly, area, w, h)
    c = HC.CrossingCounter(poly, area, blur)
    for blobs in frames:
        c.update([b for b in blobs if x0 <= b[0] < x1 and y0 <= b[1] < y1])
    return c.reported


def _search(poly, frames, n, w, h, blur, areas):
    return [(a, replay(poly, frames, a, w, h, blur)) for a in areas]


def _logspace(lo, hi, steps):
    k = (hi / lo) ** (1 / (steps - 1))
    return [lo * k ** i for i in range(steps)]


def fit(poly, frames, n, w, h, blur=BLUR) -> dict:
    """{"area", "got", "n", "lo", "hi"}: a ball area whose replayed count is nearest n (got), from the
    range of areas (lo-hi) that count the same. ValueError if nothing crossed the outline.

    Inside that range the area kept is a quarter of the way up (in log space). A one-ball blob of true
    area A counts once for areas from about 0.6A (below that it counts 2) to 3.4A (smaller than
    hubcount's noise floor); a two-ball clump counts 2 only up to about 1.2A. Single drops alone give
    about 0.6A-3.4A, whose quarter point is 0.9A; drops with clumps give about 0.6A-1.2A, quarter point
    0.7A. Both land where clumps and singles both count right; the middle (1.4A) would not."""
    if n < 1:
        raise ValueError("drop at least one ball")
    coarse = _search(poly, frames, n, w, h, blur, _logspace(AREA_LO, AREA_HI, COARSE))
    if not any(c for _, c in coarse):
        raise ValueError("Nothing crossed the outline. Check that the outline is around the hub's opening "
                         "and the balls fell through it in the picture.")
    best = min(abs(c - n) for _, c in coarse)
    idx = [i for i, (_, c) in enumerate(coarse) if abs(c - n) == best]
    lo = coarse[max(0, idx[0] - 1)][0]
    hi = coarse[min(COARSE - 1, idx[-1] + 1)][0]
    pts = sorted(set(_search(poly, frames, n, w, h, blur, _logspace(lo, hi, FINE)) + coarse))
    err = [abs(c - n) for _, c in pts]
    best = min(err)
    runs, cur = [], []                       # runs of neighbouring areas at the best error
    for i, e in enumerate(err):
        if e == best:
            cur.append(i)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    run = max(runs, key=len)
    lo, hi = pts[run[0]][0], pts[run[-1]][0]
    area = lo * (hi / lo) ** 0.25
    return {"area": round(area, 1), "got": replay(poly, frames, area, w, h, blur), "n": n,
            "lo": round(lo, 1), "hi": round(hi, 1)}
