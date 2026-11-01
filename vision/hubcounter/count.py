"""Count scored fuel from the detector alone. No scoreboard, no OCR.

Everything else that produces a fuel number in this repository reads the
broadcast's burned-in counter. That is the right answer when there IS one:
it is the field's own arithmetic, and no vision system beats it.

It is no answer at all on a field that does not render one -- a practice field,
an offseason event, a demo, somebody's own game system. There the only
statement of what scored is the ball going in, and the detector already has a
`fuel` class and a hub for each alliance. This counts the event itself.

    python3 run.py count --weights runs/<name>/weights/best.pt --source 0

## What counts as a score

A ball that goes into the hub stops being visible. So the event this looks for
is a fuel track **vanishing inside a hub region** -- and almost all of the work
here is refusing the three other things that look exactly like that:

  * **A one-frame blob.** A false detection appears and is gone. Rejected by
    `min_track_frames`: a ball that was really there was there for a while.
  * **A ball a robot drove in front of.** It vanishes, and it happens to be
    near the hub. Rejected by `require_entry`: a scored ball crossed INTO the
    hub region from outside it, and one that was already sitting there did not.
  * **A ball that passed over the hub.** It vanishes behind the structure and
    comes back the other side, often with a new track id. This is the one that
    cannot be settled in the moment, so a vanish inside a hub is held
    `reacquire_frames` before it is allowed to count, and a new track appearing
    near where it went cancels it.

That last rule is the same shape as `scoreboard.clean_series` demanding two
consecutive reads before it believes a large jump: commit late, and let the
next few frames withdraw it. Here it costs a fraction of a second of latency,
which is nothing against being confidently wrong about a score.

## Frame rate matters here, and it is why detect.py does the opposite

`detect.py` throws fuel track ids away, because it runs over frames sampled at
3 fps where a ball moves further between samples than its own width and a track
means nothing. This runs on a live camera at its native rate, where a ball is a
few pixels from where it was and tracking it is the whole method. Same class in
the same weights, opposite decision, for the same reason: what the frame rate
can support.

## When this IS the score

At a scrimmage there is no Field Management System, so nothing else is
counting and there is no better number to defer to. That is the case this was
built for, and it is why `field.py` exists: a count is not a scoreboard until
it has a match clock and a referee who can correct it. `run.py count --scoreboard` is the whole thing.

Where a real scoreboard does exist -- a broadcast with a burned-in counter --
read that instead. It is the field's own arithmetic and this is not.

Either way the limits are the same and worth saying plainly, because at a
scrimmage nothing else will catch them: it cannot see a ball occluded for its
whole flight, and a hub region that is slightly wrong costs real balls.

Which is why `run_source` times itself. The failure that matters is the silent
one -- a box too slow for the camera misses balls between the frames it does
see, and the score is simply low, with no gap and nothing odd about it. There
is no second number anywhere to disagree with it, so it has to say so itself.
"""
from __future__ import annotations

import json
from collections import deque
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

Box = Tuple[float, float, float, float]      # x, y, w, h

# A ball seen for fewer frames than this never existed. At 30 fps this is a
# tenth of a second, which is far less than a ball is visible for and far more
# than a flicker.
MIN_TRACK_FRAMES = 3

# Frames a track may go unseen before it is treated as gone. Too low and every
# brief occlusion ends a track; too high and the count lags the field.
VANISH_FRAMES = 4

# How long a vanish-in-hub is held before it counts, and how close a new track
# has to appear to cancel it. The window is the ball's time behind the hub
# structure if it merely passed over.
REACQUIRE_FRAMES = 12
REACQUIRE_PX = 90.0

# A scored ball crossed into the hub from outside it. Turning this off trades
# precision for recall on a camera where balls are first detected already over
# the hub -- see `require_entry` in the class docstring.
REQUIRE_ENTRY = True


def centre(b: Box) -> Tuple[float, float]:
    return b[0] + b[2] / 2.0, b[1] + b[3] / 2.0


def inside(region: Box, point: Tuple[float, float], pad: float = 0.0) -> bool:
    x, y, w, h = region
    px, py = point
    return (x - pad) <= px <= (x + w + pad) and (y - pad) <= py <= (y + h + pad)


def hub_of(hubs: Dict[str, Box], point: Tuple[float, float],
           pad: float = 0.0) -> Optional[str]:
    """Which alliance's hub a point is in, or None.

    Nearest centre when the two regions overlap, which they should not on a
    real field but can if the boxes were learned from a couple of bad frames.
    """
    hits = [a for a, b in hubs.items() if inside(b, point, pad)]
    if not hits:
        return None
    if len(hits) == 1:
        return hits[0]
    return min(hits, key=lambda a: (centre(hubs[a])[0] - point[0]) ** 2
               + (centre(hubs[a])[1] - point[1]) ** 2)


def learn_hubs(observations: Iterable[Dict[str, Box]]) -> Dict[str, Box]:
    """A stable hub box per alliance, from per-frame detections.

    The median of each coordinate, not the mean: the camera is fixed for a
    whole event -- `SCOUTING.md` leans on the same fact to replay one recorded
    box into every frame -- so the true box is constant and the spread is
    detector jitter plus the occasional frame where a robot's bumper was called
    a hub. A median ignores those; a mean is dragged by them.
    """
    seen: Dict[str, List[Box]] = {}
    for frame in observations:
        for alliance, box in frame.items():
            seen.setdefault(alliance, []).append(box)
    out: Dict[str, Box] = {}
    for alliance, boxes in seen.items():
        if not boxes:
            continue
        cols = list(zip(*boxes))
        out[alliance] = tuple(sorted(c)[len(c) // 2] for c in cols)
    return out


class _Track:
    __slots__ = ("tid", "first_frame", "last_frame", "first_box", "last_box",
                 "seen", "missing", "started_in")

    def __init__(self, tid: int, frame: int, box: Box, started_in: Optional[str]):
        self.tid = tid
        self.first_frame = frame
        self.last_frame = frame
        self.first_box = box
        self.last_box = box
        self.seen = 1
        self.missing = 0
        self.started_in = started_in


class _Pending:
    __slots__ = ("alliance", "point", "t", "frame", "seen")

    def __init__(self, alliance: str, point, t: float, frame: int, seen: int):
        self.alliance = alliance
        self.point = point
        self.t = t
        self.frame = frame
        self.seen = seen


class BallCounter:
    """Scored balls, from fuel tracks and hub regions.

    Pure state and arithmetic: no model, no video, no database. Everything that
    decides whether a ball counted happens here, so all of it is testable
    without a GPU.
    """

    def __init__(self, hubs: Dict[str, Box],
                 min_track_frames: int = MIN_TRACK_FRAMES,
                 vanish_frames: int = VANISH_FRAMES,
                 reacquire_frames: int = REACQUIRE_FRAMES,
                 reacquire_px: float = REACQUIRE_PX,
                 require_entry: bool = REQUIRE_ENTRY,
                 pad: float = 0.0,
                 fps: float = 30.0):
        self.hubs = dict(hubs)
        # Frame-count windows chosen on 30 fps footage; scaled so each keeps
        # its length in time on a 60 fps source (see shooting.ShotCounter).
        k = (fps or 30.0) / 30.0
        min_track_frames, vanish_frames, reacquire_frames = (
            max(1, round(v * k)) for v in (min_track_frames, vanish_frames,
                                           reacquire_frames))
        self.min_track_frames = min_track_frames
        self.vanish_frames = vanish_frames
        self.reacquire_frames = reacquire_frames
        self.reacquire_px = reacquire_px
        self.require_entry = require_entry
        self.pad = pad

        self.tracks: Dict[int, _Track] = {}
        self.pending: List[_Pending] = []
        self.totals: Dict[str, int] = {a: 0 for a in hubs}
        # Why balls did NOT count, which is what you read when the number looks
        # wrong. Silent rejection is how a counter earns distrust.
        self.rejected: Dict[str, int] = {"too_short": 0, "not_in_hub": 0,
                                         "started_inside": 0, "reacquired": 0}
        self._known: set = set()

    # -- the frame loop ---------------------------------------------------
    def update(self, frame: int, t: float,
               balls: Dict[int, Box]) -> List[Dict]:
        """One frame of fuel tracks. Returns any scores CONFIRMED this frame."""
        for tid, box in balls.items():
            track = self.tracks.get(tid)
            if track is None:
                started = hub_of(self.hubs, centre(box), self.pad)
                self.tracks[tid] = _Track(tid, frame, box, started)
                if tid not in self._known:
                    self._known.add(tid)
                    self._cancel_near(centre(box), frame)
            else:
                track.last_frame = frame
                track.last_box = box
                track.seen += 1
                track.missing = 0

        for tid in list(self.tracks):
            track = self.tracks[tid]
            if track.last_frame == frame:
                continue
            track.missing += 1
            if track.missing >= self.vanish_frames:
                self._finish(track, t, frame)
                del self.tracks[tid]

        return self._expire(frame, t)

    def flush(self, frame: int, t: float) -> List[Dict]:
        """End of stream: settle every track and every held score."""
        for tid in list(self.tracks):
            self._finish(self.tracks[tid], t, frame)
            del self.tracks[tid]
        out = self._expire(frame + self.reacquire_frames + 1, t)
        return out

    # -- decisions --------------------------------------------------------
    def _finish(self, track: _Track, t: float, frame: int) -> None:
        """A track has gone. Was it a score?"""
        if track.seen < self.min_track_frames:
            self.rejected["too_short"] += 1
            return
        point = centre(track.last_box)
        alliance = hub_of(self.hubs, point, self.pad)
        if alliance is None:
            self.rejected["not_in_hub"] += 1
            return
        if self.require_entry and track.started_in == alliance:
            # It was already in the hub region when first seen, so it did not
            # cross in -- a ball resting near the hub that a robot occluded.
            self.rejected["started_inside"] += 1
            return
        self.pending.append(_Pending(alliance, point, t, frame, track.seen))

    def _cancel_near(self, point, frame: int) -> None:
        """A new track near a held score means the ball came back out."""
        for p in list(self.pending):
            if frame - p.frame > self.reacquire_frames:
                continue
            dx = p.point[0] - point[0]
            dy = p.point[1] - point[1]
            if (dx * dx + dy * dy) ** 0.5 <= self.reacquire_px:
                self.pending.remove(p)
                self.rejected["reacquired"] += 1
                return

    def _expire(self, frame: int, t: float) -> List[Dict]:
        """Held scores whose reacquire window has passed are real."""
        out: List[Dict] = []
        for p in list(self.pending):
            if frame - p.frame <= self.reacquire_frames:
                continue
            self.pending.remove(p)
            self.totals[p.alliance] = self.totals.get(p.alliance, 0) + 1
            out.append({"t": round(p.t, 3), "alliance": p.alliance, "balls": 1,
                        "total": self.totals[p.alliance]})
        return out

    # -- output -----------------------------------------------------------
    def series(self, events: Sequence[Dict]) -> Dict[str, List[List[float]]]:
        """Cumulative per-alliance steps, the shape db.write_live expects."""
        out: Dict[str, List[List[float]]] = {a: [] for a in self.hubs}
        for e in events:
            out.setdefault(e["alliance"], []).append([e["t"], e["total"]])
        return out

    def report(self) -> List[str]:
        lines = [f"counted {sum(self.totals.values())} ball(s): "
                 + ", ".join(f"{a}={n}" for a, n in sorted(self.totals.items()))]
        named = {"too_short": "too few frames to be a ball",
                 "not_in_hub": "vanished away from any hub",
                 "started_inside": "already in the hub when first seen",
                 "reacquired": "came back out -- passed over, not in"}
        for key, n in self.rejected.items():
            if n:
                lines.append(f"  not counted: {n} {named[key]}")
        if self.pending:
            lines.append(f"  {len(self.pending)} still held, awaiting the "
                         f"reacquire window")
        return lines


def hubs_from_db(con, event_key: str) -> Dict[str, Box]:
    """The hub geometry `run.py db hub` recorded for an event, if any."""
    row = con.execute("SELECT hub_blue, hub_red FROM events WHERE event_key=?",
                      (event_key,)).fetchone()
    if not row:
        return {}
    out: Dict[str, Box] = {}
    for alliance, raw in (("blue", row["hub_blue"]), ("red", row["hub_red"])):
        if raw:
            try:
                out[alliance] = tuple(json.loads(raw))
            except (ValueError, TypeError):
                continue
    return out


# -- driving it from a model ----------------------------------------------
#
# Kept here rather than in run.py so the loop sits beside the rules it feeds,
# and torch stays out of this module's imports the same way it stays out of
# detect.py's -- `load` is the only thing that reaches for it.

LEARN_FRAMES = 90          # ~3 s at 30 fps, to fix the hub boxes
CLS_FUEL = "fuel"
HUB_CLASSES = ("hub_blue", "hub_red")


def hub_advice() -> str:
    return ("Pass the hub boxes instead -- --hub-blue X,Y,W,H --hub-red "
            "X,Y,W,H, or --event with geometry recorded by `run.py db hub` -- "
            "or train hub classes with train/subset_classes.py.")


def model_can_count(names: Dict[int, str],
                    hubs: Optional[Dict[str, Box]]) -> Optional[str]:
    """Why this model cannot count, or None when it can.

    Fuel is always needed: it is the thing being counted. Hub classes are
    needed only when the hubs have to be *learned*. This used to demand them
    unconditionally, so a fuel-only model was refused even with both hub boxes
    handed to it -- and on a fixed camera, drawing two boxes during setup is
    both easier and more exact than detecting the hubs every frame. The first
    trained model is fuel-only, and that refusal was all that stood between it
    and a working counter.

    Everything missing is named at once, so one run is enough to see what to
    fix.
    """
    have = set(names.values())
    missing = [] if CLS_FUEL in have else [CLS_FUEL]
    if not hubs:
        missing += [c for c in HUB_CLASSES if c not in have]
    if not missing:
        return None
    reason = (f"this model has no {', '.join(missing)} class"
              f"{'es' if len(missing) > 1 else ''} "
              f"({', '.join(sorted(have))}), so it cannot count balls into a "
              f"hub.")
    if any(c in HUB_CLASSES for c in missing):
        return f"{reason} {hub_advice()}"
    return f"{reason} Train one with train/subset_classes.py."


def run_source(model, source, counter_factory, conf: float = 0.25,
               tracker: str = "bytetrack.yaml",
               learn_frames: int = LEARN_FRAMES,
               hubs: Optional[Dict[str, Box]] = None,
               on_event=None, on_frame=None, max_frames: int = 0,
               expect_fps: float = 0.0, on_health=None,
               device: str = "") -> Dict:
    """Run the detector over a live source and count what goes in.

    `counter_factory(hubs) -> BallCounter` is called once the hub geometry is
    settled, which is either handed in or learned from the first
    `learn_frames`. Nothing is counted while it is still being learned, and
    that is said out loud rather than quietly producing a zero: with no hub
    there is no "in" for a ball to go.

    `stream=True` so ultralytics yields per frame instead of buffering the
    whole source, which is the difference between a counter and a batch job.

    ## Why it measures itself

    At a scrimmage there is no FMS, so nothing anywhere will notice that this
    is wrong. The failure that matters is silent: if the box cannot process
    frames as fast as the camera produces them, balls go through between the
    frames it does see, and the score is simply low -- with no gap, no error
    and nothing that looks unusual about it.

    So the loop times itself. `expect_fps` is the camera's real rate, which it
    cannot discover on its own, and `on_health` is called with what it
    measured. Nothing here decides what to do about a shortfall; it only makes
    sure the number cannot be believed by accident.
    """
    import time as _time

    from .detect import _boxes_of

    names = dict(getattr(model, "names", {}) or {})
    if not names:
        return {"error": "the model carries no class names, so nothing it "
                         "detects can be identified. Every box would be "
                         "dropped and the score would read zero."}
    # Saying why here beats a scoreboard that sits at nil all afternoon.
    reason = model_can_count(names, hubs)
    if reason:
        return {"error": reason}
    counter: Optional[BallCounter] = None
    learning: List[Dict[str, Box]] = []
    started = _time.monotonic()
    frame = 0
    events: List[Dict] = []
    # A short window rather than an average over the whole session: a board
    # that was keeping up for the first minute and is not now is exactly the
    # case worth catching, and a running mean hides it.
    recent = deque(maxlen=60)
    last_tick = started

    for result in model.track(source=source, stream=True, persist=True,
                              tracker=tracker, conf=conf, device=device,
                              verbose=False):
        t = _time.monotonic() - started
        balls: Dict[int, Box] = {}
        seen_hubs: Dict[str, Box] = {}
        for x1, y1, x2, y2, cf, cls_index, tid in _boxes_of(result):
            name = names.get(int(cls_index))
            if name is None:
                continue
            box = (min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))
            if name == CLS_FUEL:
                if tid is not None:
                    balls[int(tid)] = box
            elif name.startswith("hub_"):
                seen_hubs[name.split("_", 1)[1]] = box

        if counter is None:
            if hubs:
                counter = counter_factory(hubs)
            else:
                if seen_hubs:
                    learning.append(seen_hubs)
                if frame >= learn_frames:
                    found = learn_hubs(learning)
                    if not found:
                        return {"error": "no hub was detected in the first "
                                         f"{learn_frames} frames, so there is "
                                         "nothing for a ball to go into. Point "
                                         "it at the field, or pass the boxes in.",
                                "frames": frame}
                    counter = counter_factory(found)
                    hubs = found
        else:
            for e in counter.update(frame, t, balls):
                events.append(e)
                if on_event:
                    on_event(e)

        now = _time.monotonic()
        recent.append(now - last_tick)
        last_tick = now
        frame += 1
        if on_health and frame % 30 == 0:
            on_health(health(recent, expect_fps, frame, counter))
        if on_frame:
            on_frame(frame, t, counter)
        if max_frames and frame >= max_frames:
            break

    if counter is None:
        return {"error": "the source ended before the hubs were learned",
                "frames": frame}
    for e in counter.flush(frame, _time.monotonic() - started):
        events.append(e)
        if on_event:
            on_event(e)
    return {"events": events, "counter": counter, "frames": frame,
            "health": health(recent, expect_fps, frame, counter),
            "hubs": {a: list(b) for a, b in (hubs or {}).items()}}


def health(recent, expect_fps: float, frames: int,
           counter: Optional["BallCounter"]) -> Dict:
    """How much of what happened this counter actually saw.

    `keepingUp` is the whole point and it is None, not True, when `expect_fps`
    was not given -- without knowing the camera's rate there is no way to tell
    a slow processor from a slow camera, and guessing which would be worse than
    saying nothing.

    `missedFrac` is an estimate of the fraction of the camera's frames that
    went past unseen. A ball crosses the hub in a handful of frames, so this
    is roughly the fraction of scores at risk, not a cosmetic number.
    """
    per = (sum(recent) / len(recent)) if recent else 0.0
    fps = (1.0 / per) if per > 0 else 0.0
    out = {"fps": round(fps, 1), "frames": frames,
           "expectFps": round(expect_fps, 1) if expect_fps else None,
           "keepingUp": None, "missedFrac": None}
    if expect_fps and fps > 0:
        out["keepingUp"] = fps >= expect_fps * 0.9
        out["missedFrac"] = round(max(0.0, 1.0 - fps / expect_fps), 3)
    if counter is not None:
        out["rejected"] = dict(counter.rejected)
        out["held"] = len(counter.pending)
    return out
