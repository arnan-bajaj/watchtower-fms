"""The Watchtower website (watchtower.systemoverload.org): sign up, create an event, get the launch command.

Each event's FMS runs on that team's own computer (launch.py). This site never runs matches. It keeps:
  - accounts and events,
  - each event's starting event.yaml (PINs, vision key, name, teams) and the launch script that installs
    Watchtower and runs it,
  - a synced copy of the event's live settings (fms/settings.py: newest wins per key), so TBA keys or
    teams can be set here before event day and a relaunch on another computer gets them back,
  - the last status the event's FMS reported (current match, the address phones open).

Run:  WT_SITE_URL=https://watchtower.systemoverload.org uvicorn website.app:app  (from the repo root)
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import secrets
import sqlite3
import threading
import time

import yaml
from fastapi import Body, Cookie, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from fms import config, settings

ROOT = pathlib.Path(__file__).resolve().parent
SITE = os.environ.get("WT_SITE_URL", "http://127.0.0.1:8090").rstrip("/")
DATA = pathlib.Path(os.environ.get("WT_DATA", "data/website"))
DIST = pathlib.Path(os.environ.get("WT_DIST", "dist"))           # watchtower.tar.gz + version (deploy/build.sh)
SIGNUP_CODE = os.environ.get("WT_SIGNUP_CODE", "")                # set: sign-up needs this code
MAX_EVENTS = int(os.environ.get("WT_MAX_EVENTS", "10"))           # per account
SESSION_DAYS = 30
RESERVED = {"api", "run", "download", "static", "new", "login", "signup", "events", "admin"}

DATA.mkdir(parents=True, exist_ok=True)
_db = sqlite3.connect(DATA / "website.sqlite3", check_same_thread=False, isolation_level=None)
_db.row_factory = sqlite3.Row
_lock = threading.Lock()
_db.executescript("""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, pw TEXT NOT NULL,
  team TEXT, created REAL);
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, owner INTEGER NOT NULL,
  token TEXT UNIQUE NOT NULL, base TEXT NOT NULL, settings TEXT NOT NULL DEFAULT '{}',
  status TEXT, last_seen REAL, created REAL, deleted INTEGER DEFAULT 0);
""")


def q(sql, args=()):
    with _lock:
        return _db.execute(sql, args).fetchall()


def x(sql, args=()):
    with _lock:
        return _db.execute(sql, args)


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=ROOT.parent / "fms" / "static"), name="static")


# ------------------------------------------------------------------ accounts
def pw_hash(pw, salt=None):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def pw_ok(pw, stored):
    _, salt, _h = stored.split("$")
    return secrets.compare_digest(pw_hash(pw, bytes.fromhex(salt)), stored)


_hits: dict = {}


def throttle(request: Request, what: str, n=10, per=60):
    ip = request.client.host if request.client else "?"
    now = time.time()
    k = (ip, what)
    _hits[k] = [t for t in _hits.get(k, []) if now - t < per]
    if len(_hits[k]) >= n:
        raise HTTPException(429, "Too many tries. Wait a minute.")
    _hits[k].append(now)


def user_of(sid):
    if not sid:
        return None
    r = q("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id=? AND s.expires>?",
          (hashlib.sha256(sid.encode()).hexdigest(), time.time()))
    return r[0] if r else None


def need_user(sid):
    u = user_of(sid)
    if not u:
        raise HTTPException(401, "Log in first")
    return u


def start_session(resp: Response, uid):
    sid = secrets.token_urlsafe(32)
    x("INSERT INTO sessions VALUES(?,?,?)", (hashlib.sha256(sid.encode()).hexdigest(), uid,
                                             time.time() + SESSION_DAYS * 86400))
    resp.set_cookie("wt", sid, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                    secure=SITE.startswith("https"))


@app.post("/api/signup")
def signup(request: Request, response: Response, body: dict = Body(...)):
    throttle(request, "signup", 5)
    email = str(body.get("email", "")).strip().lower()
    pw = str(body.get("password", ""))
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email) or len(email) > 200:
        raise HTTPException(400, "Enter a real email address")
    if len(pw) < 8:
        raise HTTPException(400, "Use a password of 8 characters or more")
    if SIGNUP_CODE and not secrets.compare_digest(str(body.get("code", "")), SIGNUP_CODE):
        raise HTTPException(403, "Sign-up needs an invite code on this site")
    try:
        cur = x("INSERT INTO users(email,pw,team,created) VALUES(?,?,?,?)",
                (email, pw_hash(pw), str(body.get("team", ""))[:20], time.time()))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "That email already has an account. Log in instead.")
    start_session(response, cur.lastrowid)
    return {"ok": True}


@app.post("/api/login")
def login(request: Request, response: Response, body: dict = Body(...)):
    throttle(request, "login")
    r = q("SELECT * FROM users WHERE email=?", (str(body.get("email", "")).strip().lower(),))
    if not r or not pw_ok(str(body.get("password", "")), r[0]["pw"]):
        raise HTTPException(401, "Wrong email or password")
    start_session(response, r[0]["id"])
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response, wt: str = Cookie(None)):
    if wt:
        x("DELETE FROM sessions WHERE id=?", (hashlib.sha256(wt.encode()).hexdigest(),))
    response.delete_cookie("wt")
    return {"ok": True}


@app.get("/api/me")
def me(wt: str = Cookie(None)):
    u = user_of(wt)
    return {"user": {"email": u["email"], "team": u["team"]} if u else None, "signup_code": bool(SIGNUP_CODE)}


# ------------------------------------------------------------------ events
def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-") or "event"
    base, n = s, 2
    while s in RESERVED or q("SELECT 1 FROM events WHERE slug=?", (s,)):
        s = f"{base}-{n}"
        n += 1
    return s


def full_cfg(ev):
    """The event's config as its FMS would run it: defaults + starting event.yaml + synced settings."""
    base = config._merge(config.DEFAULTS, json.loads(ev["base"]))
    return settings.overlay(base, json.loads(ev["settings"]))


def event_row(slug, user=None, token=None):
    r = q("SELECT * FROM events WHERE slug=? AND deleted=0", (slug,))
    if not r or (user and r[0]["owner"] != user["id"]) or (token and not secrets.compare_digest(token, r[0]["token"])):
        raise HTTPException(404, "No such event")
    return r[0]


def commands(ev):
    run = f"{SITE}/run/{ev['token']}"
    return {"mac": f"curl -fsSL {run} | sh", "mac_mock": f"curl -fsSL {run} | sh -s -- --mock",
            "windows": f"irm {run}.ps1 | iex", "windows_mock": f"$env:WATCHTOWER_ARGS='--mock'; irm {run}.ps1 | iex"}


def event_public(ev, owner=False):
    st = json.loads(ev["status"] or "null")
    out = {"slug": ev["slug"], "name": full_cfg(ev)["event"]["name"], "last_seen": ev["last_seen"],
           "online": bool(ev["last_seen"] and time.time() - ev["last_seen"] < 45),
           "lan_urls": (st or {}).get("lan_urls") or [], "match": (st or {}).get("match"),
           "match_status": (st or {}).get("match_status"), "played": (st or {}).get("played")}
    if owner:
        cfg = full_cfg(ev)
        out.update(commands=commands(ev), settings=settings.public(cfg, json.loads(ev["settings"])),
                   created=ev["created"], vision=(st or {}).get("vision"), url=f"{SITE}/e/{ev['slug']}")
    return out


@app.get("/api/events")
def list_events(wt: str = Cookie(None)):
    u = need_user(wt)
    return {"events": [event_public(e) for e in q("SELECT * FROM events WHERE owner=? AND deleted=0 ORDER BY created DESC", (u["id"],))]}


@app.post("/api/events")
def create_event(body: dict = Body(...), wt: str = Cookie(None)):
    u = need_user(wt)
    if q("SELECT COUNT(*) n FROM events WHERE owner=? AND deleted=0", (u["id"],))[0]["n"] >= MAX_EVENTS:
        raise HTTPException(409, f"An account can have {MAX_EVENTS} events. Delete an old one first.")
    try:
        c = settings.clean({k: body.get(f) for k, f in (("event.name", "name"), ("event.date", "date"),
                            ("event.utc_offset_hours", "utc_offset_hours"), ("event.teams", "teams"),
                            ("event.tba_event_key", "tba_event_key")) if body.get(f) not in (None, "")})
    except ValueError as e:
        raise HTTPException(400, str(e))
    if "event.name" not in c:
        raise HTTPException(400, "Give the event a name")
    pins = []
    while len(pins) < 3:
        p = f"{secrets.randbelow(10**6):06d}"
        if p not in pins:
            pins.append(p)
    base = {"event": {k.split(".", 1)[1]: v for k, v in c.items()},
            "server": {"port": 8000, "db": "data/fms.sqlite3",
                       "pins": dict(zip(("control", "ref", "emcee"), pins)),
                       "vision_key": secrets.token_urlsafe(18)},
            "tba": {"enabled": False}}
    slug = slugify(c["event.name"])
    tok = secrets.token_urlsafe(24)
    x("INSERT INTO events(slug,owner,token,base,created) VALUES(?,?,?,?,?)",
      (slug, u["id"], tok, json.dumps(base), time.time()))
    return event_public(event_row(slug), owner=True)


@app.get("/api/events/{slug}")
def get_event(slug: str, wt: str = Cookie(None)):
    return event_public(event_row(slug, need_user(wt)), owner=True)


@app.post("/api/events/{slug}/settings")
def edit_event(slug: str, body: dict = Body(...), wt: str = Cookie(None)):
    """Same keys and rules as /control -> Setup. The event's FMS picks them up on its next sync."""
    ev = event_row(slug, need_user(wt))
    try:
        changes = settings.clean(body.get("changes") or {})
        entries = settings.merge(json.loads(ev["settings"]), settings.stamp(changes, time.time()))
        settings.check(settings.overlay(config._merge(config.DEFAULTS, json.loads(ev["base"])), entries))
    except ValueError as e:
        raise HTTPException(400, str(e))
    x("UPDATE events SET settings=? WHERE id=?", (json.dumps(entries), ev["id"]))
    return event_public(event_row(slug), owner=True)


@app.post("/api/events/{slug}/rotate")
def rotate(slug: str, wt: str = Cookie(None)):
    """New launch command; the old one (and any FMS linked with it) stops syncing."""
    ev = event_row(slug, need_user(wt))
    x("UPDATE events SET token=? WHERE id=?", (secrets.token_urlsafe(24), ev["id"]))
    return event_public(event_row(slug), owner=True)


@app.delete("/api/events/{slug}")
def delete_event(slug: str, wt: str = Cookie(None)):
    ev = event_row(slug, need_user(wt))
    x("UPDATE events SET deleted=1, token=?, slug=? WHERE id=?",
      (secrets.token_urlsafe(24), f"{ev['slug']}~deleted~{ev['id']}", ev["id"]))
    return {"ok": True}


@app.get("/api/public/{slug}")
def public_event(slug: str):
    """For phones that open the site: where this event's FMS is on the venue WiFi."""
    return event_public(event_row(slug))


# ------------------------------------------------------------------ the event's FMS (fms/cloud.py, launch)
@app.post("/api/link/{slug}/sync")
def sync(slug: str, body: dict = Body(...), x_watchtower_token: str = Header(None)):
    ev = event_row(slug, token=x_watchtower_token or "")
    theirs = body.get("settings") or {}
    merged = settings.merge(json.loads(ev["settings"]), theirs if isinstance(theirs, dict) else {})
    st = body.get("status") if isinstance(body.get("status"), dict) else {}
    x("UPDATE events SET settings=?, status=?, last_seen=? WHERE id=?",
      (json.dumps(merged), json.dumps(st)[:20000], time.time(), ev["id"]))
    return {"settings": merged}


@app.get("/api/link/{slug}/event.yaml")
def event_yaml(slug: str, x_watchtower_token: str = Header(None)):
    ev = event_row(slug, token=x_watchtower_token or "")
    base = json.loads(ev["base"])
    base["cloud"] = {"url": SITE, "event": ev["slug"], "token": ev["token"]}
    head = (f"# Watchtower: {full_cfg(ev)['event']['name']}. Written by {SITE}/e/{ev['slug']}.\n"
            "# Change settings in /control -> Setup (or on the website), not here.\n")
    return PlainTextResponse(head + yaml.safe_dump(base, sort_keys=False))


def _script(name, ev):
    t = (ROOT / name).read_text()
    for k, v in {"__SITE__": SITE, "__EVENT__": ev["slug"], "__TOKEN__": ev["token"],
                 "__NAME__": re.sub(r"[^\w .&()-]", "", full_cfg(ev)["event"]["name"])}.items():
        t = t.replace(k, v)
    return PlainTextResponse(t, headers={"Cache-Control": "no-store"})


@app.get("/run/{token}.ps1")
def run_ps1(token: str):
    r = q("SELECT * FROM events WHERE token=? AND deleted=0", (token,))
    if not r:
        return PlainTextResponse("Write-Host 'This launch command is old or the event was deleted. Copy the new one from the website.'\n", 404)
    return _script("launch.ps1", r[0])


@app.get("/run/{token}")
def run_sh(token: str):
    r = q("SELECT * FROM events WHERE token=? AND deleted=0", (token,))
    if not r:
        return PlainTextResponse("echo 'This launch command is old or the event was deleted. Copy the new one from the website.'; exit 1\n", 404)
    return _script("launch.sh", r[0])


@app.get("/download/version")
def version():
    p = DIST / "version"
    return PlainTextResponse(p.read_text().strip() if p.exists() else "dev")


@app.get("/download/watchtower.tar.gz")
def tarball():
    p = DIST / "watchtower.tar.gz"
    if not p.exists():
        raise HTTPException(404, "No build yet (deploy/build.sh)")
    return FileResponse(p, media_type="application/gzip", filename="watchtower.tar.gz")


# ------------------------------------------------------------------ pages
def site_page():
    return FileResponse(ROOT / "site.html", headers={"Cache-Control": "no-store"})


@app.get("/")
def home():
    return site_page()


@app.get("/e/{slug}")
def event_page(slug: str):
    return site_page()


@app.get("/healthz")
def health():
    return JSONResponse({"ok": True})
