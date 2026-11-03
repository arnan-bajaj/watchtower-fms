"""REEFSCAPE (2025) match timeline + scoring. Pure functions, no I/O.

Every number comes from config -> game: (fms/config.py GAME_RULES["reefscape"], overridable in
event.yaml). REEFSCAPE has no vision: the scorekeeper types each alliance's counts in review, so
the match's `adjust` holds the counts themselves ({alliance: {item: n}}) and `climbs` holds each
robot's LEAVE and BARGE result ({alliance: [{"leave": bool, "barge": "none|park|shallow|deep"}]}).
"""
from __future__ import annotations

from .game import ALLIANCES, Period, other

LEVELS = ("L1", "L2", "L3", "L4")
ALGAE = ("processor", "net")
# Count items, in the order the review form shows them.
ITEMS = tuple(f"auto_{x}" for x in LEVELS + ALGAE) + tuple(f"teleop_{x}" for x in LEVELS + ALGAE)
BARGE = ("none", "park", "shallow", "deep")
# Rankings after RS: avg coopertition, avg match points, avg AUTO points, avg BARGE points (2025 manual).
TIEBREAKS = (("coop", "coop", "Avg Coop"), ("score", "total", "Avg Match"),
             ("auto", "auto_points", "Avg Auto"), ("barge", "barge_points", "Avg Barge"))


def period_names(g: dict) -> list[str]:
    return ["auto", "teleop", "endgame"]


def build_periods(g: dict, auto_start: float, teleop_start: float | None = None,
                  first_inactive: str | None = None, offset: float = 0.0) -> list[Period]:
    """Same signature as game.build_periods (first_inactive is ignored: there are no hubs).
    Endgame is the last endgame_s of teleop, for the clock label only."""
    both = {"red": True, "blue": True}
    a0 = auto_start + offset
    a1 = a0 + g["auto_s"]
    t0 = (teleop_start + offset) if teleop_start is not None else a1 + g["auto_teleop_gap_s"]
    t1 = t0 + g["teleop_s"]
    e0 = t1 - g["endgame_s"]
    return [Period("auto", a0, a1, dict(both)), Period("teleop", t0, e0, dict(both)),
            Period("endgame", e0, t1, dict(both))]


def blank_robot() -> dict:
    return {"leave": False, "barge": "none"}


def score_match(g: dict, counts: dict | None, robots: dict | None, fouls,
                sizes: dict | None = None, playoff: bool = False,
                winner_override: str | None = None) -> dict:
    """Full breakdown. counts = {alliance: {item: n}}; robots = {alliance: [{leave, barge}]};
    sizes = robots per alliance (teams in the match, default 3); `fouls` like game.score_match."""
    counts = counts or {}
    robots = robots or {}
    rp_cfg = g["rp"]
    committed = {a: {"minor": 0, "major": 0} for a in ALLIANCES}
    for f in fouls:
        if f.get("deleted"):
            continue
        committed[f["committed_by"]][f["kind"]] += 1

    out = {}
    for a in ALLIANCES:
        c = {k: max(0, int((counts.get(a) or {}).get(k, 0) or 0)) for k in ITEMS}
        n = int((sizes or {}).get(a, 3))
        rb = [{**blank_robot(), **r} for r in (robots.get(a) or [])[:n]]
        rb += [blank_robot() for _ in range(n - len(rb))]

        leave_n = sum(1 for r in rb if r["leave"])
        leave_pts = leave_n * g["leave"]
        auto_coral = sum(c[f"auto_{lv}"] * g["coral_auto"][lv] for lv in LEVELS)
        teleop_coral = sum(c[f"teleop_{lv}"] * g["coral_teleop"][lv] for lv in LEVELS)
        auto_algae = sum(c[f"auto_{x}"] * g["algae"][x] for x in ALGAE)
        teleop_algae = sum(c[f"teleop_{x}"] * g["algae"][x] for x in ALGAE)
        barge_pts = sum(g["barge"].get(r["barge"], 0) for r in rb)

        opp = committed[other(a)]
        fouls_drawn = opp["minor"] * g["fouls"]["minor"] + opp["major"] * g["fouls"]["major"]

        auto_pts = leave_pts + auto_coral + auto_algae
        teleop_pts = teleop_coral + teleop_algae + barge_pts
        total = auto_pts + teleop_pts + fouls_drawn
        # Two independent sums must agree; catches any future edit that breaks the math.
        coral_pts = sum(c[f"{p}_{lv}"] * g[f"coral_{p}"][lv] for p in ("auto", "teleop") for lv in LEVELS)
        algae_pts = sum(c[f"{p}_{x}"] * g["algae"][x] for p in ("auto", "teleop") for x in ALGAE)
        assert total == leave_pts + coral_pts + algae_pts + barge_pts + fouls_drawn, "score sum mismatch"

        out[a] = {
            "counts": c, "robots": rb,
            "coral": {lv: c[f"auto_{lv}"] + c[f"teleop_{lv}"] for lv in LEVELS},
            "auto_coral": sum(c[f"auto_{lv}"] for lv in LEVELS),
            "processor": c["auto_processor"] + c["teleop_processor"],
            "leave_points": leave_pts, "auto_coral_points": auto_coral, "teleop_coral_points": teleop_coral,
            "coral_points": coral_pts, "algae_points": algae_pts, "barge_points": barge_pts,
            "fouls_committed": dict(committed[a]), "foul_points": fouls_drawn,
            "auto_points": auto_pts, "teleop_points": teleop_pts, "total": total,
            "auto_rp": n > 0 and leave_n == n and sum(c[f"auto_{lv}"] for lv in LEVELS) >= rp_cfg["auto_coral"],
            "barge_rp": barge_pts >= rp_cfg["barge_points"],
        }

    # Coopertition: both alliances put enough algae in their processors. It lowers the coral RP's level count.
    coop = all(out[a]["processor"] >= rp_cfg["coop_processor"] for a in ALLIANCES)
    for a in ALLIANCES:
        o = out[a]
        o["coop"] = int(coop)
        o["coral_levels_met"] = sum(1 for lv in LEVELS if o["coral"][lv] >= rp_cfg["coral_per_level"])
        o["coral_rp"] = o["coral_levels_met"] >= rp_cfg["coral_levels_coop" if coop else "coral_levels"]

    r, b = out["red"]["total"], out["blue"]["total"]
    winner = "red" if r > b else "blue" if b > r else "tie"
    if winner_override in ALLIANCES:
        winner = winner_override
    for a in ALLIANCES:
        o = out[a]
        bonus = int(o["auto_rp"]) + int(o["coral_rp"]) + int(o["barge_rp"])
        if playoff:
            o["rp"] = 0
        else:
            win_rp = rp_cfg["win"] if winner == a else rp_cfg["tie"] if winner == "tie" else 0
            o["rp"] = win_rp + bonus
        o["bonus_rp"] = bonus
    out["winner"] = winner
    out["tied_on_points"] = r == b
    return out
