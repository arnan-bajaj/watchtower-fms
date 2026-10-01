"""The Blue Alliance Trusted (write) API v1 with an offline-safe outbox.

Every write is queued in SQLite first, then a background thread sends it.
No internet at the venue? Scores keep queueing and go out when it comes back.
Full-state endpoints (rankings, alliances, one match) are de-duplicated so only
the newest pending copy is sent.

Auth: X-TBA-Auth-Sig = md5(secret + request_path + request_body).
"""
from __future__ import annotations

import hashlib
import json
import threading
import time

import requests


def fk(t):
    return f"frc{int(t)}"


class TBA:
    def __init__(self, cfg, store):
        self.cfg = cfg
        self.store = store
        self.key = cfg["event"]["tba_event_key"]
        self._wake = threading.Event()

    @property
    def configured(self):
        t = self.cfg["tba"]
        return bool(t["enabled"] and self.key and t["auth_id"] and t["auth_secret"])

    def path(self, suffix):
        return f"/api/trusted/v1/event/{self.key}/{suffix}"

    def enqueue(self, suffix, payload, dedupe=None, raw=False):
        if not self.key:
            return False
        body = payload if raw else json.dumps(payload, separators=(",", ":"))
        self.store.outbox_add(self.path(suffix), body, dedupe or suffix)
        self._wake.set()
        return True

    # ---- payload builders
    def team_list(self, teams):
        return self.enqueue("team_list/update", [fk(t) for t in teams])

    def match(self, m, bd=None, time_utc=None):
        alliances = {}
        sur = set(m.get("surrogates") or [])
        for a in ("red", "blue"):
            alliances[a] = {
                "teams": [fk(t) for t in m[a]],
                "score": bd[a]["total"] if bd else -1,
                "surrogates": [fk(t) for t in m[a] if t in sur],
                "dqs": [],
            }
        d = {"comp_level": m["comp_level"], "set_number": m["set_number"],
             "match_number": m["match_number"], "alliances": alliances,
             "time_string": m.get("scheduled") or ""}
        if time_utc:
            d["time_utc"] = time_utc
        if bd and self.cfg["tba"]["send_score_breakdown"]:
            d["score_breakdown"] = {a: _flat(bd[a]) for a in ("red", "blue")}
        return d

    def matches(self, match_dicts):
        # one request for a batch (schedule push); dedupe key covers the batch
        return self.enqueue("matches/update", match_dicts,
                            dedupe="matches:" + ",".join(sorted(self._key(d) for d in match_dicts)))

    def one_match(self, m, bd, time_utc=None):
        d = self.match(m, bd, time_utc)
        return self.enqueue("matches/update", [d], dedupe="match:" + m["key"])

    @staticmethod
    def _key(d):
        return f"{d['comp_level']}{d['set_number'] if d['comp_level'] != 'qm' else ''}m{d['match_number']}"

    def rankings(self, rows):
        payload = {
            "breakdowns": ["Ranking Score", "Avg Match", "Avg Tower", "Avg Fuel"],
            "rankings": [{
                "team_key": fk(r["team"]), "rank": r["rank"], "played": r["played"], "dqs": 0,
                "wins": r["wins"], "losses": r["losses"], "ties": r["ties"],
                "Ranking Score": r["rs"], "Avg Match": r["avg_score"],
                "Avg Tower": r["avg_tower"], "Avg Fuel": r["avg_fuel"],
            } for r in rows],
        }
        return self.enqueue("rankings/update", payload, dedupe="rankings")

    def alliances(self, alliances):
        return self.enqueue("alliance_selections/update",
                            [[fk(t) for t in a] for a in alliances], dedupe="alliances")

    def clear_rankings(self):
        return self.enqueue("rankings/update", {"breakdowns": [], "rankings": []}, dedupe="rankings")

    def delete_matches(self, partial_keys):
        """Takes matches down from TBA. A later update for the same key re-creates it."""
        return self.enqueue("matches/delete", list(partial_keys),
                            dedupe="delete:" + ",".join(sorted(partial_keys)))

    def delete_all_matches(self):
        # TBA requires the bare event key (not JSON) as the body to confirm a delete-all
        return self.enqueue("matches/delete_all", self.key, dedupe="delete_all", raw=True)

    def video(self, partial_key, youtube):
        return self.enqueue("match_videos/add", {partial_key: youtube}, dedupe="video:" + partial_key)

    def webcast(self, url):
        return self.enqueue("info/update", {"webcasts": [{"url": url}]}, dedupe="webcast")

    # ---- sender
    def run_forever(self):
        while True:
            self._wake.wait(timeout=2)
            self._wake.clear()
            while self.configured:
                row = self.store.outbox_next()
                if not row:
                    break
                ok, err, fatal = self._send(row)
                if ok:
                    self.store.x("UPDATE outbox SET state='sent', sent_at=?, attempts=attempts+1, "
                                 "last_error=NULL WHERE id=?", (time.time(), row["id"]))
                    continue
                attempts = row["attempts"] + 1
                state = "dead" if (fatal and attempts >= 3) else "pending"
                self.store.x("UPDATE outbox SET attempts=?, last_error=?, state=? WHERE id=?",
                             (attempts, err[:500], state, row["id"]))
                if state == "pending":
                    time.sleep(self.cfg["tba"]["retry_s"])

    def _send(self, row):
        t = self.cfg["tba"]
        sig = hashlib.md5((t["auth_secret"] + row["path"] + row["body"]).encode()).hexdigest()
        try:
            r = requests.post(t["base_url"] + row["path"], data=row["body"].encode(), timeout=10,
                              headers={"X-TBA-Auth-Id": t["auth_id"], "X-TBA-Auth-Sig": sig,
                                       "Content-Type": "application/json"})
        except requests.RequestException as e:
            return False, f"network: {e}", False
        if r.status_code == 200:
            return True, "", False
        # 4xx = TBA rejected the data/auth: retrying unchanged won't help (dead after 3)
        return False, f"HTTP {r.status_code}: {r.text[:300]}", 400 <= r.status_code < 500


def _flat(b):
    """Only used if tba.send_score_breakdown is on. TBA validates breakdown keys per
    season; test one match first, turn off if TBA rejects it."""
    d = {"autoFuel": b["auto_fuel"], "teleopFuel": b["teleop_fuel"], "totalFuel": b["total_fuel"],
         "autoTowerPoints": b["auto_tower_points"], "teleopTowerPoints": b["teleop_tower_points"],
         "foulPoints": b["foul_points"], "autoPoints": b["auto_points"],
         "teleopPoints": b["teleop_points"], "totalPoints": b["total"], "rp": b["rp"],
         "minorFoulCount": b["fouls_committed"]["minor"],
         "majorFoulCount": b["fouls_committed"]["major"]}
    return d
