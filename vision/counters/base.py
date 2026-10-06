"""Counter plugin interface.

A counter gets frames from ONE hub camera and returns how many NEW fuel it
counted in that frame. That's the whole contract. The FMS never sees the model;
it only receives (timestamp, hub, n). Swap models/algorithms freely.

To add your own: write a class with this interface anywhere importable and set
`counter: "my_module:MyCounter"` in config/vision.yaml (or install it as a
package with a `watchtower.counters` entry point; see counters/__init__.py).

Optional, all read with defaults so older plugins keep working:
  PLUGIN_API   the contract version it was written for (counters.PLUGIN_API)
  NAME         short name shown in --list-counters and on /control
  DESCRIPTION  one line for --list-counters
  NEEDS        e.g. ("model", "gpu") -- shown, not enforced
  OPTIONS      {config key: help text}; misspelt keys are then warned about
  status()     -> {"detail": str, "warning": str, "error": str}, sent to the
               FMS each second and shown in /control's Vision panel
  close()      called once when vision stops (release models, threads)
"""
from __future__ import annotations

import cv2


class Counter:
    PLUGIN_API = 1
    NEEDS: tuple = ()
    OPTIONS: dict = {}

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.roi = cfg.get("roi")  # [x, y, w, h] in full-resolution pixels
        self.total = 0

    def process(self, frame, t: float) -> int:
        raise NotImplementedError

    def status(self) -> dict:
        return {}

    def close(self) -> None:
        pass

    def draw(self, frame):
        if self.roi:
            x, y, w, h = self.roi
            cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 255), 2)
        cv2.putText(frame, f"{self.cfg.get('hub', '?')}: {self.total}", (20, 50),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 255, 255), 3)
        return frame


def load_yolo(weights: str):
    from ultralytics import YOLO
    import torch
    model = YOLO(weights)
    device = "mps" if torch.backends.mps.is_available() else ("cuda" if torch.cuda.is_available() else "cpu")
    return model, device


def crop_box(frame, roi, pad):
    """Crop around the ROI (+pad) so small balls get more model pixels. Returns crop, (ox, oy)."""
    H, W = frame.shape[:2]
    x, y, w, h = roi
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(W, x + w + pad), min(H, y + h + pad)
    return frame[y0:y1, x0:x1], (x0, y0)
