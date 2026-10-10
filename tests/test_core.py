"""Hand-computed expectations. Run: python -m pytest -q"""
from collections import Counter

from fms import bracket, game, schedule
from fms.tba import TBA
from fms.config import DEFAULTS

G = DEFAULTS["game"]


def test_timeline_default():
    ps = game.build_periods(G, 1000.0, first_inactive="red")
    got = [(p.name, p.start, p.end) for p in ps]
    assert got == [("auto", 1000, 1020), ("transition", 1023, 1033), ("shift1", 1033, 1058),
                   ("shift2", 1058, 1083), ("shift3", 1083, 1108), ("shift4", 1108, 1133),
                   ("endgame", 1133, 1163)]
    assert ps[-1].end - ps[1].start == 140  # teleop is 2:20
    assert [p.active["red"] for p in ps] == [True, True, False, True, False, True, True]
    assert [p.active["blue"] for p in ps] == [True, True, True, False, True, False, True]


def test_offset_and_teleop_click():
    ps = game.build_periods(G, 1000.0, teleop_start=1025.0, first_inactive="blue", offset=-2)
    assert (ps[0].start, ps[0].end) == (998, 1018)
    assert (ps[1].start, ps[1].end) == (1023, 1033)  # clicked 1025, corrected by -2


def test_attribution_and_grace():
    ps = game.build_periods(G, 1000.0, first_inactive="red")
    a = lambda t, h: game.attribute(t, h, ps, 3)
    assert a(1021, "red") == "auto"            # auto grace
    assert a(1023.5, "red") == "transition"
    assert a(1034, "red") == "transition"      # red went inactive at 1033, grace
    assert a(1037, "red") is None              # inactive hub, past grace
    assert a(1037, "blue") == "shift1"
    assert a(1059, "blue") == "shift1"         # blue inactive at 1058, grace
    assert a(1060, "red") == "shift2"
    assert a(1164, "red") == "endgame"         # end grace
    counted, unc = game.tally_fuel([(999, "red", 4), (1166, "red", 4), (1037, "red", 2)], ps, 3)
    assert sum(counted["red"].values()) == 0 and unc["red"] == 2  # before/after window ignored


def test_decide():
    assert game.decide_first_inactive(10, 8) == ("red", False)
    assert game.decide_first_inactive(3, 9) == ("blue", False)
    fi, coin = game.decide_first_inactive(5, 5)
    assert coin and fi in ("red", "blue")


def test_first_inactive_from_hub_lights():
    f = game.first_inactive_from_hubs
    assert f("SHIFT1", {"red": False, "blue": True}) == "red"   # odd shift: first_inactive is dark
    assert f("SHIFT3", {"red": True, "blue": False}) == "blue"
    assert f("SHIFT2", {"red": False, "blue": True}) == "blue"  # even shift: the other one is dark
    assert f("SHIFT4", {"red": True, "blue": False}) == "red"
    assert f("TRANSITION", {"red": True, "blue": False}) is None
    assert f("AUTO", {"red": True, "blue": True}) is None
    assert f("SHIFT1", {"red": True, "blue": True}) is None
    assert f("SHIFT1", {"red": False, "blue": False}) is None
    assert f("SHIFT1", {"red": False}) is None and f("SHIFT1", None) is None
    assert f(None, {"red": False, "blue": True}) is None and f("SHIFTX", {}) is None


def _match_events():
    return [(1005, "red", 10), (1025, "red", 5), (1040, "red", 7), (1070, "red", 40),
            (1120, "red", 30), (1140, "red", 20),
            (1005, "blue", 8), (1040, "blue", 50), (1090, "blue", 60), (1140, "blue", 10)]


def test_full_score():
    ps = game.build_periods(G, 1000.0, first_inactive="red")
    climbs = {"red": [{"auto_l1": True, "level": 3}, {"auto_l1": False, "level": 2},
                      {"auto_l1": False, "level": 0}]}
    fouls = [{"committed_by": "blue", "kind": "minor"}, {"committed_by": "blue", "kind": "major"},
             {"committed_by": "red", "kind": "minor"}, {"committed_by": "red", "kind": "minor"},
             {"committed_by": "red", "kind": "major", "deleted": 1}]
    bd = game.score_match(G, ps, _match_events(), {}, climbs, fouls)
    r, b = bd["red"], bd["blue"]
    assert (r["auto_fuel"], r["teleop_fuel"], r["total_fuel"], r["uncounted_fuel"]) == (10, 95, 105, 7)
    assert (r["auto_tower_points"], r["teleop_tower_points"], r["tower_points"]) == (15, 50, 65)
    assert r["foul_points"] == 20 and b["foul_points"] == 10
    assert (r["auto_points"], r["teleop_points"], r["total"]) == (25, 145, 190)
    assert (b["total_fuel"], b["total"]) == (128, 138)
    assert bd["winner"] == "red"
    assert (r["energized"], r["supercharged"], r["traversal"]) == (True, False, True)
    assert r["rp"] == 5 and b["rp"] == 1


def test_adjust_total_and_playoff():
    ps = game.build_periods(G, 1000.0, first_inactive="red")
    bd = game.score_match(G, ps, _match_events(), {"blue": {"shift3": -5}}, {}, [])
    assert bd["blue"]["fuel"]["shift3"] == 55 and bd["blue"]["total"] == 123
    bd = game.score_match(G, ps, [], {}, {}, [], playoff=True)
    assert bd["winner"] == "tie" and bd["red"]["rp"] == 0
    bd = game.score_match(G, ps, [], {}, {}, [], playoff=True, winner_override="blue")
    assert bd["winner"] == "blue"


def test_schedule_12_teams():
    teams = list(range(101, 113))
    ms, st = schedule.generate(teams, 9, seed=1, time_budget_s=1.5)
    assert len(ms) == 18
    c = Counter(t for m in ms for t in m["red"] + m["blue"])
    assert set(c.values()) == {9}
    assert all(len(set(m["red"] + m["blue"])) == 6 for m in ms)
    assert st["back_to_back"] == sum(len(set(a["red"] + a["blue"]) & set(b["red"] + b["blue"]))
                                     for a, b in zip(ms, ms[1:]))


def test_schedule_surrogates():
    teams = list(range(1, 12))  # 11 teams x 5 = 55 -> 60 slots, 5 surrogates
    ms, _ = schedule.generate(teams, 5, seed=2, time_budget_s=1.0)
    assert len(ms) == 10
    real = Counter(t for m in ms for t in m["red"] + m["blue"] if t not in m["surrogates"])
    sur = sum(len(m["surrogates"]) for m in ms)
    assert set(real.values()) == {5} and sur == 5


def _done(key, red_a, blue_a, winner):
    return {"key": key, "status": "committed", "red_alliance": red_a, "blue_alliance": blue_a,
            "breakdown": {"winner": winner}}


def test_bracket_flow():
    al = [[1, 2, 3], [4, 5, 6], [7, 8, 9], [10, 11, 12]]
    ms = {}
    new = bracket.pending_matches(al, ms)
    assert [(n["key"], n["red_alliance"], n["blue_alliance"]) for n in new] == [("sf1m1", 1, 4), ("sf2m1", 2, 3)]
    ms["sf1m1"] = _done("sf1m1", 1, 4, "blue")   # A4 upsets A1
    ms["sf2m1"] = _done("sf2m1", 2, 3, "red")    # A2 wins
    new = {n["key"]: (n["red_alliance"], n["blue_alliance"]) for n in bracket.pending_matches(al, ms)}
    assert new == {"sf3m1": (1, 3), "sf4m1": (2, 4)}
    ms["sf3m1"] = _done("sf3m1", 1, 3, "red")    # A1 survives
    ms["sf4m1"] = _done("sf4m1", 2, 4, "blue")   # A4 to finals, A2 drops
    new = {n["key"]: (n["red_alliance"], n["blue_alliance"]) for n in bracket.pending_matches(al, ms)}
    assert new == {"sf5m1": (1, 2)}
    ms["sf5m1"] = _done("sf5m1", 1, 2, "blue")   # A2 to finals
    new = bracket.pending_matches(al, ms)
    assert [(n["key"], n["red_alliance"], n["blue_alliance"]) for n in new] == [("f1m1", 4, 2)]
    ms["f1m1"] = _done("f1m1", 4, 2, "red")
    ms["f1m2"] = _done("f1m2", 4, 2, "blue")
    assert [n["key"] for n in bracket.pending_matches(al, ms)] == ["f1m3"]
    ms["f1m3"] = _done("f1m3", 4, 2, "red")
    assert bracket.pending_matches(al, ms) == [] and bracket.champion(ms) == 4


def test_rankings():
    bd = lambda rw, rrp, brp: {"winner": rw, "red": {"rp": rrp, "total": 100, "tower_points": 10, "fuel_points": 90},
                               "blue": {"rp": brp, "total": 80, "tower_points": 0, "fuel_points": 80}}
    ms = [{"status": "committed", "red": [1, 2, 3], "blue": [4, 5, 6], "surrogates": [3],
           "breakdown": bd("red", 4, 0)}]
    rk = {r["team"]: r for r in bracket.rankings([1, 2, 3, 4, 5, 6], ms)}
    assert rk[1]["rp"] == 4 and rk[1]["wins"] == 1 and rk[1]["played"] == 1
    assert rk[3]["played"] == 0          # surrogate doesn't count
    assert rk[4]["losses"] == 1 and rk[4]["rank"] > rk[2]["rank"]


def test_selection_straight():
    order = list(range(1, 13))
    s = bracket.selection(order, [])
    assert s["alliances"] == [[1], [2], [3], [4]] and s["picking"] == 1 and s["round"] == 1
    assert s["available"] == [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
    # round 1 A1..A4, round 2 A4..A1
    s = bracket.selection(order, [5, 6, 7, 8, 9, 10, 11, 12])
    assert s["done"] and s["picking"] is None and s["available"] == []
    assert s["alliances"] == [[1, 5, 12], [2, 6, 11], [3, 7, 10], [4, 8, 9]]
    assert [h["alliance"] for h in s["history"]] == [1, 2, 3, 4, 4, 3, 2, 1]
    assert bracket.selection(order, [5, 6, 7, 8])["picking"] == 4  # A4 picks twice in a row


def test_selection_captain_accepts():
    order = list(range(1, 13))
    s = bracket.selection(order, [2])      # A1 invites A2's captain
    assert s["alliances"] == [[1, 2], [3], [4], [5]] and s["picking"] == 2
    assert s["available"] == [4, 5, 6, 7, 8, 9, 10, 11, 12]
    s = bracket.selection(order, [2, 5])   # new A2 (3) invites the new A4 captain (5)
    assert s["alliances"] == [[1, 2], [3, 5], [4], [6]]
    s = bracket.selection(order, [2, 5, 7, 8, 9, 10, 11, 12])
    assert s["alliances"] == [[1, 2, 12], [3, 5, 11], [4, 7, 10], [6, 8, 9]]
    for bad in ([1], [2, 2], [5, 6, 7, 8, 3], [5, 6, 7, 8, 9, 10, 11, 12, 1]):
        try:
            bracket.selection(order, bad)
            assert False, bad
        except ValueError:
            pass
    try:
        bracket.selection(order[:11], [])
        assert False
    except ValueError:
        pass


class _Outbox:
    def __init__(self):
        self.rows = []

    def outbox_add(self, path, body, dedupe):
        self.rows.append((path, body, dedupe))


def test_tba_takedown_payloads():
    import hashlib
    cfg = {"event": {"tba_event_key": "2026test"},
           "tba": {"enabled": True, "auth_id": "id", "auth_secret": "sec", "base_url": "https://x"}}
    ob = _Outbox()
    t = TBA(cfg, ob)
    t.delete_matches(["qm1"])
    t.delete_all_matches()
    t.clear_rankings()
    assert ob.rows == [
        ("/api/trusted/v1/event/2026test/matches/delete", '["qm1"]', "delete:qm1"),
        ("/api/trusted/v1/event/2026test/matches/delete_all", "2026test", "delete_all"),
        ("/api/trusted/v1/event/2026test/rankings/update", '{"breakdowns":[],"rankings":[]}', "rankings"),
    ]
    sent = {}

    class R:
        status_code = 200

    import fms.tba as tm
    orig = tm.requests.post
    tm.requests.post = lambda url, data, timeout, headers: sent.update(url=url, data=data, h=headers) or R()
    try:
        path, body, _ = ob.rows[1]
        assert t._send({"path": path, "body": body}) == (True, "", False)
    finally:
        tm.requests.post = orig
    assert sent["url"] == "https://x/api/trusted/v1/event/2026test/matches/delete_all"
    assert sent["data"] == b"2026test"
    assert sent["h"]["X-TBA-Auth-Sig"] == hashlib.md5(
        b"sec/api/trusted/v1/event/2026test/matches/delete_all2026test").hexdigest()


def _example_config(tmp_path):
    import pathlib
    import shutil
    root = pathlib.Path(__file__).resolve().parent.parent / "config"
    for f in ("event.example.yaml", "vision.example.yaml"):
        shutil.copy(root / f, tmp_path / f)
    return tmp_path


def test_init_creates_configs_with_random_pins(tmp_path):
    import yaml
    from fms import config, init
    d = _example_config(tmp_path)
    r = init.init(d)
    assert sorted(r["created"]) == [str(d / "event.yaml"), str(d / "vision.yaml")]
    ev = yaml.safe_load((d / "event.yaml").read_text())
    pins = ev["server"]["pins"]
    assert pins == r["pins"] and len(set(pins.values())) == 3
    assert all(len(p) == 6 and p.isdigit() for p in pins.values())
    assert config.PLACEHOLDER not in (d / "event.yaml").read_text()
    assert ev["tba"]["enabled"] is False and ev["event"]["teams"] == []
    cfg = config.load(str(d / "event.yaml"))          # passes validation as generated
    assert cfg["server"]["pins"]["control"] == pins["control"]
    before = (d / "event.yaml").read_text()
    r2 = init.init(d)                                  # never overwrites
    assert r2["created"] == [] and r2["pins"] is None and (d / "event.yaml").read_text() == before


def test_config_refuses_placeholder_and_shared_pins(tmp_path):
    import shutil
    import pytest
    from fms import config
    d = _example_config(tmp_path)
    with pytest.raises(SystemExit, match="not found"):
        config.load(str(d / "event.yaml"))
    shutil.copy(d / "event.example.yaml", d / "event.yaml")
    with pytest.raises(SystemExit, match="control, ref, emcee"):
        config.load(str(d / "event.yaml"))
    text = (d / "event.example.yaml").read_text()
    for role, pin in (("control", "1234"), ("ref", "1234"), ("emcee", "5678")):
        text = text.replace(f'    {role}: "CHANGE-ME"', f'    {role}: "{pin}"')
    (d / "event.yaml").write_text(text)
    with pytest.raises(SystemExit, match="must differ"):
        config.load(str(d / "event.yaml"))
    (d / "event.yaml").write_text(text.replace('    ref: "1234"', '    ref: "4321"'))
    with pytest.raises(SystemExit, match="vision_key"):
        config.load(str(d / "event.yaml"))


def test_vision_key_falls_back_to_event_config(tmp_path):
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "vision"))
    import vconfig
    (tmp_path / "event.yaml").write_text('server: {vision_key: "abc123"}\n')
    (tmp_path / "v.yaml").write_text("fms_url: http://x\nhubs: {}\n")
    assert vconfig.load(tmp_path / "v.yaml")["vision_key"] == "abc123"
    (tmp_path / "v.yaml").write_text('fms_url: http://x\nvision_key: "own"\n')
    assert vconfig.load(tmp_path / "v.yaml")["vision_key"] == "own"


def test_mock_config_inherits_feeds_from_vision_yaml(tmp_path):
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "vision"))
    import vconfig
    (tmp_path / "event.yaml").write_text('server: {vision_key: "k"}\n')
    (tmp_path / "vision.yaml").write_text("feeds:\n  - {name: field, host: 10.0.0.5, port: 8411}\n")
    mock = tmp_path / "vision.mock.yaml"
    mock.write_text("defaults: {counter: mock}\n")
    assert vconfig.load(mock)["feeds"] == [{"name": "field", "host": "10.0.0.5", "port": 8411}]
    mock.write_text("feeds: []\n")                        # explicit off wins
    assert vconfig.load(mock)["feeds"] == []
    mock.write_text("feeds: {host: 10.0.0.9}\n")          # its own feeds win
    assert vconfig.load(mock)["feeds"] == {"host": "10.0.0.9"}
    (tmp_path / "vision.yaml").write_text("hubs: {}\n")   # real config without feeds: none
    mock.write_text("defaults: {counter: mock}\n")
    assert "feeds" not in vconfig.load(mock)
