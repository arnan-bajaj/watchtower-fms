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

/control -> Setup can override the feeds and set each hub's calibration (counts per ball), camera,
outline and ball size (Setup -> Cameras, hubsetup.py). The FMS bumps `config_rev` in its reply to
every POST; we then fetch /api/vision/config and apply it live, no restart. Feeds set there replace
the config file's (and --feed); null keeps them. A hub Setup changes reopens its camera if that
changed and rebuilds its counter; the other hub keeps counting.

The same reply says what Setup -> Cameras wants: pictures from each hub camera (about one a
second while the page is open), a scan for cameras, or a hub's drop test: record every frame's
yellow blobs near its outline while someone drops balls in, then fit the one-ball size to the
number dropped (dropfit.py) and send it back.
"""
from __future__ import annotations

import argparse
import base64
import csv
import pathlib
import threading
import time

import cv2
import numpy as np
import requests

import dropfit
import hubsetup
import vconfig
from count_feed import CountFeed, targets
from scaler import Scaler
from counters import (add_plugin, add_plugin_paths, list_counters, load_counter,
                      plugin_status, remove_plugin)

STOP = threading.Event()
SCAN_MAX = 6               # camera indexes tried by Find cameras


def snapshot(frame, max_w=960):
    """A JPEG for /control (base64), and the full picture size its outline is drawn in."""
    h, w = frame.shape[:2]
    small = cv2.resize(frame, None, fx=max_w / w, fy=max_w / w) if w > max_w else frame
    ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return {"w": w, "h": h, "jpeg": base64.b64encode(buf.tobytes()).decode() if ok else ""}


def scan_cameras(in_use, n=SCAN_MAX):
    """Setup -> Find cameras: a picture from every camera index that opens, skipping the ones a hub
    already has open (its own pictures show those)."""
    found = []
    for i in range(n):
        if str(i) in in_use:
            continue
        cap = cv2.VideoCapture(i)
        try:
            if not cap.isOpened():
                continue
            frame, end = None, time.time() + 2.5
            while time.time() < end:          # the first frames can be black while it wakes up
                ok, f = cap.read()
                if ok:
                    frame = f
                    if f.mean() > 8:
                        break
            if frame is not None:
                found.append({"source": str(i), **snapshot(frame, 480)})
        finally:
            cap.release()
    return found


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
        self.workers = {}                   # hub -> HubWorker
        self.hub_setup = {}                 # hub -> Setup -> Cameras entry (None: config file)
        self.want_frames = False            # Setup -> Cameras is open: send hub pictures
        self.frames = {}                    # hub -> latest snapshot() to send
        self.measured = {}                  # hub -> drop-test result to send
        self.scan_seen = None               # last Find cameras id started

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
        for h, entry in (c.get("hubs") or {}).items():
            if h in ("red", "blue") and entry != self.hub_setup.get(h):
                self.hub_setup[h] = entry
                print(f"[config] {h}: " + (f"camera {entry['source']}" + (", outline drawn" if entry.get("outline") else "")
                                          if entry else "camera from the config file"))
                if h in self.workers:
                    self.workers[h].reconfigure(entry)
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

    def handle_want(self, want):
        """What Setup -> Cameras asks for, from the FMS's reply."""
        self.want_frames = bool(want.get("frames"))
        drops = want.get("drop") or {}
        for h, w in self.workers.items():
            w.drop_req = drops.get(h)           # {"id", "n"}: recording until n arrives; None: no test
        sid = want.get("scan")
        if sid and sid != self.scan_seen:
            self.scan_seen = sid
            threading.Thread(target=self.scan, args=(sid,), daemon=True).start()

    def scan(self, sid):
        in_use = {w.cap_src: h for h, w in self.workers.items() if w.cap is not None}
        try:
            cams, err = scan_cameras(in_use), None
        except Exception as e:                  # noqa: BLE001
            cams, err = [], str(e)
        print(f"[cameras] found {len(cams)} more besides {len(in_use)} in use")
        try:
            requests.post(f"{self.url}/api/vision/cameras", timeout=10, headers={"X-Vision-Key": self.key},
                          json={"id": sid, "cameras": cams, "in_use": in_use, "error": err})
        except Exception as e:                  # noqa: BLE001
            print(f"[cameras] couldn't send the scan: {e}")

    def send_frames(self):
        with self.lock:
            frames, self.frames = self.frames, {}
        if frames:
            try:
                requests.post(f"{self.url}/api/vision/frames", timeout=5, headers={"X-Vision-Key": self.key},
                              json=frames)
            except Exception:                   # noqa: BLE001  only pictures for Setup
                pass

    def run(self):
        while not STOP.is_set():
            time.sleep(self.period)
            with self.lock:
                batch = {h: v[:] for h, v in self.buf.items()}
                status = dict(self.status)
                measured = dict(self.measured)
            if self.feeds:
                status["feeds"] = [f.status() for f in self.feeds]
            try:
                t_send = time.time()
                r = requests.post(f"{self.url}/api/vision/events", timeout=2,
                                  headers={"X-Vision-Key": self.key},
                                  json={"events": batch, "status": status, "source": "live", "measured": measured})
                r.raise_for_status()
                j = r.json()
                with self.lock:
                    for h in measured:
                        if self.measured.get(h) is measured[h]:
                            del self.measured[h]
                self.handle_want(j.get("want") or {})
                self.send_frames()
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
    """One hub: its camera, its counter, and Setup -> Cameras' pictures and drop test.
    A camera with no counter yet (outline not drawn) still sends pictures, so it can be drawn on."""

    def __init__(self, hub, base, sender, rec_dir, preview, plugins=None):
        super().__init__(daemon=True)
        self.plugins = plugins
        self.hub, self.base, self.sender, self.preview = hub, {**base, "hub": hub}, sender, preview
        self.recorder = Recorder(rec_dir, hub) if rec_dir else None
        self.frame_out = None
        self.setup = sender.hub_setup.get(hub)  # Setup -> Cameras entry; None: the config file's hub
        self.changed = threading.Event()
        self.drop_req = None                    # Setup's drop test, from the Sender: {"id", "n"} or None
        self.drop = None                        # the drop test recording: {"id", "frames", "poly", "box", "t0"}
        self.drop_done = set()                  # drop test ids already answered
        self.cap, self.cap_src = None, None
        self.cfg = hubsetup.merge(self.base, self.setup)

    def reconfigure(self, setup):
        self.setup = setup
        self.changed.set()

    def open(self, cfg):
        src = str(cfg.get("source", "")).strip()
        if src in ("", "none"):
            return None
        cap = cv2.VideoCapture(int(src) if src.isdigit() else src)
        if cfg.get("width"):
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg["width"])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg["height"])
        if cfg.get("fps"):
            cap.set(cv2.CAP_PROP_FPS, cfg["fps"])
        return cap

    def run(self):
        while not STOP.is_set():
            self.changed.clear()
            self.cfg = cfg = hubsetup.merge(self.base, self.setup)
            src = str(cfg.get("source", "")).strip()
            if src != self.cap_src:             # same camera: keep it open (no flicker, no re-prompt)
                if self.cap is not None:
                    self.cap.release()
                self.cap_src, self.cap = src, self.open(cfg)
            self.count(cfg)
        if self.cap is not None:
            self.cap.release()
        if self.recorder:
            self.recorder.close()

    def count(self, cfg):
        """Count with `cfg` until Setup changes this hub (or vision stops)."""
        src = str(cfg.get("source", "")).strip()
        name = str(cfg.get("counter"))
        why = hubsetup.waiting(cfg)
        if not src:
            while not STOP.is_set():
                self.sender.status[self.hub] = {"fps": 0, "counter": name, "idle": True, "detail": why}
                if self.changed.wait(1.0):
                    return
            return
        is_file = not src.isdigit() and src != "none" and not src.startswith(("rtsp", "http"))
        cap = self.cap
        fps_nominal = (cap.get(cv2.CAP_PROP_FPS) if cap else 0) or cfg.get("fps", 30)
        counter, error, fitted = None, None, None
        t0, idx, n_frames, t_fps, t_snap = time.time(), 0, 0, time.time(), 0.0
        fps = 0.0
        while not (STOP.is_set() or self.changed.is_set()):
            if cap is None:  # "none" source: blank frames (mock counter)
                time.sleep(1 / fps_nominal)
                ok, frame = True, np.zeros((360, 640, 3), np.uint8)
            else:
                ok, frame = cap.read()
            if not ok:
                if is_file and cfg.get("loop", True):
                    cap.release()
                    self.cap = cap = self.open(cfg)
                    t0, idx = time.time(), 0
                    continue
                self.sender.status[self.hub] = {"fps": 0, "error": f"camera {src}: no picture", "counter": name}
                if self.changed.wait(0.5):
                    break
                cap.release()
                self.cap = cap = self.open(cfg)
                continue
            if is_file:  # play files in real time so timestamps behave like a live camera
                t = t0 + idx / fps_nominal
                idx += 1
                lag = t - time.time()
                if lag > 0:
                    time.sleep(lag)
            else:
                t = time.time()
            h, w = frame.shape[:2]
            if fitted is None:
                fitted = hubsetup.fit(cfg, w, h)
            if counter is None and error is None and not why:
                try:
                    counter = load_counter(fitted, self.plugins)
                except BaseException as e:      # SystemExit from a bad name, or the plugin's own error
                    error = f"counter {name!r} failed to load: {e}"
                    print(f"{self.hub}: {error}")
            if counter is not None:
                n = counter.process(frame, t)
                if n:
                    n = self.sender.scalers[self.hub].add(n)     # calibration (Setup: counts per ball)
                if n:
                    for f in self.sender.feeds:  # first: these are on the field's auto deadline
                        f.add(self.hub, n, t)
                    self.sender.add(self.hub, t + self.sender.offset, n)
            self.drop_step(frame, fitted, w, h)
            if self.sender.want_frames and time.time() - t_snap >= 1.0:
                t_snap = time.time()
                with self.sender.lock:
                    self.sender.frames[self.hub] = {"source": src, **snapshot(frame)}
            if self.recorder and counter is not None:
                self.recorder.update(self.sender.record, frame, t + self.sender.offset, fps_nominal)
            n_frames += 1
            if time.time() - t_fps >= 1:
                fps = n_frames / (time.time() - t_fps)
                n_frames, t_fps = 0, time.time()
                st = {"fps": round(fps, 1), "counter": name, "source": src, "size": [w, h]}
                if counter is not None:
                    sc = self.sender.scalers[self.hub]
                    st.update(session_total=counter.total, counted=sc.out, scale=sc.scale,
                              clock_offset=round(self.sender.offset, 3), **plugin_status(counter))
                elif error:
                    st["error"] = error
                else:
                    st.update(idle=True, detail=why)
                if self.drop:
                    st["drop"] = {"s": round(time.time() - self.drop["t0"]), "frames": len(self.drop["frames"])}
                self.sender.status[self.hub] = st
            if self.preview:
                self.frame_out = counter.draw(frame.copy()) if counter is not None else frame
        if counter is not None:
            close = getattr(counter, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as e:                    # noqa: BLE001
                    print(f"{self.hub}: counter close() failed: {e}")

    def drop_step(self, frame, fitted, w, h):
        """Setup -> Calibrate: record this frame's blobs for the drop test, or fit once the number of
        balls dropped arrives. The counter (if any) keeps counting meanwhile."""
        req = self.drop_req
        if not req:
            self.drop = None                    # cancelled
            return
        if req["id"] in self.drop_done:
            return
        if self.drop is None or self.drop["id"] != req["id"]:
            poly = [tuple(p) for p in (fitted or {}).get("outline") or []]
            if not poly:
                self.drop_done.add(req["id"])
                self.sender.measured[self.hub] = {"id": req["id"], "error": "Draw the hub's outline first."}
                return
            from hubcounter import hubcount as HC
            box = dropfit.record_box(poly, w, h)
            self.drop = {"id": req["id"], "frames": [], "poly": poly, "eye": HC.ZoneEye(box), "t0": time.time()}
            print(f"[calibrate] {self.hub}: recording the drop test")
        d = self.drop
        if req.get("n"):
            self.drop_done.add(d["id"])
            self.drop = None
            threading.Thread(target=self.drop_fit, args=(d, int(req["n"]), w, h), daemon=True).start()
        elif len(d["frames"]) < dropfit.MAX_FRAMES:
            d["frames"].append(d["eye"].blobs(frame))

    def drop_fit(self, d, n, w, h):
        res = {"id": d["id"], "n": n, "size": [w, h], "frames": len(d["frames"])}
        try:
            res.update(dropfit.fit(d["poly"], d["frames"], n, w, h))
        except Exception as e:                  # noqa: BLE001  ValueError says why
            res["error"] = str(e)
        print(f"[calibrate] {self.hub}: {res}")
        self.sender.measured[self.hub] = res


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
    try:                                   # Setup's cameras before the first frame, if the FMS is up
        sender.apply_config()
    except Exception:                      # noqa: BLE001  the first reply will bring them
        pass
    defaults = cfg.get("defaults", {})
    workers = []
    for hub in ("red", "blue"):            # both, even if the file has neither: Setup can add cameras
        hc = (cfg.get("hubs") or {}).get(hub) or {}
        if hc.get("enabled", True):
            w = HubWorker(hub, {**defaults, **hc}, sender,
                          cfg.get("record_dir"), args.preview, cfg.get("plugins"))
            sender.workers[hub] = w
            workers.append(w)
    sender.start()
    for w in workers:
        w.start()
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
