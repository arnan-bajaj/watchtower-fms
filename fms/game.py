"""REBUILT (2026) match timeline + scoring. Pure functions, no I/O.

Every number that comes from the game manual lives in config/event.yaml -> game:.
Nothing in here hardcodes a point value, so a rules update is a config edit.

Core idea: vision only produces timestamped fuel events. Which period a ball
belongs to (and whether it counted at all) is decided HERE from the match
timeline, so the timeline can be re-anchored after the fact (start-click error)
and the score recomputes deterministically.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

ALLIANCES = ("red", "blue")


def other(a: str) -> str:
    return "blue" if a == "red" else "red"


@dataclass
class Period:
    name: str
    start: float
    end: float
    active: dict = field(default_factory=dict)  # {"red": bool, "blue": bool}

    def to_dict(self):
        return {"name": self.name, "start": self.start, "end": self.end, "active": dict(self.active)}


def period_names(g: dict) -> list[str]:
    return ["auto", "transition"] + [f"shift{i + 1}" for i in range(g["n_shifts"])] + ["endgame"]


def build_periods(g: dict, auto_start: float, teleop_start: float | None = None,
                  first_inactive: str | None = None, offset: float = 0.0) -> list[Period]:
    """auto_start/teleop_start are the operator's clicks; offset corrects click error.
    offset < 0 means "I clicked late" (real start was earlier)."""
    both = {"red": True, "blue": True}
    a0 = auto_start + offset
    a1 = a0 + g["auto_s"]
    t0 = (teleop_start + offset) if teleop_start is not None else a1 + g["auto_teleop_gap_s"]
    ps = [Period("auto", a0, a1, dict(both))]
    s = t0 + g["transition_s"]
    ps.append(Period("transition", t0, s, dict(both)))
    for i in range(g["n_shifts"]):
        if first_inactive in ALLIANCES:
            inactive = first_inactive if i % 2 == 0 else other(first_inactive)
            act = {a: a != inactive for a in ALLIANCES}
        else:  # not decided yet (only possible before auto ends)
            act = dict(both)
        ps.append(Period(f"shift{i + 1}", s, s + g["shift_s"], act))
        s += g["shift_s"]
    ps.append(Period("endgame", s, s + g["endgame_s"], dict(both)))
    return ps


def attribute(t: float, alliance: str, periods: list[Period], grace: float) -> str | None:
    """Which period a ball landing at time t counts toward, or None if it doesn't count.
    Half-open intervals [start, end). A ball within `grace` seconds after an alliance's
    hub went inactive still counts toward the period that just ended."""
    for p in periods:
        if p.start <= t < p.end and p.active[alliance]:
            return p.name
    hit = None
    for p in periods:  # periods are chronological; last match = most recent
        if p.active[alliance] and p.end <= t < p.end + grace:
            hit = p.name
    return hit


def tally_fuel(events, periods: list[Period], grace: float):
    """events: iterable of (t, hub, n). Returns (counted[hub][period], uncounted[hub])."""
    counted = {a: {p.name: 0 for p in periods} for a in ALLIANCES}
    uncounted = {a: 0 for a in ALLIANCES}
    lo, hi = periods[0].start, periods[-1].end + grace
    for t, hub, n in events:
        if hub not in ALLIANCES or t < lo or t >= hi:
            continue
        name = attribute(t, hub, periods, grace)
        if name is None:
            uncounted[hub] += n
        else:
            counted[hub][name] += n
    return counted, uncounted


def decide_first_inactive(auto_red: int, auto_blue: int, rng=None):
    """Alliance with MORE auto fuel has its hub INACTIVE in shift 1.
    Returns (first_inactive, was_coin_flip)."""
    if auto_red > auto_blue:
        return "red", False
    if auto_blue > auto_red:
        return "blue", False
    return (rng or random).choice(ALLIANCES), True


def first_inactive_from_hubs(shift: str, active: dict) -> str | None:
    """Read the auto result off live hub lights (e.g. a bioarena status reply): during an odd
    shift the first_inactive alliance is dark, during an even shift the other one is.
    None unless exactly one hub is dark in a shift."""
    if not (isinstance(shift, str) and shift.upper().startswith("SHIFT") and shift[5:].isdigit()):
        return None
    dark = [a for a in ALLIANCES if (active or {}).get(a) is False]
    lit = [a for a in ALLIANCES if (active or {}).get(a) is True]
    if len(dark) != 1 or len(lit) != 1:
        return None
    return dark[0] if int(shift[5:]) % 2 == 1 else other(dark[0])


def score_match(g: dict, periods: list[Period], events, adjust: dict | None,
                climbs: dict | None, fouls, playoff: bool = False,
                winner_override: str | None = None) -> dict:
    """Full breakdown. `fouls` = iterable of dicts with committed_by + kind."""
    adjust = adjust or {}
    climbs = climbs or {}
    names = [p.name for p in periods]
    grace = g["score_grace_s"]
    vision, uncounted = tally_fuel(events, periods, grace)
    fp = g["fuel_points"]
    tower = g["tower"]
    foul_pts = g["fouls"]
    rp_cfg = g["rp"]

    committed = {a: {"minor": 0, "major": 0} for a in ALLIANCES}
    for f in fouls:
        if f.get("deleted"):
            continue
        committed[f["committed_by"]][f["kind"]] += 1

    out = {}
    for a in ALLIANCES:
        adj = {n: int((adjust.get(a) or {}).get(n, 0)) for n in names}
        fuel = {n: max(0, vision[a][n] + adj[n]) for n in names}
        auto_fuel = fuel["auto"]
        teleop_fuel = sum(fuel[n] for n in names if n != "auto")
        total_fuel = auto_fuel + teleop_fuel

        robots = (climbs.get(a) or [])[:3]
        auto_l1 = sum(1 for r in robots if r.get("auto_l1"))
        auto_tower = auto_l1 * g["auto_tower_l1"]
        teleop_tower = sum(tower.get(f"L{int(r.get('level', 0))}", 0) for r in robots)
        tower_pts = auto_tower + teleop_tower

        opp = committed[other(a)]
        fouls_drawn = opp["minor"] * foul_pts["minor"] + opp["major"] * foul_pts["major"]

        auto_pts = auto_fuel * fp + auto_tower
        teleop_pts = teleop_fuel * fp + teleop_tower
        total = auto_pts + teleop_pts + fouls_drawn
        # Two independent sums must agree; catches any future edit that breaks the math.
        assert total == total_fuel * fp + tower_pts + fouls_drawn, "score sum mismatch"

        out[a] = {
            "vision_fuel": dict(vision[a]), "adjust": adj, "fuel": fuel,
            "auto_fuel": auto_fuel, "teleop_fuel": teleop_fuel, "total_fuel": total_fuel,
            "uncounted_fuel": uncounted[a],
            "fuel_points": total_fuel * fp,
            "auto_tower_points": auto_tower, "teleop_tower_points": teleop_tower,
            "tower_points": tower_pts,
            "fouls_committed": dict(committed[a]),
            "foul_points": fouls_drawn,
            "auto_points": auto_pts, "teleop_points": teleop_pts, "total": total,
            "energized": total_fuel >= rp_cfg["energized_fuel"],
            "supercharged": total_fuel >= rp_cfg["supercharged_fuel"],
            "traversal": tower_pts >= rp_cfg["traversal_tower_points"],
        }

    r, b = out["red"]["total"], out["blue"]["total"]
    winner = "red" if r > b else "blue" if b > r else "tie"
    if winner_override in ALLIANCES:
        winner = winner_override
    for a in ALLIANCES:
        o = out[a]
        bonus = int(o["energized"]) + int(o["supercharged"]) + int(o["traversal"])
        if playoff:
            o["rp"] = 0
        else:
            win_rp = rp_cfg["win"] if winner == a else rp_cfg["tie"] if winner == "tie" else 0
            o["rp"] = win_rp + bonus
        o["bonus_rp"] = bonus
    out["winner"] = winner
    out["tied_on_points"] = r == b
    return out
