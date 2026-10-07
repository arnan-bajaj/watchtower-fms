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
from datetime import datetime, timedelta

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


def hm(s):
    """'9:30' / '09:30' -> datetime on a dummy day. ValueError on anything else."""
    return datetime.strptime(str(s).strip(), "%H:%M")


def add_times(matches, start_hhmm, cycle_min, lunch=None):
    """Give each match a start time, one cycle apart. lunch=("12:00", "13:00") is a break no match's cycle may
    overlap: the first match that would run into it starts at lunch's end instead, and quals carry on from there."""
    t, step = hm(start_hhmm), timedelta(minutes=cycle_min)
    ls, le = (hm(lunch[0]), hm(lunch[1])) if lunch else (None, None)
    for m in matches:
        if lunch and t < le and t + step > ls:
            t = le
        m["scheduled"] = t.strftime("%H:%M")
        t += step
    return matches


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
