"""The website: accounts, events, launch script, settings sync with an event's FMS."""
import importlib
import json
import sys

import pytest
import yaml
from fastapi.testclient import TestClient


@pytest.fixture()
def site(tmp_path, monkeypatch):
    monkeypatch.setenv("WT_DATA", str(tmp_path / "site"))
    monkeypatch.setenv("WT_DIST", str(tmp_path / "dist"))
    monkeypatch.setenv("WT_SITE_URL", "https://wt.example")
    monkeypatch.delitem(sys.modules, "website.app", raising=False)
    mod = importlib.import_module("website.app")
    monkeypatch.delitem(sys.modules, "website.app")
    return mod


def _user(mod, email="a@team.org"):
    c = TestClient(mod.app, base_url="https://testserver")
    assert c.post("/api/signup", json={"email": email, "password": "longenough"}).status_code == 200
    return c


def test_signup_login_and_ownership(site):
    a = _user(site)
    assert a.get("/api/me").json()["user"]["email"] == "a@team.org"
    assert a.post("/api/signup", json={"email": "A@team.org", "password": "longenough"}).status_code == 409
    assert TestClient(site.app, base_url="https://testserver").post("/api/signup", json={"email": "x@y.z", "password": "short"}).status_code == 400
    anon = TestClient(site.app, base_url="https://testserver")
    assert anon.post("/api/login", json={"email": "a@team.org", "password": "wrong-one"}).status_code == 401
    assert anon.post("/api/login", json={"email": "a@team.org", "password": "longenough"}).status_code == 200
    assert anon.get("/api/events").json() == {"events": []}

    e = a.post("/api/events", json={"name": "Spring Scrimmage!", "teams": "101 102 103", "utc_offset_hours": "-4"}).json()
    assert e["slug"] == "spring-scrimmage" and e["settings"]["event.teams"]["value"] == [101, 102, 103]
    pins = [e["settings"][f"server.pins.{r}"]["value"] for r in ("control", "ref", "emcee")]
    assert len(set(pins)) == 3 and all(len(p) == 6 for p in pins)
    assert a.post("/api/events", json={"name": "Spring Scrimmage"}).json()["slug"] == "spring-scrimmage-2"
    assert a.post("/api/events", json={"name": ""}).status_code == 400
    assert json.loads(site.event_row("spring-scrimmage")["base"])["event"]["game"] == "rebuilt"   # the default
    assert a.post("/api/events", json={"name": "Next Year", "game": "biocore"}).status_code == 400   # not yet
    r = a.post("/api/events", json={"name": "Last Year", "game": "REEFSCAPE"}).json()           # scored by hand
    assert r["game"] == "reefscape" and json.loads(site.event_row(r["slug"])["base"])["event"]["game"] == "reefscape"
    assert a.post("/api/events", json={"name": "Odd", "game": "chess"}).status_code == 400

    b = _user(site, "b@team.org")
    assert b.get("/api/events/spring-scrimmage").status_code == 404           # not theirs
    pub = b.get("/api/public/spring-scrimmage").json()
    assert pub["name"] == "Spring Scrimmage!" and "commands" not in pub and "settings" not in pub
    assert "pins" not in str(pub)


def test_launch_script_event_yaml_and_sync(site):
    a = _user(site)
    e = a.post("/api/events", json={"name": "Fall Fest", "teams": "101 102 103 104 105 106"}).json()
    tok = e["commands"]["mac"].split("/run/")[1].split(" ")[0]
    sh = TestClient(site.app, base_url="https://testserver").get(f"/run/{tok}").text
    assert "SITE='https://wt.example'" in sh and "EVENT='fall-fest'" in sh and f"TOKEN='{tok}'" in sh
    assert "__" not in sh.replace("__init__", "")                           # every placeholder filled
    assert TestClient(site.app, base_url="https://testserver").get("/run/nope").status_code == 404

    c = TestClient(site.app, base_url="https://testserver")
    assert c.get("/api/link/fall-fest/event.yaml", headers={"X-Watchtower-Token": "bad"}).status_code == 404
    ev = yaml.safe_load(c.get("/api/link/fall-fest/event.yaml", headers={"X-Watchtower-Token": tok}).text)
    assert ev["cloud"] == {"url": "https://wt.example", "event": "fall-fest", "token": tok}
    assert ev["event"]["game"] == "rebuilt" and "game" not in ev     # the FMS takes the rules from GAME_RULES
    assert ev["event"]["teams"] == [101, 102, 103, 104, 105, 106] and ev["tba"]["enabled"] is False
    from fms import config
    config.validate(config._merge(config.DEFAULTS, ev))                     # the FMS will start on it

    # website edit (newer) and FMS edit (newer) both survive a sync; the FMS gets the merge back
    r = a.post("/api/events/fall-fest/settings", json={"changes": {"tba.auth_secret": "sek", "event.tba_event_key": "2026ffst"}})
    assert r.status_code == 200 and r.json()["settings"]["tba.auth_secret"]["value"] is True
    assert "sek" not in r.text
    assert a.post("/api/events/fall-fest/settings", json={"changes": {"server.pins.ref": ev["server"]["pins"]["control"]}}).status_code == 400
    fms_entries = {"vision.scale.red": {"v": 8, "t": 9e9}, "event.tba_event_key": {"v": "2026old", "t": 1.0}}
    j = c.post("/api/link/fall-fest/sync", headers={"X-Watchtower-Token": tok},
               json={"settings": fms_entries, "status": {"match": "qm3", "match_status": "running",
                                                          "lan_urls": ["http://192.168.1.64:8000"], "played": 2}}).json()
    assert j["settings"]["vision.scale.red"]["v"] == 8
    assert j["settings"]["event.tba_event_key"]["v"] == "2026ffst"           # website's edit was newer
    assert j["settings"]["tba.auth_secret"]["v"] == "sek"                    # the FMS needs the real secret
    pub = TestClient(site.app, base_url="https://testserver").get("/api/public/fall-fest").json()
    assert pub["online"] and pub["match"] == "qm3" and pub["lan_urls"] == ["http://192.168.1.64:8000"]

    # a new launch command cuts the old one off
    new = a.post("/api/events/fall-fest/rotate", json={}).json()
    assert tok not in new["commands"]["mac"]
    assert c.post("/api/link/fall-fest/sync", headers={"X-Watchtower-Token": tok}, json={}).status_code == 404
    assert a.delete("/api/events/fall-fest").status_code == 200
    assert a.get("/api/events").json()["events"] == []
    assert a.post("/api/events", json={"name": "Fall Fest"}).json()["slug"] == "fall-fest"   # name free again
