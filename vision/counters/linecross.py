"""Line-cross counter: Ultralytics tracker (ByteTrack) + count track IDs that cross
a horizontal line inside the ROI moving downward. Works well when the camera sees
balls fall through the hub opening with clean tracks; fragile on wide shots.

Config: line_y (full-frame px; default = ROI vertical middle), conf, imgsz, tracker.
"""
from __future__ import annotations

import cv2

from .base import Counter, load_yolo


class LineCrossCounter(Counter):
    NAME = "linecross"
    DESCRIPTION = "ByteTrack IDs crossing a line in the ROI, moving down"
    NEEDS = ("model",)
    OPTIONS = {"line_y": "full-frame y of the line (ROI middle)", "conf": "detection confidence (0.25)",
               "imgsz": "inference size (960)", "tracker": "Ultralytics tracker yaml (bytetrack.yaml)"}

    def __init__(self, cfg):
        super().__init__(cfg)
        self.model, self.device = load_yolo(cfg["weights"])
        x, y, w, h = self.roi
        self.line_y = cfg.get("line_y", y + h // 2)
        self.x0, self.x1 = x, x + w
        self.conf = cfg.get("conf", 0.25)
        self.imgsz = cfg.get("imgsz", 960)
        self.tracker = cfg.get("tracker", "bytetrack.yaml")
        self.prev_y, self.counted = {}, set()

    def process(self, frame, t):
        r = self.model.track(frame, persist=True, tracker=self.tracker, conf=self.conf,
                             imgsz=self.imgsz, device=self.device, verbose=False)[0]
        new = 0
        if r.boxes.id is not None:
            for tid, (cx, cy, w, h) in zip(r.boxes.id.int().tolist(), r.boxes.xywh.tolist()):
                if not (self.x0 <= cx <= self.x1):
                    continue
                py = self.prev_y.get(tid)
                if py is not None and py < self.line_y <= cy and tid not in self.counted:
                    self.counted.add(tid)
                    new += 1
                self.prev_y[tid] = cy
        if len(self.prev_y) > 5000:  # keep memory bounded over a long day
            self.prev_y = dict(list(self.prev_y.items())[-1000:])
        self.total += new
        return new

    def draw(self, frame):
        cv2.line(frame, (self.x0, self.line_y), (self.x1, self.line_y), (0, 0, 255), 2)
        return super().draw(frame)
