"""vision/bioarena.py against a fake bioarena on localhost (protocol v1, spec section 4)."""
import json
import pathlib
import socket
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "vision"))
from bioarena import BioarenaFeed, datagram  # noqa: E402


def test_datagram_shape():
    d = json.loads(datagram("ab12", 7, {"red": 3, "blue": 0}, age_ms=38.9))
    assert d == {"v": 1, "session": "ab12", "seq": 7, "red": 3, "blue": 0, "age_ms": 38}
    assert "age_ms" not in json.loads(datagram("ab12", 8, {"red": 3, "blue": 0}))
    assert len(datagram("x" * 32, 2**40, {"red": 10**6, "blue": 10**6}, 999, "i" * 200)) <= 512


def _recv(fake, timeout=1.0):
    fake.settimeout(timeout)
    data, addr = fake.recvfrom(2048)
    return json.loads(data), addr


def test_feed_against_fake_bioarena():
    fake = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    fake.bind(("127.0.0.1", 0))
    feed = BioarenaFeed("127.0.0.1", fake.getsockname()[1], heartbeat_s=0.1)
    feed.start()
    try:
        hb, addr = _recv(fake)  # heartbeat with nothing counted yet
        assert hb["v"] == 1 and hb["red"] == 0 and hb["blue"] == 0 and "age_ms" not in hb
        fake.sendto(json.dumps({"v": 1, "seq": hb["seq"], "shift": "AUTO"}).encode(), addr)

        t0 = time.monotonic()
        feed.add("red", 2, time.time() - 0.040)  # frame captured 40 ms ago
        d, _ = _recv(fake)
        assert time.monotonic() - t0 < 0.05       # sent on change, not on the next heartbeat
        assert (d["red"], d["blue"]) == (2, 0)
        assert 40 <= d["age_ms"] < 90
        feed.add("blue", 1, time.time())
        feed.add("red", 0, time.time())           # ignored
        feed.add("green", 5, time.time())         # ignored
        d2, _ = _recv(fake)
        assert (d2["red"], d2["blue"]) == (2, 1)  # cumulative, both hubs in one datagram

        seqs, sessions = [d["seq"], d2["seq"]], {hb["session"], d["session"], d2["session"]}
        t0 = time.monotonic()
        while time.monotonic() - t0 < 0.35:       # heartbeats keep counts, no age_ms
            h, _ = _recv(fake)
            assert (h["red"], h["blue"]) == (2, 1) and "age_ms" not in h
            seqs.append(h["seq"])
            sessions.add(h["session"])
        assert len(seqs) >= 4 and seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        assert sessions == {feed.session}

        st = feed.status()
        assert st["sent"] == {"red": 2, "blue": 1}
        assert st["reply"]["shift"] == "AUTO" and st["reply_age"] < 1
    finally:
        feed.stop.set()
        fake.close()


def test_restart_gets_new_session():
    assert BioarenaFeed("127.0.0.1", 9).session != BioarenaFeed("127.0.0.1", 9).session
