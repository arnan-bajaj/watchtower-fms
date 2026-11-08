"""Match13 predictions for /control (match13.com: an independent FRC stats site, not FIRST's).

Read only, and only from the server: the API sends no CORS headers, and the key (Setup -> Match13) stays off
browsers. A prediction goes to /control alone (GET /api/predict), never into the state every page gets, so
the field TV, the overlay and the phones never show one.

Two sources, best first:
- "match13": Match13's own forecast, when it holds this match (TBA event key + our match key, which is TBA's)
  with the same six teams and the event key's year is the game's season.
- "xp": each team's season rating. An alliance's expected score is the sum of its teams' `xp`, its variance
  the sum of their `xVar`, and red's chance is P(red - blue > 0) for independent normals. Match13's own model
  does more than this; it's the fallback for what it hasn't forecast (an offseason schedule, our bracket,
  replacement robots).

Match13 allows 60 requests a minute, 1000 an hour and 30000 a week per account, so team ratings are kept for
TEAM_TTL, a forecast (or its 404) for MATCH_TTL, and after a failure nothing is asked for DOWN_S (or a 429's
Retry-After). Pure maths in win_chance / from_ratings; Match13 does the HTTP.
"""
from __future__ import annotations

import math
import threading
import time

import requests

BASE = "https://actions.match13.com"
SEASON = {"rebuilt": 2026, "reefscape": 2025}   # the season whose ratings fit each game
TEAM_TTL = 3600
MATCH_TTL = 60
DOWN_S = 60
TIMEOUT_S = 5


class Unavailable(Exception):
    """Match13 can't be asked right now; the message says why."""


def win_chance(diff, var):
    """P(X > 0) for X ~ Normal(diff, var)."""
    if var <= 0:
        return 1.0 if diff > 0 else 0.0 if diff < 0 else 0.5
    return 0.5 * (1 + math.erf(diff / math.sqrt(2 * var)))


def from_ratings(red, blue):
    """red, blue: one {xp, xVar} per rated team -> {"red", "blue": expected scores, "win": red's chance}."""
    r, b = (sum(t.get("xp") or 0 for t in a) for a in (red, blue))
    var = sum(t.get("xVar") or 0 for t in red + blue)
    return {"red": r, "blue": b, "win": win_chance(r - b, var)}


class Match13:
    def __init__(self, cfg, game_id):
        self.cfg = cfg                      # the live CFG: Setup can change the key without a restart
        self.season = SEASON[game_id]
        self._lock = threading.Lock()
        self._cache = {}                    # path -> (time, JSON or None for a 404)
        self._down = (0.0, "")              # (until, why)
        self._for = None                    # the key the cache and _down belong to

    @property
    def key(self):
        return (self.cfg.get("match13") or {}).get("key") or ""

    @property
    def enabled(self):
        return bool(self.key)

    def _fail(self, wait, why):
        self._down = (time.time() + wait, why)
        raise Unavailable(why)

    def _get(self, path, ttl):
        """The JSON at path, or None for a 404 (Match13 holds nothing there). Raises Unavailable."""
        now = time.time()
        hit = self._cache.get(path)
        if hit and now - hit[0] < ttl:
            return hit[1]
        if now < self._down[0]:
            raise Unavailable(self._down[1])
        try:
            r = requests.get(BASE + path, timeout=TIMEOUT_S, headers={"Authorization": f"Bearer {self.key}"})
        except requests.RequestException:
            self._fail(DOWN_S, "Match13 can't be reached (no internet here?)")
        if r.status_code == 401:
            self._fail(DOWN_S, "Match13 didn't accept the key (Setup → Match13)")
        if r.status_code == 429:
            try:
                wait = max(1, int(r.headers.get("Retry-After") or DOWN_S))
            except ValueError:
                wait = DOWN_S
            self._fail(wait, f"Match13's rate limit: asking again in {wait} s")
        if r.status_code not in (200, 404):
            self._fail(DOWN_S, f"Match13 answered {r.status_code}")
        try:
            val = r.json() if r.status_code == 200 else None
        except ValueError:
            self._fail(DOWN_S, "Match13 sent something that isn't JSON")
        self._cache[path] = (now, val)
        return val

    def _forecast(self, m, event_key, teams):
        """Match13's own forecast for m, or None if it doesn't hold this match with these teams."""
        if not (event_key[:4].isdigit() and int(event_key[:4]) == self.season):
            return None
        j = self._get(f"/v1/matches/{event_key}_{m['key']}", MATCH_TTL)
        if not j or j.get("unplayed") or not j.get("pred"):
            return None
        if {int(t) for t in j.get("teams") or {}} != set(teams["red"] + teams["blue"]):
            return None                     # a replacement robot, or a schedule that differs from TBA's
        p = j["pred"]
        return {"red": p["redScore"], "blue": p["blueScore"], "win": p["winProb"],
                "rp": {a: [p.get(f"{a}Rp{i}") for i in (1, 2, 3)] for a in ("red", "blue")}}

    def predict(self, m, event_key=""):
        """{"key", "source": "match13" | "xp" | None, "red", "blue", "win" (red's chance), "rp" (match13 only),
        "missing": teams with no rating (counted as 0), "error"} for match m."""
        teams = {a: [int(t) for t in m[a]] for a in ("red", "blue")}
        out = {"key": m["key"], "source": None, "missing": [], "error": None}
        with self._lock:
            if self._for != self.key:
                self._for, self._cache, self._down = self.key, {}, (0.0, "")
            try:
                f = self._forecast(m, event_key or "", teams)
                if f:
                    return {**out, **f, "source": "match13"}
                rated = {a: [self._get(f"/v1/teams/{t}/years/{self.season}", TEAM_TTL) for t in teams[a]]
                         for a in ("red", "blue")}
            except Unavailable as e:
                return {**out, "error": str(e)}
        out["missing"] = [t for a in ("red", "blue") for t, x in zip(teams[a], rated[a]) if not x]
        if len(out["missing"]) == len(teams["red"]) + len(teams["blue"]):
            return {**out, "error": f"Match13 has no {self.season} ratings for these teams"}
        p = from_ratings([x for x in rated["red"] if x], [x for x in rated["blue"] if x])
        return {**out, **p, "source": "xp"}
