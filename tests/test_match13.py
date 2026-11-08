"""Match13 predictions (fms/match13.py) and GET /api/predict. Hand-computed values; no network."""
import pytest
import requests
from fastapi.testclient import TestClient

from fms import match13, settings
from tests.test_settings import _server

M = {"key": "qm7", "comp_level": "qm", "set_number": 1, "match_number": 7,
     "red": [101, 102, 103], "blue": [104, 105, 106]}
# xp sums: red 30 + 20 + 10 = 60, blue 25 + 15 + 10 = 50; xVar sum 40 + 30 + 20 + 10 + 0 + 0 = 100 (sd 10)
RATED = {101: (30, 40), 102: (20, 30), 103: (10, 20), 104: (25, 10), 105: (15, 0), 106: (10, 0)}
PHI_1 = 0.8413447460685429      # P(Z < 1)


class R:
    def __init__(self, code, body=None, headers=None):
        self.status_code, self._body, self.headers = code, body, headers or {}

    def json(self):
        return self._body


def fake(monkeypatch, answer):
    """Patch requests.get; answer(path) -> R. Returns the list of paths asked for."""
    asked = []

    def get(url, timeout, headers):
        assert url.startswith(match13.BASE) and headers == {"Authorization": "Bearer m13_live_test"}
        path = url[len(match13.BASE):]
        asked.append(path)
        return answer(path)
    monkeypatch.setattr(match13.requests, "get", get)
    return asked


def ratings(path, rated=RATED):
    if path.startswith("/v1/teams/"):
        t = int(path.split("/")[3])
        return R(200, {"teamNumber": t, "xp": rated[t][0], "xVar": rated[t][1]}) if t in rated else R(404)
    return R(404)


def client(game="rebuilt", key="m13_live_test"):
    return match13.Match13({"match13": {"key": key}}, game)


def test_win_chance_and_from_ratings():
    assert match13.win_chance(10, 100) == pytest.approx(PHI_1)          # one sd ahead
    assert match13.win_chance(-10, 100) == pytest.approx(1 - PHI_1)
    assert match13.win_chance(0, 50) == 0.5
    assert (match13.win_chance(3, 0), match13.win_chance(-3, 0), match13.win_chance(0, 0)) == (1.0, 0.0, 0.5)
    p = match13.from_ratings([{"xp": 30, "xVar": 40}, {"xp": 20, "xVar": 30}, {"xp": 10, "xVar": 20}],
                             [{"xp": 25, "xVar": 10}, {"xp": 15, "xVar": 0}, {"xp": 10, "xVar": 0}])
    assert p == {"red": 60, "blue": 50, "win": pytest.approx(PHI_1)}


def test_match13_forecast_when_it_holds_the_match(monkeypatch):
    pred = {"redScore": 94.4, "blueScore": 37.3, "winProb": 0.98, "redRp1": 0.81, "redRp2": 0.12, "redRp3": 0.44,
            "blueRp1": 0.24, "blueRp2": 0.0, "blueRp3": 0.1}
    teams = {str(t): {"xpPre": 1} for t in RATED}
    asked = fake(monkeypatch, lambda p: R(200, {"key": "2026test_qm7", "pred": pred, "teams": teams})
                 if p == "/v1/matches/2026test_qm7" else R(404))
    out = client().predict(M, "2026test")
    assert out == {"key": "qm7", "source": "match13", "red": 94.4, "blue": 37.3, "win": 0.98, "missing": [],
                   "error": None, "rp": {"red": [0.81, 0.12, 0.44], "blue": [0.24, 0.0, 0.1]}}
    assert asked == ["/v1/matches/2026test_qm7"]                      # no team ratings needed


def test_playoff_key_is_tbas(monkeypatch):
    asked = fake(monkeypatch, ratings)
    client().predict({**M, "key": "sf3m1", "comp_level": "sf", "set_number": 3, "match_number": 1}, "2026test")
    assert asked[0] == "/v1/matches/2026test_sf3m1"


def test_xp_fallback_when_match13_has_no_forecast(monkeypatch):
    asked = fake(monkeypatch, ratings)
    out = client().predict(M, "2026test")
    assert out["source"] == "xp" and (out["red"], out["blue"]) == (60, 50) and out["win"] == pytest.approx(PHI_1)
    assert asked == ["/v1/matches/2026test_qm7"] + [f"/v1/teams/{t}/years/2026" for t in RATED]


def test_replacement_robot_falls_back_to_xp(monkeypatch):
    # Match13 holds qm7 with 199 where this event now has 106: its forecast is for other robots.
    teams = {str(t): {} for t in (101, 102, 103, 104, 105, 199)}
    fake(monkeypatch, lambda p: R(200, {"pred": {"redScore": 1, "blueScore": 2, "winProb": 0.1}, "teams": teams})
         if p.startswith("/v1/matches/") else ratings(p))
    out = client().predict(M, "2026test")
    assert out["source"] == "xp" and (out["red"], out["blue"]) == (60, 50)


def test_no_event_key_or_another_season_skips_the_forecast(monkeypatch):
    asked = fake(monkeypatch, lambda p: ratings(p, {t: (x, v) for t, (x, v) in RATED.items()}))
    client().predict(M, "")
    assert not any(p.startswith("/v1/matches/") for p in asked)
    asked.clear()
    # A REEFSCAPE event played in 2026 rates teams on their 2025 season, and 2026's forecasts are for REBUILT.
    client("reefscape").predict(M, "2026test")
    assert asked == [f"/v1/teams/{t}/years/2025" for t in RATED]


def test_unrated_team_counts_as_zero_and_is_named(monkeypatch):
    fake(monkeypatch, lambda p: ratings(p, {t: r for t, r in RATED.items() if t != 104}))
    out = client().predict(M, "")
    # blue 15 + 10 = 25; variance 100 - 10 = 90
    assert (out["red"], out["blue"], out["missing"]) == (60, 25, [104])
    assert out["win"] == pytest.approx(match13.win_chance(35, 90))
    fake(monkeypatch, lambda p: R(404))
    out = client().predict(M, "")
    assert out["source"] is None and "no 2026 ratings" in out["error"]


def test_ratings_are_cached_and_a_new_key_starts_over(monkeypatch):
    asked = fake(monkeypatch, ratings)
    c = client()
    c.predict(M, "")
    c.predict(M, "")
    assert len(asked) == 6                                              # TEAM_TTL keeps all six
    c.cfg["match13"]["key"] = "m13_live_other"
    monkeypatch.setattr(match13.requests, "get",
                        lambda url, timeout, headers: asked.append(url) or ratings(url[len(match13.BASE):]))
    c.predict(M, "")
    assert len(asked) == 12


def test_failures_pause_asking(monkeypatch):
    asked = fake(monkeypatch, lambda p: R(401))
    c = client()
    assert c.predict(M, "")["error"] == "Match13 didn't accept the key (Setup → Match13)"
    assert c.predict(M, "")["error"] == "Match13 didn't accept the key (Setup → Match13)"
    assert len(asked) == 1                                              # DOWN_S: not asked again

    asked = fake(monkeypatch, lambda p: R(429, headers={"Retry-After": "17"}))
    c = client()
    assert c.predict(M, "")["error"] == "Match13's rate limit: asking again in 17 s"
    assert c._down[0] - match13.time.time() == pytest.approx(17, abs=1)

    def offline(url, timeout, headers):
        raise requests.ConnectionError("no route")
    monkeypatch.setattr(match13.requests, "get", offline)
    assert client().predict(M, "")["error"] == "Match13 can't be reached (no internet here?)"


def test_key_is_secret_and_stays_on_this_laptop():
    assert "match13.key" in settings.SECRET
    e = settings.stamp(settings.clean({"match13.key": "m13_live_x", "event.name": "E"}), 1.0)
    assert set(settings.shareable(e)) == {"event.name"}


def test_predict_endpoint_is_control_only_and_off_the_state(tmp_path, monkeypatch):
    srv, _ = _server(tmp_path, monkeypatch)
    c = TestClient(srv.app)
    ctl = {"X-FMS-Token": srv.token_for("control")}
    srv.store.insert_match({**M, "ord": 1, "status": "scheduled"})
    srv.store.set("current", "qm7")
    assert c.get("/api/predict", headers=ctl).json() == {"enabled": False}       # no key yet

    assert c.post("/api/settings", headers=ctl, json={"changes": {"match13.key": "m13_live_test"}}).status_code == 200
    assert c.get("/api/settings", headers=ctl).json()["settings"]["match13.key"]["value"] is True   # never the key
    fake(monkeypatch, ratings)
    assert c.get("/api/predict").status_code == 401
    assert c.get("/api/predict", headers={"X-FMS-Token": srv.token_for("ref")}).status_code == 401
    j = c.get("/api/predict", headers=ctl).json()
    assert (j["enabled"], j["source"], j["key"], j["red"], j["blue"]) == (True, "xp", "qm7", 60, 50)
    assert c.get("/api/predict?key=qm7", headers=ctl).json()["red"] == 60
    assert c.get("/api/predict?key=qm99", headers=ctl).status_code == 404
    st = str(c.get("/api/state").json())
    assert "m13_live" not in st and "match13" not in st
