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
        "game": "rebuilt",        # which game the event plays (GAME_RULES); fixed when the event is made
    },
    "display": {
        "red_side": "right",      # which side of every screen red is drawn on; /control -> Setup overrides it live
    },
    "server": {
        "host": "0.0.0.0",
        "port": 8000,
        "db": "data/fms.sqlite3",
        "pins": {"control": PLACEHOLDER, "ref": PLACEHOLDER, "emcee": PLACEHOLDER,
                 "scorer": ""},               # REEFSCAPE live scorers (/score); blank = nobody can log in as scorer
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
    # Field systems that take live hub counts (bioarena). None: vision uses its own config file's
    # feeds:; a list (set in /control -> Setup) wins over that. vision/run_vision.py fetches it.
    # control: /control runs a bioarena field over the network (fms/field.py); Setup -> Field control.
    "field": {"feeds": None, "control": {"enabled": False, "host": "", "port": 8080, "key": ""}},
    # Counts per real ball, per hub (calibration in /control -> Setup). Vision divides by it.
    # hubs: Setup -> Cameras (camera, outline, ball size per hub); None = vision's config file.
    "vision": {"scale": {"red": 1, "blue": 1}, "hubs": {"red": None, "blue": None}},
    # Match13 API key (match13.com -> your account page) for match predictions in /control (fms/match13.py).
    # Blank = no predictions. Setup -> Match13 sets it; it stays on this laptop.
    "match13": {"key": ""},
    # Set by the website's launch command (fms/cloud.py): settings backup and status. Empty = local only.
    "cloud": {"url": "", "event": "", "token": ""},
}


# Every game's rule numbers. event.yaml -> game: overrides any of them. The scoring for each is a pure
# module: fms/game.py (REBUILT, 2026) and fms/reefscape.py (REEFSCAPE, 2025).
GAME_RULES = {
    "rebuilt": {
        "auto_s": 20, "auto_teleop_gap_s": 3, "transition_s": 10, "shift_s": 25,
        "n_shifts": 4, "endgame_s": 30, "score_grace_s": 3,
        "close_auto_margin": 5,
        "fuel_points": 1, "auto_tower_l1": 15,
        "tower": {"L0": 0, "L1": 10, "L2": 20, "L3": 30},
        "fouls": {"minor": 5, "major": 15},
        "rp": {"win": 3, "tie": 1, "energized_fuel": 100, "supercharged_fuel": 360,
               "traversal_tower_points": 50},
    },
    # 2025 Game Manual section 6.4 and the regional RP thresholds (district championships and
    # Championship raised coral_per_level to 7 and barge_points to 16).
    "reefscape": {
        "auto_s": 15, "auto_teleop_gap_s": 3, "teleop_s": 135, "endgame_s": 20, "score_grace_s": 0,
        "leave": 3,
        "coral_auto": {"L1": 3, "L2": 4, "L3": 6, "L4": 7},
        "coral_teleop": {"L1": 2, "L2": 3, "L3": 4, "L4": 5},
        "algae": {"processor": 6, "net": 4},
        "barge": {"none": 0, "park": 2, "shallow": 6, "deep": 12},
        "fouls": {"minor": 2, "major": 6},
        "rp": {"win": 3, "tie": 1, "auto_coral": 1, "coral_per_level": 5, "coral_levels": 4,
               "coral_levels_coop": 3, "coop_processor": 2, "barge_points": 14},
    },
}
DEFAULTS["game"] = GAME_RULES["rebuilt"]     # load() swaps in event.game's rules


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
    raw = yaml.safe_load(p.read_text()) or {}
    cfg = _merge(DEFAULTS, raw)
    gid = str(cfg["event"]["game"] or "rebuilt").strip().lower()
    if gid not in GAME_RULES:
        raise SystemExit(f"event.game in {path} must be one of {', '.join(GAME_RULES)} (got {cfg['event']['game']!r}).")
    cfg["event"]["game"] = gid
    stray = sorted(set(raw.get("game") or {}) - set(GAME_RULES[gid]))
    if stray:   # e.g. REBUILT's game: section left in a REEFSCAPE event.yaml: its fouls would win silently
        raise SystemExit(f"game: in {path} has {', '.join(stray)}, which aren't {gid.upper()} rules (event.game). "
                         f"Remove them, or the whole game: section to use the defaults in fms/config.py.")
    cfg["game"] = _merge(GAME_RULES[gid], raw.get("game") or {})
    # secrets from env win over the file
    cfg["tba"]["auth_id"] = os.environ.get("TBA_AUTH_ID", cfg["tba"]["auth_id"])
    cfg["tba"]["auth_secret"] = os.environ.get("TBA_AUTH_SECRET", cfg["tba"]["auth_secret"])
    cfg["event"]["teams"] = [int(t) for t in cfg["event"]["teams"] or []]
    validate(cfg, path)
    return cfg


RED_SIDES = ("left", "right")
OPTIONAL_PINS = ("scorer",)    # older event.yaml files have no scorer PIN; blank = scorer logins off


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
    bad = [r for r, v in pins.items() if v == PLACEHOLDER or (not v and r not in OPTIONAL_PINS)]
    if bad:
        raise SystemExit(f"Set server.pins ({', '.join(bad)}) in {path}, or run `python -m fms.init` on a fresh copy.")
    if pins["control"] in [v for r, v in pins.items() if r != "control"]:
        raise SystemExit(f"The control PIN in {path} must differ from the ref, emcee and scorer PINs.")
    if str(cfg["server"]["vision_key"]).strip() in ("", PLACEHOLDER):
        raise SystemExit(f"Set server.vision_key in {path} to any random string.")
