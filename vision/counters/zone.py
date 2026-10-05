"""Zone counter: detect fuel inside the hub ROI, link detections frame-to-frame with a
tiny nearest-neighbor tracker, count each short track once.

Why not ByteTrack: on the broadcast test it fragmented into 112k tracks and counted 0.
Here a track only has to live `min_hits` frames inside the ROI to count, and a ball
that disappears for <= `max_missed` frames keeps its identity (no double count).

Tuning knobs (config/vision.yaml):
  conf        detector confidence (start 0.25)
  imgsz       inference size on the crop (640; 960 if balls < ~20 px)
  crop_pad    px around ROI included in the crop
  max_px      max movement between frames to be "the same ball" (scale with fps & distance)
  min_hits    frames a track must be seen before it counts (2 kills single-frame noise)
  max_missed  frames a track may vanish and still be the same ball
  min_dy      require this much downward travel before counting (0 = off)
  classes     model class ids to count (default: the class named "fuel" if the model has
              one, else every class). A model that also detects robots would otherwise
              count every robot box in the ROI as fuel.
"""
from __future__ import annotations

import math

import cv2

from .base import Counter, crop_box, load_yolo


def fuel_classes(names, cfg):
    """Class ids to keep: cfg `classes` if set, else the ids named "fuel", else None (all).

    `names` is the model's {id: name} map. Returning None keeps the old behaviour for a
    single-class model whose class is not called "fuel".
    """
    if cfg.get("classes") is not None:
        return [int(c) for c in cfg["classes"]]
    fuel = [int(i) for i, n in dict(names or {}).items() if str(n).lower() == "fuel"]
    return fuel or None


class ZoneCounter(Counter):
    NAME = "zone"
    DESCRIPTION = "Model detections inside the hub ROI, each short track counted once"
    NEEDS = ("model",)
    OPTIONS = {"conf": "detection confidence (0.25)", "imgsz": "inference size (640)",
               "crop_pad": "px around the ROI (60)", "max_px": "max move per frame (60)",
               "min_hits": "frames before a track counts (2)",
               "max_missed": "frames a ball may vanish (3)",
               "min_dy": "downward travel needed (0)",
               "classes": "model class ids to count (the class named fuel)"}

    def __init__(self, cfg):
        super().__init__(cfg)
        self.model, self.device = load_yolo(cfg["weights"])
        self.classes = fuel_classes(getattr(self.model, "names", {}), cfg)
        self.conf = cfg.get("conf", 0.25)
        self.imgsz = cfg.get("imgsz", 640)
        self.pad = cfg.get("crop_pad", 60)
        self.max_px = cfg.get("max_px", 60)
        self.min_hits = cfg.get("min_hits", 2)
        self.max_missed = cfg.get("max_missed", 3)
        self.min_dy = cfg.get("min_dy", 0)
        self.tracks = {}
        self.next_id = 0
        self.last_dets = []

    def _detect(self, frame):
        crop, (ox, oy) = crop_box(frame, self.roi, self.pad)
        r = self.model.predict(crop, conf=self.conf, imgsz=self.imgsz, device=self.device,
                               classes=self.classes, verbose=False)[0]
        x, y, w, h = self.roi
        out = []
        for cx, cy, bw, bh in r.boxes.xywh.tolist():
            fx, fy = cx + ox, cy + oy
            if x <= fx <= x + w and y <= fy <= y + h:
                out.append((fx, fy))
        return out

    def process(self, frame, t):
        dets = self._detect(frame)
        self.last_dets = dets
        unmatched = set(range(len(dets)))
        # greedy nearest-neighbor, closest pairs first
        pairs = sorted(((math.dist(tr["pos"], dets[j]), tid, j)
                        for tid, tr in self.tracks.items() for j in range(len(dets))))
        used_t = set()
        for d, tid, j in pairs:
            if d > self.max_px or tid in used_t or j not in unmatched:
                continue
            tr = self.tracks[tid]
            tr["pos"], tr["hits"], tr["missed"] = dets[j], tr["hits"] + 1, 0
            used_t.add(tid)
            unmatched.discard(j)
        for tid, tr in list(self.tracks.items()):
            if tid not in used_t:
                tr["missed"] += 1
                if tr["missed"] > self.max_missed:
                    del self.tracks[tid]
        for j in unmatched:
            self.tracks[self.next_id] = {"pos": dets[j], "y0": dets[j][1], "hits": 1,
                                         "missed": 0, "counted": False}
            self.next_id += 1
        new = 0
        for tr in self.tracks.values():
            if (not tr["counted"] and tr["hits"] >= self.min_hits
                    and tr["pos"][1] - tr["y0"] >= self.min_dy):
                tr["counted"] = True
                new += 1
        self.total += new
        return new

    def draw(self, frame):
        for fx, fy in self.last_dets:
            cv2.circle(frame, (int(fx), int(fy)), 10, (0, 200, 0), 2)
        for tr in self.tracks.values():
            if tr["counted"]:
                cv2.circle(frame, (int(tr["pos"][0]), int(tr["pos"][1])), 4, (0, 0, 255), -1)
        return super().draw(frame)
