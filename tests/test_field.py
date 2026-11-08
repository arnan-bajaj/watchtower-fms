"""fms/field.py and the Control endpoints against a fake bioarena on localhost: same websocket, messages and
refusals as its /remote_control/websocket. Short hand-picked game timings so the values can be checked by hand."""
import asyncio
import importlib
import json
import pathlib
import shutil
import socket
import sys
import threading
import time

import pytest
import uvicorn
import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.testclient import TestClient

from fms import field, settings

KEY = "shared-field-key"
GAME = {"auto_s": 1, "auto_teleop_gap_s": 1, "transition_s": 1, "shift_s": 1, "n_shifts": 4, "endgame_s": 2, "score_grace_s": 0.1}
TIMING = {"WarmupDurationSec": 0, "AutoDurationSec": 1, "PauseDurationSec": 1, "TeleopDurationSec": 7}   # 1 + 4x1 + 2 = 7
WPA = "wpa-secret-key"
ROBOTS_MISSING = "cannot start match until all robots are connected or bypassed"


def until(fn, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.01)
    raise AssertionError("condition never came true")


class FakeField:
    """Behaves like bioarena's remote control where Watchtower can tell."""

    def __init__(self):
        self.app = FastAPI()
        self.clients = []
        self.state = 0
        self.timing = dict(TIMING)
        self.st = {s: {"Team": None, "EStop": False, "Bypass": False, "AStop": False, "EStopFault": "", "DsConn": None}
                   for s in field.STATIONS}
        self.field_estop = self.field_estop_held = False
        self.allow_start = True
        self.refuse_start = None
        self.commands = []
        self.auto_winner = "random"
        self.hub = {"HubShift": "NONE", "RedHubActive": False, "BlueHubActive": False, "InShift": False}
        self.loop = None

        @self.app.websocket("/remote_control/websocket")
        async def play(ws: WebSocket):
            if ws.headers.get("x-remote-key") != KEY:
                await ws.close(code=1008)                # before accept: the client sees HTTP 403
                return
            await ws.accept()
            self.clients.append(ws)
            await ws.send_text(json.dumps({"type": "matchTiming", "data": self.timing}))
            await self.status(ws)
            await self.load(ws)
            await ws.send_text(json.dumps(self._clock()))
            try:
                while True:
                    m = json.loads(await ws.receive_text())
                    error = await self.handle(ws, m["type"], m.get("data") or {})
                    await self.status(ws)
                    await ws.send_text(json.dumps({"type": "result", "data": {"Id": m["data"]["Id"], "Error": error}}))
            except WebSocketDisconnect:
                self.clients.remove(ws)

    def _clock(self):
        return {"type": "matchTime", "data": {"MatchState": self.state, "MatchTimeSec": 0, **self.hub}}

    def _problem(self):
        if self.state != 0:
            return "cannot start match while there is a match still in progress or with results pending"
        if self.field_estop:
            return "cannot start match while field emergency stop is active"
        if any(s["EStop"] for s in self.st.values()):
            return "cannot start match while an emergency stop is active"
        if not all(s["Bypass"] or (s["DsConn"] or {}).get("RobotLinked") for s in self.st.values()):
            return ROBOTS_MISSING
        return ""

    async def status(self, ws):
        await ws.send_text(json.dumps({"type": "arenaStatus", "data": {
            "MatchState": self.state, "AllianceStations": self.st, "CanStartMatch": not self._problem(),
            "StartProblem": self._problem(), "RemoteStartAllowed": self.allow_start,
            "FieldEStop": self.field_estop, "GpioFieldEStopActive": False,
            "AccessPointStatus": "ACTIVE", "SwitchStatus": "ACTIVE", "HubCounterStatus": "NOT CONFIGURED"}}))

    async def load(self, ws):
        await ws.send_text(json.dumps({"type": "matchLoad", "data": {
            "AutoWinnerMode": self.auto_winner, "Teams": {s: self.st[s]["Team"] for s in field.STATIONS}}}))

    def _teams(self, d):
        return dict(zip(field.STATIONS, (d[k] for k in field.WIRE_NAMES)))

    async def handle(self, ws, kind, d):
        self.commands.append((kind, {k: v for k, v in d.items() if k != "Id"}))
        in_match = self.state not in (0, 6)
        if kind == "loadLineup":
            if self.state != 0:
                return "The field only takes a new lineup between matches."
            for s, t in self._teams(d).items():
                self.st[s]["Team"] = {"Id": t, "WpaKey": WPA} if t else None
            await self.load(ws)
        elif kind == "startMatch":
            if not self.allow_start:
                return "This field only allows matches to be started from its own Match Play page."
            if self._teams(d) != {s: (self.st[s]["Team"] or {}).get("Id", 0) for s in field.STATIONS}:
                return "The lineup on the field has changed. Load it again."
            if self.refuse_start or self._problem():
                return self.refuse_start or self._problem()
            await self.go(1)
        elif kind == "abortMatch":
            if not in_match:
                return "cannot abort match when it is not in progress"
            await self.go(6)
        elif kind in ("setEStop", "setBypass"):
            if not d["On"] and in_match:
                return "Cannot do that while a match is in progress."
            self.st[d["Station"]]["EStop" if kind == "setEStop" else "Bypass"] = d["On"]
        elif kind == "bypassEmptyStations":
            for s in self.st.values():
                if s["Team"] is None:
                    s["Bypass"] = True
        elif kind == "clearFieldEStop":
            if in_match:
                return "Cannot clear the field e-stop while a match is in progress."
            self.field_estop = self.field_estop_held
        elif kind == "setAutoWinnerMode":
            self.auto_winner = d["Mode"]
            await self.load(ws)
        return ""

    async def go(self, state):
        self.state = state
        for c in list(self.clients):
            await c.send_text(json.dumps(self._clock()))
            await self.status(c)

    def set_state(self, state):
        """The field moves by itself, as its own timer does."""
        asyncio.run_coroutine_threadsafe(self.go(state), self.loop).result(2)

    def connect_robots(self):
        for s in self.st.values():
            s["DsConn"] = {"DsLinked": True, "RadioLinked": True, "RioLinked": True, "RobotLinked": True, "BatteryVoltage": 12.345}
        self.push_status()

    def push_status(self):
        async def send():
            for c in list(self.clients):
                await self.status(c)
        asyncio.run_coroutine_threadsafe(send(), self.loop).result(2)

    def start(self):
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        srv = uvicorn.Server(uvicorn.Config(self.app, log_level="critical"))
        srv.install_signal_handlers = lambda: None
        self.server = srv

        async def main():
            self.loop = asyncio.get_running_loop()
            await srv.serve(sockets=[sock])
        self.thread = threading.Thread(target=lambda: asyncio.run(main()), daemon=True)
        self.thread.start()
        until(lambda: srv.started)
        return self

    def stop(self):
        self.server.should_exit = True
        self.server.force_exit = True
        self.thread.join(3)


@pytest.fixture
def fake():
    f = FakeField().start()
    yield f
    f.stop()


def _server(tmp_path, monkeypatch, field_cfg=None, game=None):
    from fms import init
    root = pathlib.Path(__file__).resolve().parent.parent / "config"
    for f in ("event.example.yaml", "vision.example.yaml"):
        shutil.copy(root / f, tmp_path / f)
    init.init(tmp_path)
    ev = yaml.safe_load((tmp_path / "event.yaml").read_text())
    ev["server"]["db"] = str(tmp_path / "fms.sqlite3")
    ev["event"]["teams"] = [101, 102, 103, 104, 105, 106, 107]
    ev["game"] = GAME if game is None else game
    if field_cfg:
        ev["field"] = {"control": field_cfg}
    (tmp_path / "event.yaml").write_text(yaml.safe_dump(ev))
    monkeypatch.setenv("FMS_CONFIG", str(tmp_path / "event.yaml"))
    monkeypatch.delitem(sys.modules, "fms.server", raising=False)
    srv = importlib.import_module("fms.server")
    monkeypatch.delitem(sys.modules, "fms.server")
    srv.store.insert_match({"key": "qm1", "comp_level": "qm", "set_number": 1, "match_number": 1, "ord": 1,
                            "red": [101, 102, 103], "blue": [104, 105, 106], "surrogates": [], "status": "scheduled",
                            "adjust": {}, "climbs": {}})
    srv.store.set("current", "qm1")
    return srv


def _linked(srv):
    until(lambda: srv.field_link.public().get("link"))


def _ctl(srv):
    return {"X-FMS-Token": srv.token_for("control")}


def _control(fake):
    return {"enabled": True, "host": "127.0.0.1", "port": fake.port, "key": KEY}


def _blocked(srv):
    return srv.field_view(srv.current()).get("blocked")


def _ready(srv, c, fake):
    """Link up, lineup loaded, robots connected: the state Start needs."""
    _linked(srv)
    assert c.post("/api/field/lineup", json={"key": "qm1"}, headers=_ctl(srv)).status_code == 200
    fake.connect_robots()
    until(lambda: _blocked(srv) is None)


def _running(srv):
    until(lambda: srv.store.match("qm1")["status"] == "running")


# ---- pure pieces
def test_lineup_fills_short_alliances_with_empty_stations():
    assert field.lineup([1, 2, 3], [4, 5, 6]) == {"R1": 1, "R2": 2, "R3": 3, "B1": 4, "B2": 5, "B3": 6}
    assert field.lineup([1], [4, 5]) == {"R1": 1, "R2": 0, "R3": 0, "B1": 4, "B2": 5, "B3": 0}


def test_expected_timing_matches_the_field_defaults(tmp_path, monkeypatch):
    # REBUILT: 20 auto, 3 gap, teleop 10 + 4x25 + 30 = 140, bioarena's config.yaml defaults
    assert _server(tmp_path, monkeypatch, game={}).expected_timing() == (20, 3, 140)


def test_settings_validate_and_key_is_secret():
    assert settings.clean({"field.control.host": " 192.168.1.10 ", "field.control.port": "8080", "field.control.enabled": "on"}) == {
        "field.control.host": "192.168.1.10", "field.control.port": 8080, "field.control.enabled": True}
    assert settings.clean({"field.control.host": ""}) == {"field.control.host": ""}
    for bad in ({"field.control.host": "bad host!"}, {"field.control.port": 70000}):
        with pytest.raises(ValueError):
            settings.clean(bad)
    entries = {"field.control.key": {"v": "x", "t": 1}, "field.control.host": {"v": "evil", "t": 2}, "event.name": {"v": "A", "t": 3}}
    assert settings.shareable(entries) == {"event.name": {"v": "A", "t": 3}}
    assert "field.control.key" in settings.SECRET


def test_website_can_neither_read_nor_set_field_control(tmp_path, monkeypatch):
    srv = _server(tmp_path, monkeypatch)
    srv.CFG["cloud"].update(url="http://site", event="e", token="t")
    srv.apply_settings(settings.stamp(settings.clean({"field.control.key": "hunter2hunter2", "field.control.host": "10.0.100.5"}), 100.0))
    sent = {}

    def exchange(_cfg, entries, _status):
        sent.update(entries)
        return {"field.control.host": {"v": "attacker.example", "t": 9e9}, "field.control.enabled": {"v": True, "t": 9e9}}
    monkeypatch.setattr(srv.cloud, "exchange", exchange)
    srv.sync_cloud()
    assert not [k for k in sent if k.startswith("field.control")]
    assert srv.CFG["field"]["control"]["host"] == "10.0.100.5" and srv.CFG["field"]["control"]["enabled"] is False


def test_start_is_unchanged_with_field_control_off(tmp_path, monkeypatch):
    srv = _server(tmp_path, monkeypatch)
    with TestClient(srv.app) as c:
        assert c.get("/api/state").json()["field"] == {"enabled": False, "aborted": False}
        r = c.post("/api/match/start", json={"key": "qm1"}, headers=_ctl(srv))
        assert r.status_code == 200 and r.json()["auto_start"] > 0
        assert srv.store.match("qm1")["status"] == "running"


# ---- connection and access
def test_wrong_key_is_reported_and_nothing_starts(tmp_path, monkeypatch, fake):
    srv = _server(tmp_path, monkeypatch, {**_control(fake), "key": "not-the-key"})
    with TestClient(srv.app) as c:
        until(lambda: srv.field_link.public().get("error"))
        v = c.get("/api/state").json()["field"]
        assert v["link"] is False and "refused this computer" in v["error"]
        r = c.post("/api/match/start", json={"key": "qm1"}, headers=_ctl(srv))
        assert r.status_code == 409 and "isn't connected" in r.json()["detail"]
        assert srv.store.match("qm1")["status"] == "scheduled"


def test_only_control_can_drive_the_field(tmp_path, monkeypatch, fake):
    srv = _server(tmp_path, monkeypatch, _control(fake))
    with TestClient(srv.app) as c:
        _linked(srv)
        ref = {"X-FMS-Token": srv.token_for("ref")}
        for path, body in (("/api/field/abort", None), ("/api/field/lineup", {"key": "qm1"}), ("/api/field/bypass_empty", None),
                           ("/api/field/clear_estop", None), ("/api/field/auto_winner", {"mode": "red"}),
                           ("/api/field/station", {"station": "R1", "action": "estop"})):
            assert c.post(path, json=body, headers=ref).status_code == 401, path
            assert c.post(path, json=body).status_code == 401, path
    assert fake.commands == []


def test_public_state_never_carries_secrets(tmp_path, monkeypatch, fake):
    srv = _server(tmp_path, monkeypatch, _control(fake))
    with TestClient(srv.app) as c:
        _ready(srv, c, fake)
        text = c.get("/api/state").text
        assert WPA not in text and KEY not in text
        assert c.get("/api/settings", headers=_ctl(srv)).json()["settings"]["field.control.key"]["value"] is True


# ---- interlocks on Start
def test_start_blocked_until_lineup_loaded_and_robots_ready(tmp_path, monkeypatch, fake):
    srv = _server(tmp_path, monkeypatch, _control(fake))
    with TestClient(srv.app) as c:
        _linked(srv)
        r = c.post("/api/match/start", json={"key": "qm1"}, headers=_ctl(srv))
        assert r.status_code == 409 and "Load lineup for qm1" in r.json()["detail"]
        assert not [k for k, _ in fake.commands if k == "startMatch"]

        assert c.post("/api/field/lineup", json={"key": "qm1"}, headers=_ctl(srv)).status_code == 200
        assert fake.commands[-1] == ("loadLineup", {"Name": "qm1", "Red1": 101, "Red2": 102, "Red3": 103,
                                                    "Blue1": 104, "Blue2": 105, "Blue3": 106})
        assert _blocked(srv) == ROBOTS_MISSING                                   # the field's own words
        assert c.post("/api/match/start", json={"key": "qm1"}, headers=_ctl(srv)).status_code == 409

        fake.connect_robots()
        until(lambda: _blocked(srv) is None)
        assert srv.field_view(srv.current())["lineup_ok"] is True


def test_start_blocked_when_match_lengths_differ(tmp_path, monkeypatch, fake):
    fake.timing["TeleopDurationSec"] = 140
    srv = _server(tmp_path, monkeypatch, _control(fake))
    with TestClient(srv.app) as c:
        _linked(srv)
        c.post("/api/field/lineup", json={"key": "qm1"}, headers=_ctl(srv))
        fake.connect_robots()
        got = until(lambda: "Match lengths differ" in (_blocked(srv) or "") and _blocked(srv))
        assert "(1, 1, 140)" in got and "(1.0, 1.0, 7.0)" in got
        assert c.post("/api/match/start", json={"key": "qm1"}, headers=_ctl(srv)).status_code == 409


def test_field_can_keep_start_to_itself_and_watchtower_still_follows(tmp_path, monkeypatch, fake):
    fake.allow_start = False
    srv = _server(tmp_path, monkeypatch, _control(fake))
    with TestClient(srv.app) as c:
        _linked(srv)
        c.post("/api/field/lineup", json={"key": "qm1"}, headers=_ctl(srv))
        fake.connect_robots()
        until(lambda: "doesn't allow remote starts" in (_blocked(srv) or ""))
        assert c.post("/api/match/start", json={"key": "qm1"}, headers=_ctl(srv)).status_code == 409
        fake.set_state(3)                                                        # the field's own Start
        _running(srv)


def test_lineup_is_refused_during_a_match(tmp_path, monkeypatch, fake):
    srv = _server(tmp_path, monkeypatch, _control(fake))
    with TestClient(srv.app) as c:
        _ready(srv, c, fake)
        fake.set_state(3)
        until(lambda: srv.field_link.in_match())
        r = c.post("/api/field/lineup", json={"key": "qm1"}, headers=_ctl(srv))
        assert r.status_code == 409 and "between matches" in r.json()["detail"]


# ---- the match follows the field
def test_timeline_starts_when_the_field_reaches_auto_not_at_the_click(tmp_path, monkeypatch, fake):
    srv = _server(tmp_path, monkeypatch, _control(fake))
    with TestClient(srv.app) as c:
        _ready(srv, c, fake)
        r = c.post("/api/match/start", json={"key": "qm1"}, headers=_ctl(srv))
        assert r.status_code == 200 and r.json()["field"] is True
        assert ("startMatch", {"Red1": 101, "Red2": 102, "Red3": 103, "Blue1": 104, "Blue2": 105, "Blue3": 106}) in fake.commands
        assert srv.store.match("qm1")["status"] == "scheduled"                   # field is in StartMatch, not Auto
        time.sleep(0.2)
        t0 = time.time()
        fake.set_state(3)
        _running(srv)
        assert t0 - 0.02 <= srv.store.match("qm1")["auto_start"] <= time.time()
        assert srv.store.get("current") == "qm1" and srv.store.get("display") == "match"


def test_a_refused_start_leaves_the_match_scheduled(tmp_path, monkeypatch, fake):
    srv = _server(tmp_path, monkeypatch, _control(fake))
    with TestClient(srv.app) as c:
        _ready(srv, c, fake)
        fake.refuse_start = "cannot start match while field emergency stop is active"
        r = c.post("/api/match/start", json={"key": "qm1"}, headers=_ctl(srv))
        assert r.status_code == 409 and r.json()["detail"] == fake.refuse_start
        assert srv.store.match("qm1")["status"] == "scheduled"
        assert srv._field_starting["key"] is None


def test_natural_end_is_not_an_abort(tmp_path, monkeypatch, fake):
    srv = _server(tmp_path, monkeypatch, _control(fake))
    with TestClient(srv.app) as c:
        _ready(srv, c, fake)
        c.post("/api/match/start", json={"key": "qm1"}, headers=_ctl(srv))
        fake.set_state(3)
        _running(srv)
        # hand-computed: 1 auto + 1 gap + 7 teleop = 9 s long; end the field 0.5 s before Watchtower's end
        srv.store.update_match("qm1", auto_start=time.time() - 8.5)
        fake.set_state(5)
        fake.set_state(6)
        until(lambda: srv.field_link.public()["state"] == "post")
        assert srv.store.match("qm1")["status"] == "running"                     # the tick loop ends it, on time
        assert not srv.field_view(srv.current())["aborted"]
        until(lambda: srv.store.match("qm1")["status"] == "review")
        assert not srv.field_view(srv.current())["aborted"]


def test_abort_ends_the_match_and_reset_replays_it(tmp_path, monkeypatch, fake):
    srv = _server(tmp_path, monkeypatch, _control(fake))
    with TestClient(srv.app) as c:
        _ready(srv, c, fake)
        c.post("/api/match/start", json={"key": "qm1"}, headers=_ctl(srv))
        fake.set_state(3)
        _running(srv)

        r = c.post("/api/match/reset", json={"key": "qm1"}, headers=_ctl(srv))    # robots still live: refuse
        assert r.status_code == 409 and "Abort it first" in r.json()["detail"]

        assert c.post("/api/field/abort", headers=_ctl(srv)).status_code == 200
        until(lambda: srv.store.match("qm1")["status"] == "review")
        assert srv.field_view(srv.current())["aborted"] is True

        assert c.post("/api/match/reset", json={"key": "qm1"}, headers=_ctl(srv)).status_code == 200
        m = srv.store.match("qm1")
        assert m["status"] == "scheduled" and m["auto_start"] is None
        assert srv.field_view(srv.current())["aborted"] is False


def test_abort_from_the_field_itself_lands_too(tmp_path, monkeypatch, fake):
    srv = _server(tmp_path, monkeypatch, _control(fake))
    with TestClient(srv.app) as c:
        _ready(srv, c, fake)
        c.post("/api/match/start", json={"key": "qm1"}, headers=_ctl(srv))
        fake.set_state(3)
        _running(srv)
        fake.set_state(6)                                                        # e.g. its field e-stop
        until(lambda: srv.store.match("qm1")["status"] == "review")
        assert srv.field_view(srv.current())["aborted"] is True


def test_a_start_from_the_fields_own_page_is_followed(tmp_path, monkeypatch, fake):
    srv = _server(tmp_path, monkeypatch, _control(fake))
    with TestClient(srv.app) as c:
        _ready(srv, c, fake)
        fake.set_state(3)
        _running(srv)


def test_a_start_with_another_lineup_is_not_followed(tmp_path, monkeypatch, fake):
    srv = _server(tmp_path, monkeypatch, _control(fake))
    with TestClient(srv.app) as c:
        _ready(srv, c, fake)
        srv.store.update_match("qm1", red=[101, 102, 107])                       # lineup edited after loading
        fake.set_state(3)
        until(lambda: srv.field_link.in_match())
        time.sleep(0.1)
        assert srv.store.match("qm1")["status"] == "scheduled"
        assert "running a match" in _blocked(srv)


# ---- stops
def test_stops_are_set_not_toggled(tmp_path, monkeypatch, fake):
    srv = _server(tmp_path, monkeypatch, _control(fake))
    with TestClient(srv.app) as c:
        _ready(srv, c, fake)
        for _ in range(3):
            assert c.post("/api/field/station", json={"station": "R2", "action": "estop"}, headers=_ctl(srv)).status_code == 200
        assert fake.st["R2"]["EStop"] is True
        assert fake.commands[-1] == ("setEStop", {"Station": "R2", "On": True})


def test_stops_work_mid_match_and_the_fields_refusal_to_undo_reaches_control(tmp_path, monkeypatch, fake):
    srv = _server(tmp_path, monkeypatch, _control(fake))
    with TestClient(srv.app) as c:
        _ready(srv, c, fake)
        fake.set_state(3)
        until(lambda: srv.field_link.in_match())
        post = lambda st, act: c.post("/api/field/station", json={"station": st, "action": act}, headers=_ctl(srv))
        assert post("R1", "estop").status_code == 200 and fake.st["R1"]["EStop"] is True
        assert post("R2", "bypass").status_code == 200 and fake.st["R2"]["Bypass"] is True
        r = post("R1", "clear_estop")
        assert r.status_code == 409 and "in progress" in r.json()["detail"] and fake.st["R1"]["EStop"] is True
        r = post("R2", "unbypass")
        assert r.status_code == 409 and fake.st["R2"]["Bypass"] is True
        assert c.post("/api/field/clear_estop", headers=_ctl(srv)).status_code == 409
        assert post("R9", "estop").status_code == 400

        fake.set_state(6)
        until(lambda: not srv.field_link.in_match())
        assert post("R1", "clear_estop").status_code == 200 and fake.st["R1"]["EStop"] is False


def test_empty_stations_can_be_bypassed(tmp_path, monkeypatch, fake):
    srv = _server(tmp_path, monkeypatch, _control(fake))
    srv.store.update_match("qm1", red=[101, 102], blue=[104, 105, 106])
    with TestClient(srv.app) as c:
        _linked(srv)
        assert c.post("/api/field/lineup", json={"key": "qm1"}, headers=_ctl(srv)).status_code == 200
        fake.connect_robots()
        fake.st["R3"]["DsConn"] = None
        fake.push_status()
        until(lambda: _blocked(srv) == ROBOTS_MISSING)
        assert c.post("/api/field/bypass_empty", headers=_ctl(srv)).status_code == 200
        assert fake.st["R3"]["Bypass"] is True
        until(lambda: _blocked(srv) is None)


def test_clearing_the_field_estop_says_so_while_the_button_is_held(tmp_path, monkeypatch, fake):
    srv = _server(tmp_path, monkeypatch, _control(fake))
    with TestClient(srv.app) as c:
        _linked(srv)
        fake.field_estop = fake.field_estop_held = True
        fake.push_status()
        until(lambda: srv.field_link.public()["estop"])
        r = c.post("/api/field/clear_estop", headers=_ctl(srv))
        assert r.status_code == 409 and "Release the button" in r.json()["detail"]
        fake.field_estop_held = False
        assert c.post("/api/field/clear_estop", headers=_ctl(srv)).status_code == 200
        assert fake.field_estop is False


def test_a_stop_with_the_link_down_points_at_the_physical_stops(tmp_path, monkeypatch, fake):
    srv = _server(tmp_path, monkeypatch, _control(fake))
    with TestClient(srv.app) as c:
        _ready(srv, c, fake)
        fake.set_state(3)
        until(lambda: srv.field_link.in_match())
        fake.stop()
        until(lambda: not srv.field_link.public()["link"])
        r = c.post("/api/field/station", json={"station": "R1", "action": "estop"}, headers=_ctl(srv))
        assert r.status_code == 409 and "physical e-stops" in r.json()["detail"]
        r = c.post("/api/field/abort", headers=_ctl(srv))
        assert r.status_code == 409 and "physical e-stops" in r.json()["detail"]


def test_field_settings_are_locked_during_a_match_and_toggle_the_link(tmp_path, monkeypatch, fake):
    srv = _server(tmp_path, monkeypatch, _control(fake))
    with TestClient(srv.app) as c:
        _linked(srv)
        fake.set_state(3)                                                        # no lineup loaded: Watchtower doesn't follow
        until(lambda: srv.field_link.in_match())
        r = c.post("/api/settings", json={"changes": {"field.control.enabled": False}}, headers=_ctl(srv))
        assert r.status_code == 409
        fake.set_state(0)
        until(lambda: not srv.field_link.in_match())
        assert c.post("/api/settings", json={"changes": {"field.control.enabled": False}}, headers=_ctl(srv)).status_code == 200
        until(lambda: not srv.field_link.public()["enabled"])
        r = c.post("/api/match/start", json={"key": "qm1"}, headers=_ctl(srv))   # back to running without the field
        assert r.status_code == 200 and "auto_start" in r.json()


def test_auto_winner_goes_to_the_field(tmp_path, monkeypatch, fake):
    srv = _server(tmp_path, monkeypatch, _control(fake))
    with TestClient(srv.app) as c:
        _linked(srv)
        assert c.post("/api/field/auto_winner", json={"mode": "counted"}, headers=_ctl(srv)).status_code == 200
        assert fake.auto_winner == "counted" and srv.field_link.public()["auto_winner"] == "counted"
        assert c.post("/api/field/auto_winner", json={"mode": "nonsense"}, headers=_ctl(srv)).status_code == 400


def test_hub_lights_from_the_link_decide_first_inactive_without_vision(tmp_path, monkeypatch, fake):
    srv = _server(tmp_path, monkeypatch, _control(fake))
    with TestClient(srv.app) as c:
        _ready(srv, c, fake)
        fake.hub = {"HubShift": "SHIFT1", "RedHubActive": False, "BlueHubActive": True, "InShift": True}   # odd shift: dark = first inactive
        c.post("/api/match/start", json={"key": "qm1"}, headers=_ctl(srv))
        fake.set_state(3)
        _running(srv)
        assert srv.field_first_inactive() == "red"
        until(lambda: srv.store.match("qm1")["fi_locked"])
        assert srv.store.match("qm1")["first_inactive"] == "red"
