"""SQLite store. Everything survives a laptop crash / server restart."""
import json
import pathlib
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS matches (
  key TEXT PRIMARY KEY, comp_level TEXT, set_number INT, match_number INT, ord INT,
  red TEXT, blue TEXT, surrogates TEXT DEFAULT '[]',
  red_alliance INT, blue_alliance INT,
  scheduled TEXT, status TEXT DEFAULT 'scheduled',
  auto_start REAL, teleop_start REAL, offset REAL DEFAULT 0,
  first_inactive TEXT, fi_locked INT DEFAULT 0, fi_coin INT DEFAULT 0,
  adjust TEXT DEFAULT '{}', climbs TEXT DEFAULT '{}', winner_override TEXT,
  video TEXT, breakdown TEXT, committed_at REAL
);
CREATE TABLE IF NOT EXISTS fuel (id INTEGER PRIMARY KEY, t REAL, hub TEXT, n INT, source TEXT);
CREATE INDEX IF NOT EXISTS fuel_t ON fuel(t);
CREATE TABLE IF NOT EXISTS fouls (
  id INTEGER PRIMARY KEY, match_key TEXT, t REAL, committed_by TEXT, kind TEXT,
  team INT, ref TEXT, deleted INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY, path TEXT, dedupe TEXT, body TEXT, created REAL,
  attempts INT DEFAULT 0, last_error TEXT, state TEXT DEFAULT 'pending', sent_at REAL);
"""

JSON_COLS = ("red", "blue", "surrogates", "adjust", "climbs", "breakdown")


class Store:
    def __init__(self, path):
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args)

    # ---- kv
    def get(self, k, default=None):
        r = self.q("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r[0]["v"]) if r else default

    def set(self, k, v):
        self.x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
               (k, json.dumps(v)))

    # ---- matches
    @staticmethod
    def _decode(r):
        for c in JSON_COLS:
            if r.get(c) is not None:
                r[c] = json.loads(r[c])
        r["fi_locked"] = bool(r.get("fi_locked"))
        r["fi_coin"] = bool(r.get("fi_coin"))
        return r

    def match(self, key):
        r = self.q("SELECT * FROM matches WHERE key=?", (key,))
        return self._decode(r[0]) if r else None

    def matches(self, comp_level=None):
        if comp_level:
            rows = self.q("SELECT * FROM matches WHERE comp_level=? ORDER BY ord", (comp_level,))
        else:
            rows = self.q("SELECT * FROM matches ORDER BY ord")
        return [self._decode(r) for r in rows]

    def insert_match(self, m):
        m = dict(m)
        for c in JSON_COLS:
            if c in m and m[c] is not None:
                m[c] = json.dumps(m[c])
        cols = ",".join(m)
        self.x(f"INSERT OR REPLACE INTO matches({cols}) VALUES({','.join('?' * len(m))})",
               tuple(m.values()))

    def update_match(self, key, **f):
        for c in JSON_COLS:
            if c in f and f[c] is not None:
                f[c] = json.dumps(f[c])
        sets = ",".join(f"{k}=?" for k in f)
        self.x(f"UPDATE matches SET {sets} WHERE key=?", (*f.values(), key))

    def delete_matches(self, comp_level):
        self.x("DELETE FROM matches WHERE comp_level=?", (comp_level,))

    def next_ord(self):
        r = self.q("SELECT COALESCE(MAX(ord),0)+1 AS n FROM matches")
        return r[0]["n"]

    # ---- fuel
    def add_fuel(self, rows, source):
        with self.lock:
            self.db.executemany("INSERT INTO fuel(t,hub,n,source) VALUES(?,?,?,?)",
                                [(float(t), hub, int(n), source) for t, hub, n in rows])

    def fuel_between(self, t0, t1):
        return [(r["t"], r["hub"], r["n"]) for r in
                self.q("SELECT t,hub,n FROM fuel WHERE t>=? AND t<? ORDER BY t", (t0, t1))]

    def replace_fuel(self, hub, t0, t1, rows, source):
        with self.lock:
            self.db.execute("BEGIN")
            self.db.execute("DELETE FROM fuel WHERE hub=? AND t>=? AND t<?", (hub, t0, t1))
            self.db.executemany("INSERT INTO fuel(t,hub,n,source) VALUES(?,?,?,?)",
                                [(float(t), hub, int(n), source) for t, n in rows])
            self.db.execute("COMMIT")

    # ---- fouls
    def add_foul(self, match_key, committed_by, kind, team, ref):
        cur = self.x("INSERT INTO fouls(match_key,t,committed_by,kind,team,ref) VALUES(?,?,?,?,?,?)",
                     (match_key, time.time(), committed_by, kind, team, ref))
        return cur.lastrowid

    def fouls(self, match_key):
        return self.q("SELECT * FROM fouls WHERE match_key=? AND deleted=0 ORDER BY t", (match_key,))

    # ---- TBA outbox
    def outbox_add(self, path, body, dedupe):
        with self.lock:
            self.db.execute("DELETE FROM outbox WHERE dedupe=? AND state='pending'", (dedupe,))
            self.db.execute("INSERT INTO outbox(path,dedupe,body,created) VALUES(?,?,?,?)",
                            (path, dedupe, body, time.time()))

    def outbox_next(self):
        r = self.q("SELECT * FROM outbox WHERE state='pending' ORDER BY id LIMIT 1")
        return r[0] if r else None

    def outbox_stats(self):
        rows = self.q("SELECT state, COUNT(*) AS n FROM outbox GROUP BY state")
        s = {r["state"]: r["n"] for r in rows}
        last = self.q("SELECT last_error, path FROM outbox WHERE last_error IS NOT NULL "
                      "ORDER BY id DESC LIMIT 1")
        s["last_error"] = last[0] if last else None
        return s
