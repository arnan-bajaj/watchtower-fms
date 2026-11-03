"""Run one event's Watchtower on this computer: the FMS and the hub counter.

The website's launch command installs Watchtower and then runs this. Run it again any time:

  python launch.py EVENT_FOLDER              # FMS + hub cameras (set up in /control -> Setup -> Cameras)
  python launch.py EVENT_FOLDER --mock       # FMS + fake fuel, no cameras (rehearsal)
  python launch.py EVENT_FOLDER --no-vision  # FMS only (score by hand in review, or vision elsewhere)
  python launch.py EVENT_FOLDER --vision-config my-vision.yaml   # your own vision config instead

A REEFSCAPE (2025) event has no hub counter: scorers count it live on /score and /control commits.

EVENT_FOLDER holds config/event.yaml (written by the website) and data/ (matches, fuel, backups).
Everything else (teams, TBA keys, PINs, field system, cameras, calibration) is in /control -> Setup.
No camera is opened until one is picked there. Ctrl+C stops everything this started.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser

APP = pathlib.Path(__file__).resolve().parent
MOCK = """# Written by launch.py --mock: fake fuel, no cameras or model.
fms_url: http://127.0.0.1:{port}
record_dir: ""
defaults: {{counter: mock, rate_per_s: 3}}
hubs: {{red: {{source: none}}, blue: {{source: none}}}}
"""
CAMERAS = """# Written by launch.py on every start. Each hub's camera, outline and ball size are set in
# /control -> Setup -> Cameras; the shipped colour counter needs no model. Setup -> Test -> Record
# saves each hub camera into test_dir (the event folder's recordings/).
fms_url: http://127.0.0.1:{port}
record_dir: ""
test_dir: '{tests}'
defaults: {{counter: colour, width: 1920, height: 1080, fps: 60}}
hubs: {{red: {{}}, blue: {{}}}}
"""


def up(port, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=1)
            return True
        except OSError:
            time.sleep(0.3)
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("event", help="the event's folder (has config/event.yaml)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--mock", action="store_true", help="fake fuel, no cameras")
    g.add_argument("--no-vision", action="store_true", help="no hub counter at all")
    g.add_argument("--vision-config", metavar="FILE", help="run vision on this config instead of Setup's cameras")
    g.add_argument("--cameras", action="store_true", help=argparse.SUPPRESS)   # the default now; old commands
    ap.add_argument("--preview", action="store_true", help="show what each camera sees in a window")
    ap.add_argument("--port", type=int, help="serve on this port instead of event.yaml's (8000)")
    ap.add_argument("--no-browser", action="store_true", help="don't open /control")
    args, vision_args = ap.parse_known_args()

    ev = pathlib.Path(args.event).expanduser().resolve()
    cfg_path = ev / "config" / "event.yaml"
    if not cfg_path.exists():
        sys.exit(f"No {cfg_path}. Run the launch command from your event's page on the website again.")
    os.chdir(ev)                                   # data/ and backups live next to the event's config
    sys.path.insert(0, str(APP))
    from fms import cloud, config                  # noqa: E402
    cfg = config.load(str(cfg_path))
    port = args.port or cfg["server"]["port"]
    env = {**os.environ, "FMS_CONFIG": str(cfg_path), "FMS_PORT": str(port), "PYTHONPATH": str(APP),
           "PYTHONUNBUFFERED": "1"}

    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=1)
        sys.exit(f"Something already answers on port {port} (another Watchtower?). Stop it first (Ctrl+C in its terminal).")
    except OSError:
        pass

    procs = []
    fms = subprocess.Popen([sys.executable, "-m", "fms.server"], cwd=ev, env=env)
    procs.append(fms)
    if sys.platform == "darwin" and shutil.which("caffeinate"):      # no sleep while the FMS runs
        procs.append(subprocess.Popen(["caffeinate", "-dimsu", "-w", str(fms.pid)]))

    def stop(*_):
        for p in reversed(procs):
            if p.poll() is None:
                p.send_signal(signal.SIGINT)
        for p in procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        sys.exit(0)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    if not up(port):
        stop()
    vcfg = None
    hand = cfg["event"]["game"] != "rebuilt"
    if hand:
        pass                                       # REEFSCAPE: scored by hand, no hub counter
    elif args.mock:
        vcfg = ev / "config" / "vision.mock.yaml"
        vcfg.write_text(MOCK.format(port=port))
    elif args.vision_config:
        vcfg = pathlib.Path(args.vision_config).expanduser().resolve()
    elif not args.no_vision:
        vcfg = ev / "config" / "vision.cameras.yaml"
        vcfg.write_text(CAMERAS.format(port=port, tests=(ev / "recordings").as_posix()))
    if vcfg:
        try:
            import cv2  # noqa: F401
        except ImportError:
            print("\nThe hub counter needs OpenCV, which didn't install. Run the launch command again to retry.")
            vcfg = None
    if vcfg:
        vargs = [sys.executable, "run_vision.py", "--config", str(vcfg), *vision_args]
        if args.preview:
            vargs.append("--preview")
        procs.append(subprocess.Popen(vargs, cwd=APP / "vision", env=env))

    urls = cloud.lan_urls(port) or [f"http://localhost:{port}"]
    from fms import settings                       # noqa: E402  PINs as Setup has them now
    from fms.store import Store                    # noqa: E402
    live = settings.overlay(cfg, Store(cfg["server"]["db"]).get("settings") or {})
    pins = live["server"]["pins"]
    print(f"""
  Watchtower is running: {live['event']['name']}
  Control (this computer):      http://localhost:{port}/control
  Phones on the same WiFi:      {urls[0]}   (ref, {'score, ' if hand else ''}emcee, display)
  PINs: control {pins['control']}  ref {pins['ref']}  emcee {pins['emcee']}{f"  scorer {pins.get('scorer') or 'off'}" if hand else ''}   (change them in /control -> Setup)
  Hub counter: {'none (REEFSCAPE: scorers count live on /score)' if hand else 'fake fuel' if args.mock
                else 'off' if not vcfg else vcfg.name if args.vision_config
                else 'pick each hub camera in /control -> Setup -> Cameras'}
  Ctrl+C here stops Watchtower. Your matches are saved in {ev / 'data'}.
""", flush=True)
    if not args.no_browser:
        webbrowser.open(f"http://localhost:{port}/control")
    while True:
        if fms.poll() is not None:
            print("The FMS stopped. See the messages above.")
            stop()
        time.sleep(1)


if __name__ == "__main__":
    main()
