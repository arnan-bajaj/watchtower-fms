"""Camera presets: capture format, picture settings and the fuel colour gate.

Before this, a camera ran on whatever its driver chose. A UVC webcam's own
choices hurt counting: auto exposure stretches the shutter in a dim gym
and the frame rate falls (webcams drop to 15-24 fps; the Einstein replays
measured 10.5% error at 60 fps, 12.6% at 30 and 25.1% at 20), and
uncompressed 1280x720 is 1.8 MB a frame, which USB 2 (~40 MB/s) carries at
about 20 fps at best, so 60 fps needs MJPG. Auto white balance also moves the hue
of the yellow fuel as the field's red and blue lights change, and the
colour gate is a fixed hue range.

A camera in cams.json may now carry

    "preset": "elp-ov4689",                       which one it started from
    "image":  {"fourcc": "MJPG", "auto_exposure": false, "exposure_ms": 8,
               "auto_wb": false, "wb_kelvin": 4600,
               "brightness": null, "contrast": null, "saturation": null,
               "gain": null}                         null: the driver's own
    "colour": {"hue_lo": 15, "hue_hi": 40, "sat_min": 55, "val_min": 45}

alongside its existing "fps" and "size". A preset is those four things
under a name: two are built in, and the page saves more to
camera-presets.json next to the setup file.

Only a local camera (a number) takes `image`; a recording, a stream or a
Wi-Fi camera keeps its own picture. Drivers differ in what they accept
(macOS's AVFoundation takes almost none of these; V4L2 and Windows take
most), so every setting is read back after it is set and the ones a camera
ignored are reported rather than assumed.

Standard library only at import: OpenCV is imported inside apply_image.
"""
from __future__ import annotations

import json
import math
import os
import re
from typing import Callable, Dict, List, Optional, Tuple

# The colour gate the counter has always used (hubcount.LOOSE_LO / LOOSE_HI,
# OpenCV HSV: hue 0-179, saturation and value 0-255).
STANDARD_COLOUR = {"hue_lo": 15, "hue_hi": 40, "sat_min": 55, "val_min": 45}

IMAGE_KEYS = ("fourcc", "auto_exposure", "exposure_ms", "auto_wb", "wb_kelvin",
              "brightness", "contrast", "saturation", "gain")
COLOUR_KEYS = tuple(STANDARD_COLOUR)

BUILTIN: List[Dict] = [
    {
        "id": "elp-ov4689",
        "label": "ELP OV4689 2.8-12 mm varifocal, 60 fps",
        # 1280x720 rather than 1920x1080: both run at 60 fps on this sensor,
        # and a 1080p MJPG frame costs about twice the decode time, on a
        # budget of 16.7 ms a frame. Zoom the varifocal lens in on the hub
        # instead of adding pixels.
        "fps": 60, "size": "1280x720",
        "image": {"fourcc": "MJPG",
                  # Manual: under auto the shutter lengthens in a dim gym and
                  # 60 fps is lost. 8 ms is half a frame, and a ball at
                  # ~10 m/s moves about half its width in it; raise it (to
                  # at most 16) if the picture is too dark.
                  "auto_exposure": False, "exposure_ms": 8,
                  # Fixed: auto white balance shifts the fuel's hue as the
                  # field lights change. 4600 K is a mid gym LED; nudge it
                  # until the fuel overlay covers the balls.
                  "auto_wb": False, "wb_kelvin": 4600,
                  "brightness": None, "contrast": None, "saturation": None,
                  "gain": None},
        "colour": dict(STANDARD_COLOUR),
        "note": "Not yet checked on the camera itself: look at the picture "
                "and the fuel overlay, adjust, then Save as preset.",
    },
    {
        "id": "usb-webcam",
        "label": "USB webcam (standard)",
        "fps": 30, "size": "1280x720",
        "image": {"fourcc": "MJPG", "auto_exposure": True, "exposure_ms": None,
                  "auto_wb": True, "wb_kelvin": None,
                  "brightness": None, "contrast": None, "saturation": None,
                  "gain": None},
        "colour": dict(STANDARD_COLOUR),
        "note": "The camera's own automatic picture. Adjust the colours if the "
                "fuel overlay misses balls, then Save as preset.",
    },
]

# Ranges the page's controls use and check_* enforces.
EXPOSURE_MS = (0.1, 100.0)
WB_KELVIN = (2000, 8000)


def slug(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return s[:40] or "preset"


def check_image(raw: Optional[Dict]) -> Dict:
    """A camera's `image`, validated; unknown keys dropped, None = driver's."""
    out: Dict = {}
    for k, v in (raw or {}).items():
        if k not in IMAGE_KEYS:
            continue
        if v in (None, ""):
            out[k] = None
        elif k == "fourcc":
            v = str(v).strip().upper()
            if v and len(v) != 4:
                raise ValueError(f"a video format is four letters (MJPG, YUYV), not {v!r}")
            out[k] = v or None
        elif k in ("auto_exposure", "auto_wb"):
            out[k] = bool(v)
        else:
            try:
                f = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be a number")
            if k == "exposure_ms" and not EXPOSURE_MS[0] <= f <= EXPOSURE_MS[1]:
                raise ValueError(f"exposure is {EXPOSURE_MS[0]} to {EXPOSURE_MS[1]:.0f} ms")
            if k == "wb_kelvin" and not WB_KELVIN[0] <= f <= WB_KELVIN[1]:
                raise ValueError(f"white balance is {WB_KELVIN[0]} to {WB_KELVIN[1]} K")
            out[k] = round(f, 2)
    return out


def check_colour(raw: Optional[Dict]) -> Dict:
    """A camera's `colour` gate, validated, missing keys from the standard."""
    c = dict(STANDARD_COLOUR)
    for k in COLOUR_KEYS:
        if raw and raw.get(k) not in (None, ""):
            try:
                c[k] = int(round(float(raw[k])))
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be a number")
    if not 0 <= c["hue_lo"] < c["hue_hi"] <= 179:
        raise ValueError("the hue range runs 0 to 179, low end below the high end")
    for k in ("sat_min", "val_min"):
        if not 0 <= c[k] <= 255:
            raise ValueError(f"{k} is 0 to 255")
    return c


def gate(colour: Optional[Dict]) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
    """The OpenCV inRange bounds for a `colour` dict (or the standard)."""
    c = check_colour(colour)
    return (c["hue_lo"], c["sat_min"], c["val_min"]), (c["hue_hi"], 255, 255)


# -- presets ---------------------------------------------------------------------

def load_saved(path: Optional[str]) -> List[Dict]:
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    for p in data.get("presets") or []:
        try:
            out.append(_preset(p))
        except (ValueError, KeyError, TypeError):
            continue                       # a hand-broken entry is skipped, not fatal
    return out


def _preset(p: Dict) -> Dict:
    return {"id": str(p["id"]), "label": str(p.get("label") or p["id"]),
            "fps": float(p.get("fps") or 0), "size": str(p.get("size") or ""),
            "image": check_image(p.get("image")), "colour": check_colour(p.get("colour")),
            "note": str(p.get("note") or "")}


def all_presets(path: Optional[str]) -> List[Dict]:
    return [dict(p, builtin=True) for p in BUILTIN] + \
           [dict(p, builtin=False) for p in load_saved(path)]


def find(presets: List[Dict], pid: str) -> Dict:
    for p in presets:
        if p["id"] == pid:
            return p
    raise KeyError(f"no preset {pid!r}")


def save_preset(path: str, label: str, cam: Dict) -> Dict:
    """Store a camera's fps, size, image and colour under `label`; a saved
    preset with the same name is replaced, a built-in one never is."""
    label = str(label).strip()
    if not label:
        raise ValueError("give the preset a name")
    pid = slug(label)
    if any(p["id"] == pid for p in BUILTIN):
        pid = "my-" + pid
    new = _preset({"id": pid, "label": label, "fps": cam.get("fps") or 0,
                   "size": cam.get("size") or "", "image": cam.get("image") or {},
                   "colour": cam.get("colour") or {}})
    saved = [p for p in load_saved(path) if p["id"] != pid] + [new]
    _write(path, saved)
    return new


def delete_preset(path: str, pid: str) -> None:
    if any(p["id"] == pid for p in BUILTIN):
        raise ValueError("built-in presets cannot be deleted")
    _write(path, [p for p in load_saved(path) if p["id"] != pid])


def _write(path: str, presets: List[Dict]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "presets": presets}, f, indent=2)
    os.replace(tmp, path)


# -- applying to a camera ----------------------------------------------------------

def exposure_value(ms: float, backend: str) -> float:
    """OpenCV's CAP_PROP_EXPOSURE is in different units per backend: V4L2
    takes exposure_absolute in 100 us steps; DirectShow and Media Foundation
    take log2 of seconds (-7 = 1/128 s)."""
    if backend == "V4L2":
        return round(ms * 10)
    return round(math.log2(ms / 1000.0))


def auto_exposure_value(on: bool, backend: str) -> float:
    """V4L2 menu values (1 manual, 3 aperture priority); elsewhere OpenCV's
    0.25 / 0.75 convention."""
    if backend == "V4L2":
        return 3 if on else 1
    return 0.75 if on else 0.25


def apply_image(cap, image: Optional[Dict], out: Callable[[str], None] = print,
                name: str = "camera") -> Dict[str, str]:
    """Set what can be set before frames are read. Returns {setting: "ok" |
    "ignored"} and reports the ignored ones: a driver that drops a setting
    says nothing, and a silently auto-exposed camera is the 20 fps failure
    this exists to prevent."""
    import cv2

    image = check_image(image)
    try:
        backend = cap.getBackendName()
    except Exception:
        backend = ""
    plan: List[Tuple[str, int, float]] = []
    if image.get("auto_exposure") is not None:
        plan.append(("auto exposure", cv2.CAP_PROP_AUTO_EXPOSURE,
                     auto_exposure_value(image["auto_exposure"], backend)))
    if image.get("exposure_ms") is not None and image.get("auto_exposure") is not True:
        plan.append(("exposure", cv2.CAP_PROP_EXPOSURE,
                     exposure_value(image["exposure_ms"], backend)))
    if image.get("auto_wb") is not None:
        plan.append(("auto white balance", cv2.CAP_PROP_AUTO_WB,
                     1 if image["auto_wb"] else 0))
    if image.get("wb_kelvin") is not None and image.get("auto_wb") is not True:
        plan.append(("white balance", cv2.CAP_PROP_WB_TEMPERATURE, image["wb_kelvin"]))
    for k, prop in (("brightness", cv2.CAP_PROP_BRIGHTNESS),
                    ("contrast", cv2.CAP_PROP_CONTRAST),
                    ("saturation", cv2.CAP_PROP_SATURATION),
                    ("gain", cv2.CAP_PROP_GAIN)):
        if image.get(k) is not None:
            plan.append((k, prop, image[k]))
    result: Dict[str, str] = {}
    for label, prop, value in plan:
        try:
            ok = cap.set(prop, float(value))
        except Exception:
            ok = False
        result[label] = "ok" if ok else "ignored"
    ignored = [k for k, v in result.items() if v == "ignored"]
    if ignored:
        out(f"{name}: this camera driver ({backend or 'unknown'}) ignored "
            f"{', '.join(ignored)}; it keeps its own")
    return result


def set_fourcc(cap, fourcc: Optional[str]) -> None:
    """Before size and fps: a UVC camera lists its 60 fps modes under MJPG only."""
    if fourcc:
        import cv2
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc[:4]))
