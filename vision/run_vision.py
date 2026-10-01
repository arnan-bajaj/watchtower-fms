"""Vision scoring process. Completely separate from the FMS.

  cd vision && python run_vision.py --config ../config/vision.yaml [--preview]

Per hub: camera -> counter plugin -> (timestamp, n) -> batched POST to the FMS.
If the FMS is unreachable, events are buffered and re-sent (nothing is lost).
While the FMS says a match is running, raw hub video + per-frame timestamps are
recorded so the match can be re-counted later with a better model (rescore.py).

Restarting this process mid-match loses only the seconds it was down.

Optionally (`bioarena:` in the config) every count is also sent straight to a bioarena
field over UDP, so bioarena can decide the auto winner and light the hubs (bioarena.py).
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import threading
import time

import cv2
import numpy as np
import requests

import vconfig
from bioarena import BioarenaFeed
from counters import load_counter

STOP = threading.Event()


class Sender(threading.Thread):
    def __init__(self, url, key, period=0.25, feed=None):
        super().__init__(daemon=True)
        self.feed = feed
        self.url, self.key, self.period = url.rstrip("/"), key, period
        self.buf = {"red": [], "blue": []}
        self.status = {}
        self.lock = threading.Lock()
        self.record = None
        self.ok = False

    def add(self, hub, t, n):
        with self.lock:
            self.buf[hub].append([t, n])

    def run(self):
        while not STOP.is_set():
            time.sleep(self.period)
            with self.lock:
                batch = {h: v[:] for h, v in self.buf.items()}
                status = dict(self.status)
            if self.feed:
                status["bioarena"] = self.feed.status()
            try:
                r = requests.post(f"{self.url}/api/vision/events", timeout=2,
                                  headers={"X-Vision-Key": self.key},
                                  json={"events": batch, "status": status, "source": "live"})
                r.raise_for_status()
                self.record = r.json().get("record")
                with self.lock:  # drop only what we actually sent
                    for h in batch:
                        del self.buf[h][:len(batch[h])]
                self.ok = True
            except Exception as e:
                if self.ok:
                    print(f"[sender] FMS unreachable, buffering: {e}")
                self.ok = False


class Recorder:
    def __init__(self, root, hub):
        self.root, self.hub = pathlib.Path(root), hub
        self.root.mkdir(parents=True, exist_ok=True)
        self.cur, self.vw, self.csvf, self.csvw = None, None, None, None

    def update(self, rec_id, frame, t, fps):
        if rec_id != self.cur:
            self.close()
            self.cur = rec_id
            if rec_id:
                h, w = frame.shape[:2]
                base = self.root / f"{rec_id}_{self.hub}"
                self.vw = cv2.VideoWriter(str(base) + ".mp4", cv2.VideoWriter_fourcc(*"mp4v"),
                                          fps, (w, h))
                self.csvf = open(str(base) + ".csv", "w", newline="")
                self.csvw = csv.writer(self.csvf)
                self.csvw.writerow(["frame", "t"])
                self.n = 0
                print(f"[rec] {base}.mp4")
        if self.vw is not None:
            self.vw.write(frame)
            self.csvw.writerow([self.n, f"{t:.4f}"])
            self.n += 1

    def close(self):
        if self.vw is not None:
            self.vw.release()
            self.csvf.close()
        self.vw = None


class HubWorker(threading.Thread):
    def __init__(self, hub, cfg, sender, rec_dir, preview):
        super().__init__(daemon=True)
        self.hub, self.cfg, self.sender, self.preview = hub, {**cfg, "hub": hub}, sender, preview
        self.recorder = Recorder(rec_dir, hub) if rec_dir else None
        self.frame_out = None

    def open(self):
        src = self.cfg["source"]
        if src == "none":
            return None
        cap = cv2.VideoCapture(int(src) if str(src).isdigit() else src)
        if self.cfg.get("width"):
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg["width"])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg["height"])
        if self.cfg.get("fps"):
            cap.set(cv2.CAP_PROP_FPS, self.cfg["fps"])
        return cap

    def run(self):
        counter = load_counter(self.cfg)
        src = str(self.cfg["source"])
        is_file = not src.isdigit() and src != "none" and not src.startswith(("rtsp", "http"))
        cap = self.open()
        fps_nominal = (cap.get(cv2.CAP_PROP_FPS) if cap else 0) or self.cfg.get("fps", 30)
        t0, idx, n_frames, t_fps = time.time(), 0, 0, time.time()
        fps = 0.0
        while not STOP.is_set():
            if cap is None:  # "none" source: blank frames (mock counter)
                time.sleep(1 / fps_nominal)
                ok, frame = True, np.zeros((360, 640, 3), np.uint8)
            else:
                ok, frame = cap.read()
            if not ok:
                if is_file and self.cfg.get("loop", True):
                    cap.release()
                    cap = self.open()
                    t0, idx = time.time(), 0
                    continue
                self.sender.status[self.hub] = {"fps": 0, "error": "camera read failed",
                                                "counter": self.cfg["counter"]}
                time.sleep(0.5)
                cap.release()
                cap = self.open()
                continue
            if is_file:  # play files in real time so timestamps behave like a live camera
                t = t0 + idx / fps_nominal
                idx += 1
                lag = t - time.time()
                if lag > 0:
                    time.sleep(lag)
            else:
                t = time.time()
            n = counter.process(frame, t)
            if n:
                if self.sender.feed:  # first: this one is on bioarena's auto deadline
                    self.sender.feed.add(self.hub, n, t)
                self.sender.add(self.hub, t, n)
            if self.recorder:
                self.recorder.update(self.sender.record, frame, t, fps_nominal)
            n_frames += 1
            if time.time() - t_fps >= 1:
                fps = n_frames / (time.time() - t_fps)
                n_frames, t_fps = 0, time.time()
                self.sender.status[self.hub] = {"fps": round(fps, 1), "counter": self.cfg["counter"],
                                                "session_total": counter.total}
            if self.preview:
                self.frame_out = counter.draw(frame.copy())
        if self.recorder:
            self.recorder.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="../config/vision.yaml")
    ap.add_argument("--preview", action="store_true")
    args = ap.parse_args()
    cfg = vconfig.load(args.config)
    feed = None
    if cfg.get("bioarena"):
        b = cfg["bioarena"]
        feed = BioarenaFeed(b["host"], b.get("port", 8411), b.get("bind", ""))
        feed.start()
        print(f"bioarena feed -> {feed.dest[0]}:{feed.dest[1]} session {feed.session}")
    sender = Sender(cfg["fms_url"], cfg["vision_key"], feed=feed)
    sender.start()
    defaults = cfg.get("defaults", {})
    workers = []
    for hub in ("red", "blue"):
        if hub in cfg["hubs"] and cfg["hubs"][hub].get("enabled", True):
            w = HubWorker(hub, {**defaults, **cfg["hubs"][hub]}, sender,
                          cfg.get("record_dir"), args.preview)
            w.start()
            workers.append(w)
    print(f"vision running: {[w.hub for w in workers]} -> {cfg['fms_url']}  (Ctrl+C to stop)")
    try:
        while True:
            if args.preview:  # macOS: all GUI calls must be on the main thread
                for w in workers:
                    if w.frame_out is not None:
                        f = w.frame_out
                        s = 960 / f.shape[1]
                        cv2.imshow(w.hub, cv2.resize(f, None, fx=s, fy=s))
                if cv2.waitKey(15) & 0xFF == ord("q"):
                    break
            else:
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    STOP.set()
    time.sleep(0.5)


if __name__ == "__main__":
    main()
