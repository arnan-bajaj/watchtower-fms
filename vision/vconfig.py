"""Load a vision config. If it has no vision_key, use server.vision_key from the event.yaml in the
same folder, so the two can't drift apart. If it has no `feeds:` key at all, use the feeds from the
vision.yaml in the same folder, so a mock rehearsal (vision.mock.yaml) streams counts to the same
field system (e.g. bioarena) as the real cameras would. `feeds: []` turns that off."""
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
    real = p.parent / "vision.yaml"
    if "feeds" not in cfg and real.exists() and real.resolve() != p.resolve():
        try:
            feeds = (yaml.safe_load(real.read_text()) or {}).get("feeds")
        except yaml.YAMLError as e:
            print(f"feeds: not taken from {real} (unreadable: {e})")
            feeds = None
        if feeds:
            cfg["feeds"] = feeds
            print(f"feeds: using the ones in {real} (set feeds: [] in {p.name} to turn them off)")
    return cfg
