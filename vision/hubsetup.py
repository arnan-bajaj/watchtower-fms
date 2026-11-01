"""Setup -> Cameras: a hub's camera, outline and ball size from /control, as the config its counter runs on.
The ball size comes from the drop test (dropfit.py).

/control stores each hub as the live setting `vision.hubs.<hub>` = {source, size, outline, ball_area}
(fms/settings.py); vision fetches it with the rest of /api/vision/config. None keeps the vision config
file's hub. No cv2 here, so tests can import it.
"""
from __future__ import annotations

DRAWN = ("outline", "roi", "line", "setup")   # any of these gives the colour counter a zone
COLOUR = ("colour", "tbavid-colour")


def merge(base: dict, setup) -> dict:
    """The file's hub config (defaults + hubs.<hub>) with Setup's entry on top. Setup's outline
    replaces the file's zone keys; its picture size travels along as `setup_size` for `fit`."""
    cfg = dict(base)
    if not setup:
        return cfg
    cfg["source"] = str(setup["source"])
    for k in DRAWN:
        cfg.pop(k, None)
    cfg.pop("ball_area", None)
    if setup.get("outline"):
        cfg["outline"] = [list(p) for p in setup["outline"]]
        cfg["setup_size"] = list(setup.get("size") or [0, 0])
        if setup.get("ball_area"):
            cfg["ball_area"] = float(setup["ball_area"])
    return cfg


def fit(cfg: dict, w: int, h: int) -> dict:
    """Ready for a w x h picture: a Setup outline drawn at another size is scaled to this one, with
    its ball area. Nothing is guessed: a hub without a ball area waits for its drop test (dropfit.py)."""
    cfg = dict(cfg)
    size = cfg.pop("setup_size", None)
    if cfg.get("outline") and size and size[0] and size[1] and (size[0], size[1]) != (w, h):
        sx, sy = w / size[0], h / size[1]
        cfg["outline"] = [[x * sx, y * sy] for x, y in cfg["outline"]]
        if cfg.get("ball_area"):
            cfg["ball_area"] = cfg["ball_area"] * sx * sy
    return cfg


def waiting(cfg: dict):
    """Why this hub can't count yet (shown in /control), or None. Only the colour counter waits for an
    outline and a drop test; mock, zone etc. run as configured."""
    src = str(cfg.get("source", "")).strip()
    if not src:
        return "no camera: pick one in Setup → Cameras"
    if cfg.get("counter") in COLOUR:
        if not any(cfg.get(k) for k in DRAWN):
            return "draw this hub's outline in Setup → Cameras"
        if cfg.get("outline") and not cfg.get("ball_area"):
            return "calibrate this hub (drop test) in Setup → Cameras"
    return None
