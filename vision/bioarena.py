"""Feed live hub counts to a bioarena field ("Hub FUEL Counter Feed", protocol v1).

bioarena never takes an auto winner from outside. In its "Counted" auto-winner mode it
decides the winner itself at T+23.000 s on its own match clock, from the counts it has
RECEIVED by then, and lights the hubs from that call. So the way to forward the auto
result is to be bioarena's counter: stream counts fast enough that every auto ball
arrives before T+23.

One UDP datagram carries both hubs, sent on every count and as a 10 Hz heartbeat:

  {"v":1,"session":"c1f3a9d2","seq":4821,"red":57,"blue":0,"age_ms":38}

red/blue are cumulative since this process started and never reset; bioarena baselines
them per match. `session` is new on every start so bioarena can tell a restart from a
lost packet. bioarena replies with its match state; the latest reply is passed on to the
FMS (via the normal vision status) so Watchtower can follow what the hub lights showed.

Off unless config/vision.yaml has a `bioarena:` block.
"""
from __future__ import annotations

import json
import secrets
import select
import socket
import threading
import time

HUBS = ("red", "blue")


def datagram(session, seq, counts, age_ms=None, info=None) -> bytes:
    msg = {"v": 1, "session": session, "seq": seq, "red": counts["red"], "blue": counts["blue"]}
    if age_ms is not None:
        msg["age_ms"] = max(0, int(age_ms))
    if info:
        msg["info"] = info[:64]
    return json.dumps(msg, separators=(",", ":")).encode()


class BioarenaFeed(threading.Thread):
    def __init__(self, host, port=8411, bind="", heartbeat_s=0.1):
        super().__init__(daemon=True)
        self.dest = (socket.gethostbyname(host), int(port))
        self.session = secrets.token_hex(4)
        self.heartbeat_s = heartbeat_s
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((bind or "", 0))  # bind: force the source address bioarena allows
        self.sock.setblocking(False)
        self.lock = threading.Lock()
        self.counts = {h: 0 for h in HUBS}
        self.seq = 0
        self.last_sent = 0.0
        self.reply, self.reply_at = None, 0.0
        self.error = None
        self.info = None
        self.stop = threading.Event()

    def add(self, hub, n, t_capture):
        """Call the moment a counter counts. Sends immediately; `t_capture` (time.time() of the
        confirming frame) gives bioarena the camera-to-send latency as age_ms."""
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
            except OSError:  # ICMP port unreachable etc.: bioarena not listening right now
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
        """What the FMS gets: our counts, the link state, and bioarena's latest reply."""
        age = time.time() - self.reply_at if self.reply else None
        with self.lock:
            counts = dict(self.counts)
        return {"dest": f"{self.dest[0]}:{self.dest[1]}", "session": self.session, "sent": counts,
                "error": self.error, "reply": self.reply,
                "reply_age": None if age is None else round(age, 2)}
