"""4-alliance double elimination + best-of-3 finals, and qualification rankings.

Bracket (TBA keys):
  sf1m1  A1 vs A4                  (upper)
  sf2m1  A2 vs A3                  (upper)
  sf3m1  L(sf1) vs L(sf2)          (lower, loser out)
  sf4m1  W(sf1) vs W(sf2)          (upper final)
  sf5m1  L(sf4) vs W(sf3)          (lower final, loser out)
  f1m1.. W(sf4) vs W(sf5)          (first to 2 wins)
Red = better (lower-numbered) alliance, except finals where red = upper-bracket winner.
"""
from __future__ import annotations

SPEC = [
    ("sf1m1", ("seed", 1), ("seed", 4)),
    ("sf2m1", ("seed", 2), ("seed", 3)),
    ("sf3m1", ("L", "sf1m1"), ("L", "sf2m1")),
    ("sf4m1", ("W", "sf1m1"), ("W", "sf2m1")),
    ("sf5m1", ("L", "sf4m1"), ("W", "sf3m1")),
]


def _result(matches, key):
    m = matches.get(key)
    if not m or m["status"] != "committed" or not m.get("breakdown"):
        return None
    w = m["breakdown"]["winner"]
    if w not in ("red", "blue"):
        return None
    lose = "blue" if w == "red" else "red"
    return m[f"{w}_alliance"], m[f"{lose}_alliance"]


def _resolve(src, matches):
    kind, v = src
    if kind == "seed":
        return v
    r = _result(matches, v)
    if r is None:
        return None
    return r[0] if kind == "W" else r[1]


def pending_matches(alliances, matches):
    """Return new playoff match dicts that can now be created. `matches` = {key: row}."""
    new = []
    for key, s1, s2 in SPEC:
        if key in matches:
            continue
        a, b = _resolve(s1, matches), _resolve(s2, matches)
        if a is None or b is None:
            continue
        red, blue = (a, b) if a < b else (b, a)
        new.append(_mk("sf", int(key[2:key.index("m")]), 1, red, blue, alliances))
    # finals
    fa, fb = _resolve(("W", "sf4m1"), matches), _resolve(("W", "sf5m1"), matches)
    if fa is not None and fb is not None:
        fkeys = sorted((k for k in matches if k.startswith("f1m")), key=lambda k: int(k[3:]))
        wins = {fa: 0, fb: 0}
        all_done = True
        for k in fkeys:
            r = _result(matches, k)
            if r is None:
                all_done = False
                break
            wins[r[0]] += 1
        if all_done and max(wins.values()) < 2:
            new.append(_mk("f", 1, len(fkeys) + 1, fa, fb, alliances))
    return new


def champion(matches):
    fkeys = [k for k in matches if k.startswith("f1m")]
    wins = {}
    for k in fkeys:
        r = _result(matches, k)
        if r:
            wins[r[0]] = wins.get(r[0], 0) + 1
    for a, w in wins.items():
        if w >= 2:
            return a
    return None


def _mk(level, set_no, num, red_a, blue_a, alliances):
    return {"key": f"{level}{set_no}m{num}", "comp_level": level, "set_number": set_no,
            "match_number": num, "red": alliances[red_a - 1], "blue": alliances[blue_a - 1],
            "red_alliance": red_a, "blue_alliance": blue_a, "surrogates": []}


# ---------------------------------------------------------------- rankings
def rankings(teams, qual_matches):
    rows = {t: {"team": t, "played": 0, "wins": 0, "losses": 0, "ties": 0, "rp": 0,
                "score": 0, "tower": 0, "fuel": 0} for t in teams}
    for m in qual_matches:
        if m["status"] != "committed" or not m.get("breakdown"):
            continue
        bd = m["breakdown"]
        sur = set(m.get("surrogates") or [])
        for a in ("red", "blue"):
            for t in m[a]:
                if t in sur or t not in rows:
                    continue
                r = rows[t]
                r["played"] += 1
                r["rp"] += bd[a]["rp"]
                r["score"] += bd[a]["total"]
                r["tower"] += bd[a]["tower_points"]
                r["fuel"] += bd[a]["fuel_points"]
                if bd["winner"] == a:
                    r["wins"] += 1
                elif bd["winner"] == "tie":
                    r["ties"] += 1
                else:
                    r["losses"] += 1
    out = []
    for r in rows.values():
        p = r["played"]
        r["rs"] = round(r["rp"] / p, 3) if p else 0.0
        r["avg_score"] = round(r["score"] / p, 2) if p else 0.0
        r["avg_tower"] = round(r["tower"] / p, 2) if p else 0.0
        r["avg_fuel"] = round(r["fuel"] / p, 2) if p else 0.0
        out.append(r)
    # Tiebreakers: RS, avg match score, avg tower, avg fuel, then team number
    # (FRC uses a random final tiebreaker; team number keeps it deterministic).
    out.sort(key=lambda r: (-r["rs"], -r["avg_score"], -r["avg_tower"], -r["avg_fuel"], r["team"]))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return out
