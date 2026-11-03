"""REEFSCAPE (2025): hand-computed expectations for the timeline, scoring, RPs and the hand-scoring API."""
import pytest

from fms import bracket, reefscape
from fms.config import GAME_RULES

G = GAME_RULES["reefscape"]


def test_timeline():
    ps = reefscape.build_periods(G, 1000.0)
    assert [(p.name, p.start, p.end) for p in ps] == [("auto", 1000, 1015), ("teleop", 1018, 1133),
                                                      ("endgame", 1133, 1153)]
    assert ps[-1].end - ps[1].start == 135                       # teleop is 2:15, endgame its last 20 s
    ps = reefscape.build_periods(G, 1000.0, teleop_start=1020.0, first_inactive="red", offset=-1)
    assert [(p.start, p.end) for p in ps] == [(999, 1014), (1019, 1134), (1134, 1154)]
    assert all(p.active == {"red": True, "blue": True} for p in ps)   # no hubs


RED = {"auto_L1": 1, "auto_L4": 1, "auto_processor": 1,
       "teleop_L1": 4, "teleop_L2": 5, "teleop_L3": 5, "teleop_L4": 4, "teleop_processor": 1, "teleop_net": 2}
BLUE = {"auto_L2": 1, "teleop_L1": 5, "teleop_L2": 5, "teleop_L3": 5, "teleop_processor": 2}
ROBOTS = {"red": [{"leave": True, "barge": "deep"}, {"leave": True, "barge": "shallow"}, {"leave": True, "barge": "park"}],
          "blue": [{"leave": True, "barge": "deep"}, {"leave": True}]}         # third blue robot: no entry
FOULS = [{"committed_by": "blue", "kind": "minor"}, {"committed_by": "blue", "kind": "major"},
         {"committed_by": "red", "kind": "minor"}, {"committed_by": "red", "kind": "minor"},
         {"committed_by": "red", "kind": "major", "deleted": 1}]


def test_full_score():
    bd = reefscape.score_match(G, {"red": RED, "blue": BLUE}, ROBOTS, FOULS)
    r, b = bd["red"], bd["blue"]
    # red auto: leave 3x3=9, coral L1 3 + L4 7 = 10, processor 6 -> 25
    assert (r["leave_points"], r["auto_coral_points"], r["auto_points"]) == (9, 10, 25)
    # red teleop: coral 4x2 + 5x3 + 5x4 + 4x5 = 63, algae 6 + 2x4 = 14, barge 12+6+2 = 20 -> 97
    assert (r["teleop_coral_points"], r["barge_points"], r["teleop_points"]) == (63, 20, 97)
    assert (r["coral_points"], r["algae_points"]) == (73, 20)
    assert r["foul_points"] == 8 and b["foul_points"] == 4             # blue minor 2 + major 6; red 2 minors
    assert r["total"] == 130                                           # 25 + 97 + 8
    # blue: leave 2x3 + L2 4 = 10 auto; coral 10+15+20 = 45, processor 2x6 = 12, barge 12 -> 69; +4 -> 83
    assert (b["auto_points"], b["teleop_points"], b["total"]) == (10, 69, 83)
    assert r["coral"] == {"L1": 5, "L2": 5, "L3": 5, "L4": 5} and b["coral"] == {"L1": 5, "L2": 6, "L3": 5, "L4": 0}
    assert r["coop"] == b["coop"] == 1                                 # both put 2 algae in the processor
    assert (r["auto_rp"], r["coral_rp"], r["barge_rp"]) == (True, True, True)
    assert (b["auto_rp"], b["coral_rp"], b["barge_rp"]) == (False, True, False)   # 3 levels is enough with coop
    assert bd["winner"] == "red" and r["rp"] == 6 and b["rp"] == 1


def test_coral_rp_needs_four_levels_without_coopertition():
    bd = reefscape.score_match(G, {"red": RED, "blue": {**BLUE, "teleop_processor": 1}}, ROBOTS, [])
    b = bd["blue"]
    assert b["processor"] == 1 and b["coop"] == 0 and b["coral_levels_met"] == 3 and not b["coral_rp"]
    assert bd["red"]["coral_rp"]                                        # red has all four anyway
    assert b["total"] == 73 and b["rp"] == 0                            # 83 - 6 (one fewer processor algae) - 4 (no fouls)


def test_auto_rp_and_robot_count():
    one = {"red": {"auto_L1": 1}}
    two = {"red": [{"leave": True}, {"leave": True}]}
    assert not reefscape.score_match(G, one, two, [])["red"]["auto_rp"]                       # 3rd robot didn't leave
    assert reefscape.score_match(G, one, two, [], sizes={"red": 2})["red"]["auto_rp"]          # 2-robot alliance
    assert not reefscape.score_match(G, {}, two, [], sizes={"red": 2})["red"]["auto_rp"]       # no auto coral
    bd = reefscape.score_match(G, {"red": {"teleop_L1": -4}}, {"red": [{"barge": "deep"}] * 5}, [])
    assert bd["red"]["total"] == 36 and len(bd["red"]["robots"]) == 3  # negatives clamp to 0; extra robots ignored


def test_barge_rp_threshold_and_playoff():
    at = reefscape.score_match(G, {}, {"red": [{"barge": "deep"}, {"barge": "park"}]}, [])["red"]
    assert at["barge_points"] == 14 and at["barge_rp"]
    below = reefscape.score_match(G, {}, {"red": [{"barge": "deep"}, {"barge": "none"}]}, [])["red"]
    assert below["barge_points"] == 12 and not below["barge_rp"]
    bd = reefscape.score_match(G, {}, {}, [], playoff=True)
    assert bd["winner"] == "tie" and bd["tied_on_points"] and bd["red"]["rp"] == 0
    assert reefscape.score_match(G, {}, {}, [], playoff=True, winner_override="blue")["winner"] == "blue"


def test_rankings_use_reefscape_tiebreakers():
    def bd(coop, total, auto, barge):
        return {"rp": 2, "coop": coop, "total": total, "auto_points": auto, "barge_points": barge}
    ms = [{"status": "committed", "red": [1, 2, 3], "blue": [4, 5, 6], "surrogates": [],
           "breakdown": {"winner": "tie", "red": bd(0, 90, 30, 14), "blue": bd(1, 50, 10, 2)}}]
    rk = bracket.rankings([1, 2, 3, 4, 5, 6], ms, reefscape.TIEBREAKS)
    assert [r["team"] for r in rk] == [4, 5, 6, 1, 2, 3]                # equal RS: avg coop decides first
    assert (rk[0]["avg_coop"], rk[0]["avg_score"], rk[3]["avg_auto"], rk[3]["avg_barge"]) == (1, 50, 30, 14)


def _reef_server(tmp_path, monkeypatch):
    import importlib
    import sys
    import yaml
    from fms import init
    from tests.test_core import _example_config
    d = _example_config(tmp_path)
    init.init(d)
    ev = yaml.safe_load((d / "event.yaml").read_text())
    ev["server"]["db"] = str(tmp_path / "fms.sqlite3")
    ev["event"]["game"] = "reefscape"
    ev["event"]["teams"] = [101, 102, 103, 104, 105, 106]
    ev.pop("game")                                                     # the example's game: is REBUILT's
    (d / "event.yaml").write_text(yaml.safe_dump(ev))
    monkeypatch.setenv("FMS_CONFIG", str(d / "event.yaml"))
    monkeypatch.delitem(sys.modules, "fms.server", raising=False)
    srv = importlib.import_module("fms.server")
    monkeypatch.delitem(sys.modules, "fms.server")
    return srv, d / "event.yaml"


def test_config_rejects_the_other_games_rules(tmp_path, monkeypatch):
    import yaml
    from fms import config
    _, path = _reef_server(tmp_path, monkeypatch)
    cfg = config.load(str(path))
    assert cfg["event"]["game"] == "reefscape" and cfg["game"]["fouls"] == {"minor": 2, "major": 6}
    ev = yaml.safe_load(path.read_text())
    path.write_text(yaml.safe_dump({**ev, "game": {"fouls": {"minor": 5}}}))
    assert config.load(str(path))["game"]["fouls"] == {"minor": 5, "major": 6}   # a REEFSCAPE override is fine
    path.write_text(yaml.safe_dump({**ev, "game": {"n_shifts": 4, "fouls": {"minor": 5}}}))
    with pytest.raises(SystemExit, match="n_shifts"):
        config.load(str(path))
    path.write_text(yaml.safe_dump({**ev, "event": {**ev["event"], "game": "chess"}}))
    with pytest.raises(SystemExit, match="event.game"):
        config.load(str(path))


def test_hand_scoring_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    srv, _ = _reef_server(tmp_path, monkeypatch)
    c = TestClient(srv.app)
    ctl = {"X-FMS-Token": srv.token_for("control")}
    st = c.get("/api/state").json()
    assert st["game_id"] == "reefscape" and st["game"]["leave"] == 3
    assert [k for k, _ in st["rank_cols"]] == ["avg_coop", "avg_score", "avg_auto", "avg_barge"]
    csv = "1,101,102,103,104,105,106\n2,101,102,103,104,105,106"
    c.post("/api/schedule/save", json={"matches": [m for m in c.post("/api/schedule/import", json={"csv": csv},
                                       headers=ctl).json()["matches"]]}, headers=ctl)
    assert c.post("/api/match/start", json={"key": "qm1"}, headers=ctl).status_code == 200
    for a, counts in (("red", RED), ("blue", BLUE)):
        for item, n in counts.items():
            assert c.post("/api/match/count", json={"key": "qm1", "alliance": a, "item": item, "value": n}, headers=ctl).status_code == 200
    for a, rs in ROBOTS.items():
        for i, r in enumerate(rs):
            assert c.post("/api/match/robot", json={"key": "qm1", "alliance": a, "idx": i, **r}, headers=ctl).status_code == 200
    for f in FOULS[:4]:
        c.post("/api/fouls", json={"key": "qm1", **f}, headers=ctl)
    bad = [("/api/match/count", {"alliance": "red", "item": "auto_L5", "value": 1}),
           ("/api/match/count", {"alliance": "red", "item": "auto_L1", "value": -1}),
           ("/api/match/robot", {"alliance": "red", "idx": 3, "leave": True}),
           ("/api/match/robot", {"alliance": "red", "idx": 0, "barge": "high"}),
           ("/api/match/adjust", {"alliance": "red", "period": "auto", "total": 3}),      # REBUILT only
           ("/api/match/climb", {"alliance": "red", "idx": 0, "level": 3}),
           ("/api/match/first_inactive", {"alliance": "red"})]
    for path, body in bad:
        assert c.post(path, json={"key": "qm1", **body}, headers=ctl).status_code == 400, path
    sc = c.get("/api/match/qm1").json()["score"]
    assert (sc["red"]["total"], sc["blue"]["total"], sc["red"]["rp"], sc["blue"]["rp"]) == (130, 83, 6, 1)

    srv.store.update_match("qm1", status="review")                     # what the tick loop does at the end
    assert c.post("/api/match/commit", json={"key": "qm1"}, headers=ctl).json()["next"] == "qm2"
    assert c.post("/api/match/count", json={"key": "qm1", "alliance": "red", "item": "auto_L1", "value": 0}, headers=ctl).status_code == 409
    rk = {r["team"]: r for r in c.get("/api/state").json()["rankings"]}
    assert rk[101]["rp"] == 6 and rk[101]["avg_auto"] == 25 and rk[104]["avg_barge"] == 12 and rk[104]["avg_coop"] == 1
    rows = c.get("/api/export.csv").text.splitlines()
    head, qm1 = rows[0].split(","), rows[1].split(",")
    assert qm1[head.index("red")] == "130" and qm1[head.index("red_teleop_net")] == "2"
    assert qm1[head.index("blue_barge_points")] == "12" and qm1[head.index("blue_rp")] == "1"

    import json
    from fms.tba import TBA, _flat
    from tests.test_core import _Outbox
    ob = _Outbox()
    TBA({"event": {"tba_event_key": "2025test"}, "tba": {"enabled": True, "auth_id": "i", "auth_secret": "s",
                                                         "base_url": "https://x"}}, ob).rankings(srv.ranks(), srv.RULES.TIEBREAKS)
    body = json.loads(ob.rows[0][1])
    assert body["breakdowns"] == ["Ranking Score", "Avg Coop", "Avg Match", "Avg Auto", "Avg Barge"]
    assert {"team_key": "frc101", "Avg Auto": 25, "Avg Barge": 20}.items() <= body["rankings"][0].items()
    flat = _flat(srv.store.match("qm1")["breakdown"]["red"])
    assert (flat["totalPoints"], flat["autoCoralPoints"], flat["endGameBargePoints"], flat["coralBonusAchieved"]) == (130, 10, 20, True)
