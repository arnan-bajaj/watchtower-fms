"""Live settings (/control -> Setup) on top of event.yaml, and their sync rules. Hand-computed values."""
import importlib
import pathlib
import shutil
import sys

import pytest
import yaml
from fastapi.testclient import TestClient

from fms import settings


def _server(tmp_path, monkeypatch, edit=None):
    from fms import init
    root = pathlib.Path(__file__).resolve().parent.parent / "config"
    for f in ("event.example.yaml", "vision.example.yaml"):
        shutil.copy(root / f, tmp_path / f)
    init.init(tmp_path)
    ev = yaml.safe_load((tmp_path / "event.yaml").read_text())
    ev["server"]["db"] = str(tmp_path / "fms.sqlite3")
    ev["event"]["teams"] = [101, 102, 103]
    if edit:
        edit(ev)
    (tmp_path / "event.yaml").write_text(yaml.safe_dump(ev))
    monkeypatch.setenv("FMS_CONFIG", str(tmp_path / "event.yaml"))
    monkeypatch.delitem(sys.modules, "fms.server", raising=False)
    srv = importlib.import_module("fms.server")
    monkeypatch.delitem(sys.modules, "fms.server")
    return srv, ev


def test_clean_and_validators():
    c = settings.clean({"event.teams": "254, 1678 frc971\n604", "event.tba_event_key": " 2026ABCD ",
                        "event.cycle_min": "7.5", "event.qual_start": "9:05", "tba.enabled": "on",
                        "field.feeds": ["192.168.1.10:8411", {"host": "10.0.100.5"}],
                        "vision.scale.red": 9, "event.date": None})
    assert c == {"event.teams": [254, 1678, 971, 604], "event.tba_event_key": "2026abcd",
                 "event.cycle_min": 7.5, "event.qual_start": "09:05", "tba.enabled": True,
                 "field.feeds": [{"name": "field", "host": "192.168.1.10", "port": 8411},
                                 {"name": "field", "host": "10.0.100.5", "port": 8411}],
                 "vision.scale.red": 9, "event.date": None}
    assert settings.clean({"field.feeds": []}) == {"field.feeds": []}           # off
    for bad in ({"event.teams": "254 254"}, {"event.teams": "abc"}, {"event.tba_event_key": "abcd"},
                {"server.pins.ref": "12a4"}, {"field.feeds": ["bad host!:80"]}, {"field.feeds": ["1.2.3.4:70000"]},
                {"vision.scale.blue": 0}, {"event.date": "10/10/2026"}, {"server.db": "/etc/passwd"},
                {"event.name": "  "}):
        with pytest.raises(ValueError):
            settings.clean(bad)


def test_merge_newest_wins_per_key_and_tombstones():
    a = {"event.name": {"v": "A", "t": 10}, "event.date": {"v": "2026-10-10", "t": 50}}
    b = {"event.name": {"v": "B", "t": 20}, "event.date": {"v": None, "t": 40, "del": True},
         "server.db": {"v": "x", "t": 99}}                                      # not a setting: dropped
    m = settings.merge(a, b)
    assert m == {"event.name": {"v": "B", "t": 20}, "event.date": {"v": "2026-10-10", "t": 50}}
    assert settings.merge(b, a) == m                                            # order doesn't matter
    late = settings.merge(m, {"event.date": {"v": None, "t": 60, "del": True}})
    base = {"event": {"name": "Y", "date": "", "teams": []}}
    assert settings.overlay(base, late)["event"] == {"name": "B", "date": "", "teams": []}


def test_setup_changes_apply_live_and_survive_restart(tmp_path, monkeypatch):
    srv, ev = _server(tmp_path, monkeypatch)
    c = TestClient(srv.app)
    ctl = {"X-FMS-Token": srv.token_for("control")}
    assert c.get("/api/settings").status_code == 401
    assert c.post("/api/settings", json={"changes": {"event.name": "X"}},
                  headers={"X-FMS-Token": srv.token_for("ref")}).status_code == 401

    r = c.post("/api/settings", headers=ctl, json={"changes": {
        "event.name": "Spring Scrimmage", "event.teams": "101 102 103 104", "event.tba_event_key": "2026test",
        "tba.auth_id": "id1", "tba.auth_secret": "s3cret", "tba.enabled": True}})
    assert r.status_code == 200, r.text
    st = c.get("/api/state").json()
    assert st["event"]["name"] == "Spring Scrimmage" and st["event"]["teams"] == [101, 102, 103, 104]
    assert st["tba"]["key"] == "2026test" and st["tba"]["configured"] is True
    assert "s3cret" not in c.get("/api/state").text and "s3cret" not in c.get("/api/settings", headers=ctl).text
    s = c.get("/api/settings", headers=ctl).json()["settings"]
    assert s["tba.auth_secret"] == {"value": True, "source": "setup"}
    assert s["event.date"]["source"] == "event.yaml"

    # invalid whole-config result: nothing changes
    pin = ev["server"]["pins"]["ref"]
    r = c.post("/api/settings", headers=ctl, json={"changes": {"event.name": "Nope", "server.pins.control": pin}})
    assert r.status_code == 400 and "differ" in r.json()["detail"]
    assert c.get("/api/state").json()["event"]["name"] == "Spring Scrimmage"

    # back to event.yaml
    c.post("/api/settings", headers=ctl, json={"changes": {"event.name": None}})
    assert c.get("/api/state").json()["event"]["name"] == ev["event"]["name"]

    monkeypatch.delitem(sys.modules, "fms.server", raising=False)
    srv2 = importlib.import_module("fms.server")                               # restart on the same files
    st2 = TestClient(srv2.app).get("/api/state").json()
    assert st2["event"]["teams"] == [101, 102, 103, 104] and st2["tba"]["key"] == "2026test"


def test_pin_change_logs_role_out_and_wipe_keeps_settings(tmp_path, monkeypatch):
    srv, ev = _server(tmp_path, monkeypatch)
    c = TestClient(srv.app)
    old_ref = srv.token_for("ref")
    ctl = {"X-FMS-Token": srv.token_for("control")}
    c.post("/api/settings", headers=ctl, json={"changes": {"server.pins.ref": "4242"}})
    assert srv.token_for("ref") != old_ref
    assert srv.role_of(old_ref) is None
    assert c.post("/api/login", json={"role": "ref", "pin": ev["server"]["pins"]["ref"]}).status_code == 401
    assert c.post("/api/login", json={"role": "ref", "pin": "4242"}).json()["token"] == srv.token_for("ref")
    r = c.post("/api/admin/wipe", headers=ctl, json={"scope": "all", "pin": ev["server"]["pins"]["control"]})
    assert r.status_code == 200
    assert srv.CFG["server"]["pins"]["ref"] == "4242" and srv.store.get("settings")["server.pins.ref"]["v"] == "4242"


def test_login_throttle(tmp_path, monkeypatch):
    srv, ev = _server(tmp_path, monkeypatch)
    c = TestClient(srv.app)
    for _ in range(10):
        assert c.post("/api/login", json={"role": "control", "pin": "000000x"}).status_code == 401
    r = c.post("/api/login", json={"role": "control", "pin": ev["server"]["pins"]["control"]})
    assert r.status_code == 429                                                 # even the right PIN waits
    srv._login_fails[:] = [t - 61 for t in srv._login_fails]                    # a minute later
    assert c.post("/api/login", json={"role": "control", "pin": ev["server"]["pins"]["control"]}).status_code == 200


def test_vision_config_follows_setup(tmp_path, monkeypatch):
    srv, ev = _server(tmp_path, monkeypatch)
    c = TestClient(srv.app)
    vk = {"X-Vision-Key": ev["server"]["vision_key"]}
    assert c.get("/api/vision/config", headers={"X-Vision-Key": "wrong"}).status_code == 401
    assert c.get("/api/vision/config", headers=vk).json() == {"rev": 0, "feeds": None, "scale": {"red": 1.0, "blue": 1.0},
                                                              "hubs": {"red": None, "blue": None}}
    ctl = {"X-FMS-Token": srv.token_for("control")}
    c.post("/api/settings", headers=ctl, json={"changes": {"event.name": "Only a name"}})
    assert c.get("/api/vision/config", headers=vk).json()["rev"] == 0            # vision not bothered
    c.post("/api/settings", headers=ctl, json={"changes": {"field.feeds": "192.168.1.10:8411", "vision.scale.red": 9}})
    vc = c.get("/api/vision/config", headers=vk).json()
    assert vc == {"rev": 1, "feeds": [{"name": "field", "host": "192.168.1.10", "port": 8411}],
                  "scale": {"red": 9.0, "blue": 1.0}, "hubs": {"red": None, "blue": None}}
    r = c.post("/api/vision/events", headers=vk, json={"events": {}, "status": {}}).json()
    assert r["config_rev"] == 1 and abs(r["now"] - __import__("time").time()) < 5
    assert r["want"] == {"frames": False, "scan": 0, "drop": {}}


def test_setup_cameras_flow(tmp_path, monkeypatch):
    """Setup -> Cameras: pick a camera, draw the outline, calibrate by drop test; vision sees each step."""
    import base64
    srv, ev = _server(tmp_path, monkeypatch)
    c = TestClient(srv.app)
    vk = {"X-Vision-Key": ev["server"]["vision_key"]}
    ctl = {"X-FMS-Token": srv.token_for("control")}
    ref = {"X-FMS-Token": srv.token_for("ref")}
    assert c.post("/api/vision/want", headers=ref, json={"scan": True}).status_code == 401
    assert c.get("/api/vision/image/hub/red", headers=ctl).status_code == 404

    # Find cameras: the FMS asks, vision answers with a picture per camera
    asked = c.post("/api/vision/want", headers=ctl, json={"scan": True, "frames": True}).json()["scan"]["asked"]
    want = c.post("/api/vision/events", headers=vk, json={"events": {}, "status": {}}).json()["want"]
    assert want["scan"] == asked and want["frames"] is True
    jpg = base64.b64encode(b"\xff\xd8fake").decode()
    c.post("/api/vision/cameras", headers=vk, json={"id": asked, "cameras": [{"source": "1", "w": 1920, "h": 1080, "jpeg": jpg}],
                                                    "in_use": {"0": "blue"}})
    st = c.get("/api/vision/setup", headers=ctl).json()
    assert st["scan"]["id"] == asked and st["scan"]["cameras"] == [{"source": "1", "w": 1920, "h": 1080}]
    assert st["scan"]["in_use"] == {"0": "blue"}
    assert c.get("/api/vision/image/cam/1", headers=ctl).content == b"\xff\xd8fake"

    # pick camera 1 for red, then the outline; each reaches vision through /api/vision/config
    bad = {"source": "1", "size": [1920, 1080], "outline": [[1, 2], [3, 4]]}
    assert c.post("/api/settings", headers=ctl, json={"changes": {"vision.hubs.red": bad}}).status_code == 400
    c.post("/api/settings", headers=ctl, json={"changes": {"vision.hubs.red": {"source": 1}}})
    assert c.get("/api/vision/config", headers=vk).json()["hubs"]["red"] == {"source": "1", "size": [0, 0], "outline": [], "ball_area": None}
    c.post("/api/vision/frames", headers=vk, json={"red": {"jpeg": jpg, "w": 1280, "h": 720, "source": "1"}})
    assert c.get("/api/vision/setup", headers=ctl).json()["frames"]["red"]["w"] == 1280
    assert c.get("/api/vision/image/hub/red", headers=ctl).content == b"\xff\xd8fake"
    hub = {"source": "1", "size": [1280, 720], "outline": [[400, 300], [800, 300], [780, 420], [420, 420]]}
    c.post("/api/settings", headers=ctl, json={"changes": {"vision.hubs.red": hub}})
    assert c.get("/api/vision/config", headers=vk).json()["hubs"]["red"]["outline"][2] == [780, 420]

    # drop test: start (vision records), the number dropped (vision fits), the fit saved; vision
    # measured at 1920x1080 while the outline was drawn at 1280x720: 900 px -> 400 px; scale back to 1
    c.post("/api/settings", headers=ctl, json={"changes": {"vision.scale.red": 8}})
    assert c.post("/api/vision/want", headers=ctl, json={"drop": "red", "n": 10}).status_code == 409   # not started
    assert c.post("/api/vision/want", headers=ctl, json={"drop": "red"}).json()["drop"] == {"red": {"n": None}}
    want = c.post("/api/vision/events", headers=vk, json={"events": {}, "status": {}}).json()["want"]
    did = want["drop"]["red"]["id"]
    assert want["drop"]["red"]["n"] is None
    assert c.post("/api/vision/want", headers=ctl, json={"drop": "red", "n": "x"}).status_code == 400
    c.post("/api/vision/want", headers=ctl, json={"drop": "red", "n": 20})
    want = c.post("/api/vision/events", headers=vk, json={"events": {}, "status": {}}).json()["want"]
    assert want["drop"]["red"] == {"id": did, "n": 20}
    c.post("/api/vision/events", headers=vk, json={"events": {}, "status": {}, "measured": {
        "red": {"id": did + 1, "area": 5.0, "size": [1920, 1080]}}})               # a stale answer: ignored
    assert c.get("/api/vision/config", headers=vk).json()["hubs"]["red"]["ball_area"] is None
    c.post("/api/vision/events", headers=vk, json={"events": {}, "status": {}, "measured": {
        "red": {"id": did, "area": 900.0, "got": 20, "n": 20, "size": [1920, 1080]}}})
    st = c.get("/api/vision/setup", headers=ctl).json()
    assert st["drop"] == {} and st["drop_result"]["red"]["ok"] and "20 of the 20" in st["drop_result"]["red"]["msg"]
    vc = c.get("/api/vision/config", headers=vk).json()
    assert vc["hubs"]["red"]["ball_area"] == 400.0 and vc["scale"]["red"] == 1.0
    # a test that found nothing says why and changes nothing; cancel drops a running one
    c.post("/api/vision/want", headers=ctl, json={"drop": "red"})
    want = c.post("/api/vision/events", headers=vk, json={"events": {}, "status": {}}).json()["want"]
    c.post("/api/vision/events", headers=vk, json={"events": {}, "status": {}, "measured": {
        "red": {"id": want["drop"]["red"]["id"], "error": "Nothing crossed the outline."}}})
    st = c.get("/api/vision/setup", headers=ctl).json()
    assert not st["drop_result"]["red"]["ok"] and "Nothing crossed" in st["drop_result"]["red"]["msg"]
    assert c.get("/api/vision/config", headers=vk).json()["hubs"]["red"]["ball_area"] == 400.0
    c.post("/api/vision/want", headers=ctl, json={"drop": "blue"})
    assert c.post("/api/vision/want", headers=ctl, json={"drop": "blue", "cancel": True}).json()["drop"] == {}
    # back to the config file
    c.post("/api/settings", headers=ctl, json={"changes": {"vision.hubs.red": None}})
    assert c.get("/api/vision/config", headers=vk).json()["hubs"]["red"] is None


def test_cloud_sync_merges_website_edits(tmp_path, monkeypatch):
    srv, ev = _server(tmp_path, monkeypatch, edit=lambda e: e.update(
        cloud={"url": "https://example.invalid", "event": "spring", "token": "tok"}))
    ctl = {"X-FMS-Token": srv.token_for("control")}
    c = TestClient(srv.app)
    c.post("/api/settings", headers=ctl, json={"changes": {"event.name": "Local name"}})
    sent = {}

    def fake_exchange(cfg, entries, status):
        sent.update(cfg=cfg, entries=entries, status=status)
        # website: an older name (loses), a newer team list (wins), and an invalid PIN clash (rejected whole)
        return {"event.name": {"v": "Website name", "t": 1.0},
                "event.teams": {"v": [101, 102, 103, 104, 105], "t": 9e9}}
    monkeypatch.setattr(srv.cloud, "exchange", fake_exchange)
    srv.sync_cloud()
    assert sent["cfg"]["event"] == "spring" and sent["entries"]["event.name"]["v"] == "Local name"
    assert set(sent["status"]) >= {"match", "lan_urls", "played"}
    st = c.get("/api/state").json()["event"]
    assert st["name"] == "Local name" and st["teams"] == [101, 102, 103, 104, 105]
