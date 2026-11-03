"""Setup -> Cameras: a hub's camera, outline, exit line and ball size from /control, as the config its
counter runs on. The ball size comes from the drop test (dropfit.py); Measure's estimate (`measured`)
only draws the ruler and cross-checks it, so it never reaches the counter.

/control stores each hub as the live setting `vision.hubs.<hub>` = {source, size, outline, exit,
ball_area, measured, picture, colour} (fms/settings.py); `exit` is None or {"line": [[x, y], [x, y]],
"out": [x, y]}; `picture` is None or the camera's {fps, size, fourcc, auto_exposure, exposure_ms,
auto_wb, wb_kelvin, brightness, contrast, saturation, gain} (camprofile's `image`, applied when the
camera opens; null values = the camera's own); `colour` is None (standard) or the fuel colour range.
Vision fetches it with the rest of /api/vision/config. None keeps the vision config file's hub.
No cv2 here, so tests can import it.
"""
from __future__ import annotations

DRAWN = ("outline", "roi", "line", "setup")   # any of these gives the colour counter a zone
IMAGE = ("fourcc", "auto_exposure", "exposure_ms", "auto_wb", "wb_kelvin",   # camprofile.IMAGE_KEYS
         "brightness", "contrast", "saturation", "gain")
SCALED = ("outline", "line")                   # point lists drawn at `setup_size`
COLOUR = ("colour", "tbavid-colour")


def merge(base: dict, setup) -> dict:
    """The file's hub config (defaults + hubs.<hub>) with Setup's entry on top. Setup's outline
    replaces the file's zone keys; its picture size travels along as `setup_size` for `fit`."""
    cfg = dict(base)
    if not setup:
        return cfg
    cfg["source"] = str(setup["source"])
    for k in (*DRAWN, "out", "ball_area"):
        cfg.pop(k, None)
    pic = setup.get("picture") or {}
    if pic.get("fps"):
        cfg["fps"] = pic["fps"]
    if pic.get("size"):
        cfg["width"], cfg["height"] = pic["size"]
    image = {k: pic[k] for k in IMAGE if pic.get(k) is not None}
    if image:
        cfg["image"] = image
    if setup.get("colour"):
        cfg["colour"] = dict(setup["colour"])
    if setup.get("outline"):
        cfg["outline"] = [list(p) for p in setup["outline"]]
        cfg["setup_size"] = list(setup.get("size") or [0, 0])
        if setup.get("ball_area"):
            cfg["ball_area"] = float(setup["ball_area"])
        ex = setup.get("exit")
        if ex and ex.get("line") and ex.get("out"):        # counted alongside the outline (counters/colour.py)
            cfg["line"] = [list(p) for p in ex["line"]]
            cfg["out"] = list(ex["out"])
    return cfg


def fit(cfg: dict, w: int, h: int) -> dict:
    """Ready for a w x h picture: a Setup outline (and exit line) drawn at another size is scaled to this
    one, with its ball area. Nothing is guessed: a hub without a ball area waits for its drop test
    (dropfit.py)."""
    cfg = dict(cfg)
    size = cfg.pop("setup_size", None)
    if cfg.get("outline") and size and size[0] and size[1] and (size[0], size[1]) != (w, h):
        sx, sy = w / size[0], h / size[1]
        for k in SCALED:
            if cfg.get(k):
                cfg[k] = [[x * sx, y * sy] for x, y in cfg[k]]
        if cfg.get("out"):
            cfg["out"] = [cfg["out"][0] * sx, cfg["out"][1] * sy]
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


def camera_key(cfg: dict):
    """What the camera is opened with: a change reopens it (the rest only rebuilds the counter)."""
    return (str(cfg.get("source", "")).strip(), cfg.get("width"), cfg.get("height"), cfg.get("fps"),
            tuple(sorted((cfg.get("image") or {}).items())))
