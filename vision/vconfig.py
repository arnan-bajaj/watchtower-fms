"""Load a vision config. If it has no vision_key, use server.vision_key from the event.yaml in the
same folder, so the two can't drift apart."""
import pathlib

import yaml


def load(path):
    p = pathlib.Path(path)
    cfg = yaml.safe_load(p.read_text()) or {}
    ev = p.parent / "event.yaml"
    if not cfg.get("vision_key") and ev.exists():
        cfg["vision_key"] = ((yaml.safe_load(ev.read_text()) or {}).get("server") or {}).get("vision_key")
    if not cfg.get("vision_key"):
        raise SystemExit(f"No vision_key: set server.vision_key in {ev} (or vision_key in {p})")
    return cfg
