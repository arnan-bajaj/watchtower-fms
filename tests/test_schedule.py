"""Qualification schedule generator and quality report. Expected values are hand-computed in the comments."""
from collections import Counter

import pytest

from fms import schedule

# A real 11-team, 17-match schedule (lunch after qm10) that the old generator made, team numbers relabelled
# one-to-one to generic ones (repo rule: no real event's teams in tests). 1110 is the team with the triple.
# Rows are "blue | red".
FIXTURE = """\
1104 1102 1101 | 1103 1105 1107
1110 1111 1104 | 1106 1108 1109
1101 1106 1105 | 1102 1103 1107
1108 1104 1110 | 1111 1101 1109
1102 1107 1111 | 1103 1108 1106
1107 1105 1101 | 1110 1109 1104
1108 1111 1106 | 1102 1105 1103
1111 1107 1104 | 1110 1109 1101
1108 1109 1105 | 1106 1103 1110
1111 1103 1101 | 1102 1106 1104
1110 1109 1107 | 1102 1108 1105
1104 1106 1103 | 1101 1110 1111
1107 1109 1108 | 1105 1111 1102
1102 1101 1106 | 1104 1105 1110
1103 1111 1109 | 1108 1110 1107
1105 1110 1102 | 1106 1104 1107
1109 1105 1111 | 1108 1101 1103"""


def _fixture():
    out = []
    for line in FIXTURE.splitlines():
        blue, red = (list(map(int, h.split())) for h in line.split("|"))
        out.append({"red": red, "blue": blue, "surrogates": []})
    return out


def test_bounds_by_hand():
    assert schedule.sessions(5, [2]) == [0, 0, 1, 1, 1]
    # 11 teams: 12 - 11 = 1 forced repeat per pair; 16 pairs minus the one across lunch = 15
    assert schedule.lower_bound(11, 17, [10]) == 15
    assert schedule.lower_bound(11, 17, []) == 16
    assert schedule.lower_bound(12, 18, [9]) == 0
    # 8 teams: 4 per pair, 17 pairs = 68; triples 18 - 16 = 2 per window, 16 windows = 32
    assert schedule.lower_bound(8, 18, []) == 68 and schedule.triples_min(8, 18, []) == 32
    # windows qm1-3 .. qm8-10 (8) and qm11-13 .. qm16-18 (6) for 6 teams: 6 each = 84
    assert schedule.triples_min(6, 18, [10]) == 84
    assert schedule.triples_min(9, 18, []) == 0
    assert schedule.match_count(11, 9) == 17       # 99 slots -> 17 matches (102 slots, 3 surrogates)
    ms = [{"scheduled": t} for t in ("11:44", "11:52", "13:00", "13:08")]
    assert schedule.breaks_from_times(ms, 8) == [2]
    ms = [{"scheduled": t} for t in ("11:45", "11:52", "12:00")]   # 7.5 min cycle, truncated to minutes
    assert schedule.breaks_from_times(ms, 7.5) == []


def test_fixture_report():
    r = schedule.report(_fixture(), breaks=[10])
    # per-pair repeats: 1,1,1,2,1,1,1,2,2 | (lunch) | 1,1,2,1,2,1 = 20; 1 more across lunch is not counted
    assert (r["back_to_back"], r["lower_bound"], r["excess_back_to_back"]) == (20, 15, 5)
    assert r["triples"] == [{"team": 1110, "matches": ["qm14", "qm15", "qm16"]}]
    assert r["triples_min"] == 0
    assert "1110 plays three in a row: qm14–qm15–qm16" in r["violations"]
    per = {p["team"]: p for p in r["per_team"]}
    assert per[1110]["back_to_back"] == 4 and per[1110]["min_gap"] == 0
    # three teams play 10, unflagged: the fixture never marked its surrogates
    assert sorted(t for t, p in per.items() if p["matches"] == 10) == [1105, 1110, 1111]
    assert sum("no appearance is flagged surrogate" in v for v in r["violations"]) == 3
    # without the break the qm10/qm11 repeat (1102) counts too
    assert schedule.report(_fixture())["back_to_back"] == 21


def test_report_catches_broken_schedules():
    ms = [{"red": [1, 2, 3], "blue": [4, 5, 5], "surrogates": [9]},
          {"red": [1, 2, 3], "blue": [4, 5, 6], "surrogates": [1, 1]}]
    v = schedule.report(ms)["violations"]
    assert "qm1: a team is in it twice" in v
    assert "qm1: surrogate 9 isn't playing in it" in v
    assert "1 is flagged surrogate 2 times; at most once" in v


def _check(ms, r, teams, breaks):
    assert r["violations"] == []
    assert all(len(m["red"]) == 3 and len(m["blue"]) == 3 and len(set(m["red"] + m["blue"])) == 6 for m in ms)
    c = Counter(t for m in ms for t in m["red"] + m["blue"])
    assert set(c) == set(teams) and max(c.values()) - min(c.values()) <= 1
    extra = (6 * len(ms)) % len(teams)
    sur = [t for m in ms for t in m["surrogates"]]
    assert len(sur) == extra == len(set(sur))
    assert all(c[t] == max(c.values()) for t in sur)
    assert r["back_to_back"] == r["lower_bound"]
    assert len(r["triples"]) == r["triples_min"]


def test_generate_11_teams_17_matches_lunch_after_10():
    teams = list(range(1101, 1112))
    ms, r = schedule.generate(teams, matches=17, breaks=[10], seed=1)
    _check(ms, r, teams, [10])
    assert r["back_to_back"] == 15
    assert r["max_team_back_to_back"] <= 2 and r["triples"] == []
    # 15 b2bs over 11 teams, spread evenly: 4 teams at 2, 7 at 1
    assert sorted(Counter(p["back_to_back"] for p in r["per_team"]).items()) == [(1, 7), (2, 4)]
    sur = [t for m in ms for t in m["surrogates"]]
    assert len(sur) == 3 and len(set(sur)) == 3
    # the surrogate appearance is the team's third match (FRC convention)
    for t in sur:
        mine = [i for i, m in enumerate(ms) if t in m["red"] + m["blue"]]
        assert t in ms[mine[2]]["surrogates"]


# Below 9 teams some triples are forced (at least 18 - 2N teams play all of any 3 consecutive matches), so
# for 6 and 8 the "no three in a row" rule can't hold; the generator reaches that minimum instead.
# 12 teams: the bound is 0, which forces each session to alternate the same two groups of 6.
@pytest.mark.parametrize("n", [6, 8, 11, 12, 18, 24, 40])
@pytest.mark.parametrize("with_break", [False, True])
def test_generate_hits_lower_bound(n, with_break):
    teams = list(range(1001, 1001 + n))
    m = schedule.match_count(n, 8)
    breaks = [m // 2] if with_break else []
    ms, r = schedule.generate(teams, 8, breaks=breaks, seed=7)
    assert len(ms) == m
    _check(ms, r, teams, breaks)


def test_same_seed_same_schedule():
    teams = list(range(1101, 1115))
    a = schedule.generate(teams, 6, breaks=[7], seed="abc")
    assert a == schedule.generate(teams, 6, breaks=[7], seed="abc")
    assert a[0] != schedule.generate(teams, 6, breaks=[7], seed="abd")[0]
    assert a[1]["seed"] == "abc"


def test_schedule_preview_save_and_report(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from tests.test_core import _server
    srv, _ = _server(tmp_path, monkeypatch)
    srv.CFG["event"]["teams"] = list(range(1101, 1112))
    c = TestClient(srv.app)
    ctl = {"X-FMS-Token": srv.token_for("control")}
    # 11 teams x 9 = 17 matches at 8 min from 10:00: qm1-qm10 end 11:20; qm11 (11:20+8 > 11:25) waits for 12:00
    body = {"matches_per_team": 9, "start": "10:00", "cycle_min": 8, "lunch": "11:25", "lunch_end": "12:00",
            "after_lunch": True, "seed": "42"}
    p = c.post("/api/schedule/preview", json=body, headers=ctl).json()
    assert (p["seed"], p["breaks"], p["matches"][10]["scheduled"]) == ("42", [10], "12:00")
    assert (p["report"]["back_to_back"], p["report"]["lower_bound"], p["report"]["violations"]) == (15, 15, [])
    assert c.post("/api/schedule/preview", json=body, headers=ctl).json()["matches"] == p["matches"]
    assert c.post("/api/schedule/preview", json={**body, "seed": ""}, headers=ctl).json()["seed"].isdigit()

    c.post("/api/schedule/save", json={"matches": p["matches"], "seed": p["seed"], "breaks": p["breaks"],
                                       "matches_per_team": 9}, headers=ctl)
    assert srv.store.get("schedule_meta")["seed"] == "42"
    r = c.get("/api/schedule/report", headers=ctl).json()
    assert (r["back_to_back"], r["seed"], r["surrogates"]) == (15, "42", 3)

    rows = c.get("/api/export.csv").text.splitlines()
    assert rows[0].split(",")[-1] == "surrogates"
    sur = [row.split(",")[-1] for row in rows[1:]]
    assert sorted(int(t) for s in sur for t in s.split()) == sorted(t for m in p["matches"] for t in m["surrogates"])
    # TBA schedule push carries them as frc keys in alliances.*.surrogates
    m = next(m for m in srv.store.matches("qm") if m["surrogates"])
    d = srv.tba.match(m)
    a = "red" if m["surrogates"][0] in m["red"] else "blue"
    assert f"frc{m['surrogates'][0]}" in d["alliances"][a]["surrogates"]
