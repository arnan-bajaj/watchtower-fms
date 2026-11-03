"""The built-in `colour` counter: hubcounter's ColourCounter (copied unchanged from YOLOv26-FRC-Model)
plus Setup -> Cameras' exit line on the same hub as its outline.

Given a plain config, ColourCounter counts an `outline` or an exit `line` (with `out`), not both;
only a cams.json `setup:` gives it both. Setup stores the two side by side (hubsetup.merge), so here
a hub with both gets both zones, and its HubTally takes the larger of the two counts, never their
sum: they count the same balls going in and coming out. status() adds the exits-against-outline
cross-check (HubTally.check) to the line /control shows.

`colour` ({hue_lo, hue_hi, sat_min, val_min}, OpenCV HSV, Setup -> Fuel colour) is the range of colours
that count as fuel; without it, the standard range the counter has always used (camprofile.gate).
"""
from __future__ import annotations

from hubcounter.camprofile import gate
from hubcounter.fms_counter import ColourCounter


class Colour(ColourCounter):
    NAME = "colour"
    OPTIONS = {**ColourCounter.OPTIONS,
               "colour": "what counts as fuel: {hue_lo, hue_hi, sat_min, val_min} (OpenCV HSV; standard 15, 40, 55, 45)"}

    def __init__(self, cfg: dict):
        self.gate = gate(cfg.get("colour"))
        super().__init__(cfg)

    def _build(self, frame) -> None:
        super()._build(frame)
        for eye in self.eyes:
            eye.lo, eye.hi = self.gate

    def _zone_specs(self, cfg: dict) -> list:
        if cfg.get("outline") and cfg.get("line") and cfg.get("out") and not cfg.get("setup"):
            return [("outline", [tuple(p) for p in cfg["outline"]], None),
                    ("exit", [tuple(p) for p in cfg["line"]], tuple(cfg["out"]))]
        return super()._zone_specs(cfg)

    def status(self) -> dict:
        out = super().status()
        c = self.tally.check(self._key) if self.tally is not None else None
        if c:
            out["detail"] = (out.get("detail", "") + f" · exit {c['exit']} / outline {c['due']}"
                             + (f" ({round(c['ratio'] * 100)}%)" if c["ratio"] is not None else "")).lstrip(" ·")
            if c["warn"]:
                out.setdefault("warning", f"exit line {c['exit']} against outline {c['due']}: one of the two is "
                                          "missing balls (the hub counts the larger)")
        return out
