"""Draw a model's tracks on a video: small labels, trails, and gaps bridged.

    python3 run.py track --weights models/fuel_best.pt --source match.mp4

What `YOLO(...).predict(save=True)` drew was a "fuel 0.85" label on every one
of ~300 balls a frame, which buried the picture, and no tracking at all. This
tracks with `fuel_track.yaml`, draws thin boxes with at most a small id, and
a trail behind each moving ball.

A ball fades at the top of its arc -- it slows, it is small, it is against
the crowd -- and the detector drops it for a few frames. `Coaster` keeps
drawing it where its last motion says it is, as a hollow circle, for up to
`coast` frames, so a flight reads as one flight. It is display only: nothing
is counted from a coasted position.

Streams frame by frame, so a whole match cannot run the machine out of
memory the way predict(..., save=True) without stream=True did.
"""
from __future__ import annotations

import math
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, Tuple

TRACKER = str(Path(__file__).with_name("fuel_track.yaml"))
Box = Tuple[float, float, float, float]          # x1, y1, x2, y2


class BallTracker:
    """Follows detections frame to frame by DISTANCE, not box overlap.

    ByteTrack (fuel_track.yaml) links a detection to a track by how much its
    box overlaps the track's straight-line prediction. A ball 12-20 px wide
    turning over at the top of its arc slows, stops and falls: the
    prediction and the ball stop overlapping, and the track breaks. On 4 s of
    Einstein 4, 19 of 20 flights ByteTrack lost while rising or at the top
    still had a detection of confidence 0.1-0.8 right where the ball was in
    the following frames -- the detector saw it; the linking dropped it --
    and only 1 of 25 flying tracks got through its apex.

    This predicts each track from its last velocity and takes the nearest
    detection within a gate that grows with the ball's size, its speed and
    how long it has been missing. Confident detections (>= `start_conf`)
    may start a track; weaker ones may only continue one, the same rule
    ByteTrack's second association uses. Pure Python: tested without a model.
    """

    def __init__(self, start_conf: float = 0.25, max_missing: int = 30,
                 gate_sizes: float = 2.0, gate_speed: float = 1.0):
        self.start_conf = start_conf
        self.max_missing = max_missing
        self.gate_sizes = gate_sizes
        self.gate_speed = gate_speed
        self.tracks: Dict[int, Dict] = {}
        self.next_id = 1

    def update(self, dets: List[Tuple[Box, float]]) -> Dict[int, Box]:
        """dets: [(x1, y1, x2, y2), conf]. Returns {track id: box} matched
        this frame."""
        cand = []
        for tid, t in self.tracks.items():
            gap = t["missing"] + 1
            px = t["c"][0] + t["v"][0] * gap
            py = t["c"][1] + t["v"][1] * gap
            speed = math.hypot(*t["v"])
            gate = (self.gate_sizes * t["size"] + self.gate_speed * speed) * (1 + 0.25 * t["missing"])
            for j, (bx, cf) in enumerate(dets):
                cx, cy = (bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2
                d = math.hypot(cx - px, cy - py)
                if d <= gate:
                    # a size far off the track's is another object passing
                    size = max(bx[2] - bx[0], bx[3] - bx[1])
                    if 0.4 <= size / max(t["size"], 1.0) <= 2.5:
                        cand.append((d / max(gate, 1e-6), tid, j))
        out: Dict[int, Box] = {}
        used_t, used_d = set(), set()
        for _, tid, j in sorted(cand):
            if tid in used_t or j in used_d:
                continue
            used_t.add(tid)
            used_d.add(j)
            t = self.tracks[tid]
            bx = dets[j][0]
            c = ((bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2)
            gap = t["missing"] + 1
            v = ((c[0] - t["c"][0]) / gap, (c[1] - t["c"][1]) / gap)
            # smooth the velocity a little: one jittery box must not throw
            # the next prediction off
            t["v"] = (0.6 * v[0] + 0.4 * t["v"][0], 0.6 * v[1] + 0.4 * t["v"][1]) \
                if t["age"] > 1 else v
            t["c"], t["box"], t["missing"] = c, bx, 0
            t["size"] = 0.7 * t["size"] + 0.3 * max(bx[2] - bx[0], bx[3] - bx[1])
            t["age"] += 1
            out[tid] = bx
        for tid in list(self.tracks):
            if tid not in used_t:
                self.tracks[tid]["missing"] += 1
                if self.tracks[tid]["missing"] > self.max_missing:
                    del self.tracks[tid]
        for j, (bx, cf) in enumerate(dets):
            if j in used_d or cf < self.start_conf:
                continue
            tid = self.next_id
            self.next_id += 1
            c = ((bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2)
            self.tracks[tid] = {"c": c, "v": (0.0, 0.0), "box": bx,
                                "size": max(bx[2] - bx[0], bx[3] - bx[1], 1.0),
                                "missing": 0, "age": 1}
            out[tid] = bx
        return out


class Coaster:
    """Where each track is, was, and -- for a few frames after it is lost --
    probably is. Pure state, so it is tested without a model."""

    def __init__(self, coast: int = 8, trail: int = 12):
        self.coast = coast
        self.trail = trail
        self.last: Dict[int, Tuple[int, Box, Tuple[float, float]]] = {}
        self.trails: Dict[int, deque] = {}

    def update(self, frame: int, boxes: Dict[int, Box]):
        """Returns (seen, coasting): seen {id: box}, coasting {id: box} for
        tracks missing this frame but lost for no more than `coast` frames."""
        for tid, b in boxes.items():
            c = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
            v = (0.0, 0.0)
            if tid in self.last:
                f0, b0, _ = self.last[tid]
                c0 = ((b0[0] + b0[2]) / 2, (b0[1] + b0[3]) / 2)
                dt = max(1, frame - f0)
                v = ((c[0] - c0[0]) / dt, (c[1] - c0[1]) / dt)
            self.last[tid] = (frame, b, v)
            self.trails.setdefault(tid, deque(maxlen=self.trail)).append(c)
        coasting: Dict[int, Box] = {}
        for tid, (f0, b, v) in list(self.last.items()):
            gap = frame - f0
            if gap == 0:
                continue
            if gap > self.coast:
                del self.last[tid]
                self.trails.pop(tid, None)
                continue
            if abs(v[0]) + abs(v[1]) < 0.5:
                continue            # a ball lying still is not coasted
            dx, dy = v[0] * gap, v[1] * gap
            coasting[tid] = (b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy)
        return boxes, coasting

    def trail_of(self, tid: int) -> List[Tuple[float, float]]:
        return list(self.trails.get(tid, ()))

    def moving_trail(self, tid: int, min_px: float = 12.0,
                     straightness: float = 0.6) -> List[Tuple[float, float]]:
        """The trail, only if the ball has really gone somewhere: balls in a
        pile or a robot's hopper jitter in place, and every one of them
        drawing a zigzag buried the flights on Einstein 4."""
        pts = self.trail_of(tid)
        if len(pts) < 3:
            return []
        net = math.hypot(pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1])
        path = sum(math.hypot(b[0] - a[0], b[1] - a[1])
                   for a, b in zip(pts, pts[1:]))
        if net < min_px or net < straightness * path:
            return []
        return pts


ASSIST_CONF = 0.2     # below BallTracker.start_conf: may continue, never start
ASSIST_COLOUR = (255, 0, 255)   # magenta: found by colour, not by the model


def colour_assist(frame, prev, model_boxes: List[Box], ball_px: float,
                  min_moving: float = 0.3) -> List[Tuple[Box, float]]:
    """Moving yellow balls the model did not report, found by colour.

    The released models were trained on labels from autolabel_fuel.py, whose
    field line drops every ball more than ~4 ball-widths above the far edge
    of the field -- to keep crowd shirts out -- so the high part of every
    shot sat in the training frames unlabelled, and the model learned that a
    ball against the crowd is background. On 4 s of Einstein 4, 1071 moving
    ball-shaped yellow blobs had no model box at >= 0.1: 516 had no score at
    all and 555 only 0.01-0.1, so no confidence threshold recovers them.

    A blob here is kept if it is yellow under the counter's gate, round,
    ball-sized (0.25-4x the frame's median ball area), not inside a model
    box, and at least `min_moving` of it changed since `prev` -- a shot ball
    moves, a shirt in the stands mostly does not. They are returned at
    ASSIST_CONF, below the tracker's start threshold, so colour alone can
    extend a ball the model found (a shot is detected at launch) but can
    never invent one. Needs no retraining; retraining on labels that keep
    balls in flight is still the real fix.
    """
    import math as _m

    import cv2
    import numpy as np
    from .hubcount import LOOSE_HI, LOOSE_LO

    k3 = np.ones((3, 3), np.uint8)
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = cv2.morphologyEx(cv2.inRange(hsv, np.array(LOOSE_LO, np.uint8),
                                        np.array(LOOSE_HI, np.uint8)),
                            cv2.MORPH_OPEN, k3)
    diff = cv2.absdiff(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY),
                       cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY))
    moving = cv2.dilate((diff > 25).astype(np.uint8), k3)
    a_ball = _m.pi * ball_px * ball_px / 4
    n, lab, st, cen = cv2.connectedComponentsWithStats(mask)
    out = []
    for j in range(1, n):
        x, y, w, h, a = (int(v) for v in st[j])
        if not (0.25 * a_ball <= a <= 4.0 * a_ball):
            continue
        if not (0.6 <= w / max(h, 1) <= 1.7) or a < 0.5 * w * h:
            continue
        cx, cy = cen[j]
        if any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in model_boxes):
            continue
        if moving[y:y + h, x:x + w][lab[y:y + h, x:x + w] == j].mean() < min_moving:
            continue
        out.append(((float(x), float(y), float(x + w), float(y + h)), ASSIST_CONF))
    return out


COLOURS = {"fuel": (0, 215, 255), "robot_blue": (255, 140, 30),
           "robot_red": (60, 60, 255), "hub_blue": (255, 140, 30),
           "hub_red": (60, 60, 255)}


def render(weights: str, source: str, out: str, imgsz: int = 960,
           conf: float = 0.1, labels: str = "id", trails: bool = True,
           coast: int = 8, device: Optional[str] = None,
           max_frames: int = 0, progress=print,
           tracker: str = "distance", assist: bool = True) -> Dict:
    """`tracker`: "distance" (BallTracker, one per class) or "bytetrack"
    (Ultralytics with fuel_track.yaml). `assist`: add moving yellow balls
    the model missed (`colour_assist`; distance tracker only), drawn in
    magenta so what came from colour stays visible."""
    import cv2
    from ultralytics import YOLO

    model = YOLO(weights)
    names = model.names
    cap = cv2.VideoCapture(source)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cap.release()
    coaster = Coaster(coast=coast)
    writer = None
    frame = 0
    kw = dict(stream=True, imgsz=imgsz, conf=conf, max_det=1000, verbose=False)
    if device:
        kw["device"] = device
    trackers: Dict[int, BallTracker] = {}
    if tracker == "bytetrack":
        results = model.track(source, persist=True, tracker=TRACKER, **kw)
    else:
        results = model.predict(source, **kw)
    history: deque = deque(maxlen=3)
    fuel_cls = next((k for k, v in names.items() if v == "fuel"), None)
    ball_px = 18.0
    assisted_total = 0
    for r in results:
        raw = r.orig_img
        history.append(raw)
        img = raw.copy()
        if writer is None:
            h, w = img.shape[:2]
            writer = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*"mp4v"),
                                     fps, (w, h))
        # Line and text scale with the picture, from a 1080p baseline.
        s = img.shape[0] / 1080.0
        thin = max(1, round(s))
        font = 0.38 * s
        b = r.boxes
        boxes: Dict[int, Box] = {}
        cls_of: Dict[int, str] = {}
        have = b is not None and len(b)
        xyxy = b.xyxy.tolist() if have else []
        cls = b.cls.int().tolist() if have else []
        confs = b.conf.tolist() if have else []
        colour_idx: set = set()
        if tracker == "bytetrack":
            ids = (b.id.int().tolist() if have and b.id is not None
                   else [None] * len(xyxy))
        else:
            if assist and fuel_cls is not None and len(history) == 3:
                fuel = [bx for bx, c in zip(xyxy, cls) if c == fuel_cls]
                if fuel:
                    sz = sorted(max(bx[2] - bx[0], bx[3] - bx[1]) for bx in fuel)
                    ball_px = 0.8 * ball_px + 0.2 * sz[len(sz) // 2]
                extra = colour_assist(raw, history[0], [tuple(bx) for bx in xyxy],
                                      ball_px)
                assisted_total += len(extra)
                colour_idx = set(range(len(xyxy), len(xyxy) + len(extra)))
                xyxy += [list(e[0]) for e in extra]
                cls += [fuel_cls] * len(extra)
                confs += [e[1] for e in extra]
            # one tracker per class, so a robot never takes a ball's id; ids
            # are offset per class to stay unique on screen
            ids = [None] * len(xyxy)
            for c in set(cls):
                idx = [i for i, k in enumerate(cls) if k == c]
                t = trackers.setdefault(c, BallTracker())
                got = t.update([(tuple(xyxy[i]), confs[i]) for i in idx])
                back = {tuple(xyxy[i]): i for i in idx}
                for tid, bx in got.items():
                    ids[back[tuple(bx)]] = tid + 100000 * c
        for i, (box, c, tid) in enumerate(zip(xyxy, cls, ids)):
            if i in colour_idx and tid is None:
                continue        # colour alone, not continuing a ball: not drawn
            name = names.get(c, str(c))
            colour = ASSIST_COLOUR if i in colour_idx else COLOURS.get(name, (200, 200, 200))
            x1, y1, x2, y2 = (int(v) for v in box)
            cv2.rectangle(img, (x1, y1), (x2, y2), colour, thin)
            if tid is None:
                continue
            boxes[tid] = tuple(box)
            cls_of[tid] = name
            text = {"none": "", "id": f"{tid}",
                    "full": f"{name} {tid}"}.get(labels, "")
            if text:
                cv2.putText(img, text, (x1, max(y1 - 3, 8)),
                            cv2.FONT_HERSHEY_SIMPLEX, font, colour,
                            1, cv2.LINE_AA)
        seen, coasting = coaster.update(frame, boxes)
        if trails:
            for tid in seen:
                pts = coaster.moving_trail(tid, min_px=12.0 * s)
                if pts:
                    colour = COLOURS.get(cls_of.get(tid, ""), (200, 200, 200))
                    for (xa, ya), (xb, yb) in zip(pts, pts[1:]):
                        cv2.line(img, (int(xa), int(ya)), (int(xb), int(yb)),
                                 colour, thin, cv2.LINE_AA)
        for tid, (x1, y1, x2, y2) in coasting.items():
            c = (int((x1 + x2) / 2), int((y1 + y2) / 2))
            cv2.circle(img, c, max(3, int((x2 - x1) / 2)), (255, 255, 255),
                       thin, cv2.LINE_AA)
        writer.write(img)
        frame += 1
        if progress and frame % 100 == 0:
            progress(f"frame {frame}" + (f"/{total}" if total else ""))
        if max_frames and frame >= max_frames:
            break
    if writer is not None:
        writer.release()
    return {"frames": frame, "out": out, "colour_assisted": assisted_total}
