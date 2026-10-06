"""A counter plugin to copy. Save a copy here as e.g. `my_counter.py` (no
leading underscore -- files starting with "_" are skipped), change NAME, and
put `counter: my-counter` in config/vision.yaml. Nothing else to edit.

Check it shows up:  python run_vision.py --list-counters
Try it on a video:  python rescore.py <recording> (see README)

The contract is one method: process(frame, t) returns how many NEW fuel went
in during that frame. Everything else here is optional (counters/base.py).
"""
import cv2

from counters.base import Counter


class MyCounter(Counter):
    NAME = "my-counter"                       # what `counter:` says in vision.yaml
    DESCRIPTION = "Bright yellow pixels appearing inside the roi (an example, not a real counter)"
    NEEDS = ()                                # e.g. ("model", "gpu"); shown, not enforced
    OPTIONS = {"threshold": "fraction of the roi that must turn yellow to count one ball"}

    def __init__(self, cfg):
        super().__init__(cfg)                 # sets self.cfg, self.roi, self.total
        self.threshold = float(cfg.get("threshold", 0.02))
        self.was_on = False

    def process(self, frame, t):
        x, y, w, h = self.roi or (0, 0, frame.shape[1], frame.shape[0])
        hsv = cv2.cvtColor(frame[y:y + h, x:x + w], cv2.COLOR_BGR2HSV)
        yellow = cv2.inRange(hsv, (20, 120, 120), (35, 255, 255))
        on = yellow.mean() / 255 > self.threshold
        new = 1 if on and not self.was_on else 0
        self.was_on = on
        self.total += new
        return new

    def status(self):                         # optional: shown on /control
        return {"detail": f"{self.total} counted"}
