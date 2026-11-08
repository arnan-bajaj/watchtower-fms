"""Field control: /control runs a bioarena field over the network, so a match starts and ends once.

bioarena enables the robots and stays its own program on the field network; none of its code belongs
in this MIT repo. This module only talks to it, over the websocket bioarena opens for this:
GET /remote_control/websocket with the X-Remote-Key header, from the one address allowed in bioarena's
Settings -> Watchtower Remote Control (protocol: docs/watchtower-remote-control.md in bioarena).

The field runs each match on its own clock and carries on if this link drops. Its physical e-stops
don't depend on this link, and the buttons here are never the only way to stop a robot.
"""
from __future__ import annotations

import json
import threading
import time

from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

STATIONS = ("R1", "R2", "R3", "B1", "B2", "B3")
WIRE_NAMES = ("Red1", "Red2", "Red3", "Blue1", "Blue2", "Blue3")        # the field's lineup keys, same order
STATES = ("pre", "starting", "warmup", "auto", "pause", "teleop", "post")   # bioarena's MatchState, in order
IN_MATCH = ("starting", "warmup", "auto", "pause", "teleop")
AUTO_WINNERS = ("random", "red", "blue", "counted")
RETRY_S = 2             # between connection attempts
STALE_S = 3             # bioarena sends status twice a second; this long without one and the link is dead
COMMAND_S = 3           # how long the field gets to answer a command
SETTINGS_HINT = "bioarena's Settings → Watchtower Remote Control"


class FieldError(Exception):
    """A command the field refused or didn't answer; the text goes to the operator."""


def lineup(red, blue):
    """Station -> team for a match. A short alliance leaves a station empty (0, as bioarena has it)."""
    return dict(zip(STATIONS, (list(red) + [0, 0, 0])[:3] + (list(blue) + [0, 0, 0])[:3]))


def _wire(teams):
    return {w: teams[s] for w, s in zip(WIRE_NAMES, STATIONS)}


def _station(s, team):
    ds = s.get("DsConn") or {}
    return {"team": team, "ds": bool(ds.get("DsLinked")), "radio": bool(ds.get("RadioLinked")),
            "rio": bool(ds.get("RioLinked")), "robot": bool(ds.get("RobotLinked")),
            "battery": round(ds.get("BatteryVoltage") or 0, 1), "enabled": bool(ds.get("Enabled")),
            "bypass": bool(s.get("Bypass")), "estop": bool(s.get("EStop")), "astop": bool(s.get("AStop")),
            "fault": s.get("EStopFault") or "", "wrong": ds.get("WrongStation") or ""}


class FieldLink:
    """One connection to one field, kept up by run_forever on its own thread. on_state(old, new) runs on
    that thread whenever the field's match state changes, never for the state found on connecting."""

    def __init__(self, on_state=None):
        self.on_state = on_state
        self.cond = threading.Condition()       # guards everything below; commands wait on it
        self.cfg = None                         # {"host", "port", "key"} while field control is on
        self.gen = 0                            # bumped by configure(): the open connection ends itself
        self.closing = False
        self.ws = self.status = self.load = self.clock = self.match_timing = self.state = self.error = None
        self.seen = 0.0
        self.next_id = 0
        self.results: dict = {}                 # command id -> None (waiting) or the field's error text
        self._wake = threading.Event()

    # ---- connection
    def configure(self, c):
        """Apply field.control. Reconnects only if something the connection depends on changed."""
        want = {"host": c["host"], "port": int(c["port"]), "key": c["key"]} if c["enabled"] and c["host"] else None
        with self.cond:
            if want == self.cfg:
                return
            self.cfg, self.error = want, None
            self.gen += 1
            ws = self.ws
        if ws:
            ws.close()
        self._wake.set()

    def close(self):
        with self.cond:
            self.closing = True
        self.configure({"enabled": False, "host": "", "port": 0, "key": ""})
        self._wake.set()

    def run_forever(self):
        while not self.closing:
            with self.cond:
                cfg, gen = self.cfg, self.gen
            if cfg:
                try:
                    self._session(cfg, gen)
                except FieldError as e:
                    self._down(gen, str(e))
                except InvalidStatus as e:
                    code = e.response.status_code
                    self._down(gen, f"the field refused this computer. Check the address and key in {SETTINGS_HINT}"
                               if code == 403 else "this bioarena has no remote control. Update it"
                               if code == 404 else f"the field answered HTTP {code}")
                except Exception as e:                       # noqa: BLE001  never take the FMS down
                    self._down(gen, f"can't reach {cfg['host']}:{cfg['port']} ({type(e).__name__})")
            self._wake.wait(RETRY_S if cfg else None)
            self._wake.clear()

    def _down(self, gen, error):
        with self.cond:
            if gen == self.gen:
                self.error = error
            self.cond.notify_all()

    def _session(self, cfg, gen):
        with connect(f"ws://{cfg['host']}:{cfg['port']}/remote_control/websocket", proxy=None,
                     additional_headers={"X-Remote-Key": cfg["key"]}, open_timeout=4, close_timeout=1) as ws:
            with self.cond:
                if gen != self.gen:
                    return
                self.ws, self.seen, self.error = ws, time.time(), None
            try:
                while True:
                    with self.cond:
                        if gen != self.gen:
                            return
                    try:
                        raw = ws.recv(timeout=1)
                    except TimeoutError:
                        if time.time() - self.seen > STALE_S:
                            raise FieldError("the field stopped answering") from None
                        continue
                    self._message(json.loads(raw))
            finally:
                with self.cond:
                    self.ws = self.status = self.load = self.clock = self.match_timing = self.state = None
                    self.cond.notify_all()

    def _message(self, m):
        kind, data = m.get("type"), m.get("data")
        with self.cond:
            old = self.state
            self.seen = time.time()
            if kind == "arenaStatus":
                self.status = data
            elif kind == "matchTime":
                self.clock = data
            elif kind == "matchLoad":
                self.load = data
            elif kind == "matchTiming":
                self.match_timing = data
            elif kind == "result" and data["Id"] in self.results:
                self.results[data["Id"]] = data["Error"]
            if kind in ("arenaStatus", "matchTime"):
                self.state = STATES[data["MatchState"]]
            new = self.state
            self.cond.notify_all()
        if old is not None and new != old and self.on_state:
            try:
                self.on_state(old, new)
            except Exception as e:                           # noqa: BLE001  a bug here mustn't drop the link
                print(f"[field] state change {old} -> {new} failed: {e!r}")

    # ---- reading (hold self.cond in the _ ones)
    def _up(self):
        return self.ws is not None and self.state is not None and self.status is not None \
            and time.time() - self.seen <= STALE_S

    def _raw(self, station):
        return ((self.status or {}).get("AllianceStations") or {}).get(station) or {}

    def _lineup(self):
        t = (self.load or {}).get("Teams") or {}
        return {s: (t.get(s) or {}).get("Id", 0) for s in STATIONS}

    @property
    def enabled(self):
        with self.cond:
            return self.cfg is not None

    def in_match(self):
        with self.cond:
            return self._up() and self.state in IN_MATCH

    def lineup(self):
        with self.cond:
            return self._lineup()

    def timing(self):
        """(auto, pause, teleop) seconds the field will run, or None before it has said."""
        with self.cond:
            t = self.match_timing
            return (t["AutoDurationSec"], t["PauseDurationSec"], t["TeleopDurationSec"]) if t else None

    def hubs(self):
        """(shift, {"red": lit, "blue": lit}) while the field is in a shift, for game.first_inactive_from_hubs."""
        with self.cond:
            c = self.clock or {}
            if not self._up() or not c.get("InShift"):
                return None
            return c.get("HubShift"), {"red": bool(c.get("RedHubActive")), "blue": bool(c.get("BlueHubActive"))}

    def public(self):
        """What every page may see. Hand-picked fields: bioarena's own messages carry team WPA keys."""
        with self.cond:
            if not self.cfg:
                return {"enabled": False}
            up = self._up()
            out = {"enabled": True, "dest": f"{self.cfg['host']}:{self.cfg['port']}", "link": up,
                   "state": self.state if up else None, "error": self.error}
            if up:
                st, teams = self.status, self._lineup()
                out.update(time=(self.clock or {}).get("MatchTimeSec"), start_problem=st.get("StartProblem") or "",
                           remote_start=bool(st.get("RemoteStartAllowed")),
                           estop=bool(st.get("FieldEStop") or st.get("GpioFieldEStopActive")),
                           access_point=st.get("AccessPointStatus"), switch=st.get("SwitchStatus"),
                           hub_counter=st.get("HubCounterStatus"),
                           auto_winner=(self.load or {}).get("AutoWinnerMode"), lineup=teams,
                           stations={s: _station(self._raw(s), teams[s]) for s in STATIONS})
            return out

    # ---- commands (each blocks until the field answers; call off the event loop)
    def command(self, kind, **args):
        with self.cond:
            ws = self.ws
            if not self._up():
                raise FieldError("The field isn't connected."
                                 + (" Use the physical e-stops." if kind in ("abortMatch", "setEStop") else ""))
            self.next_id += 1
            cid = self.next_id
            self.results[cid] = None
        try:
            ws.send(json.dumps({"type": kind, "data": {"Id": cid, **args}}))
            end = time.monotonic() + COMMAND_S
            with self.cond:
                while self.results[cid] is None:
                    left = end - time.monotonic()
                    if left <= 0 or self.ws is not ws:
                        raise FieldError(f"The field didn't answer ({kind}).")
                    self.cond.wait(left)
                error = self.results[cid]
        except FieldError:
            raise
        except Exception as e:                               # noqa: BLE001
            raise FieldError(f"The link to the field just dropped ({type(e).__name__}).") from None
        finally:
            with self.cond:
                self.results.pop(cid, None)
        if error:
            raise FieldError(error)

    def load_lineup(self, teams, name):
        self.command("loadLineup", Name=name, **_wire(teams))

    def start(self, teams):
        self.command("startMatch", **_wire(teams))

    def abort(self):
        self.command("abortMatch")

    def set_estop(self, station, on):
        self.command("setEStop", Station=station, On=on)

    def set_bypass(self, station, on):
        self.command("setBypass", Station=station, On=on)

    def bypass_empty(self):
        self.command("bypassEmptyStations")

    def clear_field_estop(self):
        self.command("clearFieldEStop")
        if self.public().get("estop"):
            raise FieldError("The field e-stop is still active. Release the button first.")

    def set_auto_winner(self, mode):
        self.command("setAutoWinnerMode", Mode=mode)
