"""The trained fuel model as a second hub counter, blended with the colour one.

The colour counter (hubcount.CrossingCounter) over-counts: on Einstein 1 it
read 99% / 117% of the official totals at the buzzer, because balls that clip
the rim and drop behind the hub look like scores from in front. The model
counter (detections on a crop around the hub -> trackvis.BallTracker ->
count.BallCounter) under-counts the other way, 81% / 103%, because the
tracker loses balls fired in streams. They fail on different balls, so a
weighted mean of the two is nearer than either: on Einstein 1, the one
Einstein match fuel_relabel.pt never trained on, colour alone was 9.1% mean
error against the official checkpoints, the model alone 12.1%, the mean of
the two 7.8% (CHANGELOG, 2026-10-02).

Both halves are cumulative and never go down, so a weighted mean of them,
rounded, never goes down either -- the feed's rule holds.

The model half is slower to report: BallCounter confirms a score only after
the ball has been gone `vanish` frames and nothing has reappeared near it for
`reacquire` frames. Half of every ball therefore arrives that much later than
the colour half; DEFAULT_MODEL holds the hold to what the tuning measured.

`ModelCounter` is pure state (no torch, no OpenCV) so it is tested without a
model; `ModelEye` runs the network and needs requirements-detect.txt.
"""
from __future__ import annotations

import math
import os
import sys
from typing import Dict, List, Optional, Sequence, Tuple

from .count import REACQUIRE_PX, BallCounter
from .trackvis import ASSIST_CONF, BallTracker

Point = Tuple[float, float]
XYXY = Tuple[float, float, float, float]

# Geometry of the crop the model sees, in widths of the hub's outline. Chosen
# on the broadcasts, where an outline was ~200 px wide: a 640 px square, 140 px
# above the outline's centre (shots arc in from above), detections kept within
# 250 px of it. On 640 px crops at full resolution moving-ball recall was 93%
# against 84% for the whole 1920 px frame at imgsz 960, at the same cost.
CROP_WIDTHS = 3.2
CROP_UP_WIDTHS = 0.7
NEAR_WIDTHS = 1.25
CROP_MIN_PX = 640
MODEL_IMGSZ = 640
MODEL_FPS = 30.0        # tuned at 30 fps: every other frame of a 60 fps source
DET_CONF = 0.05         # what the model reports; the tracker uses >= KEEP_CONF
KEEP_CONF = 0.1
# A model that cannot keep up is worse than none: its half of the blend
# stops rising, so the zone reads about half the colour count. Run on 4 CPU
# cores it skipped ~90% of frames and the page read 52 against colour's 103
# after 40 s of Einstein 1. Past MODEL_WARMUP_FRAMES offered, a model that
# has skipped more than MODEL_MAX_SKIP of them is dropped for the session.
MODEL_WARMUP_FRAMES = 300       # 10 s at 30 fps: loading and first inference
# ... or 10 s of wall time, whichever comes first. A slow CPU also drags the
# camera loop down with it -- frc-fms's runner fell from 60 to 13 fps with the
# combo on 4 cores -- so 300 frames took 45 s to arrive and the counting was
# wrong for all of AUTO before the model was dropped.
MODEL_WARMUP_S = 10.0
MODEL_MIN_OFFERED = 30          # but never judged on fewer frames than this
MODEL_MAX_SKIP = 0.2

# Tuned on Einstein 4/5/8/1 with fuel_relabel.pt (deploy/HUB_FEED.md, "The
# combo, tuned"): the BallCounter windows in 30 fps frames, the hub box's pad
# in outline widths (15 px on a 194 px broadcast outline), and whether
# colour_assist adds moving yellow blobs. The settings the three-match fits
# agreed on; they also hold a score for the shortest time tried -- gone 2
# frames, not back within 2 -- about 170 ms at 30 fps where reacquire 6 took
# 300 ms, which the AUTO call's ~200 ms budget needs.
DEFAULT_MODEL = {"min_track": 2, "vanish": 2, "reacquire": 2,
                 "require_entry": True, "pad": 0.08, "assist": False}
# The model's share. 0.7 fit Einstein 4/5/8 best, but the model trained on
# those three; on Einstein 1, which it never saw, 0.5-0.6 was better (4.3% /
# 4.2% against 4.9% at 0.7) and the fits were flat from 0.5 to 0.7 (6.9 /
# 6.6 / 6.7%), so the plain mean -- the least trust in the model.
DEFAULT_WEIGHT = 0.5

# `model: built-in` (cams.json, the page's "Use built-in model", frc-fms's
# vision.yaml) is fuel_relabel.pt shipped inside the Hub Counter app, or
# models/fuel_relabel.pt in a checkout. A name rather than the file's path,
# because the path inside an app changes when the app is moved or updated
# and the saved setup would then point at nothing.
BUILTIN = "built-in"
BUILTIN_FILE = "fuel_relabel.pt"
_CHECKOUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bundled_model() -> Optional[str]:
    """Path of the built-in model, or None in a build or checkout without it."""
    roots = [getattr(sys, "_MEIPASS", None), _CHECKOUT]
    for r in roots:
        p = os.path.join(r, "models", BUILTIN_FILE) if r else ""
        if p and os.path.isfile(p):
            return p
    return None


def resolve_weights(weights: str) -> str:
    """A weights path as given, or the built-in model's real path."""
    if weights == BUILTIN:
        p = bundled_model()
        if p is None:
            raise FileNotFoundError(f"no built-in fuel model here: put {BUILTIN_FILE} "
                                    f"in models/ or choose the .pt file")
        return p
    return weights
REF_OUTLINE_PX = 194.0  # the broadcast outline width REACQUIRE_PX was set on


def crop_box(poly: Sequence[Point], width: int, height: int) -> Tuple[int, int, int, int]:
    """The square the model is run on for one outline, inside the frame."""
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    w = max(max(xs) - min(xs), 1.0)
    side = int(min(max(CROP_MIN_PX, CROP_WIDTHS * w), width, height))
    cx = (min(xs) + max(xs)) / 2
    cy = (min(ys) + max(ys)) / 2 - CROP_UP_WIDTHS * w
    x0 = int(min(max(cx - side / 2, 0), width - side))
    y0 = int(min(max(cy - side / 2, 0), height - side))
    return x0, y0, x0 + side, y0 + side


class ModelCounter:
    """One hub from model detections: BallTracker -> BallCounter on the
    outline's bounding box. Pure state; `update` takes one model frame's
    detections in full-frame pixels and returns the rise in `reported`."""

    def __init__(self, poly: Sequence[Point], fps: float = MODEL_FPS,
                 min_track: int = DEFAULT_MODEL["min_track"],
                 vanish: int = DEFAULT_MODEL["vanish"],
                 reacquire: int = DEFAULT_MODEL["reacquire"],
                 require_entry: bool = DEFAULT_MODEL["require_entry"],
                 pad: float = DEFAULT_MODEL["pad"],   # outline widths
                 assist: bool = DEFAULT_MODEL["assist"]):
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        self.box = (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
        width = max(self.box[2], 1.0)
        self.margin = NEAR_WIDTHS * width
        self.assist = assist
        self.tracker = BallTracker()
        self.counter = BallCounter({"hub": self.box}, min_track_frames=min_track,
                                   vanish_frames=vanish,
                                   reacquire_frames=reacquire,
                                   reacquire_px=REACQUIRE_PX * width / REF_OUTLINE_PX,
                                   require_entry=require_entry,
                                   pad=pad * width, fps=fps)
        self.frame = 0
        self.reported = 0

    def near(self, b: XYXY) -> bool:
        x, y, w, h = self.box
        m = self.margin
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        return x - m <= cx <= x + w + m and y - m <= cy <= y + h + m

    def update(self, t: float, dets: Sequence[Tuple[XYXY, float]],
               assist: Sequence[XYXY] = ()) -> int:
        return self.count(t, self.track(dets, assist))

    def track(self, dets: Sequence[Tuple[XYXY, float]],
              assist: Sequence[XYXY] = ()) -> Dict[int, XYXY]:
        """The tracker's step. Split from `count` so the tuning could replay
        one tracking pass under every counter setting."""
        keep = [(b, c) for b, c in dets if c >= KEEP_CONF and self.near(b)]
        if self.assist:
            keep += [(tuple(b), ASSIST_CONF) for b in assist if self.near(b)]
        return self.tracker.update(keep)

    def count(self, t: float, got: Dict[int, XYXY]) -> int:
        self.counter.update(self.frame, t, {tid: (b[0], b[1], b[2] - b[0], b[3] - b[1])
                                            for tid, b in got.items()})
        self.frame += 1
        now = self.counter.totals.get("hub", 0)
        rise = max(0, now - self.reported)
        self.reported += rise
        return rise


def blend(colour: int, model: int, weight: float) -> int:
    """The zone's count: round(weight * model + (1 - weight) * colour).
    Monotone in both, so it never goes down while they do not."""
    return int(math.floor(weight * model + (1.0 - weight) * colour + 0.5))


class ModelEye:
    """Runs the model on every model zone of one camera, one batch a frame.

    Loads Ultralytics lazily: the API host and the colour-only counter never
    import torch. `device` is passed to Ultralytics; "" picks CUDA, then
    Apple MPS, then CPU (`pick_device`). On a 4-core CPU two 640 px crops
    took 0.3 s, too slow to keep up live; a laptop GPU or Apple silicon is
    needed.
    """

    def __init__(self, weights: str, polys: Dict[str, Sequence[Point]],
                 device: str = ""):
        from ultralytics import YOLO
        self.model = YOLO(resolve_weights(weights))
        self.device = device or pick_device()
        self.polys = dict(polys)
        self.crops: Dict[str, Tuple[int, int, int, int]] = {}
        self.prev = None
        self.ball_px = 18.0

    def detect(self, frame) -> Tuple[List[Tuple[XYXY, float]], List[XYXY]]:
        """Every crop's detections in full-frame pixels, and colour_assist's
        moving yellow blobs the model did not report."""
        from .trackvis import colour_assist
        if not self.crops:
            h, w = frame.shape[:2]
            self.crops = {n: crop_box(p, w, h) for n, p in self.polys.items()}
        names = list(self.crops)
        imgs = [frame[y0:y1, x0:x1] for x0, y0, x1, y1 in (self.crops[n] for n in names)]
        rs = self.model.predict(imgs, imgsz=MODEL_IMGSZ, conf=DET_CONF,
                                max_det=1500, classes=[0], verbose=False,
                                device=self.device)
        dets: List[Tuple[XYXY, float]] = []
        for n, r in zip(names, rs):
            x0, y0 = self.crops[n][:2]
            for b, c in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist()):
                dets.append(((b[0] + x0, b[1] + y0, b[2] + x0, b[3] + y0), float(c)))
        assist: List[XYXY] = []
        if self.prev is not None:
            strong = sorted(max(b[2] - b[0], b[3] - b[1]) for b, c in dets if c >= KEEP_CONF)
            if strong:
                self.ball_px = 0.8 * self.ball_px + 0.2 * strong[len(strong) // 2]
            assist = [e[0] for e in colour_assist(frame, self.prev,
                                                  [b for b, c in dets if c >= KEEP_CONF],
                                                  self.ball_px)]
        self.prev = frame
        return dets, assist


def pick_device() -> str:
    """CUDA, then Apple MPS, then CPU. Ultralytics left to itself never picks
    MPS: on a Mac it would run the model on the CPU, which on 4 cores
    managed 2.6 fps against the 30 the counter was tuned at."""
    import torch
    if torch.cuda.is_available():
        return "cuda"
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    return "cpu"


def too_slow(offered: int, skipped: int, elapsed: float = 0.0) -> bool:
    """Has the model fallen far enough behind to be dropped? Judged after
    MODEL_WARMUP_FRAMES offered or MODEL_WARMUP_S seconds, whichever is first."""
    warm = offered >= MODEL_WARMUP_FRAMES or (
        elapsed >= MODEL_WARMUP_S and offered >= MODEL_MIN_OFFERED)
    return warm and skipped > MODEL_MAX_SKIP * offered


def _close_all() -> None:
    """At exit, let each model thread finish its frame. A daemon thread
    still inside torch when Python shuts down aborts the process
    ("terminate called without an active exception"); the plugin run lost
    its printed result that way, and frc-fms's runner would end the same
    way on Ctrl-C."""
    for w in list(_LIVE):
        w.close(wait=2.0)


import atexit as _atexit
import weakref as _weakref
_LIVE: "_weakref.WeakSet" = _weakref.WeakSet()
_atexit.register(_close_all)


class ModelWorker:
    """The model half on its own thread, fed the newest frame.

    `offer` never blocks: a frame the model has not got to is replaced by
    the next one and counted as skipped. The colour half never waits for
    the model -- a frame held for it would be a frame late for the AUTO
    call. Shared by hubcount.run (our counter) and fms_counter.ComboCounter
    (inside frc-fms's runner), so both skip and give up the same way.

    `counters` maps a name to its ModelCounter; `on_rise(name, tag)` is
    called from the worker thread when one rises, `tag` being what was
    offered with the frame (our counter passes its capture time, which
    timestamps the score). When the model is `too_slow`, it
    stops, `off` turns true and `on_off(offered, skipped, tag)` is called once.
    `on_behind(skipped)` is called at most every 10 s while frames are being
    skipped. An exception from the model stops the worker and is kept in
    `error` (and passed to `on_error`) -- a CUDA/MPS failure must not pass
    silently.
    """

    def __init__(self, eye, counters: Dict[str, ModelCounter], on_rise,
                 on_off=None, on_behind=None, on_error=None, name: str = "model"):
        import threading
        self.eye = eye
        self.counters = counters
        self.on_rise, self.on_off = on_rise, on_off
        self.on_behind, self.on_error = on_behind, on_error
        self.offered = self.skipped = 0
        self.off = False
        self.error = ""
        self._job = None
        self._t0 = 0.0
        self._busy = False
        self._reset = False
        self._done = False
        self._ready = threading.Condition()
        self._thread = threading.Thread(target=self._loop, daemon=True, name=name)
        self._thread.start()
        _LIVE.add(self)

    def offer(self, frame, t: float, tag=None) -> None:
        if self.off or self._done:
            return
        with self._ready:
            if self.offered == 0:
                import time
                self._t0 = time.monotonic()
            self.offered += 1
            if self._job is not None:
                self.skipped += 1
            self._job = (frame, t, tag)
            self._ready.notify()

    def reset(self) -> None:
        """After a gap in the picture: forget tracks and the last frame, for
        the reason the colour half forgets its blobs."""
        with self._ready:
            self._job, self._reset = None, True

    def wait_idle(self, timeout: float = 30.0) -> None:
        """Block until the model has finished every frame offered. For a
        recording read faster than real time, where nothing may be skipped."""
        import time
        end = time.monotonic() + timeout
        while ((self._job is not None or self._busy) and not self._done
               and time.monotonic() < end):
            time.sleep(0.002)

    def close(self, wait: float = 0.0) -> None:
        with self._ready:
            self._done = True
            self._ready.notify()
        if wait:
            self._thread.join(wait)

    def _loop(self) -> None:
        import time
        said = time.monotonic()
        while True:
            with self._ready:
                while self._job is None and not self._done:
                    self._ready.wait(0.1)
                if self._done:
                    return
                job, self._job = self._job, None
                reset, self._reset = self._reset, False
                self._busy = True
            if reset:
                self.eye.prev = None
                for mc in self.counters.values():
                    mc.tracker = BallTracker()
            frame, t, tag = job
            try:
                dets, assist = self.eye.detect(frame)
            except Exception as e:
                self.error = str(e)
                self._done = True
                if self.on_error:
                    self.on_error(e)
                return
            for name, mc in self.counters.items():
                if mc.update(t, dets, assist):
                    self.on_rise(name, tag)
            self._busy = False
            if too_slow(self.offered, self.skipped, time.monotonic() - self._t0):
                self.off = True
                self._done = True
                if self.on_off:
                    self.on_off(self.offered, self.skipped, tag)
                return
            if self.skipped and self.on_behind and time.monotonic() - said > 10.0:
                said = time.monotonic()
                self.on_behind(self.skipped)


def model_stride(fps: float) -> int:
    """Run the model on every n-th frame so it sees ~MODEL_FPS: the counter's
    windows were tuned there, and every frame doubles the cost for nothing
    measured (the model counter at 60 fps was 25% vs 17% at 30)."""
    return max(1, int(round((fps or MODEL_FPS) / MODEL_FPS)))


def parse_model(raw: Optional[Dict]) -> Optional[Dict]:
    """A camera's "model" entry from the setup file, checked and filled in."""
    if not raw:
        return None
    if not raw.get("weights"):
        raise ValueError("model: needs \"weights\", the .pt file")
    w = float(raw.get("weight", DEFAULT_WEIGHT))
    if not 0.0 <= w <= 1.0:
        raise ValueError(f"model: weight is the model's share, 0 to 1 (got {w})")
    cfg = dict(DEFAULT_MODEL)
    cfg.update({k: raw[k] for k in DEFAULT_MODEL if k in raw})
    return {"weights": str(raw["weights"]), "weight": w,
            "device": str(raw.get("device") or ""), "counter": cfg}
