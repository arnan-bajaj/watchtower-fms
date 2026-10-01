"""First-run setup:  python -m fms.init

Creates config/event.yaml and config/vision.yaml from the committed examples, with random PINs and a
random vision key. Never overwrites an existing file."""
from __future__ import annotations

import pathlib
import secrets

from .config import PLACEHOLDER

ROLES = ("control", "ref", "emcee")


def init(config_dir="config"):
    d = pathlib.Path(config_dir)
    out = {"created": [], "kept": [], "pins": None}
    ev = d / "event.yaml"
    if ev.exists():
        out["kept"].append(str(ev))
    else:
        text = (d / "event.example.yaml").read_text()
        pins = {}
        for role in ROLES:
            while True:  # distinct, so a ref can't log in as the scorekeeper
                pin = f"{secrets.randbelow(10**6):06d}"
                if pin not in pins.values():
                    break
            pins[role] = pin
            text = _fill(text, f'    {role}: "{PLACEHOLDER}"', f'    {role}: "{pin}"')
        text = _fill(text, f'  vision_key: "{PLACEHOLDER}"', f'  vision_key: "{secrets.token_urlsafe(18)}"')
        ev.write_text(text)
        out["created"].append(str(ev))
        out["pins"] = pins
    vis = d / "vision.yaml"
    if vis.exists():
        out["kept"].append(str(vis))
    else:
        vis.write_text((d / "vision.example.yaml").read_text())
        out["created"].append(str(vis))
    return out


def _fill(text, line, new):
    if text.count(line) != 1:
        raise SystemExit(f"event.example.yaml is missing the line {line.strip()!r}")
    return text.replace(line, new)


if __name__ == "__main__":
    r = init()
    for f in r["created"]:
        print(f"created {f}")
    for f in r["kept"]:
        print(f"kept    {f} (already exists)")
    if r["pins"]:
        print("\nPINs (also in config/event.yaml; change them there any time):")
        for role, pin in r["pins"].items():
            print(f"  {role:8} {pin}")
        print("\nNext: set event name, date and teams in config/event.yaml, then ./run.sh ../config/vision.mock.yaml")
