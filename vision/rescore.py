"""Re-count a recorded match with a (better) model and replace that hub's fuel in the FMS.

  python rescore.py --match qm7 --hub red --video recordings/qm7_1760112345_red.mp4 \
      --config ../config/vision.yaml [--weights new.pt] [--dry-run]

Uses the per-frame timestamps saved next to the video, so events land on the same
clock as the live match. The match must be in review (reopen it if committed).
"""
from __future__ import annotations

import argparse
import csv
import pathlib

import cv2
import requests

import vconfig
from counters import add_plugin_paths, load_counter


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--match", required=True)
    ap.add_argument("--hub", required=True, choices=["red", "blue"])
    ap.add_argument("--video", required=True)
    ap.add_argument("--config", default="../config/vision.yaml")
    ap.add_argument("--weights")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = vconfig.load(a.config)
    hc = {**cfg.get("defaults", {}), **cfg["hubs"][a.hub], "hub": a.hub}
    if a.weights:
        hc["weights"] = a.weights
    ts = [float(r["t"]) for r in csv.DictReader(open(pathlib.Path(a.video).with_suffix(".csv")))]
    add_plugin_paths(cfg.get("plugin_paths"), pathlib.Path(a.config).parent)
    counter = load_counter(hc, cfg.get("plugins"))
    cap = cv2.VideoCapture(a.video)
    events, i = [], 0
    while True:
        ok, frame = cap.read()
        if not ok or i >= len(ts):
            break
        n = counter.process(frame, ts[i])
        if n:
            events.append([ts[i], n])
        i += 1
    print(f"{i} frames, counted {sum(n for _, n in events)} fuel for {a.hub}")
    if a.dry_run:
        return
    r = requests.post(f"{cfg['fms_url'].rstrip('/')}/api/vision/replace",
                      headers={"X-Vision-Key": cfg["vision_key"]},
                      json={"key": a.match, "hub": a.hub, "events": events, "source": "rescore"})
    print(r.status_code, r.json())


if __name__ == "__main__":
    main()
