"""Live settings: what /control -> Setup can change without editing event.yaml or restarting.

event.yaml stays the starting point. Each Setup edit is stored in SQLite as
{key: {"v": value, "t": unix time}}, and applied on top of event.yaml in place, so every handler that
reads CFG at call time sees it at once. Reverting a key to event.yaml stores a tombstone ({"v": None,
"t": ..., "del": True}) instead of dropping it, so a synced copy (the website, fms/cloud.py) can tell
"reverted later" from "never set". Two copies merge per key, newest `t` wins.

Pure functions only; fms/server.py owns the store and the HTTP side.
"""
from __future__ import annotations

import copy
import ipaddress
import re
from datetime import datetime

from . import config

_HHMM = re.compile(r"^\d{1,2}:\d{2}$")


def _str(n):
    def v(x):
        x = str(x if x is not None else "").strip()
        if len(x) > n:
            raise ValueError(f"at most {n} characters")
        return x
    return v


def _name(x):
    x = _str(80)(x)
    if not x:
        raise ValueError("can't be blank")
    return x


def _date(x):
    x = _str(10)(x)
    if x:
        datetime.strptime(x, "%Y-%m-%d")
    return x


def _num(lo, hi):
    def v(x):
        f = float(x)
        if not lo <= f <= hi:
            raise ValueError(f"must be {lo} to {hi}")
        return int(f) if f == int(f) else f
    return v


def _bool(x):
    if isinstance(x, bool):
        return x
    if str(x).lower() in ("1", "true", "yes", "on"):
        return True
    if str(x).lower() in ("0", "false", "no", "off", ""):
        return False
    raise ValueError("must be true or false")


def _hhmm(x):
    x = _str(5)(x)
    if not _HHMM.match(x):
        raise ValueError("use HH:MM, e.g. 09:30")
    return datetime.strptime(x, "%H:%M").strftime("%H:%M")


def _event_key(x):
    x = _str(20)(x).lower()
    if x and not re.match(r"^\d{4}[a-z0-9]+$", x):
        raise ValueError("looks like 2026abcd (year, then the event code)")
    return x


def _teams(x):
    if isinstance(x, str):
        x = re.split(r"[\s,;]+", x.strip())
    out = []
    for t in x or []:
        if t in ("", None):
            continue
        n = int(str(t).strip().lstrip("frc"))
        if not 1 <= n <= 99999:
            raise ValueError(f"{t} isn't a team number")
        if n in out:
            raise ValueError(f"{n} is listed twice")
        out.append(n)
    return out


def _pin(x):
    x = _str(12)(x)
    if not re.match(r"^\d{4,12}$", x):
        raise ValueError("4 to 12 digits")
    return x


def _pin_or_off(x):
    """The scorer PIN may be blank: nobody can log in as scorer."""
    return "" if x in (None, "") or str(x).strip() == "" else _pin(x)


def _host(x):
    x = _str(253)(x)
    try:
        ipaddress.ip_address(x)
    except ValueError:
        if not re.match(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$", x):
            raise ValueError(f"{x!r} isn't an IP address or host name") from None
    return x


def _host_or_blank(x):
    return _host(x) if str(x if x is not None else "").strip() else ""


def _feeds(x):
    """None = vision uses its own config file's feeds; [] = off; else [{name, host, port}]."""
    if x is None:
        return None
    if isinstance(x, dict):
        x = [x]
    if isinstance(x, str):                           # "192.168.1.10:8411, 10.0.100.5"
        x = [t for t in re.split(r"[\s,;]+", x) if t]
    out = []
    for f in x:
        if isinstance(f, str):                       # "192.168.1.10:8411"
            host, _, port = f.strip().rpartition(":") if ":" in f else (f.strip(), "", "")
            f = {"host": host, "port": port}
        host = _host(f.get("host"))
        if not host:
            continue
        port = int(_num(1, 65535)(f.get("port") or 8411))
        name = _str(40)(f.get("name")) or "field"
        out.append({"name": name, "host": host, "port": port})
    return out


def _point(p, what):
    if not (isinstance(p, (list, tuple)) and len(p) == 2):
        raise ValueError(f"{what} points are [x, y]")
    return [round(float(_num(0, 10000)(v)), 1) for v in p]


COLOUR_STANDARD = {"hue_lo": 15, "hue_hi": 40, "sat_min": 55, "val_min": 45}   # vision's camprofile.STANDARD_COLOUR


def fuel_colour(x):
    """Setup -> Fuel colour: None (the standard range) or {hue_lo, hue_hi, sat_min, val_min} in OpenCV HSV
    (hue 0-179, the rest 0-255); a missing key is the standard's."""
    if x is None:
        return None
    if not isinstance(x, dict):
        raise ValueError("a fuel colour is {hue_lo, hue_hi, sat_min, val_min}")
    c = {k: int(round(float(_num(0, 255)(x.get(k, d))))) for k, d in COLOUR_STANDARD.items()}
    if not c["hue_lo"] < c["hue_hi"] <= 179:
        raise ValueError("the fuel colour's hue runs 0 to 179, the low end below the high end")
    return c


def _picture(x):
    """Setup -> Picture: None (the camera's own and the vision file's size and fps) or {fps, size, fourcc,
    auto_exposure, exposure_ms, auto_wb, wb_kelvin, brightness, contrast, saturation, gain}; any None =
    the camera's own. Applied by vision when it opens a local camera (camprofile.apply_image)."""
    if x is None:
        return None
    if not isinstance(x, dict):
        raise ValueError("picture settings are {fps, size, exposure_ms, ...}")
    blank = lambda v: v in (None, "")
    out = {"fps": None if blank(x.get("fps")) else int(_num(1, 240)(x["fps"]))}
    size = x.get("size")
    if isinstance(size, str) and size.strip():
        size = size.lower().replace("×", "x").split("x")
    if blank(size) or size == []:
        out["size"] = None
    else:
        if not (isinstance(size, (list, tuple)) and len(size) == 2):
            raise ValueError("a picture size is width x height, e.g. 1280x720")
        out["size"] = [int(_num(160, 7680)(v)) for v in size]
    f = str(x.get("fourcc") or "").strip().upper()
    if f and not (len(f) == 4 and f.isalnum()):
        raise ValueError("a video format is four letters, e.g. MJPG or YUYV")
    out["fourcc"] = f or None
    for k in ("auto_exposure", "auto_wb"):
        out[k] = None if x.get(k) is None else bool(x[k])
    for k, lo, hi in (("exposure_ms", 0.1, 100), ("wb_kelvin", 2000, 8000), ("brightness", -10000, 10000),
                      ("contrast", -10000, 10000), ("saturation", -10000, 10000), ("gain", -10000, 10000)):
        out[k] = None if blank(x.get(k)) else round(float(_num(lo, hi)(x[k])), 2)
    return out if any(v is not None for v in out.values()) else None


def _hubcam(x):
    """Setup -> Cameras, one hub: None = vision's config file decides; else {"source": camera index or
    address, "size": [w, h] of the picture the outline was drawn on, "outline": [[x, y], ...] in those
    pixels (or [] = not drawn yet), "exit": None or {"line": [[x, y], [x, y]], "out": [x, y]} (an exit
    line counted beside the outline; out = a point on the side balls leave towards), "ball_area": one
    ball's pixel area at that size from the drop test (None = not calibrated), "measured": Measure's
    estimate of it (draws the ruler, checks the drop test; never counted with), "picture": the camera's
    settings (_picture), "colour": the range of colours that count as fuel (fuel_colour)}."""
    if x is None:
        return None
    if not isinstance(x, dict):
        raise ValueError("must be {source, outline, size, ball_area}")
    src = _str(300)(x.get("source"))
    if not src:
        raise ValueError("pick a camera")
    size = x.get("size") or [0, 0]
    if not (isinstance(size, (list, tuple)) and len(size) == 2):
        raise ValueError("size is [width, height]")
    w, h = (int(_num(0, 10000)(v)) for v in size)
    pts = [_point(p, "outline") for p in x.get("outline") or []]
    if pts and not 3 <= len(pts) <= 24:
        raise ValueError("an outline needs 3 to 24 points")
    if pts and not (w and h):
        raise ValueError("an outline needs the picture size it was drawn on")
    ex = x.get("exit") or None
    if ex is not None:
        if not isinstance(ex, dict):
            raise ValueError("an exit line is {line: [[x, y], [x, y]], out: [x, y]}")
        if not pts:
            raise ValueError("draw the outline before the exit line")
        line = [_point(p, "exit line") for p in ex.get("line") or []]
        if len(line) != 2 or line[0] == line[1]:
            raise ValueError("an exit line needs two different ends")
        out = _point(ex.get("out"), "exit line")
        (ax, ay), (bx, by), (ox, oy) = line[0], line[1], out
        if abs((bx - ax) * (oy - ay) - (by - ay) * (ox - ax)) < 1e-6:
            raise ValueError("click the exit line's out side off the line, where balls go once they're out")
        ex = {"line": line, "out": out}
    ball, meas = (None if v in (None, "") else round(float(_num(1, 1e6)(v)), 1)
                  for v in (x.get("ball_area"), x.get("measured")))
    return {"source": src, "size": [w, h], "outline": pts, "exit": ex, "ball_area": ball, "measured": meas,
            "picture": _picture(x.get("picture")), "colour": fuel_colour(x.get("colour"))}


# key -> validator. Dotted keys are paths into the event.yaml structure.
FIELDS = {
    "event.name": _name,
    "event.date": _date,
    "event.utc_offset_hours": _num(-12, 14),
    "event.tba_event_key": _event_key,
    "event.teams": _teams,
    "event.qual_start": _hhmm,
    "event.cycle_min": _num(1, 60),
    "event.lunch": _hhmm,
    "event.lunch_end": _hhmm,
    "event.quals_after_lunch": _bool,
    "event.day_end": _hhmm,
    "tba.enabled": _bool,
    "tba.auth_id": _str(200),
    "tba.auth_secret": _str(200),
    "tba.send_score_breakdown": _bool,
    "server.pins.control": _pin,
    "server.pins.ref": _pin,
    "server.pins.emcee": _pin,
    "server.pins.scorer": _pin_or_off,
    "field.feeds": _feeds,
    "field.control.enabled": _bool,
    "field.control.host": _host_or_blank,
    "field.control.port": _num(1, 65535),
    "field.control.key": _str(200),
    "vision.scale.red": _num(0.05, 100),
    "vision.scale.blue": _num(0.05, 100),
    "vision.hubs.red": _hubcam,
    "vision.hubs.blue": _hubcam,
}
SECRET = {"tba.auth_secret", "field.control.key"}   # never sent to a browser; Setup shows only whether it's set
# Stay on the laptop running the match: never sent to the website, and never taken from it. The website
# must have no way to point this FMS at a field, or to learn a field's key.
LOCAL = ("field.control.",)


def get_path(d, key):
    for k in key.split("."):
        d = d[k]
    return d


def set_path(d, key, value):
    *head, last = key.split(".")
    for k in head:
        d = d.setdefault(k, {})
    d[last] = value


def clean(changes: dict) -> dict:
    """Validate {key: value}; value None means 'back to event.yaml'. ValueError names the key."""
    out = {}
    for k, v in (changes or {}).items():
        if k not in FIELDS:
            raise ValueError(f"{k}: not a setting")
        if v is None:
            out[k] = None
            continue
        try:
            out[k] = FIELDS[k](v)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{k}: {e}") from None
    return out


def stamp(changes: dict, t: float) -> dict:
    """Cleaned changes -> stored entries."""
    return {k: ({"v": None, "t": t, "del": True} if v is None else {"v": v, "t": t}) for k, v in changes.items()}


def merge(a: dict, b: dict) -> dict:
    """Per key, the newer entry wins (ties: b). Unknown keys are dropped."""
    out = {k: e for k, e in (a or {}).items() if k in FIELDS}
    for k, e in (b or {}).items():
        if k in FIELDS and isinstance(e, dict) and "t" in e and (k not in out or e["t"] >= out[k]["t"]):
            out[k] = e
    return out


def shareable(entries: dict) -> dict:
    """The entries that may go to, or come from, the website (fms/cloud.py)."""
    return {k: e for k, e in (entries or {}).items() if not k.startswith(LOCAL)}


def overlay(base: dict, entries: dict) -> dict:
    """event.yaml config + live settings -> the config the server runs on (a new dict)."""
    cfg = copy.deepcopy(base)
    for k, e in (entries or {}).items():
        if k in FIELDS and not e.get("del"):
            set_path(cfg, k, copy.deepcopy(e["v"]))
    return cfg


def check(cfg: dict):
    """Whole-config rules (distinct PINs, lunch order, ...). ValueError with the reason."""
    try:
        config.validate(cfg, "Setup")
    except SystemExit as e:
        raise ValueError(str(e)) from None


def apply_in_place(cfg: dict, new: dict):
    """Make `cfg` equal `new` without replacing the dicts other modules hold (G = CFG["game"], tba.cfg)."""
    for k in list(cfg):
        if k not in new:
            del cfg[k]
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            apply_in_place(cfg[k], v)
        else:
            cfg[k] = v


def public(cfg: dict, entries: dict) -> dict:
    """What Setup shows: every value (secrets as set/not set) and where it comes from."""
    out = {}
    for k in FIELDS:
        v = get_path(cfg, k)
        src = "setup" if k in entries and not entries[k].get("del") else "event.yaml"
        out[k] = {"value": (bool(v) if k in SECRET else v), "source": src}
    return out
