"""Counter plugins: how vision finds, checks and loads a hub counter.

A counter is any class with `__init__(cfg)` and `process(frame, t) -> int`
(new fuel in that frame). That is still the whole required contract. See
`base.Counter` for the optional parts a plugin can add.

Where `counter:` in config/vision.yaml is looked up, in this order:

1. Built-in short names: `zone`, `linecross`, `mock`.
2. Installed plugins: any Python package that registers classes under the
   entry-point group `watchtower.counters` (e.g. in its pyproject.toml:
   `[project.entry-points."watchtower.counters"] my-counter = "pkg.mod:Class"`).
   `pip install` the package into this venv and `counter: my-counter` works.
   A folder in `plugin_paths:` that has such a pyproject.toml gets the same
   short names without being installed.
3. `plugins:` in config/vision.yaml: your own short names,
   `plugins: {mine: "my_module:MyCounter"}`.
4. A full `"module:Class"`, from anywhere on the path. `plugin_paths:` in
   config/vision.yaml (folders, relative to that file) adds to the path, so a
   checkout of another repo can be used without installing it or setting
   PYTHONPATH.

5. The `vision/plugins/` folder, the easy way. Drop in a `my_counter.py` whose
   class has a `NAME`, and `counter: <that NAME>` works with nothing else to
   edit. Or add a whole repo with
   `python run_vision.py --add-plugin ~/dev/OtherRepo`. That writes
   `plugins/OtherRepo.path`, a one-line file holding the folder, which works
   the same on macOS, Windows and Linux (no symlinks).

`python run_vision.py --list-counters` prints what is available.

Nothing here imports cv2 or a model: the registry is tested, and the list
printed, on machines without the vision stack.
"""
from __future__ import annotations

import difflib
import importlib
import os
import pathlib
import sys

PLUGIN_API = 1                       # bump on a breaking change to the contract
ENTRY_POINT_GROUP = "watchtower.counters"

BUILTIN = {
    "zone": "counters.zone:ZoneCounter",
    "linecross": "counters.linecross:LineCrossCounter",
    "mock": "counters.mock:MockCounter",
}

# Keys the runner itself reads from a hub's config; never "unknown" to a plugin.
RUNNER_KEYS = {"counter", "hub", "source", "enabled", "fps", "width", "height", "loop",
               "roi", "weights", "plugins", "plugin_paths"}


_PATH_PLUGINS: dict = {}           # short names read from plugin_paths' pyproject.toml
PLUGINS_DIR = pathlib.Path(__file__).resolve().parent.parent / "plugins"


def _named_classes(pyfile: pathlib.Path) -> dict:
    """{NAME: class name} for the counter classes in a dropped-in file (ones
    with a NAME or a process() method), read from its source without importing
    it, so a broken or slow plugin file cannot stop vision from starting or
    break --list-counters for the others. A class without a NAME is listed
    under its class name in lower case."""
    import ast
    try:
        tree = ast.parse(pyfile.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return {}
    out = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        methods = {b.name for b in node.body if isinstance(b, ast.FunctionDef)}
        name = None
        for b in node.body:
            if (isinstance(b, ast.Assign) and any(getattr(t, "id", "") == "NAME" for t in b.targets)
                    and isinstance(b.value, ast.Constant) and isinstance(b.value.value, str)):
                name = b.value.value
        if name or "process" in methods:      # a NAME alone: a subclass tweaking a built-in
            out[name or node.name.lower()] = node.name
    return out


def scan_plugins_dir(folder: pathlib.Path = None) -> dict:
    """{name: (spec, source)} from the drop-in folder: *.py files, *.path files
    (a folder to add, one line), and sub-folders such as a cloned repo. Files
    and folders starting with '_' or '.' are skipped (the _example.py template)."""
    folder = pathlib.Path(folder or PLUGINS_DIR)
    if not folder.is_dir():
        return {}
    found = {}
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
    for f in sorted(folder.iterdir()):
        if f.name.startswith(("_", ".")):
            continue
        if f.suffix == ".py":
            for name, cls in _named_classes(f).items():
                found[name] = (f"{f.stem}:{cls}", f"plugins/{f.name}")
        elif f.suffix == ".path" or f.is_dir():
            target = f.read_text(encoding="utf-8").strip() if f.suffix == ".path" else str(f)
            try:
                add_plugin_paths([target], base=folder)
            except SystemExit:
                found[f"{f.stem} (missing)"] = (f"{f.stem}:missing", f"plugins/{f.name} -> {target} not found")
                continue
            q = pathlib.Path(os.path.expanduser(target))
            q = (q if q.is_absolute() else folder / q).resolve()
            for name, spec in _pyproject_plugins(q).items():
                found[name] = (spec, f"plugins/{f.name}")
            for py in sorted(q.glob("*.py")) if f.is_dir() else []:
                if not py.name.startswith(("_", ".")):
                    for name, cls in _named_classes(py).items():
                        found.setdefault(name, (f"{py.stem}:{cls}", f"plugins/{f.name}/{py.name}"))
    return found


def add_plugin(path: str, folder: pathlib.Path = None) -> list:
    """--add-plugin: remember a folder (a repo with counters) in the drop-in
    folder as <name>.path. Returns the counter names it provides."""
    folder = pathlib.Path(folder or PLUGINS_DIR)
    q = pathlib.Path(os.path.expanduser(path)).resolve()
    if not q.is_dir():
        raise SystemExit(f"--add-plugin: {path} is not a folder")
    names = list(_pyproject_plugins(q)) + [n for py in q.glob("*.py")
                                          if not py.name.startswith(("_", "."))
                                          for n in _named_classes(py)]
    if not names:
        raise SystemExit(f"--add-plugin: no counters in {q} (no 'watchtower.counters' in its "
                         f"pyproject.toml, and no .py with a counter class at its top)")
    folder.mkdir(exist_ok=True)
    (folder / f"{q.name}.path").write_text(str(q) + "\n", encoding="utf-8")
    return names


def remove_plugin(name: str, folder: pathlib.Path = None) -> bool:
    f = pathlib.Path(folder or PLUGINS_DIR) / f"{name}.path"
    if f.is_file():
        f.unlink()
        return True
    return False


def _pyproject_plugins(folder: pathlib.Path) -> dict:
    """{name: spec} from a folder's pyproject.toml `watchtower.counters`."""
    f = folder / "pyproject.toml"
    if not f.is_file():
        return {}
    try:
        import tomllib
    except ImportError:                                  # Python < 3.11
        return {}
    try:
        data = tomllib.loads(f.read_text())
    except (OSError, ValueError):
        return {}
    eps = (data.get("project", {}).get("entry-points", {}) or {}).get(ENTRY_POINT_GROUP, {})
    return {str(k): str(v) for k, v in eps.items()}


def add_plugin_paths(paths, base=None) -> list:
    """Put `plugin_paths:` folders on the import path; relative ones are taken
    from `base` (the config file's folder). Returns the folders added."""
    added = []
    for p in paths or []:
        q = pathlib.Path(os.path.expanduser(str(p)))
        if not q.is_absolute() and base is not None:
            q = pathlib.Path(base) / q
        q = q.resolve()
        if not q.is_dir():
            raise SystemExit(f"plugin_paths: {p} is not a folder")
        if str(q) not in sys.path:
            sys.path.insert(0, str(q))
        for name, spec in _pyproject_plugins(q).items():
            _PATH_PLUGINS[name] = (spec, f"plugin_paths ({q.name})")
        added.append(str(q))
    return added


def _entry_points() -> dict:
    """{name: (spec, distribution)} for installed plugins."""
    from importlib import metadata
    try:
        eps = metadata.entry_points(group=ENTRY_POINT_GROUP)
    except TypeError:                                    # Python < 3.10
        eps = metadata.entry_points().get(ENTRY_POINT_GROUP, [])
    out = {}
    for ep in eps:
        dist = getattr(getattr(ep, "dist", None), "name", "") or "installed"
        out[ep.name] = (ep.value, dist)
    return out


def available(plugins=None) -> dict:
    """{short name: {"spec": "module:Class", "source": where it came from}}."""
    out = {n: {"spec": s, "source": "built-in"} for n, s in BUILTIN.items()}
    for n, (s, dist) in _entry_points().items():
        out.setdefault(n, {"spec": s, "source": f"installed ({dist})"})
    for n, (s, where) in _PATH_PLUGINS.items():
        out.setdefault(n, {"spec": s, "source": where})
    for n, (s, where) in scan_plugins_dir().items():
        out.setdefault(n, {"spec": s, "source": where})
    for n, s in (plugins or {}).items():
        out[n] = {"spec": str(s), "source": "config plugins:"}
    return out


def resolve(name: str, plugins=None) -> str:
    """The "module:Class" for a `counter:` value."""
    known = available(plugins)
    if name in known:
        return known[name]["spec"]
    if ":" in str(name):
        return str(name)
    close = difflib.get_close_matches(str(name), list(known), n=1)
    hint = f" (did you mean {close[0]!r}?)" if close else ""
    raise SystemExit(f"counter {name!r} not found{hint}. Known: {', '.join(sorted(known))}. "
                     f"Run `python run_vision.py --list-counters`.")


def load_class(spec: str):
    mod, _, cls = spec.partition(":")
    if not cls:
        raise SystemExit(f"counter {spec!r} must be \"module:Class\"")
    return getattr(importlib.import_module(mod), cls)


def describe(cls) -> dict:
    """A plugin's self-description; every field is optional on the class."""
    doc = (cls.__doc__ or "").strip().splitlines()
    return {"name": getattr(cls, "NAME", cls.__name__),
            "description": getattr(cls, "DESCRIPTION", doc[0] if doc else ""),
            "needs": list(getattr(cls, "NEEDS", ())),
            "options": dict(getattr(cls, "OPTIONS", {}) or {}),
            "api": int(getattr(cls, "PLUGIN_API", 1))}


def check(cls, cfg: dict) -> list:
    """Warnings for a hub's config against what the plugin declares. A plugin
    written for a newer contract than this runner is refused outright."""
    d = describe(cls)
    if d["api"] > PLUGIN_API:
        raise SystemExit(f"counter {d['name']} needs plugin API {d['api']}; this "
                         f"vision supports {PLUGIN_API}. Update watchtower-fms.")
    if not d["options"]:
        return []                  # the plugin did not declare its keys
    # Only keys that look like a misspelt option: the `defaults:` block is
    # shared by every hub and counter, so another counter's keys there are
    # normal, but `confidence` beside a declared `conf` is a typo that would
    # otherwise silently keep the default.
    known = set(d["options"]) | RUNNER_KEYS
    out = []
    for k in cfg:
        if k in known:
            continue
        # A long form of a short option (`confidence` for `conf`) scores low
        # on similarity, so a shared prefix counts too.
        close = [o for o in d["options"] if len(o) >= 3 and len(k) >= 3
                 and (k.startswith(o) or o.startswith(k))][:1] \
            or difflib.get_close_matches(k, list(d["options"]), n=1, cutoff=0.75)
        if close:
            out.append(f"{cfg.get('hub', '?')}: {d['name']} has no option {k!r} "
                       f"(did you mean {close[0]!r}?)")
    return out


def load_counter(cfg: dict, plugins=None, warn=print):
    spec = resolve(cfg["counter"], plugins if plugins is not None else cfg.get("plugins"))
    cls = load_class(spec)
    for w in check(cls, cfg):
        warn(f"warning: {w}")
    return cls(cfg)


def plugin_status(counter) -> dict:
    """The plugin's own status line(s), if it reports any. A broken status()
    must not take the counting thread down with it."""
    fn = getattr(counter, "status", None)
    if not callable(fn):
        return {}
    try:
        st = fn() or {}
        return {k: v for k, v in dict(st).items() if k in ("detail", "warning", "error")}
    except Exception as e:                                # noqa: BLE001
        return {"warning": f"status() failed: {e}"}


def list_counters(plugins=None) -> str:
    """The text `run_vision.py --list-counters` prints."""
    lines = []
    for name, info in sorted(available(plugins).items()):
        try:
            d = describe(load_class(info["spec"]))
            needs = f"  [needs {', '.join(d['needs'])}]" if d["needs"] else ""
            lines.append(f"{name:<18} {d['description']}{needs}\n{'':<18} {info['spec']}  ({info['source']})")
            for k, h in d["options"].items():
                lines.append(f"{'':<20}{k}: {h}")
        except Exception as e:                            # noqa: BLE001
            lines.append(f"{name:<18} cannot load: {e}\n{'':<18} {info['spec']}  ({info['source']})")
    return "\n".join(lines)
