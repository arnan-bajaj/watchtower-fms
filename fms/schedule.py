"""Qualification schedule generator and quality report.

A schedule is a list of matches {"red": [3 teams], "blue": [3 teams], "surrogates": [teams]}; slot order is
station 1-3. `breaks` are match numbers a break follows ([10] = lunch after qm10); they split the day into
sessions, and nothing is back-to-back across a break.

The math the generator works against:
- Two consecutive matches fill 12 slots, so with N teams at least max(0, 12 - N) teams play both. The
  back-to-back lower bound is that times the number of consecutive pairs inside a session.
- Three consecutive matches fill 18 slots and a team not playing all three fills at most 2, so at least
  max(0, 18 - 2N) teams play three in a row. Zero from 9 teams up; below 9 teams triples cannot be avoided.
- Slots that don't divide evenly go to distinct teams as one extra appearance each, flagged surrogate. As in
  FRC, the surrogate appearance is the team's third match (its last, if it has fewer than three).

Generator: randomized greedy construction, then simulated annealing with slot swaps, over several restarts
derived from one seed. The work is a fixed number of moves, not a time budget, so a seed always gives the
same schedule.
"""
from __future__ import annotations

import csv
import io
import itertools
import math
import random
from collections import Counter
from datetime import datetime, timedelta

# Penalty weights. b2b and triples dwarf everything else; spread evens out the b2bs that are forced.
W_B2B, W_TRIPLE, W_SPREAD = 2000.0, 20000.0, 200.0
W_SUR, W_PMAX, W_PSQ, W_OMAX, W_OSQ = 20.0, 30.0, 3.0, 15.0, 1.5
W_COLOR, W_STATION = 1.0, 0.5

PARTNER = ((0, 1), (0, 2), (1, 2), (3, 4), (3, 5), (4, 5))
OPP = tuple((a, b) for a in range(3) for b in range(3, 6))
MOVES = 150_000     # annealing moves per generate(), shared across restarts (~2 s in CPython)
RESTARTS = 8


def sessions(n_matches, breaks=()):
    """Session number of each match (0-based list)."""
    bs, out, s = {int(b) for b in breaks}, [], 0
    for i in range(n_matches):
        out.append(s)
        if i + 1 in bs:
            s += 1
    return out


def lower_bound(n_teams, n_matches, breaks=()):
    """Fewest back-to-backs any schedule can have."""
    se = sessions(n_matches, breaks)
    return sum(max(0, 12 - n_teams) for a, b in zip(se, se[1:]) if a == b)


def triples_min(n_teams, n_matches, breaks=()):
    """Fewest (team, 3-match window) triples any schedule can have. Zero from 9 teams up."""
    se = sessions(n_matches, breaks)
    return sum(max(0, 18 - 2 * n_teams) for a, c in zip(se, se[2:]) if a == c)


def match_count(n_teams, matches_per_team):
    return math.ceil(n_teams * matches_per_team / 6)


def breaks_from_times(matches, cycle_min):
    """Match numbers followed by a gap longer than one cycle (lunch). Times are HH:MM, so allow a minute."""
    out = []
    for i in range(len(matches) - 1):
        a, b = matches[i].get("scheduled"), matches[i + 1].get("scheduled")
        if a and b and (hm(b) - hm(a)).total_seconds() / 60 > cycle_min + 1:
            out.append(i + 1)
    return out


# ------------------------------------------------------------------ generator
def _greedy(n, m_count, se, base, extra, rng):
    """Build match by match: everyone who sat out the last match plays (when N < 12 that's the only way to
    stay at the bound), repeats come from teams that didn't also play the match before, the most-behind teams
    go first. Extra appearances go to whoever is picked at `base` while extras remain."""
    played, last, b2b = [0] * n, [-99] * n, [0] * n
    extras, ms = 0, []
    for i in range(m_count):
        prev = set(ms[i - 1]) if i and se[i - 1] == se[i] else set()
        prev2 = set(ms[i - 2]) if i > 1 and se[i - 2] == se[i] else set()
        left = m_count - i
        pick = []
        for _ in range(6):
            best, bs = None, None
            for t in range(n):
                if t in pick:
                    continue
                need = base - played[t]
                if need < 0 or (need == 0 and extras >= extra):
                    continue
                s = rng.random() - 10 * need - 0.5 * min(i - last[t], n)
                if need == 0:
                    s += 500
                if need >= left:
                    s -= 1e7
                if t in prev:
                    s += 1e4 + 100 * b2b[t] + (1e5 if t in prev2 else 0)
                if bs is None or s < bs:
                    best, bs = t, s
            if best is None:
                return None
            if played[best] == base:
                extras += 1
            pick.append(best)
        for t in pick:
            if t in prev:
                b2b[t] += 1
            played[t] += 1
            last[t] = i
        rng.shuffle(pick)
        ms.append(pick)
    return ms


class _Anneal:
    """Schedule in team indices with an incrementally maintained cost."""

    def __init__(self, n, ms, se, base, extra):
        self.n, self.ms, self.se, self.base, self.extra = n, ms, se, base, extra
        self.thr = max(1, n // 6)     # a surrogate gap shorter than this is "short"
        self.at = [dict() for _ in range(n)]
        for i, m in enumerate(ms):
            for p, t in enumerate(m):
                self.at[t][i] = p
        size = len(ms) + 2
        self.P, self.O = [0] * (n * n), [0] * (n * n)
        self.hP, self.hO = [0] * size, [0] * size
        self.sq = {"P": 0, "O": 0}
        self.mx = {"P": 0, "O": 0}
        for i in range(len(ms)):
            self._pairs(i, 1)
        self.tc = [self._team(t) for t in range(n)]
        self.team_sum = sum(self.tc)

    def _team(self, t):
        d, se = self.at[t], self.se
        idx = sorted(d)
        c = tri = sur = 0
        for a, b in zip(idx, idx[1:]):
            if b == a + 1 and se[a] == se[b]:
                c += 1
        for a, b in zip(idx, idx[2:]):
            if b == a + 2 and se[a] == se[b]:
                tri += 1
        red = sum(1 for i in idx if d[i] < 3)
        st = [0, 0, 0]
        for i in idx:
            st[d[i] % 3] += 1
        if self.extra and len(idx) > self.base:
            k = min(2, len(idx) - 1)
            for j in (k - 1, k + 1):
                if 0 <= j < len(idx) and se[idx[j]] == se[idx[k]]:
                    sur += max(0, self.thr - (abs(idx[j] - idx[k]) - 1))
        return (W_B2B * c + W_SPREAD * c * c + W_TRIPLE * tri + W_SUR * sur
                + W_COLOR * (2 * red - len(idx)) ** 2 + W_STATION * sum(x * x for x in st))

    def _bump(self, kind, x, y, sign):
        arr, hist = (self.P, self.hP) if kind == "P" else (self.O, self.hO)
        k = x * self.n + y if x < y else y * self.n + x
        c = arr[k]
        hist[c] -= 1
        c += sign
        arr[k] = c
        hist[c] += 1
        self.sq[kind] += 2 * c - 1 if sign > 0 else -2 * c - 1
        mx = self.mx[kind]
        if c > mx:
            mx = c
        while mx and not hist[mx]:
            mx -= 1
        self.mx[kind] = mx

    def _pairs(self, i, sign):
        m = self.ms[i]
        for a, b in PARTNER:
            self._bump("P", m[a], m[b], sign)
        for a, b in OPP:
            self._bump("O", m[a], m[b], sign)

    def _slot(self, i, p, sign):
        """Pairs of the team in slot p of match i only."""
        m = self.ms[i]
        x, red = m[p], p < 3
        for q in range(6):
            if q != p:
                self._bump("P" if (q < 3) == red else "O", x, m[q], sign)

    def cost(self):
        return (self.team_sum + W_PMAX * self.mx["P"] + W_PSQ * self.sq["P"]
                + W_OMAX * self.mx["O"] + W_OSQ * self.sq["O"])

    def _retally(self, ts):
        for t in ts:
            c = self._team(t)
            self.team_sum += c - self.tc[t]
            self.tc[t] = c

    def swap(self, i, pi, j, pj):
        """Swap slot pi of match i with slot pj of match j (i == j: within one match)."""
        x, y = self.ms[i][pi], self.ms[j][pj]
        if j != i:
            self._slot(i, pi, -1)
            self._slot(j, pj, -1)
        elif (pi < 3) != (pj < 3):
            self._pairs(i, -1)
        self.ms[i][pi], self.ms[j][pj] = y, x
        del self.at[x][i]
        if j != i:
            del self.at[y][j]
        self.at[x][j], self.at[y][i] = pj, pi
        if j != i:
            self._slot(i, pi, 1)
            self._slot(j, pj, 1)
        elif (pi < 3) != (pj < 3):
            self._pairs(i, 1)
        self._retally((x, y))

    def run(self, iters, rng, t0=10.0, t1=0.05):
        ms, nm = self.ms, len(self.ms)
        cur = self.cost()
        best, best_ms = cur, [m[:] for m in ms]
        decay = (t1 / t0) ** (1 / max(1, iters))
        temp = t0
        for _ in range(iters):
            temp *= decay
            i, pi = rng.randrange(nm), rng.randrange(6)
            if nm > 1 and rng.random() < 0.6:
                j = rng.randrange(nm - 1)
                j += j >= i
                pj = rng.randrange(6)
                if ms[i][pi] in ms[j] or ms[j][pj] in ms[i]:
                    continue
            else:
                j, pj = i, rng.randrange(5)
                pj += pj >= pi
            self.swap(i, pi, j, pj)
            new = self.cost()
            d = new - cur
            if d <= 0 or rng.random() < math.exp(-d / temp):
                cur = new
                if cur < best - 1e-9:
                    best, best_ms = cur, [m[:] for m in ms]
            else:
                self.swap(i, pi, j, pj)
        return best, best_ms


def _to_matches(ms, teams, base, extra):
    out = [{"red": [teams[t] for t in m[:3]], "blue": [teams[t] for t in m[3:]], "surrogates": []} for m in ms]
    if extra:
        apps = {}
        for i, m in enumerate(ms):
            for t in m:
                apps.setdefault(t, []).append(i)
        for t, idx in apps.items():
            if len(idx) > base:
                out[idx[min(2, len(idx) - 1)]]["surrogates"].append(teams[t])
    return out


def generate(teams, matches_per_team=None, *, matches=None, breaks=(), seed=0, moves=MOVES, restarts=RESTARTS):
    """Returns (matches, report). Give matches_per_team (match count = ceil(N*m/6)) or matches directly.
    Same teams, size, breaks and seed -> same schedule."""
    teams = [int(t) for t in teams]
    n = len(teams)
    if n < 6:
        raise ValueError("need at least 6 teams")
    if len(set(teams)) != n:
        raise ValueError("duplicate team numbers")
    m_count = int(matches) if matches is not None else match_count(n, int(matches_per_team))
    if m_count < 1:
        raise ValueError("need at least one match")
    breaks = sorted({int(b) for b in breaks if 0 < int(b) < m_count})
    se = sessions(m_count, breaks)
    base, extra = divmod(6 * m_count, n)
    best, best_ms = None, None
    for r in range(restarts):
        rng = random.Random(f"{seed}/{r}")
        ms = _greedy(n, m_count, se, base, extra, rng)
        if ms is None:
            continue
        c, ms = _Anneal(n, ms, se, base, extra).run(moves // restarts, rng)
        if best is None or c < best:
            best, best_ms = c, ms
    out = _to_matches(best_ms, teams, base, extra)
    rep = report(out, teams, breaks)
    rep["seed"] = seed
    return out, rep


# ------------------------------------------------------------------ quality report
def report(matches, teams=None, breaks=()):
    """Quality of any schedule (generated, imported or saved). breaks as in generate()."""
    teams = sorted(set(int(t) for t in (teams or [])) | {t for m in matches for t in m["red"] + m["blue"]})
    n, m_count = len(teams), len(matches)
    breaks = sorted({int(b) for b in breaks if 0 < int(b) < m_count})
    se = sessions(m_count, breaks)
    label = [m.get("key") or f"qm{i + 1}" for i, m in enumerate(matches)]
    viol, notes = [], []
    apps = {t: [] for t in teams}
    sur = Counter()
    partner, opp = Counter(), Counter()
    for i, m in enumerate(matches):
        ts = m["red"] + m["blue"]
        if len(m["red"]) != 3 or len(m["blue"]) != 3:
            viol.append(f"{label[i]}: needs 3 red and 3 blue teams")
        if len(set(ts)) != len(ts):
            viol.append(f"{label[i]}: a team is in it twice")
        for t in dict.fromkeys(ts):
            apps[t].append((i, "red" if t in m["red"] else "blue",
                            (m["red"] if t in m["red"] else m["blue"]).index(t) + 1))
        for t in m.get("surrogates") or []:
            if t in ts:
                sur[t] += 1
            else:
                viol.append(f"{label[i]}: surrogate {t} isn't playing in it")
        for side in (m["red"], m["blue"]):
            for x, y in itertools.combinations(sorted(set(side)), 2):
                partner[(x, y)] += 1
        for x in set(m["red"]):
            for y in set(m["blue"]) - {x}:
                opp[tuple(sorted((x, y)))] += 1

    same = lambda a, b: se[a] == se[b]
    counts = {t: len(a) for t, a in apps.items()}
    lo = min(counts.values(), default=0)
    hi = max(counts.values(), default=0)
    if hi - lo > 1:
        viol.append(f"match counts range from {lo} to {hi}; they may differ by at most 1")
    triples, per_team = [], []
    total_b2b = 0
    for t in teams:
        idx = [i for i, _, _ in apps[t]]
        b2b = sum(1 for a, b in zip(idx, idx[1:]) if b == a + 1 and same(a, b))
        total_b2b += b2b
        gaps = [b - a - 1 for a, b in zip(idx, idx[1:]) if same(a, b)]
        for a, b, c in zip(idx, idx[1:], idx[2:]):
            if c == a + 2 and same(a, c):
                triples.append({"team": t, "matches": [label[a], label[b], label[c]]})
        extra = hi > lo and counts[t] == hi
        if sur[t] > 1:
            viol.append(f"{t} is flagged surrogate {sur[t]} times; at most once")
        elif extra and not sur[t]:
            viol.append(f"{t} plays {counts[t]} matches (one extra) but no appearance is flagged surrogate")
        elif sur[t] and not extra:
            viol.append(f"{t} is flagged surrogate but doesn't play an extra match")
        sur_at = next((label[i] for i, _ in enumerate(matches) if t in (matches[i].get("surrogates") or [])), None)
        st = Counter(s for _, _, s in apps[t])
        per_team.append({"team": t, "matches": counts[t], "surrogate": sur_at, "back_to_back": b2b,
                         "min_gap": min(gaps) if gaps else None,
                         "red": sum(1 for _, a, _ in apps[t] if a == "red"),
                         "blue": sum(1 for _, a, _ in apps[t] if a == "blue"),
                         "stations": [st[1], st[2], st[3]]})
    tmin = triples_min(n, m_count, breaks)
    if len(triples) > tmin:
        viol += [f"{x['team']} plays three in a row: {'–'.join(x['matches'])}" for x in triples]
    elif triples:
        notes.append(f"With {n} teams some teams must play three in a row; this schedule has the minimum "
                     f"({len(triples)}).")
    lb = lower_bound(n, m_count, breaks)
    return {"teams": n, "matches": m_count, "breaks": breaks,
            "back_to_back": total_b2b, "lower_bound": lb, "excess_back_to_back": total_b2b - lb,
            "max_team_back_to_back": max((p["back_to_back"] for p in per_team), default=0),
            "triples": triples, "triples_min": tmin,
            "surrogates": sum(sur.values()),
            "max_partner_repeat": max(partner.values(), default=0),
            "max_opp_repeat": max(opp.values(), default=0),
            "per_team": per_team, "violations": viol, "notes": notes}


# ------------------------------------------------------------------ times and import
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
