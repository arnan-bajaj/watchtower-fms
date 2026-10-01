"""Live count feeds: stream hub counts over UDP to field systems that decide the auto winner
and drive the hub lights themselves (e.g. bioarena's "Hub FUEL Counter Feed", protocol v1).

Such a field never takes an auto winner from outside. It decides at its own deadline (bioarena:
T+23.000 s on its match clock) from the counts it has RECEIVED by then. So the way to forward
the auto result is to be the field's counter: send every count the moment it happens.

One datagram carries both hubs, sent on every count and as a heartbeat (default 10 Hz):

  {"v":1,"session":"c1f3a9d2","seq":4821,"red":57,"blue":0,"age_ms":38}

red/blue are cumulative since this process started and never reset; the receiver baselines
them per match. `session` is new on every start so it can tell a restart from a lost packet.
Replies (the field's match state and hub lights) are kept and passed on to the FMS through
the normal vision status, so Watchtower can follow what the lights actually showed.

Any number of destinations, each with its own address, port and source address:
config/vision.yaml `feeds:` and/or `run_vision.py --feed HOST:PORT`. None = off.
"""
from __future__ import annotations

import json
import secrets
import select
import socket
import threading
import time

HUBS = ("red", "blue")
DEFAULT_PORT = 8411  # bioarena's default


def datagram(session, seq, counts, age_ms=None, info=None) -> bytes:
    msg = {"v": 1, "session": session, "seq": seq, "red": counts["red"], "blue": counts["blue"]}
    if age_ms is not None:
        msg["age_ms"] = max(0, int(age_ms))
    if info:
        msg["info"] = info[:64]
    return json.dumps(msg, separators=(",", ":")).encode()


def parse_target(s: str) -> dict:
    """"10.0.100.5:8411" or "10.0.100.5" -> {"host": ..., "port": ...}."""
    host, _, port = s.strip().rpartition(":") if ":" in s else (s.strip(), "", "")
    return {"host": host, "port": int(port) if port else DEFAULT_PORT}


def targets(cfg: dict, cli: list[str] | None = None) -> list[dict]:
    """Feed destinations from config `feeds:` (a list, or one mapping) plus --feed flags.
    Duplicates (same host:port) are dropped."""
    raw = cfg.get("feeds") or []
    if isinstance(raw, dict):
        raw = [raw]
    out, seen = [], set()
    for t in [*raw, *(parse_target(s) for s in cli or [])]:
        t = {"port": DEFAULT_PORT, **t}
        if t.get("enabled", True) and (t["host"], int(t["port"])) not in seen:
            seen.add((t["host"], int(t["port"])))
            out.append(t)
    return out


class CountFeed(threading.Thread):
    def __init__(self, host, port=DEFAULT_PORT, bind="", heartbeat_s=0.1, name=None):
        super().__init__(daemon=True)
        self.dest = (socket.gethostbyname(host), int(port))
        self.name_ = name or f"{host}:{port}"
        self.session = secrets.token_hex(4)
        self.heartbeat_s = float(heartbeat_s)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((bind or "", 0))  # bind: force the source address the receiver allows
        self.sock.setblocking(False)
        self.lock = threading.Lock()
        self.counts = {h: 0 for h in HUBS}
        self.seq = 0
        self.last_sent = 0.0
        self.reply, self.reply_at = None, 0.0
        self.error = None
        self.info = None
        self.stop = threading.Event()

    @classmethod
    def from_target(cls, t: dict):
        return cls(t["host"], t.get("port", DEFAULT_PORT), t.get("bind", ""),
                   t.get("heartbeat_s", 0.1), t.get("name"))

    def add(self, hub, n, t_capture):
        """Call the moment a counter counts. Sends immediately; `t_capture` (time.time() of the
        confirming frame) gives the receiver the camera-to-send latency as age_ms."""
        if hub not in HUBS or n <= 0:
            return
        with self.lock:
            self.counts[hub] += n
            self._send((time.time() - t_capture) * 1000)

    def _send(self, age_ms=None):  # caller holds self.lock
        self.seq += 1
        try:
            self.sock.sendto(datagram(self.session, self.seq, self.counts, age_ms, self.info), self.dest)
            self.error = None
        except OSError as e:  # e.g. field Ethernet unplugged; keep counting, next send catches up
            self.error = str(e)
        self.last_sent = time.monotonic()

    def _recv(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(2048)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:  # ICMP port unreachable etc.: receiver not listening right now
                return
            if addr[0] != self.dest[0]:
                continue
            try:
                r = json.loads(data)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("v") == 1:
                self.reply, self.reply_at = r, time.time()

    def run(self):
        while not self.stop.is_set():
            wait = max(0.0, self.last_sent + self.heartbeat_s - time.monotonic())
            readable, _, _ = select.select([self.sock], [], [], wait)
            if readable:
                self._recv()
            with self.lock:
                if time.monotonic() - self.last_sent >= self.heartbeat_s:
                    self._send()

    def status(self) -> dict:
        """What the FMS gets: our counts, the link state, and the receiver's latest reply."""
        age = time.time() - self.reply_at if self.reply else None
        with self.lock:
            counts = dict(self.counts)
        return {"name": self.name_, "dest": f"{self.dest[0]}:{self.dest[1]}", "session": self.session,
                "sent": counts, "error": self.error, "reply": self.reply,
                "reply_age": None if age is None else round(age, 2)}
