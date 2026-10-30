"""Vision scoring process. Completely separate from the FMS.

  cd vision && python run_vision.py --config ../config/vision.yaml [--preview]

Per hub: camera -> counter plugin -> (timestamp, n) -> batched POST to the FMS.
If the FMS is unreachable, events are buffered and re-sent (nothing is lost).
While the FMS says a match is running, raw hub video + per-frame timestamps are
recorded so the match can be re-counted later with a better model (rescore.py).

Restarting this process mid-match loses only the seconds it was down.

Optionally every count is also sent straight to field systems over UDP (`feeds:` in the
config, or --feed HOST:PORT), so e.g. bioarena can decide the auto winner and light the
hubs (count_feed.py).

/control -> Setup can override the feeds and set each hub's calibration (counts per ball). The
FMS bumps `config_rev` in its reply to every POST; we then fetch /api/vision/config and apply it
live, no restart. Feeds set there replace the config file's (and --feed); null keeps them.
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
from count_feed import CountFeed, targets
from scaler import Scaler
from counters import (add_plugin, add_plugin_paths, list_counters, load_counter,
                      plugin_status, remove_plugin)

STOP = threading.Event()


class Sender(threading.Thread):
    def __init__(self, url, key, period=0.25, feeds=()):
        super().__init__(daemon=True)
        self.feeds = list(feeds)
        self.file_feeds = self.feeds        # restored if Setup goes back to "use the config file"
        self.url, self.key, self.period = url.rstrip("/"), key, period
        self.buf = {"red": [], "blue": []}
        self.status = {}
        self.lock = threading.Lock()
        self.record = None
        self.ok = False
        self.rev = None                     # /api/vision/config revision applied
        self.scalers = {"red": Scaler(), "blue": Scaler()}
        self.offset = 0.0                   # FMS clock - ours (vision on another computer)
        self._clock = []                    # (rtt, offset) samples

    def _clock_sample(self, t_send, t_recv, server_now):
        if server_now is None:
            return
        self._clock = (self._clock + [(t_recv - t_send, server_now - (t_send + t_recv) / 2)])[-40:]
        rtt, off = min(self._clock)         # the fastest round trip is the most trustworthy
        if abs(off) > 0.05 or abs(self.offset) > 0.05:   # same computer: leave it at 0
            self.offset = off

    def apply_config(self):
        """Fetch Setup's vision settings: field feeds and calibration."""
        r = requests.get(f"{self.url}/api/vision/config", timeout=3, headers={"X-Vision-Key": self.key})
        r.raise_for_status()
        c = r.json()
        for h, sc in (c.get("scale") or {}).items():
            if h in self.scalers and float(sc) != self.scalers[h].scale:
                self.scalers[h].set(sc)
                print(f"[config] {h}: {sc} counts per ball")
        if c.get("feeds") is None:
            if self.feeds is not self.file_feeds:
                self.set_feeds(None)
        else:
            want = targets({"feeds": c["feeds"]})
            if [(t["host"], int(t["port"])) for t in want] != [(f.host_, f.dest[1]) for f in self.feeds]:
                self.set_feeds(want)
        self.rev = c.get("rev")

    def set_feeds(self, tgts):
        old = self.feeds
        if tgts is None:
            new = self.file_feeds
        else:
            new = []
            for t in tgts:
                try:
                    f = CountFeed.from_target(t)
                except OSError as e:
                    print(f"[config] feed {t.get('host')}: {e}")
                    continue
                if old:                       # keep cumulative totals: the field baselines per match
                    f.counts = dict(old[0].counts)
                f.start()
                new.append(f)
                print(f"[config] count feed {f.name_} -> {f.dest[0]}:{f.dest[1]} session {f.session}")
        self.feeds = new
        for f in old:
            if f not in new and f not in self.file_feeds:
                f.stop.set()
        if not new:
            print("[config] no field feed")

    def add(self, hub, t, n):
        with self.lock:
            self.buf[hub].append([t, n])

    def run(self):
        while not STOP.is_set():
            time.sleep(self.period)
            with self.lock:
                batch = {h: v[:] for h, v in self.buf.items()}
                status = dict(self.status)
            if self.feeds:
                status["feeds"] = [f.status() for f in self.feeds]
            try:
                t_send = time.time()
                r = requests.post(f"{self.url}/api/vision/events", timeout=2,
                                  headers={"X-Vision-Key": self.key},
                                  json={"events": batch, "status": status, "source": "live"})
                r.raise_for_status()
                j = r.json()
                self._clock_sample(t_send, time.time(), j.get("now"))
                self.record = j.get("record")
                if j.get("config_rev") is not None and j["config_rev"] != self.rev:
                    try:
                        self.apply_config()
                    except Exception as e:          # noqa: BLE001  keep counting on the old settings
                        print(f"[config] couldn't apply the FMS settings: {e}")
                        self.rev = j["config_rev"]
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
    def __init__(self, hub, cfg, sender, rec_dir, preview, plugins=None):
        super().__init__(daemon=True)
        self.plugins = plugins
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
        try:
            counter = load_counter(self.cfg, self.plugins)
        except BaseException as e:      # SystemExit from a bad name, or the plugin's own error
            # Before, a plugin that failed to load ended this thread silently and
            # /control showed the hub as "never connected". Say why, there and here.
            msg = f"counter {self.cfg.get('counter')!r} failed to load: {e}"
            print(f"{self.hub}: {msg}")
            self.sender.status[self.hub] = {"fps": 0, "error": msg, "counter": str(self.cfg.get("counter"))}
            return
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
                n = self.sender.scalers[self.hub].add(n)     # calibration (Setup: counts per ball)
            if n:
                for f in self.sender.feeds:  # first: these are on the field's auto deadline
                    f.add(self.hub, n, t)
                self.sender.add(self.hub, t + self.sender.offset, n)
            if self.recorder:
                self.recorder.update(self.sender.record, frame, t + self.sender.offset, fps_nominal)
            n_frames += 1
            if time.time() - t_fps >= 1:
                fps = n_frames / (time.time() - t_fps)
                n_frames, t_fps = 0, time.time()
                sc = self.sender.scalers[self.hub]
                self.sender.status[self.hub] = {"fps": round(fps, 1), "counter": self.cfg["counter"],
                                                "session_total": counter.total, "counted": sc.out,
                                                "scale": sc.scale, "clock_offset": round(self.sender.offset, 3),
                                                **plugin_status(counter)}
            if self.preview:
                self.frame_out = counter.draw(frame.copy())
        if self.recorder:
            self.recorder.close()
        close = getattr(counter, "close", None)
        if callable(close):
            try:
                close()
            except Exception as e:                        # noqa: BLE001
                print(f"{self.hub}: counter close() failed: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="../config/vision.yaml")
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--feed", action="append", metavar="HOST:PORT",
                    help="also stream live counts to this field system (repeatable; adds to config feeds:)")
    ap.add_argument("--list-counters", action="store_true",
                    help="list every counter plugin available (built-in, installed, plugin_paths) and exit")
    ap.add_argument("--add-plugin", metavar="FOLDER",
                    help="add a folder of counters (e.g. another repo) to vision/plugins/ and exit")
    ap.add_argument("--remove-plugin", metavar="NAME",
                    help="remove a folder added with --add-plugin (its folder name) and exit")
    args = ap.parse_args()
    if args.add_plugin:
        names = add_plugin(args.add_plugin)
        print(f"added: counter: {' / '.join(names)} now works in config/vision.yaml")
        return
    if args.remove_plugin:
        print("removed" if remove_plugin(args.remove_plugin) else f"no plugin {args.remove_plugin!r} in vision/plugins/")
        return
    if args.list_counters:
        import yaml
        p = pathlib.Path(args.config)
        raw = (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}
        add_plugin_paths(raw.get("plugin_paths"), p.parent)
        print(list_counters(raw.get("plugins")))
        return
    cfg = vconfig.load(args.config)
    add_plugin_paths(cfg.get("plugin_paths"), pathlib.Path(args.config).parent)
    feeds = [CountFeed.from_target(t) for t in targets(cfg, args.feed)]
    for f in feeds:
        f.start()
        print(f"count feed {f.name_} -> {f.dest[0]}:{f.dest[1]} session {f.session}")
    sender = Sender(cfg["fms_url"], cfg["vision_key"], feeds=feeds)
    sender.start()
    defaults = cfg.get("defaults", {})
    workers = []
    for hub in ("red", "blue"):
        if hub in cfg["hubs"] and cfg["hubs"][hub].get("enabled", True):
            w = HubWorker(hub, {**defaults, **cfg["hubs"][hub]}, sender,
                          cfg.get("record_dir"), args.preview, cfg.get("plugins"))
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
