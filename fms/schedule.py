"""Qualification schedule generator.

Structure: concatenate `matches_per_team` random permutations of the team list,
chunk into 6-team matches (3 red, 3 blue). That guarantees equal appearances.
If N*m is not divisible by 6, extra appearances are filled with surrogates.
Then hill-climb with swaps to minimize: back-to-back matches, repeated partners,
repeated opponents, red/blue imbalance.

Hard fact for 12 teams: consecutive matches can only avoid sharing teams if the
same two groups of 6 alternate forever. Any mixing costs some back-to-backs.
The optimizer minimizes them; it cannot make them zero without killing mixing.
"""
from __future__ import annotations

import csv
import io
import itertools
import random
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

W_B2B, W_PARTNER, W_OPP, W_COLOR, W_GAP = 30.0, 3.0, 1.0, 1.0, 0.5


def _chunks(seq):
    return [seq[i:i + 6] for i in range(0, len(seq), 6)]


def _build(teams, m, rng):
    for _ in range(200):
        seq = []
        for _ in range(m):
            p = teams[:]
            rng.shuffle(p)
            seq += [(t, False) for t in p]
        short = (-len(seq)) % 6
        if short:
            tail = {t for t, _ in seq[len(seq) - (6 - short):]}
            pool = [t for t in teams if t not in tail]
            rng.shuffle(pool)
            seq += [(t, True) for t in pool[:short]]
        if all(len({t for t, _ in c}) == 6 for c in _chunks(seq)):
            return seq
    return None


def stats(seq):
    ms = _chunks(seq)
    b2b = 0
    for a, b in zip(ms, ms[1:]):
        b2b += len({t for t, _ in a} & {t for t, _ in b})
    partner, opp, color = Counter(), Counter(), Counter()
    last_seen, gaps = {}, []
    for i, mt in enumerate(ms):
        red, blue = [t for t, _ in mt[:3]], [t for t, _ in mt[3:]]
        for side in (red, blue):
            for x, y in itertools.combinations(sorted(side), 2):
                partner[(x, y)] += 1
        for x in red:
            color[x] += 1
            for y in blue:
                opp[tuple(sorted((x, y)))] += 1
        for x in blue:
            color[x] -= 1
        for t, _ in mt:
            if t in last_seen:
                gaps.append(i - last_seen[t] - 1)
            last_seen[t] = i
    return {
        "back_to_back": b2b,
        "partner_sq": sum(c * c for c in partner.values()),
        "max_partner_repeat": max(partner.values(), default=0),
        "opp_sq": sum(c * c for c in opp.values()),
        "max_opp_repeat": max(opp.values(), default=0),
        "color_sq": sum(c * c for c in color.values()),
        "min_gap": min(gaps, default=0),
        "gap_var": (sum((g - sum(gaps) / len(gaps)) ** 2 for g in gaps) / len(gaps)) if gaps else 0,
    }


def cost(seq):
    s = stats(seq)
    return (W_B2B * s["back_to_back"] + W_PARTNER * s["partner_sq"] + W_OPP * s["opp_sq"]
            + W_COLOR * s["color_sq"] + W_GAP * s["gap_var"])


def _valid(seq, i, j):
    for idx in (i, j):
        c = idx // 6
        ts = [t for t, _ in seq[c * 6:c * 6 + 6]]
        if len(set(ts)) != 6:
            return False
    return True


def generate(teams, matches_per_team, seed=None, time_budget_s=4.0):
    teams = [int(t) for t in teams]
    if len(teams) < 6:
        raise ValueError("need at least 6 teams")
    rng = random.Random(seed)
    best, best_c = None, float("inf")
    deadline = time.time() + time_budget_s
    while time.time() < deadline:
        seq = _build(teams, matches_per_team, rng)
        if seq is None:
            continue
        c = cost(seq)
        for _ in range(1500):
            i, j = rng.randrange(len(seq)), rng.randrange(len(seq))
            if i // 6 == j // 6:
                if (i % 6 < 3) == (j % 6 < 3):
                    continue  # same match, same side: no-op
            seq[i], seq[j] = seq[j], seq[i]
            if _valid(seq, i, j):
                c2 = cost(seq)
                if c2 <= c:
                    c = c2
                    continue
            seq[i], seq[j] = seq[j], seq[i]
        if c < best_c:
            best, best_c = seq[:], c
    return to_matches(best), stats(best)


def to_matches(seq):
    out = []
    for c in _chunks(seq):
        out.append({"red": [t for t, _ in c[:3]], "blue": [t for t, _ in c[3:]],
                    "surrogates": [t for t, s in c if s]})
    return out


def add_times(matches, start_hhmm, cycle_min):
    t = datetime.strptime(start_hhmm, "%H:%M")
    for m in matches:
        m["scheduled"] = t.strftime("%H:%M")
        t += timedelta(minutes=cycle_min)
    return matches


def hhmm(text):
    """'9:5'/'09:05' -> '09:05'; blank -> None (no time). ValueError otherwise."""
    text = (text or "").strip()
    return datetime.strptime(text, "%H:%M").strftime("%H:%M") if text else None


def clock_to_epoch(text, date, utc_offset_hours):
    """'13:05:42' local on `date` (YYYY-MM-DD) -> unix time, using the event's UTC offset
    (not the laptop's timezone). ValueError on a bad time or date."""
    t = datetime.strptime(f"{date} {text.strip()}", "%Y-%m-%d %H:%M:%S")
    return (t - timedelta(hours=utc_offset_hours)).replace(tzinfo=timezone.utc).timestamp()


def retime(matches, from_key, start_hhmm, cycle_min):
    """New times for unplayed matches from `from_key` on (running behind, lunch moved).
    `matches` in play order; played or running ones keep their time and take no slot.
    Returns {key: "HH:MM"}."""
    keys = [m["key"] for m in matches]
    if from_key not in keys:
        raise ValueError(f"No match {from_key}")
    if cycle_min <= 0:
        raise ValueError("Cycle must be more than 0 minutes")
    t = datetime.strptime(start_hhmm, "%H:%M")
    out = {}
    for m in matches[keys.index(from_key):]:
        if m["status"] != "scheduled":
            continue
        out[m["key"]] = t.strftime("%H:%M")
        t += timedelta(minutes=cycle_min)
    return out


def lineup(red, blue, roster):
    """New teams for one match (a robot broke and another team filled in).
    red/blue: 3 slots each like "6059", or "6059*" for a surrogate.
    Returns {"red", "blue", "surrogates"}; ValueError on a bad slot, wrong count, a repeat,
    or a team not in the event's team list."""
    out = {"surrogates": []}
    for a, slots in (("red", red), ("blue", blue)):
        slots = [str(s).strip() for s in slots if str(s).strip()]
        if len(slots) != 3:
            raise ValueError(f"{a.title()} needs 3 teams (got {len(slots)})")
        out[a] = []
        for s in slots:
            if not s.rstrip("*").isdigit():
                raise ValueError(f"{s!r} isn't a team number")
            out[a].append(int(s.rstrip("*")))
            if s.endswith("*"):
                out["surrogates"].append(int(s.rstrip("*")))
    every = out["red"] + out["blue"]
    twice = sorted({t for t in every if every.count(t) > 1})
    if twice:
        raise ValueError(f"In this match twice: {', '.join(map(str, twice))}")
    missing = [t for t in every if t not in roster]
    if missing:
        raise ValueError(f"Not in the event's team list: {', '.join(map(str, missing))} "
                         "(add to event.teams in config/event.yaml and restart)")
    return out


def parse_csv(text):
    """Rows: match,red1,red2,red3,blue1,blue2,blue3[,time]. Header row optional.
    Mark a surrogate with a trailing '*' (e.g. 6059*)."""
    out = []
    for row in csv.reader(io.StringIO(text.strip())):
        row = [c.strip() for c in row if c.strip() != ""]
        if not row or not row[0].isdigit():
            continue
        slots = row[1:7]
        teams = [int(s.rstrip("*")) for s in slots]
        m = {"red": teams[:3], "blue": teams[3:6],
             "surrogates": [int(s.rstrip("*")) for s in slots if s.endswith("*")]}
        if len(row) > 7:
            m["scheduled"] = row[7]
        out.append(m)
    return out
