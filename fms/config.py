import copy
import os
import pathlib
from datetime import datetime

import yaml

PLACEHOLDER = "CHANGE-ME"  # value in config/event.example.yaml; fms.init replaces it

DEFAULTS = {
    "event": {
        "name": "FRC Scrimmage",
        "date": "",               # YYYY-MM-DD, used for TBA time_utc
        "utc_offset_hours": -7,   # PDT in October
        "tba_event_key": "",
        "teams": [],
        "qual_start": "09:30",
        "cycle_min": 8,
        "lunch": "12:00",             # lunch starts; quals must end by then unless quals_after_lunch
        "lunch_end": "13:00",
        "quals_after_lunch": False,   # True: schedule times skip lunch and quals continue until day_end
        "day_end": "17:00",
    },
    "display": {
        "red_side": "right",      # which side of every screen red is drawn on; /control -> Setup overrides it live
    },
    "server": {
        "host": "0.0.0.0",
        "port": 8000,
        "db": "data/fms.sqlite3",
        "pins": {"control": PLACEHOLDER, "ref": PLACEHOLDER, "emcee": PLACEHOLDER},
        "vision_key": PLACEHOLDER,
    },
    "tba": {
        "enabled": False,
        "base_url": "https://www.thebluealliance.com",
        "auth_id": "",
        "auth_secret": "",
        "send_score_breakdown": False,
        "retry_s": 15,
    },
    "game": {
        "auto_s": 20, "auto_teleop_gap_s": 3, "transition_s": 10, "shift_s": 25,
        "n_shifts": 4, "endgame_s": 30, "score_grace_s": 3,
        "close_auto_margin": 5,
        "fuel_points": 1, "auto_tower_l1": 15,
        "tower": {"L0": 0, "L1": 10, "L2": 20, "L3": 30},
        "fouls": {"minor": 5, "major": 15},
        "rp": {"win": 3, "tie": 1, "energized_fuel": 100, "supercharged_fuel": 360,
               "traversal_tower_points": 50},
    },
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: str | None = None) -> dict:
    path = path or os.environ.get("FMS_CONFIG", "config/event.yaml")
    p = pathlib.Path(path)
    if not p.exists():
        raise SystemExit(f"{path} not found. Run `python -m fms.init` to create it from config/event.example.yaml.")
    cfg = _merge(DEFAULTS, yaml.safe_load(p.read_text()) or {})
    # secrets from env win over the file
    cfg["tba"]["auth_id"] = os.environ.get("TBA_AUTH_ID", cfg["tba"]["auth_id"])
    cfg["tba"]["auth_secret"] = os.environ.get("TBA_AUTH_SECRET", cfg["tba"]["auth_secret"])
    cfg["event"]["teams"] = [int(t) for t in cfg["event"]["teams"] or []]
    validate(cfg, path)
    return cfg


RED_SIDES = ("left", "right")


def validate(cfg, path="config/event.yaml"):
    """Refuse to serve with placeholder or shared PINs: anyone on the venue WiFi can open the pages."""
    ev = cfg["event"]
    for k in ("qual_start", "lunch", "lunch_end", "day_end"):
        try:
            datetime.strptime(str(ev[k]).strip(), "%H:%M")
        except ValueError:
            raise SystemExit(f"event.{k} in {path} must be a time like \"13:00\" (got {ev[k]!r}).")
    if datetime.strptime(str(ev["lunch_end"]).strip(), "%H:%M") <= datetime.strptime(str(ev["lunch"]).strip(), "%H:%M"):
        raise SystemExit(f"event.lunch_end in {path} must be after event.lunch.")
    if cfg["display"]["red_side"] not in RED_SIDES:
        raise SystemExit(f"display.red_side in {path} must be left or right.")
    pins = {r: str(v).strip() for r, v in cfg["server"]["pins"].items()}
    bad = [r for r, v in pins.items() if not v or v == PLACEHOLDER]
    if bad:
        raise SystemExit(f"Set server.pins ({', '.join(bad)}) in {path}, or run `python -m fms.init` on a fresh copy.")
    if pins["control"] in (pins.get("ref"), pins.get("emcee")):
        raise SystemExit(f"The control PIN in {path} must differ from the ref and emcee PINs.")
    if str(cfg["server"]["vision_key"]).strip() in ("", PLACEHOLDER):
        raise SystemExit(f"Set server.vision_key in {path} to any random string.")
