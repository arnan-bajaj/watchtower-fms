"""Counter plugins: lookup, plugin_paths, installed entry points, checks, status.

Pure: no cv2, no model. Plugins under test are written to a temp folder.
"""
import pathlib
import sys
import textwrap

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "vision"))
import counters as C  # noqa: E402

PLUG = '''
class Fancy:
    """Counts nothing, nicely."""
    PLUGIN_API = 1
    NAME = "fancy"
    DESCRIPTION = "a test counter"
    NEEDS = ("gpu",)
    OPTIONS = {"conf": "confidence", "imgsz": "size"}
    def __init__(self, cfg):
        self.cfg, self.total = cfg, 0
    def process(self, frame, t):
        return 0
    def status(self):
        return {"detail": "all good", "junk": 1}

class Old:
    """A plugin written before any of this existed."""
    def __init__(self, cfg):
        self.total = 0
    def process(self, frame, t):
        return 1

class Future(Old):
    PLUGIN_API = 99

class Broken(Old):
    def status(self):
        raise RuntimeError("boom")
'''


@pytest.fixture()
def plugdir(tmp_path, monkeypatch):
    (tmp_path / "myplug.py").write_text(textwrap.dedent(PLUG))
    monkeypatch.setattr(C, "_entry_points", lambda: {})
    yield tmp_path
    sys.modules.pop("myplug", None)
    if str(tmp_path) in sys.path:
        sys.path.remove(str(tmp_path))


def test_builtins_and_full_names_resolve(plugdir):
    assert C.resolve("zone") == "counters.zone:ZoneCounter"
    assert C.resolve("pkg.mod:Thing") == "pkg.mod:Thing"
    assert C.resolve("mine", {"mine": "myplug:Fancy"}) == "myplug:Fancy"


def test_unknown_name_suggests_the_close_one(plugdir):
    with pytest.raises(SystemExit, match="did you mean 'zone'"):
        C.resolve("zonee")


def test_plugin_paths_load_without_pythonpath(plugdir):
    C.add_plugin_paths(["."], base=plugdir)
    c = C.load_counter({"counter": "myplug:Fancy", "hub": "red"})
    assert c.process(None, 0.0) == 0


def test_plugin_paths_relative_to_the_config_folder(plugdir, tmp_path):
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    added = C.add_plugin_paths([".."], base=cfgdir)
    assert added == [str(tmp_path.resolve())]
    with pytest.raises(SystemExit, match="not a folder"):
        C.add_plugin_paths(["nope"], base=cfgdir)


def test_installed_plugin_found_by_short_name(plugdir, monkeypatch):
    C.add_plugin_paths([str(plugdir)])
    monkeypatch.setattr(C, "_entry_points", lambda: {"fancy": ("myplug:Fancy", "fancy-dist")})
    assert C.available()["fancy"]["source"] == "installed (fancy-dist)"
    assert C.load_counter({"counter": "fancy", "hub": "red"}).process(None, 0) == 0
    # A built-in name cannot be taken over by an installed package.
    monkeypatch.setattr(C, "_entry_points", lambda: {"zone": ("myplug:Fancy", "evil")})
    assert C.resolve("zone") == "counters.zone:ZoneCounter"


def test_plugin_paths_folder_with_pyproject_gives_short_names(plugdir, monkeypatch):
    monkeypatch.setattr(C, "_PATH_PLUGINS", {})
    (plugdir / "pyproject.toml").write_text(
        '[project]\nname = "x"\n[project.entry-points."watchtower.counters"]\n'
        'nice-one = "myplug:Fancy"\n')
    C.add_plugin_paths([str(plugdir)])
    assert C.available()["nice-one"]["source"].startswith("plugin_paths")
    assert C.load_counter({"counter": "nice-one", "hub": "red"}).process(None, 0) == 0


def test_misspelt_option_is_warned_and_others_left_alone(plugdir):
    C.add_plugin_paths([str(plugdir)])
    warned = []
    C.load_counter({"counter": "myplug:Fancy", "hub": "red", "confidence": 0.3,
                    "crop_pad": 60, "imgsz": 640}, warn=warned.append)
    assert len(warned) == 1 and "'confidence'" in warned[0] and "'conf'" in warned[0]


def test_plugin_for_a_newer_contract_is_refused(plugdir):
    C.add_plugin_paths([str(plugdir)])
    with pytest.raises(SystemExit, match="plugin API 99"):
        C.load_counter({"counter": "myplug:Future", "hub": "red"})


def test_old_plugins_still_work_and_describe_themselves(plugdir):
    C.add_plugin_paths([str(plugdir)])
    cls = C.load_class("myplug:Old")
    d = C.describe(cls)
    assert d["name"] == "Old" and d["description"].startswith("A plugin written")
    assert d["api"] == 1 and d["options"] == {}
    assert C.load_counter({"counter": "myplug:Old", "hub": "red"}).process(None, 0) == 1
    assert C.plugin_status(cls({})) == {}


def test_status_is_filtered_and_a_broken_one_does_not_crash(plugdir):
    C.add_plugin_paths([str(plugdir)])
    assert C.plugin_status(C.load_class("myplug:Fancy")({})) == {"detail": "all good"}
    st = C.plugin_status(C.load_class("myplug:Broken")({}))
    assert "boom" in st["warning"]


def test_list_shows_needs_and_options_and_survives_a_bad_plugin(plugdir):
    C.add_plugin_paths([str(plugdir)])
    out = C.list_counters({"fancy": "myplug:Fancy", "gone": "nosuchmodule:X"})
    assert "a test counter  [needs gpu]" in out and "conf: confidence" in out
    assert "gone" in out and "cannot load" in out


def test_dropped_in_file_is_found_by_name_without_importing(tmp_path, monkeypatch):
    drop = tmp_path / "plugins"
    drop.mkdir()
    (drop / "dropme.py").write_text(textwrap.dedent('''
        class Mine:
            NAME = "dropped"
            def __init__(self, cfg): self.total = 0
            def process(self, frame, t): return 3
        class Helper:                  # no NAME, no process: not a counter
            pass
        raise RuntimeError("only runs if imported")
    '''))
    (drop / "_skipme.py").write_text("class X:\n    NAME = 'skipped'\n")
    (drop / "broken.py").write_text("class (:\n")
    monkeypatch.setattr(C, "PLUGINS_DIR", drop)
    monkeypatch.setattr(C, "_entry_points", lambda: {})
    found = C.available()
    assert found["dropped"] == {"spec": "dropme:Mine", "source": "plugins/dropme.py"}
    assert "skipped" not in found and "helper" not in found
    sys.modules.pop("dropme", None)


def test_dropped_in_file_loads_and_cannot_take_a_builtin(tmp_path, monkeypatch):
    drop = tmp_path / "plugins"
    drop.mkdir()
    (drop / "dropok.py").write_text(
        "class Z:\n    NAME = 'zone'\n    def process(self, f, t): return 0\n"
        "class Ok:\n    def __init__(self, cfg): self.total = 0\n    def process(self, f, t): return 5\n")
    monkeypatch.setattr(C, "PLUGINS_DIR", drop)
    monkeypatch.setattr(C, "_entry_points", lambda: {})
    assert C.resolve("zone") == "counters.zone:ZoneCounter"
    assert C.load_counter({"counter": "ok", "hub": "red"}).process(None, 0) == 5
    sys.modules.pop("dropok", None)
    sys.path.remove(str(drop))


def test_add_plugin_remembers_a_repo_and_remove_forgets_it(plugdir, tmp_path, monkeypatch):
    drop = tmp_path / "plugins"
    monkeypatch.setattr(C, "PLUGINS_DIR", drop)
    monkeypatch.setattr(C, "_PATH_PLUGINS", {})
    (plugdir / "pyproject.toml").write_text(
        '[project]\nname = "x"\n[project.entry-points."watchtower.counters"]\n'
        'repo-one = "myplug:Fancy"\n')
    names = C.add_plugin(str(plugdir))
    assert "repo-one" in names and "fancy" in names
    assert (drop / f"{plugdir.name}.path").read_text().strip() == str(plugdir.resolve())
    assert C.available()["repo-one"]["source"] == f"plugins/{plugdir.name}.path"
    assert C.load_counter({"counter": "repo-one", "hub": "red"}).process(None, 0) == 0
    assert C.remove_plugin(plugdir.name) and not C.remove_plugin(plugdir.name)
    with pytest.raises(SystemExit, match="no counters"):
        C.add_plugin(str(drop))


def test_add_plugin_path_that_went_missing_is_listed_not_fatal(tmp_path, monkeypatch):
    drop = tmp_path / "plugins"
    drop.mkdir()
    (drop / "gone.path").write_text(str(tmp_path / "nowhere") + "\n")
    monkeypatch.setattr(C, "PLUGINS_DIR", drop)
    monkeypatch.setattr(C, "_entry_points", lambda: {})
    assert "not found" in C.available()["gone (missing)"]["source"]
    assert C.resolve("zone") == "counters.zone:ZoneCounter"


def test_the_template_is_a_valid_counter():
    tpl = pathlib.Path(C.__file__).resolve().parent.parent / "plugins" / "_example.py"
    assert C._named_classes(tpl) == {"my-counter": "MyCounter"}
