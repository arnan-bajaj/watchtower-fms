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
number dropped (dropfit.py) and send it back. Also from there: Measure (5 s of still balls near the
outline, or anywhere before one is drawn: an estimate for the ruler, never the ball size), the fuel
colour preview (what the colour range takes as fuel painted magenta on Setup's pictures), Test -> Record (every hub camera into
`test_dir`, default `recordings`), and Test -> Check a recording (re-count one of those files, or a
match recording, with the hub's current setup against a hand count). Each hub reports its
capture-to-sent latency against the field feed's spec (hubcount.latency_summary).
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
from hubcounter import camprofile
from hubcounter import hubcount as HC
from count_feed import CountFeed, targets
from scaler import Scaler
from counters import (add_plugin, add_plugin_paths, list_counters, load_counter,
                      plugin_status, remove_plugin)

STOP = threading.Event()
SCAN_MAX = 6               # camera indexes tried by Find cameras
LATENCY_KEEP = 300         # recent counts the latency check looks at
FILES_EVERY_S = 5.0        # how often the recordings folder is listed for Test -> Check a recording


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


def fuel_view(frame, colour):
    """Setup -> Fuel colour -> Show what counts as fuel: the picture with every pixel in the colour range
    painted magenta (the old Hub Counter page's overlay). The frame itself if no preview is on."""
    if not colour:
        return frame
    try:
        lo, hi = camprofile.gate(colour)
    except ValueError:
        return frame
    h, w = frame.shape[:2]
    out = frame.copy()
    out[HC.yellow_mask(frame, (0, 0, w, h), lo, hi) > 0] = (255, 0, 255)
    return out


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
        self.sized = {}                     # hub -> Measure result to send
        self.scan_seen = None               # last Find cameras id started
        self.dirs = {}                      # "match" / "test" -> folder of recordings (Test -> Check)
        self.test_record = None             # Test -> Record: the recording's id while it runs
        self.preview = {}                   # hub -> Fuel colour range to paint magenta on its pictures
        self.check_seen = None              # last Check a recording id started
        self.check_prog = None              # {"id", "frames", "frac"} while a recording is re-counted
        self.checked = None                 # its result, to send
        self._files, self._files_t = [], 0.0

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
        def counted(e):                     # Measure's estimate changes nothing the counter uses
            return {k: v for k, v in e.items() if k != "measured"} if e else e
        for h, entry in (c.get("hubs") or {}).items():
            if h in ("red", "blue") and counted(entry) != counted(self.hub_setup.get(h)):
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
        drops, sizes = want.get("drop") or {}, want.get("size") or {}
        for h, w in self.workers.items():
            w.drop_req = drops.get(h)           # {"id", "n"}: recording until n arrives; None: no test
            w.size_req = sizes.get(h)           # {"id"}: Measure
        self.test_record = want.get("record") or None
        self.preview = want.get("preview") or {}
        chk = want.get("check")
        if chk and chk.get("id") != self.check_seen:
            self.check_seen = chk["id"]
            self.check_prog = {"id": chk["id"], "frames": 0, "frac": 0.0}
            threading.Thread(target=self.check_recording, args=(chk,), daemon=True).start()
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

    def files(self):
        """The recordings Test -> Check a recording can pick from, newest first."""
        if time.time() - self._files_t < FILES_EVERY_S:
            return self._files
        self._files_t, out, seen = time.time(), [], set()
        for kind, d in self.dirs.items():
            if not d or not d.is_dir() or d.resolve() in seen:
                continue
            seen.add(d.resolve())
            for f in d.glob("*.mp4"):
                try:
                    st = f.stat()
                except OSError:
                    continue
                hub = f.stem.rsplit("_", 1)[-1]
                out.append({"name": f.name, "dir": kind, "hub": hub if hub in ("red", "blue") else None,
                            "mb": round(st.st_size / 1e6, 1), "t": round(st.st_mtime)})
        self._files = sorted(out, key=lambda f: -f["t"])[:40]
        return self._files

    def check_recording(self, req):
        res = {"id": req["id"], "name": req.get("name"), "hub": req.get("hub")}
        try:
            res.update(self._check(req))
        except Exception as e:                  # noqa: BLE001  ValueError says why
            res["error"] = str(e)
        print(f"[check] {res}")
        with self.lock:
            self.checked, self.check_prog = res, None

    def _check(self, req):
        """Re-count one recording with this hub's setup as it is now: the hub's count, and with an exit
        line also the outline and the exit line alone, each against the hand count."""
        w = self.workers.get(req.get("hub"))
        if w is None:
            raise ValueError("Vision isn't running that hub.")
        d = self.dirs.get(req.get("dir"))
        path = d / pathlib.Path(str(req.get("name") or "")).name if d else None
        if path is None or not path.is_file():
            raise ValueError("That recording isn't there any more.")
        cfg = hubsetup.merge(w.base, w.setup)
        why = hubsetup.waiting({**cfg, "source": str(path)})
        if why:
            raise ValueError(f"This hub can't count yet: {why}.")
        hand = int(req["hand"]) if req.get("hand") else None
        cap = cv2.VideoCapture(str(path))
        try:
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            n_all = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
            stamps = path.with_suffix(".csv")
            ts = [float(r["t"]) for r in csv.DictReader(open(stamps))] if stamps.exists() else None
            ok, frame = cap.read()
            if not ok:
                raise ValueError("Couldn't read that recording.")
            fh, fw = frame.shape[:2]
            fitted = hubsetup.fit(cfg, fw, fh)
            ways = [("what the hub counts now", fitted)]
            if fitted.get("line") and fitted.get("outline"):
                ways += [("outline alone", {k: v for k, v in fitted.items() if k not in ("line", "out")}),
                         ("exit line alone", {k: v for k, v in fitted.items() if k != "outline"})]
            counters = [(label, load_counter(c, w.plugins, warn=lambda *_: None)) for label, c in ways]
            i = 0
            while ok and not STOP.is_set():
                t = ts[i] if ts and i < len(ts) else i / fps
                for _, c in counters:
                    c.process(frame, t)
                i += 1
                if i % 30 == 0:
                    self.check_prog = {"id": req["id"], "frames": i, "frac": round(i / n_all, 3) if n_all else None}
                ok, frame = cap.read()
        finally:
            cap.release()
        scale, rows = self.scalers[req["hub"]].scale, []
        for label, c in counters:
            n = int(c.total / scale + 1e-9)          # counts per ball, as the live count
            row = {"label": label, "count": n}
            if hand:
                row.update(error=n - hand, pct=round(100 * abs(n - hand) / hand, 1))
            rows.append(row)
            close = getattr(c, "close", None)
            if callable(close):
                close()
        return {"rows": rows, "hand": hand, "frames": i, "seconds": round(i / fps, 1)}

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
                measured, sized, checked = dict(self.measured), dict(self.sized), self.checked
            if self.feeds:
                status["feeds"] = [f.status() for f in self.feeds]
            status["files"] = self.files()
            if self.check_prog:
                status["checking"] = dict(self.check_prog)
            try:
                t_send = time.time()
                r = requests.post(f"{self.url}/api/vision/events", timeout=2,
                                  headers={"X-Vision-Key": self.key},
                                  json={"events": batch, "status": status, "source": "live", "measured": measured,
                                        "sized": sized, "checked": checked})
                r.raise_for_status()
                j = r.json()
                with self.lock:
                    for got, sent in ((self.measured, measured), (self.sized, sized)):
                        for h in sent:
                            if got.get(h) is sent[h]:
                                del got[h]
                    if self.checked is checked:
                        self.checked = None
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

    def __init__(self, hub, base, sender, rec_dir, preview, plugins=None, test_dir=None):
        super().__init__(daemon=True)
        self.plugins = plugins
        self.hub, self.base, self.sender, self.preview = hub, {**base, "hub": hub}, sender, preview
        self.recorder = Recorder(rec_dir, hub) if rec_dir else None      # every match (record_dir)
        self.tester = Recorder(test_dir, hub) if test_dir else None      # Test -> Record
        self.size_req, self.sizing, self.size_done = None, None, set()  # Measure: request, recording, answered
        self.lat = []                           # capture-to-sent ms of recent counts
        self.frame_out = None
        self.setup = sender.hub_setup.get(hub)  # Setup -> Cameras entry; None: the config file's hub
        self.changed = threading.Event()
        self.drop_req = None                    # Setup's drop test, from the Sender: {"id", "n"} or None
        self.drop = None                        # the drop test recording: {"id", "frames", "poly", "box", "t0"}
        self.drop_done = set()                  # drop test ids already answered
        self.cap, self.cap_src, self.cap_key = None, None, None
        self.ignored = []                       # picture settings this camera's driver didn't take
        self.cfg = hubsetup.merge(self.base, self.setup)

    def reconfigure(self, setup):
        self.setup = setup
        self.changed.set()

    def open(self, cfg):
        src = str(cfg.get("source", "")).strip()
        if src in ("", "none"):
            return None
        cap = cv2.VideoCapture(int(src) if src.isdigit() else src)
        image = cfg.get("image") if src.isdigit() else None   # a file or stream has its picture baked in
        if image:
            camprofile.set_fourcc(cap, image.get("fourcc"))     # first: 60 fps modes are often MJPG only
        if cfg.get("width"):
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg["width"])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg["height"])
        if cfg.get("fps"):
            cap.set(cv2.CAP_PROP_FPS, cfg["fps"])
        self.ignored = []
        if image:
            try:
                res = camprofile.apply_image(cap, image, name=f"{self.hub} camera {src}")
                self.ignored = [k for k, v in res.items() if v == "ignored"]
            except ValueError as e:
                print(f"{self.hub}: picture settings not applied: {e}")
        return cap

    def run(self):
        while not STOP.is_set():
            self.changed.clear()
            self.cfg = cfg = hubsetup.merge(self.base, self.setup)
            src, key = str(cfg.get("source", "")).strip(), hubsetup.camera_key(cfg)
            if key != self.cap_key:             # same camera and picture: keep it open (no flicker, no re-prompt)
                if self.cap is not None:
                    self.cap.release()
                self.cap_src, self.cap_key, self.cap = src, key, self.open(cfg)
            self.count(cfg)
        if self.cap is not None:
            self.cap.release()
        for r in (self.recorder, self.tester):
            if r:
                r.close()

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
            mono = time.monotonic()
            captured = HC.frame_time(cap.get(cv2.CAP_PROP_POS_MSEC), mono) if cap is not None and not is_file else mono
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
                    self.lat = (self.lat + [(time.monotonic() - captured) * 1000])[-LATENCY_KEEP:]
                    self.sender.add(self.hub, t + self.sender.offset, n)
            self.drop_step(frame, fitted, w, h)
            self.size_step(frame, fitted, w, h)
            if self.tester:
                self.tester.update(self.sender.test_record, frame, t + self.sender.offset, fps_nominal)
            if self.sender.want_frames and time.time() - t_snap >= 1.0:
                t_snap = time.time()
                pic = fuel_view(frame, self.sender.preview.get(self.hub))
                with self.sender.lock:
                    self.sender.frames[self.hub] = {"source": src, "fuel": pic is not frame, **snapshot(pic)}
            if self.recorder and counter is not None:
                self.recorder.update(self.sender.record, frame, t + self.sender.offset, fps_nominal)
            n_frames += 1
            if time.time() - t_fps >= 1:
                fps = n_frames / (time.time() - t_fps)
                n_frames, t_fps = 0, time.time()
                st = {"fps": round(fps, 1), "counter": name, "source": src, "size": [w, h]}
                if self.ignored:
                    st["ignored"] = self.ignored
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
                if self.sizing:
                    st["sizing"] = {"s": round(time.time() - self.sizing["t0"], 1)}
                if self.lat:
                    st["latency"] = HC.latency_summary(self.lat)
                if self.tester and self.tester.vw is not None:
                    st["recording"] = {"name": f"{self.tester.cur}_{self.hub}.mp4", "frames": self.tester.n}
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
            lo, hi = camprofile.gate((fitted or {}).get("colour"))        # the hub's fuel colour, as it counts
            box = dropfit.record_box(poly, w, h)
            self.drop = {"id": req["id"], "frames": [], "poly": poly, "eye": HC.ZoneEye(box, lo=lo, hi=hi), "t0": time.time()}
            print(f"[calibrate] {self.hub}: recording the drop test")
        d = self.drop
        if req.get("n"):
            self.drop_done.add(d["id"])
            self.drop = None
            threading.Thread(target=self.drop_fit, args=(d, int(req["n"]), w, h), daemon=True).start()
        elif len(d["frames"]) < dropfit.MAX_FRAMES:
            d["frames"].append(d["eye"].blobs(frame))

    def size_step(self, frame, fitted, w, h):
        """Setup -> Measure: for MEASURE_S seconds, the areas of lone round blobs near the outline (the old
        page's Measure, hubcount.measure), or anywhere in the picture before an outline is drawn (so its
        ruler can help draw the first one), then their median as an estimate. Counting carries on."""
        req = self.size_req
        if not req or req["id"] in self.size_done:
            self.sizing = None
            return
        if self.sizing is None or self.sizing["id"] != req["id"]:
            poly = [tuple(p) for p in (fitted or {}).get("outline") or []]
            box = dropfit.record_box(poly, w, h) if poly else (0, 0, w, h)
            self.sizing = {"id": req["id"], "box": box, "areas": [], "frames": 0, "t0": time.time(),
                           "gate": camprofile.gate((fitted or {}).get("colour"))}
        sz = self.sizing
        sz["areas"] += HC.isolated_ball_areas(frame, sz["box"], *sz["gate"])
        sz["frames"] += 1
        if time.time() - sz["t0"] >= dropfit.MEASURE_S:
            self.size_done.add(sz["id"])
            self.sizing = None
            res = {"id": sz["id"], "size": [w, h]}
            try:
                res.update(dropfit.estimate(sz["areas"], sz["frames"]))
            except ValueError as e:
                res["error"] = str(e)
            print(f"[measure] {self.hub}: {res}")
            with self.sender.lock:
                self.sender.sized[self.hub] = res

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
    test_dir = cfg.get("test_dir") or cfg.get("record_dir") or "recordings"     # Test -> Record
    sender.dirs = {k: pathlib.Path(d) for k, d in (("match", cfg.get("record_dir")), ("test", test_dir)) if d}
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
                          cfg.get("record_dir"), args.preview, cfg.get("plugins"), test_dir)
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
