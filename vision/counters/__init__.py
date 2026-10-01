import importlib

BUILTIN = {
    "zone": "counters.zone:ZoneCounter",
    "linecross": "counters.linecross:LineCrossCounter",
    "mock": "counters.mock:MockCounter",
}


def load_counter(cfg: dict):
    spec = BUILTIN.get(cfg["counter"], cfg["counter"])
    mod, cls = spec.split(":")
    return getattr(importlib.import_module(mod), cls)(cfg)
