import copy
import os
import pathlib

import yaml

DEFAULTS = {
    "event": {
        "name": "FRC Scrimmage",
        "date": "",               # YYYY-MM-DD, used for TBA time_utc
        "utc_offset_hours": -7,   # PDT in October
        "tba_event_key": "",
        "teams": [],
        "qual_start": "09:30",
        "cycle_min": 8,
        "lunch": "12:00",
    },
    "server": {
        "host": "0.0.0.0",
        "port": 8000,
        "db": "data/fms.sqlite3",
        "pins": {"control": "0000", "ref": "1111", "emcee": "2222"},
        "vision_key": "change-me",
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
    data = {}
    p = pathlib.Path(path)
    if p.exists():
        data = yaml.safe_load(p.read_text()) or {}
    cfg = _merge(DEFAULTS, data)
    # secrets from env win over the file
    cfg["tba"]["auth_id"] = os.environ.get("TBA_AUTH_ID", cfg["tba"]["auth_id"])
    cfg["tba"]["auth_secret"] = os.environ.get("TBA_AUTH_SECRET", cfg["tba"]["auth_secret"])
    cfg["event"]["teams"] = [int(t) for t in cfg["event"]["teams"]]
    return cfg
