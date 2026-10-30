"""Vision side of Setup: calibration (counts per ball), live feed changes, clock offset. Hand-computed."""
import pathlib
import socket
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "vision"))

from scaler import Scaler  # noqa: E402


def test_scaler_divides_and_carries_remainder():
    s = Scaler(8)                    # one ball reads as 8 counts (the 10th Street Showdown camera)
    assert [s.add(n) for n in (3, 4, 1, 8, 2, 14)] == [0, 0, 1, 1, 0, 2]   # raw 3,7,8,16,18,32
    assert (s.raw, s.out) == (32, 4)
    s.set(1)                         # recalibrated: from now on 1:1, no jump for what came before
    assert s.add(2) == 2 and s.out == 6
    s.set(2.5)
    assert [s.add(1) for _ in range(5)] == [0, 0, 1, 0, 1]                  # 1..5 raw / 2.5 = 0.4,0.8,1.2,1.6,2.0
    assert Scaler().add(5) == 5      # default 1: unchanged


def test_sender_applies_setup_feeds_and_scale(monkeypatch):
    import run_vision
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    port = rx.getsockname()[1]
    replies = [{"rev": 1, "feeds": [{"name": "field", "host": "127.0.0.1", "port": port}], "scale": {"red": 8, "blue": 1}},
               {"rev": 2, "feeds": None, "scale": {"red": 8, "blue": 2}},
               {"rev": 3, "feeds": [], "scale": {"red": 8, "blue": 2}}]

    class R:
        def __init__(self, j): self.j = j
        def raise_for_status(self): pass
        def json(self): return self.j
    monkeypatch.setattr(run_vision.requests, "get", lambda *a, **k: R(replies.pop(0)))
    s = run_vision.Sender("http://fms", "k", feeds=[])
    s.apply_config()
    assert s.rev == 1 and s.scalers["red"].scale == 8 and len(s.feeds) == 1
    assert s.feeds[0].dest == ("127.0.0.1", port)
    s.feeds[0].add("red", 3, __import__("time").time())
    rx.settimeout(2)
    got = [rx.recv(2048) for _ in range(5)]          # heartbeats (red 0) may come first
    assert any(b'"red":3' in g for g in got)
    first = s.feeds[0]
    s.apply_config()                 # null: back to the config file's feeds (none here)
    assert s.feeds == [] and first.stop.is_set() and s.scalers["blue"].scale == 2
    s.apply_config()
    assert s.feeds == [] and s.rev == 3
    rx.close()


def test_clock_offset_uses_fastest_round_trip():
    import run_vision
    same = run_vision.Sender("http://fms", "k")
    same._clock_sample(100.0, 100.002, 100.001)       # same computer: offset stays 0
    assert same.offset == 0.0
    s = run_vision.Sender("http://fms", "k")          # FMS laptop's clock is ~5 s ahead
    s._clock_sample(200.0, 200.4, 205.5)               # slow trip: offset 5.3
    s._clock_sample(300.0, 300.02, 305.0)              # fast trip wins: offset 5.0 - 0.01 = 4.99
    assert abs(s.offset - 4.99) < 1e-9
