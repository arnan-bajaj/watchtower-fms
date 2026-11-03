"""FMS server. Run:  python -m fms.server  (from the repo root)"""
from __future__ import annotations

import asyncio
import base64
import copy
import csv
import hashlib
import io
import json
import os
import pathlib
import random
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import Body, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import bracket, cloud, config, game, reefscape, schedule, settings
from .store import Store
from .tba import TBA

CFG = config.load()
if os.environ.get("FMS_PORT"):   # launch.py --port; before BASE so live settings don't undo it
    CFG["server"]["port"] = int(os.environ["FMS_PORT"])
BASE = copy.deepcopy(CFG)      # event.yaml as loaded; Setup's live settings go on top (fms/settings.py)
G = CFG["game"]
GRACE = G["score_grace_s"]
# The event's game is fixed when it's made (event.game). REEFSCAPE is scored by hand: no vision, no hubs.
REEF = CFG["event"]["game"] == "reefscape"
RULES = reefscape if REEF else game        # build_periods, period_names, TIEBREAKS
STATIC = pathlib.Path(__file__).parent / "static"
store = Store(CFG["server"]["db"])
settings.apply_in_place(CFG, settings.overlay(BASE, store.get("settings") or {}))
tba = TBA(CFG, store)
SECRET = store.get("secret") or secrets.token_hex(16)
store.set("secret", SECRET)

clients: set[WebSocket] = set()
vision_status: dict = {}
feed_status: list = []        # live count feeds to field systems, relayed by vision (count_feed.py)
feed_seen = 0.0
# Setup -> Cameras, in memory only: what /control asks vision for, and the pictures it sends back.
# drop: hub -> {"id", "n"} (drop test); size: hub -> {"id"} (Measure); record: Test -> Record's id or None;
# check: Test -> Check a recording {"id", "dir", "name", "hub", "hand"} or None; preview: hub -> {"colour",
# "until"} (Fuel colour -> Show what counts as fuel, a colour range not saved yet)
vision_want = {"frames_until": 0.0, "scan": 0, "drop": {}, "size": {}, "record": None, "check": None, "preview": {}}
vision_frames: dict = {}      # hub -> {"jpeg": bytes, "w", "h", "source", "t"}
vision_cams: dict = {"id": 0, "t": 0, "cameras": [], "in_use": {}, "error": None}
vision_drop: dict = {}        # hub -> {"ok", "msg", "t"} (the last drop test's outcome)
vision_size: dict = {}        # hub -> {"ok", "msg", "t"} (the last Measure's outcome)
vision_files: list = []       # recordings vision can re-count (Test -> Check a recording)
vision_check: dict = {"progress": None, "result": None}
vision_unlock = {"key": None}  # the running match whose cameras Control unlocked (Setup -> Run)
LOCKED = "Cameras are locked while a match runs. Unlock them in Setup → Run first."
_field_applied: set = set()   # (match key, auto_start) whose auto result came from a field's lights
_dirty = threading.Event()  # set from any thread; tick loop broadcasts


# ------------------------------------------------------------------ auth
def token_for(role):
    # The PIN is part of it, so changing a role's PIN in Setup logs that role out everywhere.
    pin = str(CFG["server"]["pins"].get(role, ""))
    return hashlib.sha256(f"{SECRET}:{role}:{pin}".encode()).hexdigest()[:32]


def role_of(tok):
    for r in ("control", "ref", "emcee", "scorer"):
        if tok and secrets.compare_digest(tok, token_for(r)):
            return r
    return None


def need(tok, *roles):
    r = role_of(tok)
    if r is None or (r != "control" and r not in roles):
        raise HTTPException(401, "Log in again (wrong or missing PIN)")
    return r


# ------------------------------------------------------------------ match engine
def current():
    k = store.get("current")
    return store.match(k) if k else None


def periods_of(m):
    if not m or m.get("auto_start") is None:
        return None
    return RULES.build_periods(G, m["auto_start"], m.get("teleop_start"),
                               m.get("first_inactive"), m.get("offset") or 0.0)


def compute(m):
    ps = periods_of(m)
    if not ps:
        return None, None
    playoff = m["comp_level"] != "qm"
    wo = m.get("winner_override") if playoff else None
    if REEF:
        bd = reefscape.score_match(G, m.get("adjust"), m.get("climbs"), store.fouls(m["key"]),
                                   sizes={a: len(m[a]) for a in game.ALLIANCES}, playoff=playoff, winner_override=wo)
        return ps, bd
    events = store.fuel_between(ps[0].start, ps[-1].end + GRACE)
    bd = game.score_match(G, ps, events, m.get("adjust"), m.get("climbs"), store.fouls(m["key"]),
                          playoff=playoff, winner_override=wo)
    return ps, bd


def ranks():
    return bracket.rankings(CFG["event"]["teams"], store.matches("qm"), RULES.TIEBREAKS)


def push_ranks():
    tba.rankings(ranks(), RULES.TIEBREAKS)


def suggested_video(m):
    w = store.get("webcast") or {}
    if not (w.get("vod_id") and w.get("stream_start") and m.get("auto_start")):
        return None
    t = int(m["auto_start"] + (m.get("offset") or 0) - w["stream_start"] - 10)
    return f"{w['vod_id']}?t={max(0, t)}"


def time_utc(m):
    d, s = CFG["event"]["date"], m.get("scheduled")
    if not d or not s:
        return None
    local = datetime.strptime(f"{d} {s}", "%Y-%m-%d %H:%M")
    return (local - timedelta(hours=CFG["event"]["utc_offset_hours"])).strftime("%Y-%m-%dT%H:%M:%S")


def match_public(m, with_detail=False):
    d = {k: m.get(k) for k in ("key", "comp_level", "set_number", "match_number", "red", "blue",
                               "surrogates", "red_alliance", "blue_alliance", "scheduled",
                               "status", "auto_start", "teleop_start", "offset",
                               "first_inactive", "fi_locked", "fi_coin", "winner_override",
                               "video")}
    bd = m.get("breakdown")
    d["red_score"] = bd["red"]["total"] if bd else None
    d["blue_score"] = bd["blue"]["total"] if bd else None
    d["winner"] = bd["winner"] if bd else None
    if with_detail:
        ps, live = compute(m)
        d["periods"] = [p.to_dict() for p in ps] if ps else None
        d["score"] = bd if m["status"] == "committed" and bd else live
        d["climbs"] = m.get("climbs") or {}
        d["fouls"] = store.fouls(m["key"])
        d["suggested_video"] = suggested_video(m)
    return d


def build_state():
    m = current()
    ms = store.matches()
    return {
        "now": time.time(),
        "event": {k: CFG["event"][k] for k in ("name", "teams", "tba_event_key", "lunch", "lunch_end",
                                                    "quals_after_lunch", "day_end", "qual_start", "cycle_min")},
        "game": G,
        "game_id": CFG["event"]["game"],
        "rank_cols": [[f"avg_{n}", label] for n, _, label in RULES.TIEBREAKS],
        "current": match_public(m, True) if m else None,
        "matches": [match_public(x) for x in ms],
        "rankings": ranks(),
        "alliances": store.get("alliances"),
        "champion": bracket.champion({x["key"]: x for x in ms}),
        "vision": {h: {**v, "age": round(time.time() - v["seen"], 1)} for h, v in vision_status.items()},
        "feeds": feeds_public(),
        "tba": {"configured": tba.configured, "key": tba.key, **store.outbox_stats()},
        "webcast": store.get("webcast") or {},
        "selection": selection_public(),
        "display": store.get("display") or "match",
        "red_side": red_side(),
        "red_side_default": CFG["display"]["red_side"],
        "red_side_source": "setup" if store.get("red_side") else "event.yaml",
    }


def red_side():
    """Screen order only: Setup's live choice, else display.red_side from event.yaml."""
    return store.get("red_side") or CFG["display"]["red_side"]


def selection_public():
    sel = store.get("selection")
    if not sel:
        return None
    s = bracket.selection(sel["order"], sel["picks"])
    s["order"] = sel["order"]
    s["saved"] = bool(sel.get("saved"))
    return s


async def broadcast():
    if not clients:
        return
    msg = json.dumps(build_state())
    dead = []
    for ws in list(clients):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


def _feed_link(f):
    return time.time() - feed_seen <= 2 and f.get("reply_age") is not None and f["reply_age"] <= 1


def field_first_inactive():
    """The auto result as a fed field's hub lights show it (first feed with a fresh answer)."""
    for f in feed_status:
        r = f.get("reply") or {}
        fi = _feed_link(f) and game.first_inactive_from_hubs(r.get("shift"), r.get("hub_active"))
        if fi:
            return fi
    return None


def feeds_public():
    out = []
    for f in feed_status:
        r = f.get("reply") or {}
        out.append({"name": f.get("name"), "dest": f.get("dest"), "error": f.get("error"),
                    "sent": f.get("sent"), "link": _feed_link(f),
                    "match_state": r.get("match_state"), "shift": r.get("shift"),
                    "first_inactive": game.first_inactive_from_hubs(r.get("shift"), r.get("hub_active"))
                    if _feed_link(f) else None})
    return out


async def tick():
    last = 0.0
    while True:
        await asyncio.sleep(0.25)
        now = time.time()
        push = _dirty.is_set()
        m = current()
        if m and m["status"] == "running":
            ps = periods_of(m)
            if not REEF and m.get("first_inactive") is None and now >= ps[0].end + GRACE:
                _, bd = compute(m)
                fi, coin = game.decide_first_inactive(bd["red"]["auto_fuel"], bd["blue"]["auto_fuel"])
                store.update_match(m["key"], first_inactive=fi, fi_coin=int(coin))
                push = True
            # With a fed field (e.g. bioarena), its hub lights ARE the auto result: adopt them
            # once per match, as soon as a shift shows them. Later manual overrides still stick.
            fi = field_first_inactive() if now >= ps[0].end and not REEF else None
            if fi and (m["key"], m["auto_start"]) not in _field_applied:
                _field_applied.add((m["key"], m["auto_start"]))
                m = store.match(m["key"])
                if fi != m.get("first_inactive"):
                    print(f"[field] {m['key']}: field lights show {fi} first inactive "
                          f"(Watchtower had {m.get('first_inactive')})")
                    store.update_match(m["key"], first_inactive=fi, fi_coin=0)
                store.update_match(m["key"], fi_locked=1)
                push = True
            if now >= ps[-1].end + GRACE:
                store.update_match(m["key"], status="review")
                push = True
            if now - last >= 0.5:
                push = True
        if push:
            _dirty.clear()
            last = now
            await broadcast()


def mark_dirty():
    _dirty.set()


@asynccontextmanager
async def lifespan(app):
    threading.Thread(target=tba.run_forever, daemon=True).start()
    if CFG["cloud"]["url"] and CFG["cloud"]["token"]:
        threading.Thread(target=cloud.run_forever, args=(sync_cloud,), daemon=True).start()
    task = asyncio.create_task(tick())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def revalidate_static(request, call_next):
    # Pages are no-store; make CSS/JS revalidate too, or phones keep an old style.css after an update.
    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def page(name):
    return FileResponse(STATIC / f"{name}.html", headers={"Cache-Control": "no-store"})


@app.get("/")
def index():
    return page("index")


for _p in ("control", "ref", "emcee", "score", "display", "overlay"):
    app.add_api_route(f"/{_p}", (lambda n=_p: page(n)), methods=["GET"])


@app.get("/manifest/{role}.json")
def manifest(role: str):
    names = {"ref": "FMS Ref", "emcee": "FMS Emcee", "score": "FMS Scorer", "control": "FMS Control",
             "display": "FMS Display"}
    return JSONResponse({"name": names.get(role, "FMS"), "short_name": names.get(role, "FMS"),
                         "start_url": f"/{role}", "display": "standalone",
                         "background_color": "#e8e9e4", "theme_color": "#1d2127",
                         "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}]})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    await ws.send_text(json.dumps(build_state()))
    try:
        while True:
            await ws.receive_text()  # clients may ping; content ignored
    except WebSocketDisconnect:
        clients.discard(ws)


@app.get("/api/state")
def api_state():
    return build_state()


_login_fails: list = []   # times of recent wrong PINs (all roles, all devices)


@app.post("/api/login")
def login(body: dict = Body(...)):
    # 6-digit PINs: after 10 wrong tries in a minute, refuse for the rest of that minute, so guessing
    # one from a phone on the venue WiFi would take weeks. The right PIN still works between tries.
    now = time.time()
    _login_fails[:] = [t for t in _login_fails if now - t < 60]
    if len(_login_fails) >= 10:
        raise HTTPException(429, "Too many wrong PINs. Wait a minute and try again.")
    role, pin = body.get("role"), str(body.get("pin", ""))
    if role in CFG["server"]["pins"] and not str(CFG["server"]["pins"][role]).strip():
        raise HTTPException(403, f"No {role} PIN is set. Control sets one in /control -> Setup -> PINs.")
    if role not in CFG["server"]["pins"] or not secrets.compare_digest(pin, str(CFG["server"]["pins"][role])):
        _login_fails.append(now)
        raise HTTPException(401, "Wrong PIN")
    return {"token": token_for(role), "role": role}


# ------------------------------------------------------------------ live settings (Setup)
def settings_entries():
    return store.get("settings") or {}


def apply_settings(entries):
    """Store and run on new settings entries. ValueError (nothing changed) if the result is invalid."""
    new = settings.overlay(BASE, entries)
    settings.check(new)
    vision_before = copy.deepcopy((CFG["field"], CFG["vision"]))
    store.set("settings", entries)
    settings.apply_in_place(CFG, new)
    if (CFG["field"], CFG["vision"]) != vision_before:
        store.set("vision_rev", (store.get("vision_rev") or 0) + 1)
    mark_dirty()


@app.get("/api/settings")
def get_settings(x_fms_token: str = Header(None)):
    need(x_fms_token)
    return {"settings": settings.public(CFG, settings_entries()), "vision_key": CFG["server"]["vision_key"],
            "lan_urls": cloud.lan_urls(CFG["server"]["port"]),
            "cloud": {"url": CFG["cloud"]["url"], "event": CFG["cloud"]["event"], **cloud.status}}


def running_match():
    return next((m for m in store.matches() if m["status"] == "running"), None)


def cams_locked():
    """Setup -> Cameras can't change a hub's camera, outline or ball size while a match runs (a stray
    click would change the count mid-match), unless Control unlocked it for this match."""
    m = running_match()
    return bool(m) and vision_unlock["key"] != m["key"]


@app.post("/api/settings")
def post_settings(body: dict = Body(...), x_fms_token: str = Header(None)):
    """{"changes": {key: value}}; a value of null puts that key back to event.yaml."""
    need(x_fms_token)
    if any(str(k).startswith("vision.hubs.") for k in body.get("changes") or {}) and cams_locked():
        raise HTTPException(409, LOCKED)
    try:
        changes = settings.clean(body.get("changes") or {})
        apply_settings(settings.merge(settings_entries(), settings.stamp(changes, time.time())))
    except ValueError as e:
        raise HTTPException(400, str(e))
    cloud.poke()
    return {"settings": settings.public(CFG, settings_entries())}


def sync_cloud():
    """One round with the website (fms/cloud.py calls this): send our settings and status, take theirs."""
    m = current()
    status = {"match": m["key"] if m else None, "match_status": m["status"] if m else None,
              "played": sum(1 for x in store.matches() if x["status"] == "committed"),
              "lan_urls": cloud.lan_urls(CFG["server"]["port"]),
              "vision": {h: v.get("fps") for h, v in vision_status.items() if time.time() - v["seen"] < 5}}
    theirs = cloud.exchange(CFG["cloud"], settings_entries(), status)
    if theirs is None:
        return
    merged = settings.merge(settings_entries(), theirs)
    if merged != settings_entries():
        try:
            apply_settings(merged)
        except ValueError as e:
            cloud.status["error"] = f"website settings not applied: {e}"


# ------------------------------------------------------------------ schedule
def _schedule_locked():
    return any(m["auto_start"] is not None or m["status"] == "committed" for m in store.matches("qm"))


def _day(body):
    """Schedule clock from the Schedule form, falling back to event.yaml. Times come back as HH:MM."""
    e = CFG["event"]
    try:
        start, ls, le, de = (schedule.hm(body.get(k) or e[c]).strftime("%H:%M") for k, c in (
            ("start", "qual_start"), ("lunch", "lunch"), ("lunch_end", "lunch_end"), ("day_end", "day_end")))
        cycle = float(body.get("cycle_min") or e["cycle_min"])
    except ValueError:
        raise HTTPException(400, "Times must look like 13:00, and the cycle must be a number")
    if le <= ls:
        raise HTTPException(400, "Lunch must end after it starts")
    after = bool(body.get("after_lunch", e["quals_after_lunch"]))
    return {"start": start, "cycle": cycle, "lunch": (ls, le), "after_lunch": after, "day_end": de}


def _preview(matches, day):
    """Fill in times (unless every match has one) and say when quals end and whether that's in time."""
    ls, le = day["lunch"]
    if not all(m.get("scheduled") for m in matches):
        schedule.add_times(matches, day["start"], day["cycle"], day["lunch"] if day["after_lunch"] else None)
    end = (schedule.hm(matches[-1]["scheduled"]) + timedelta(minutes=day["cycle"])).strftime("%H:%M")
    limit = day["day_end"] if day["after_lunch"] else ls
    return {"matches": matches, "quals_end": end, "fits": schedule.hm(end) <= schedule.hm(limit),
            "limit": limit, "limit_name": "the end of the day" if day["after_lunch"] else "lunch",
            "lunch": {"start": ls, "end": le} if day["after_lunch"] else None,
            "after_lunch": sum(schedule.hm(m["scheduled"]) >= schedule.hm(le) for m in matches)}


def _lunch_breaks(m_count, day):
    """Match numbers the lunch break follows, for a schedule of m_count matches on this day's clock."""
    if not day["after_lunch"]:
        return []
    ms = schedule.add_times([{} for _ in range(m_count)], day["start"], day["cycle"], day["lunch"])
    return schedule.breaks_from_times(ms, day["cycle"])


@app.post("/api/schedule/preview")
def schedule_preview(body: dict = Body(...), x_fms_token: str = Header(None)):
    need(x_fms_token)
    mpt = int(body.get("matches_per_team", 6))
    day = _day(body)
    seed = str(body.get("seed") or "").strip() or str(random.SystemRandom().randrange(1, 1_000_000))
    teams = CFG["event"]["teams"]
    if len(teams) < 6:
        raise HTTPException(400, "Add at least 6 team numbers to event.teams in config/event.yaml, then restart the server")
    breaks = _lunch_breaks(schedule.match_count(len(teams), mpt), day)
    matches, rep = schedule.generate(teams, mpt, breaks=breaks, seed=seed)
    out = _preview(matches, day)
    out.update(report=rep, seed=seed, breaks=breaks, matches_per_team=mpt)
    return out


@app.post("/api/schedule/import")
def schedule_import(body: dict = Body(...), x_fms_token: str = Header(None)):
    need(x_fms_token)
    matches = schedule.parse_csv(body.get("csv", ""))
    if not matches:
        raise HTTPException(400, "No rows parsed. Format: match,red1,red2,red3,blue1,blue2,blue3[,HH:MM]")
    unknown = sorted({t for m in matches for t in m["red"] + m["blue"]} - set(CFG["event"]["teams"]))
    day = _day(body)
    try:
        out = _preview(matches, day)
    except ValueError:
        raise HTTPException(400, "A time in the CSV isn't HH:MM")
    out["unknown_teams"] = unknown
    out["breaks"] = schedule.breaks_from_times(matches, day["cycle"])
    out["report"] = schedule.report(matches, CFG["event"]["teams"], out["breaks"])
    return out


@app.post("/api/schedule/save")
async def schedule_save(body: dict = Body(...), x_fms_token: str = Header(None)):
    need(x_fms_token)
    if _schedule_locked():
        raise HTTPException(409, "A qualification match already started. Schedule is locked.")
    store.delete_matches("qm")
    base = store.next_ord()
    for i, m in enumerate(body["matches"], 1):
        store.insert_match({"key": f"qm{i}", "comp_level": "qm", "set_number": 1, "match_number": i,
                            "ord": base + i, "red": m["red"], "blue": m["blue"],
                            "surrogates": m.get("surrogates", []), "scheduled": m.get("scheduled"),
                            "status": "scheduled", "adjust": {}, "climbs": {}})
    # How it was made, so a generated schedule can be reproduced from its seed.
    store.set("schedule_meta", {"source": "generated" if body.get("seed") else "import",
                                "seed": body.get("seed"), "matches_per_team": body.get("matches_per_team"),
                                "breaks": body.get("breaks") or [], "saved_at": time.time()})
    store.set("current", "qm1")
    mark_dirty()
    return {"ok": True, "count": len(body["matches"])}


@app.post("/api/schedule/retime")
async def schedule_retime(body: dict = Body(...), x_fms_token: str = Header(None)):
    """Shift the times of unplayed matches; teams and results are untouched, so this works mid-event."""
    need(x_fms_token)
    ms = store.matches()
    try:
        cycle = float(body.get("cycle_min") or CFG["event"]["cycle_min"])
        times = schedule.retime(ms, body["from"], body["start"], cycle)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, f"Need a match, a start time as HH:MM and a cycle in minutes ({e})")
    for k, t in times.items():
        store.update_match(k, scheduled=t)
    changed = [m for m in store.matches() if m["key"] in times]
    if changed:
        tba.matches([tba.match(m, None, time_utc(m)) for m in changed])
    mark_dirty()
    return {"count": len(times), "last": changed[-1]["scheduled"] if changed else None}


@app.post("/api/schedule/time")
async def schedule_time(body: dict = Body(...), x_fms_token: str = Header(None)):
    """Set one match's time (blank clears it). Only the time changes, so any match can be edited."""
    need(x_fms_token)
    m = _get(body.get("key") or "")
    try:
        t = schedule.hhmm(body.get("scheduled"))
    except ValueError:
        raise HTTPException(400, "Enter the time as HH:MM, e.g. 13:05")
    store.update_match(m["key"], scheduled=t)
    m = store.match(m["key"])
    tba.one_match(m, m.get("breakdown"), time_utc(m))  # breakdown = what TBA last got (kept while reopened)
    mark_dirty()
    return {"ok": True, "scheduled": t}


@app.post("/api/schedule/teams")
async def schedule_teams(body: dict = Body(...), x_fms_token: str = Header(None)):
    """Change who plays in one match (replacement robots). A committed match must be reopened first;
    recommitting it re-sends the result and rankings with the new teams."""
    need(x_fms_token)
    m = _get(body.get("key") or "")
    if m["status"] == "running":
        raise HTTPException(409, f"{m['key']} is running. Press Reset match first.")
    _editable(m)
    try:
        lu = schedule.lineup(body.get("red") or [], body.get("blue") or [], CFG["event"]["teams"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    store.update_match(m["key"], **lu)
    m = store.match(m["key"])
    tba.one_match(m, m.get("breakdown"), time_utc(m))  # breakdown = what TBA last got (kept while reopened)
    mark_dirty()
    return {"ok": True}


@app.post("/api/schedule/add")
async def schedule_add(body: dict = Body(...), x_fms_token: str = Header(None)):
    """Add one qual match (e.g. one deleted by mistake). With `auto_start` (HH:MM:SS on event day,
    when auto really started) it goes straight to review, scored from the raw fuel still stored for
    that time; without it, it's scheduled like any other match."""
    need(x_fms_token)
    qms = store.matches("qm")
    try:
        lu = schedule.lineup(body.get("red") or [], body.get("blue") or [], CFG["event"]["teams"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        n = int(body.get("match_number") or 0) or max((x["match_number"] for x in qms), default=0) + 1
    except ValueError:
        n = 0
    if n < 1:
        raise HTTPException(400, "Match number must be a whole number, 1 or more")
    try:
        t = schedule.hhmm(body.get("scheduled"))
    except ValueError:
        raise HTTPException(400, "Enter the time as HH:MM, e.g. 13:05")
    a0 = None
    if (body.get("auto_start") or "").strip():
        date = CFG["event"]["date"] or datetime.now().strftime("%Y-%m-%d")
        try:
            a0 = schedule.clock_to_epoch(body["auto_start"], date, CFG["event"]["utc_offset_hours"])
        except ValueError:
            raise HTTPException(400, f"Enter auto start as HH:MM:SS, e.g. 13:05:42 (event date {date})")
    key = f"qm{n}"
    if store.match(key):
        raise HTTPException(409, f"{key} already exists")
    if a0 is not None:
        if a0 > time.time():
            raise HTTPException(400, "Auto start is in the future (check the time and event.date)")
        span = (lambda ps: ps[-1].end - ps[0].start)(RULES.build_periods(G, a0))
        clash = [x["key"] for x in store.matches() if x.get("auto_start") is not None
                 and abs(x["auto_start"] + (x.get("offset") or 0) - a0) < span]
        if clash:  # the same fuel would count in both matches
            raise HTTPException(409, f"That auto start overlaps {', '.join(clash)}")
    before = [x["ord"] for x in qms if x["match_number"] < n]
    after = [x["ord"] for x in qms if x["match_number"] > n]
    ord_ = max(before) + 1 if before else min(after) if after else store.next_ord()
    store.x("UPDATE matches SET ord=ord+1 WHERE ord>=?", (ord_,))  # make room, keep play order
    store.insert_match({"key": key, "comp_level": "qm", "set_number": 1, "match_number": n, "ord": ord_,
                        **lu, "scheduled": t, "status": "review" if a0 is not None else "scheduled",
                        "auto_start": a0, "adjust": {}, "climbs": {}})
    m = store.match(key)
    tba.one_match(m, None, time_utc(m))
    mark_dirty()
    return {"ok": True, "key": key, "status": m["status"]}


@app.post("/api/schedule/delete")
async def schedule_delete(body: dict = Body(...), x_fms_token: str = Header(None)):
    """Drop one qual match (running behind). Others keep their numbers; it also comes off TBA.
    Raw fuel stays (it's keyed by time, not match); the match's fouls are soft-deleted."""
    need(x_fms_token)
    m = _get(body.get("key") or "")
    if m["comp_level"] != "qm":
        raise HTTPException(400, "Playoff matches come from the bracket and can't be deleted")
    if m["status"] == "running":
        raise HTTPException(409, f"{m['key']} is running. Press Reset match first.")
    _editable(m)
    store.x("DELETE FROM matches WHERE key=?", (m["key"],))
    store.x("UPDATE fouls SET deleted=1 WHERE match_key=?", (m["key"],))
    if store.get("current") == m["key"]:
        ms = store.matches()
        nxt = next((x for x in ms if x["ord"] > m["ord"] and x["status"] == "scheduled"),
                   next((x for x in ms if x["status"] == "scheduled"), None))
        store.set("current", nxt["key"]) if nxt else store.delete("current")
    tba.delete_matches([m["key"]])
    push_ranks()
    mark_dirty()
    return {"ok": True}

@app.get("/api/schedule/report")
def schedule_report(x_fms_token: str = Header(None)):
    """Quality report for the saved qualification schedule."""
    need(x_fms_token)
    qm = store.matches("qm")
    meta = store.get("schedule_meta") or {}
    breaks = meta.get("breaks")
    if breaks is None:      # saved before schedule_meta existed: infer lunch from the clock
        breaks = schedule.breaks_from_times(qm, float(CFG["event"]["cycle_min"]))
    rep = schedule.report(qm, CFG["event"]["teams"], breaks)
    rep["seed"] = meta.get("seed")
    return rep


@app.post("/api/tba/schedule")
def tba_schedule(x_fms_token: str = Header(None)):
    need(x_fms_token)
    if not tba.key:
        raise HTTPException(400, "Set event.tba_event_key in config/event.yaml first")
    tba.team_list(CFG["event"]["teams"])
    qm = store.matches("qm")
    tba.matches([tba.match(m, m.get("breakdown"), time_utc(m)) for m in qm])
    return {"queued": len(qm)}


# ------------------------------------------------------------------ match control
def _get(key=None):
    m = store.match(key) if key else current()
    if not m:
        raise HTTPException(404, "No such match")
    return m


def _editable(m):
    if m["status"] == "committed":
        raise HTTPException(409, f"{m['key']} is committed. Reopen it to edit.")


@app.post("/api/match/select")
async def select(body: dict = Body(...), x_fms_token: str = Header(None)):
    need(x_fms_token)
    running = [m for m in store.matches() if m["status"] == "running"]
    if running and running[0]["key"] != body["key"]:
        raise HTTPException(409, f"{running[0]['key']} is running")
    _get(body["key"])
    store.set("current", body["key"])
    mark_dirty()
    return {"ok": True}


@app.post("/api/match/start")
async def start(body: dict = Body(default={}), x_fms_token: str = Header(None)):
    need(x_fms_token)
    now = time.time()
    m = _get(body.get("key"))
    if m["status"] != "scheduled":
        raise HTTPException(409, f"{m['key']} is {m['status']}. Reset it to restart.")
    if any(x["status"] == "running" for x in store.matches()):
        raise HTTPException(409, "Another match is running")
    store.update_match(m["key"], auto_start=now, status="running", offset=0.0,
                       first_inactive=None, fi_locked=0, fi_coin=0)
    store.set("current", m["key"])
    store.set("display", "match")
    mark_dirty()
    return {"ok": True, "auto_start": now}


@app.post("/api/match/teleop")
async def teleop(body: dict = Body(default={}), x_fms_token: str = Header(None)):
    need(x_fms_token)
    now = time.time()
    m = _get(body.get("key"))
    if m["status"] != "running":
        raise HTTPException(409, "Match not running")
    store.update_match(m["key"], teleop_start=now)
    mark_dirty()
    return {"ok": True}


@app.post("/api/match/offset")
async def offset(body: dict = Body(...), x_fms_token: str = Header(None)):
    need(x_fms_token)
    m = _get(body.get("key"))
    _editable(m)
    off = max(-15.0, min(15.0, float(body["offset"])))
    store.update_match(m["key"], offset=off)
    mark_dirty()
    return {"ok": True, "offset": off}


@app.post("/api/match/first_inactive")
async def first_inactive(body: dict = Body(...), x_fms_token: str = Header(None)):
    role = need(x_fms_token, "emcee")
    _rebuilt_only()
    m = _get(body.get("key"))
    _editable(m)
    a = body.get("alliance") or m.get("first_inactive")
    if a not in game.ALLIANCES:
        raise HTTPException(409, "Auto result not decided yet")
    if role == "emcee" and a != m.get("first_inactive"):
        raise HTTPException(403, "Only Control can override the auto result")
    store.update_match(m["key"], first_inactive=a, fi_locked=int(bool(body.get("lock", True))))
    mark_dirty()
    return {"ok": True}


@app.post("/api/match/adjust")
async def adjust(body: dict = Body(...), x_fms_token: str = Header(None)):
    """Send {"total": n} to set the final count for a period (server computes the delta),
    or {"delta": n} to nudge it."""
    need(x_fms_token)
    _rebuilt_only()
    m = _get(body.get("key"))
    _editable(m)
    a, p = body["alliance"], body["period"]
    if a not in game.ALLIANCES or p not in game.period_names(G):
        raise HTTPException(400, "bad alliance/period")
    _, bd = compute(m)
    if bd is None:
        raise HTTPException(409, "Match hasn't started")
    adj = m.get("adjust") or {}
    adj.setdefault(a, {})
    if "total" in body:
        adj[a][p] = int(body["total"]) - bd[a]["vision_fuel"][p]
    else:
        adj[a][p] = int(adj[a].get(p, 0)) + int(body["delta"])
    store.update_match(m["key"], adjust=adj)
    mark_dirty()
    return {"ok": True}


@app.post("/api/match/climb")
async def climb(body: dict = Body(...), x_fms_token: str = Header(None)):
    need(x_fms_token)
    _rebuilt_only()
    m = _get(body.get("key"))
    _editable(m)
    a, i = body["alliance"], int(body["idx"])
    c = m.get("climbs") or {}
    robots = c.get(a) or [{"auto_l1": False, "level": 0} for _ in range(3)]
    lvl = int(body.get("level", robots[i]["level"]))
    if lvl not in (0, 1, 2, 3):
        raise HTTPException(400, "level must be 0-3")
    robots[i] = {"auto_l1": bool(body.get("auto_l1", robots[i]["auto_l1"])), "level": lvl}
    c[a] = robots
    store.update_match(m["key"], climbs=c)
    mark_dirty()
    return {"ok": True}


def _rebuilt_only():
    if REEF:
        raise HTTPException(400, "Not part of REEFSCAPE (this event's game)")


def _reef_only():
    if not REEF:
        raise HTTPException(400, "Only for REEFSCAPE events")


@app.post("/api/match/count")
async def count(body: dict = Body(...), x_fms_token: str = Header(None)):
    """REEFSCAPE: {"alliance", "item" (reefscape.ITEMS), "value": n} sets one count Control typed."""
    need(x_fms_token)
    _reef_only()
    m = _get(body.get("key"))
    _editable(m)
    a, item = body.get("alliance"), body.get("item")
    if a not in game.ALLIANCES or item not in reefscape.ITEMS:
        raise HTTPException(400, "bad alliance/item")
    try:
        n = int(body.get("value") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "Enter a whole number")
    if not 0 <= n <= 999:
        raise HTTPException(400, "Counts are 0 to 999")
    c = m.get("adjust") or {}
    c.setdefault(a, {})[item] = n
    store.update_match(m["key"], adjust=c)
    mark_dirty()
    return {"ok": True}


def _scorable(role, m):
    """REEFSCAPE scoring edits. Scorers (/score) only touch a started match; control can edit any uncommitted one."""
    _reef_only()
    _editable(m)
    if role != "control" and m["status"] not in ("running", "review"):
        raise HTTPException(409, f"{m['key']} hasn't started yet")


@app.post("/api/match/tally")
async def tally(body: dict = Body(...), x_fms_token: str = Header(None)):
    """REEFSCAPE live scoring: {"key", "alliance", "item" (L1-L4|processor|net), "period" (auto|teleop), "delta": 1|-1}.
    Adds to the count instead of setting it, so several scorers can tap the same alliance. Never below 0."""
    role = need(x_fms_token, "scorer")
    m = _get(body.get("key"))
    _scorable(role, m)
    a, item, period = body.get("alliance"), body.get("item"), body.get("period")
    if a not in game.ALLIANCES or item not in reefscape.LEVELS + reefscape.ALGAE or period not in ("auto", "teleop"):
        raise HTTPException(400, "bad alliance/item/period")
    delta = body.get("delta")
    if delta not in (1, -1):
        raise HTTPException(400, "delta must be 1 or -1")
    k = f"{period}_{item}"
    c = m.get("adjust") or {}
    n = max(0, int(c.setdefault(a, {}).get(k, 0) or 0) + delta)
    c[a][k] = n
    store.update_match(m["key"], adjust=c)
    mark_dirty()
    return {"ok": True, "item": k, "value": n}


@app.post("/api/match/robot")
async def robot(body: dict = Body(...), x_fms_token: str = Header(None)):
    """REEFSCAPE: {"alliance", "idx", "leave": bool, "barge": none|park|shallow|deep} for one robot."""
    role = need(x_fms_token, "scorer")
    m = _get(body.get("key"))
    _scorable(role, m)
    a = body.get("alliance")
    if a not in game.ALLIANCES:
        raise HTTPException(400, "bad alliance")
    n = len(m[a])
    try:
        i = int(body["idx"])
    except (KeyError, TypeError, ValueError):
        i = -1
    if not 0 <= i < n:
        raise HTTPException(400, f"idx must be 0-{n - 1}")
    c = m.get("climbs") or {}
    robots = [{**reefscape.blank_robot(), **r} for r in (c.get(a) or [])[:n]]
    robots += [reefscape.blank_robot() for _ in range(n - len(robots))]
    if "leave" in body:
        robots[i]["leave"] = bool(body["leave"])
    if "barge" in body:
        if body["barge"] not in reefscape.BARGE:
            raise HTTPException(400, "barge must be " + ", ".join(reefscape.BARGE))
        robots[i]["barge"] = body["barge"]
    c[a] = robots
    store.update_match(m["key"], climbs=c)
    mark_dirty()
    return {"ok": True}


@app.post("/api/match/winner")
async def winner(body: dict = Body(...), x_fms_token: str = Header(None)):
    need(x_fms_token)
    m = _get(body.get("key"))
    _editable(m)
    w = body.get("winner")
    store.update_match(m["key"], winner_override=w if w in game.ALLIANCES else None)
    mark_dirty()
    return {"ok": True}


@app.post("/api/match/video")
async def video(body: dict = Body(...), x_fms_token: str = Header(None)):
    need(x_fms_token)
    m = _get(body.get("key"))
    v = (body.get("youtube") or "").strip() or None
    if v and v.lower() == "none":  # no video, not even the suggested one (TBA can't remove one already sent)
        v = "none"
    store.update_match(m["key"], video=v)
    if m["status"] == "committed" and v and v != "none":
        tba.video(m["key"], v)
    mark_dirty()
    return {"ok": True}


@app.post("/api/match/reset")
async def reset(body: dict = Body(...), x_fms_token: str = Header(None)):
    need(x_fms_token)
    m = _get(body.get("key"))
    _editable(m)
    store.update_match(m["key"], status="scheduled", auto_start=None, teleop_start=None, offset=0.0,
                       first_inactive=None, fi_locked=0, fi_coin=0, adjust={}, climbs={},
                       winner_override=None, breakdown=None)
    if body.get("clear_fouls"):
        store.x("UPDATE fouls SET deleted=1 WHERE match_key=?", (m["key"],))
    mark_dirty()
    return {"ok": True}


@app.post("/api/match/commit")
async def commit(body: dict = Body(...), x_fms_token: str = Header(None)):
    need(x_fms_token)
    m = _get(body.get("key"))
    if m["status"] != "review":
        raise HTTPException(409, f"{m['key']} is {m['status']}; only a finished match in review can be committed")
    _, bd = compute(m)
    if m["comp_level"] != "qm" and bd["winner"] not in game.ALLIANCES:
        raise HTTPException(409, "Playoff match tied: pick a winner (or reset and replay) first")
    store.update_match(m["key"], breakdown=bd, status="committed", committed_at=time.time())
    m = store.match(m["key"])
    tba.one_match(m, bd, time_utc(m))
    if m["comp_level"] == "qm":
        push_ranks()
    vid = m.get("video") or suggested_video(m)
    if vid and vid != "none":
        store.update_match(m["key"], video=vid)
        tba.video(m["key"], vid)
    if m["comp_level"] != "qm":
        _advance_bracket()
    nxt = next((x for x in store.matches() if x["status"] == "scheduled"), None)
    if nxt:
        store.set("current", nxt["key"])
    mark_dirty()
    return {"ok": True, "breakdown": bd, "next": nxt["key"] if nxt else None}


@app.post("/api/match/reopen")
async def reopen(body: dict = Body(...), x_fms_token: str = Header(None)):
    need(x_fms_token)
    m = _get(body.get("key"))
    if m["status"] != "committed":
        raise HTTPException(409, "Not committed")
    later = [x for x in store.matches() if x["comp_level"] != "qm" and x["ord"] > m["ord"]]
    if m["comp_level"] != "qm" and any(x["auto_start"] for x in later):
        raise HTTPException(409, "A later playoff match already started; can't change this result")
    store.update_match(m["key"], status="review")
    store.set("current", m["key"])
    mark_dirty()
    return {"ok": True}


@app.get("/api/match/{key}")
def match_detail(key: str):
    return match_public(_get(key), True)


# ------------------------------------------------------------------ fouls
@app.post("/api/fouls")
async def add_foul(body: dict = Body(...), x_fms_token: str = Header(None)):
    need(x_fms_token, "ref")
    m = _get(body.get("key"))
    _editable(m)
    if body["committed_by"] not in game.ALLIANCES or body["kind"] not in ("minor", "major"):
        raise HTTPException(400, "bad foul")
    fid = store.add_foul(m["key"], body["committed_by"], body["kind"],
                         body.get("team"), (body.get("ref") or "")[:40])
    mark_dirty()
    return {"ok": True, "id": fid, "match": m["key"]}


@app.post("/api/fouls/{fid}")
async def edit_foul(fid: int, body: dict = Body(...), x_fms_token: str = Header(None)):
    need(x_fms_token, "ref")
    f = store.q("SELECT * FROM fouls WHERE id=?", (fid,))
    if not f:
        raise HTTPException(404, "no foul")
    _editable(_get(f[0]["match_key"]))
    if body.get("delete"):
        store.x("UPDATE fouls SET deleted=1 WHERE id=?", (fid,))
    if "team" in body:
        store.x("UPDATE fouls SET team=? WHERE id=?", (body["team"], fid))
    mark_dirty()
    return {"ok": True}


# ------------------------------------------------------------------ alliances / playoffs
def _advance_bracket():
    al = store.get("alliances")
    if not al:
        return
    ms = {x["key"]: x for x in store.matches() if x["comp_level"] != "qm"}
    for nm in bracket.pending_matches(al, ms):
        nm.update(ord=store.next_ord(), status="scheduled", adjust={}, climbs={})
        store.insert_match(nm)
        tba.one_match(nm, None)


def _playoffs_started():
    return any(x["auto_start"] for x in store.matches() if x["comp_level"] != "qm")


@app.post("/api/alliances")
async def alliances(body: dict = Body(...), x_fms_token: str = Header(None)):
    need(x_fms_token)
    _save_alliances([[int(t) for t in a if t] for a in body["alliances"]])
    return {"ok": True}


def _save_alliances(al):
    flat = [t for a in al for t in a]
    if len(al) != 4 or any(len(a) < 2 for a in al):
        raise HTTPException(400, "Need 4 alliances with at least captain + 1 pick")
    if len(flat) != len(set(flat)):
        raise HTTPException(400, "A team is on two alliances")
    if _playoffs_started():
        raise HTTPException(409, "Playoffs already started; alliances are locked")
    for x in [x for x in store.matches() if x["comp_level"] != "qm"]:
        store.x("DELETE FROM matches WHERE key=?", (x["key"],))
    store.set("alliances", al)
    tba.alliances(al)
    _advance_bracket()
    if not any(x["status"] == "scheduled" and x["comp_level"] == "qm" for x in store.matches()):
        store.set("current", "sf1m1")
    mark_dirty()


# Live selection: only the frozen rank order and the list of picks are stored; everything else is
# replayed by bracket.selection, so undo is just dropping the last pick.
def _sel():
    sel = store.get("selection")
    if not sel:
        raise HTTPException(409, "Alliance selection hasn't started")
    return sel


@app.post("/api/selection/start")
async def selection_start(x_fms_token: str = Header(None)):
    need(x_fms_token)
    if _playoffs_started():
        raise HTTPException(409, "Playoffs already started; alliances are locked")
    order = [r["team"] for r in ranks()]
    try:
        bracket.selection(order, [])
    except ValueError as e:
        raise HTTPException(400, str(e))
    store.set("selection", {"order": order, "picks": [], "started": time.time()})
    store.set("display", "selection")
    mark_dirty()
    return {"ok": True}


def _set_picks(sel, picks):
    try:
        bracket.selection(sel["order"], picks)
    except ValueError as e:
        raise HTTPException(409, str(e))
    sel["picks"], sel["saved"] = picks, False
    store.set("selection", sel)
    mark_dirty()
    return {"ok": True}


@app.post("/api/selection/pick")
async def selection_pick(body: dict = Body(...), x_fms_token: str = Header(None)):
    need(x_fms_token)
    sel = _sel()
    return _set_picks(sel, sel["picks"] + [int(body["team"])])


@app.post("/api/selection/undo")
async def selection_undo(x_fms_token: str = Header(None)):
    need(x_fms_token)
    sel = _sel()
    if not sel["picks"]:
        raise HTTPException(409, "No picks to undo")
    return _set_picks(sel, sel["picks"][:-1])


@app.post("/api/selection/cancel")
async def selection_cancel(x_fms_token: str = Header(None)):
    need(x_fms_token)
    store.delete("selection")
    store.set("display", "match")
    mark_dirty()
    return {"ok": True}


@app.post("/api/selection/finish")
async def selection_finish(x_fms_token: str = Header(None)):
    need(x_fms_token)
    sel = _sel()
    s = bracket.selection(sel["order"], sel["picks"])
    if not s["done"]:
        raise HTTPException(409, "Selection isn't finished")
    _save_alliances(s["alliances"])
    sel["saved"] = True
    store.set("selection", sel)
    mark_dirty()
    return {"ok": True}


@app.post("/api/display")
async def display_mode(body: dict = Body(...), x_fms_token: str = Header(None)):
    need(x_fms_token)
    mode = body.get("mode")
    if mode not in ("match", "selection"):
        raise HTTPException(400, "mode must be match or selection")
    store.set("display", mode)
    mark_dirty()
    return {"ok": True}


@app.post("/api/red_side")
async def set_red_side(body: dict = Body(...), x_fms_token: str = Header(None)):
    """side: left | right, or null to fall back to event.yaml. Presentation only; no data changes."""
    need(x_fms_token)
    side = body.get("side")
    if side is None:
        store.delete("red_side")
    elif side in config.RED_SIDES:
        store.set("red_side", side)
    else:
        raise HTTPException(400, "side must be left, right or null")
    mark_dirty()
    return {"red_side": red_side(), "override": store.get("red_side")}


# ------------------------------------------------------------------ wipe
@app.post("/api/admin/wipe")
async def wipe(body: dict = Body(...), x_fms_token: str = Header(None)):
    need(x_fms_token)
    if str(body.get("pin", "")) != str(CFG["server"]["pins"]["control"]):
        raise HTTPException(403, "Wrong Control PIN")
    scope = body.get("scope")
    if scope not in ("all", "playoffs"):
        raise HTTPException(400, "scope must be all or playoffs")
    if any(m["status"] == "running" for m in store.matches()):
        raise HTTPException(409, "A match is running")
    db = pathlib.Path(CFG["server"]["db"])
    dest = db.parent / "backups" / f"{db.stem}-{datetime.now():%Y%m%d-%H%M%S}-before-wipe-{scope}.sqlite3"
    store.backup(str(dest))
    store.wipe(scope)
    if scope == "playoffs":
        nxt = next((m for m in store.matches("qm") if m["status"] != "committed"), None)
        if nxt:
            store.set("current", nxt["key"])
        else:
            store.delete("current")
    mark_dirty()
    return {"ok": True, "backup": str(dest)}


# ------------------------------------------------------------------ TBA / webcast / export
@app.post("/api/tba/push_all")
def push_all(x_fms_token: str = Header(None)):
    need(x_fms_token)
    tba.team_list(CFG["event"]["teams"])
    ms = store.matches()
    if ms:
        tba.matches([tba.match(m, m.get("breakdown"), time_utc(m)) for m in ms])
    push_ranks()
    if store.get("alliances"):
        tba.alliances(store.get("alliances"))
    return {"ok": True}


@app.post("/api/tba/rankings")
def push_rankings(x_fms_token: str = Header(None)):
    need(x_fms_token)
    push_ranks()
    return {"ok": True}


@app.post("/api/tba/delete_match")
def tba_delete_match(body: dict = Body(...), x_fms_token: str = Header(None)):
    """Removes one match from TBA only. Local data is untouched; committing it again re-sends it."""
    need(x_fms_token)
    if not tba.key:
        raise HTTPException(400, "No TBA event key configured")
    m = _get(body.get("key"))
    tba.delete_matches([m["key"]])
    if body.get("refresh_rankings", True):
        push_ranks()
    mark_dirty()
    return {"ok": True}


@app.post("/api/tba/delete_all")
def tba_delete_all(x_fms_token: str = Header(None)):
    """Takes every match (the schedule and any results) and the rankings off TBA.
    Local data is untouched; 'Send schedule to TBA' puts the schedule back."""
    need(x_fms_token)
    if not tba.key:
        raise HTTPException(400, "No TBA event key configured")
    tba.delete_all_matches()
    tba.clear_rankings()
    mark_dirty()
    return {"ok": True}


@app.post("/api/tba/retry")
def tba_retry(x_fms_token: str = Header(None)):
    need(x_fms_token)
    store.x("UPDATE outbox SET state='pending', attempts=0 WHERE state='dead'")
    tba._wake.set()
    return {"ok": True}


@app.post("/api/webcast")
async def webcast(body: dict = Body(...), x_fms_token: str = Header(None)):
    need(x_fms_token)
    w = store.get("webcast") or {}
    if "vod_id" in body:
        w["vod_id"] = body["vod_id"].strip()
    if body.get("stream_start_now"):
        w["stream_start"] = time.time()
    if body.get("url"):
        w["url"] = body["url"].strip()
        tba.webcast(w["url"])
    store.set("webcast", w)
    mark_dirty()
    return w


@app.get("/api/export.csv")
def export():
    buf = io.StringIO()
    wr = csv.writer(buf)
    head = ["match", "status", "red1", "red2", "red3", "blue1", "blue2", "blue3", "red", "blue"]
    if REEF:
        cols = [*reefscape.ITEMS, "leave_points", "barge_points", "foul_points", "rp"]
        per = lambda b: [b["counts"][k] for k in reefscape.ITEMS] + [b["leave_points"], b["barge_points"],
                                                                       b["foul_points"], b["rp"]]
    else:
        names = game.period_names(G)
        cols = [*names, "tower", "fouls_drawn", "rp"]
        per = lambda b: [b["fuel"][n] for n in names] + [b["tower_points"], b["foul_points"], b["rp"]]
    for a in ("red", "blue"):
        head += [f"{a}_{c}" for c in cols]
    head.append("surrogates")       # last, so existing column positions don't move
    wr.writerow(head)
    for m in store.matches():
        bd = m.get("breakdown")
        row = [m["key"], m["status"], *m["red"], *m["blue"],
               bd["red"]["total"] if bd else "", bd["blue"]["total"] if bd else ""]
        for a in ("red", "blue"):
            row += per(bd[a]) if bd else [""] * len(cols)
        row.append(" ".join(str(t) for t in m.get("surrogates") or []))
        wr.writerow(row)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv")


# ------------------------------------------------------------------ vision
def _vision_auth(key):
    if not secrets.compare_digest(str(key or ""), str(CFG["server"]["vision_key"])):
        raise HTTPException(401, "bad vision key")


@app.post("/api/vision/events")
def vision_events(body: dict = Body(...), x_vision_key: str = Header(None)):
    global feed_seen
    _vision_auth(x_vision_key)
    rows = []
    for hub, evs in (body.get("events") or {}).items():
        if hub in game.ALLIANCES:
            rows += [(t, hub, n) for t, n in evs if n]
    if rows:
        store.add_fuel(rows, body.get("source", "live"))
    status = dict(body.get("status") or {})
    if isinstance(status.get("feeds"), list):
        feed_status[:] = [f for f in status.pop("feeds") if isinstance(f, dict)]
        feed_seen = time.time()
    if isinstance(status.get("files"), list):
        vision_files[:] = [f for f in status.pop("files") if isinstance(f, dict)]
    chk = vision_want["check"]
    prog = status.pop("checking", None)
    vision_check["progress"] = prog if chk and isinstance(prog, dict) and prog.get("id") == chk["id"] else None
    res = body.get("checked")
    if chk and isinstance(res, dict) and res.get("id") == chk["id"]:
        vision_check.update(result={**res, "t": time.time()}, progress=None)
        vision_want["check"] = None
    for hub, st in status.items():
        vision_status[hub] = {**st, "seen": time.time()}
    if rows:
        mark_dirty()
    m = current()
    rec = None
    if m and m.get("auto_start"):
        ps = periods_of(m)
        if m["status"] == "running" or (m["status"] == "review" and time.time() < ps[-1].end + 10):
            rec = f"{m['key']}_{int(m['auto_start'])}"
    for hub, res in (body.get("measured") or {}).items():
        d = vision_want["drop"].get(hub)
        if hub in game.ALLIANCES and isinstance(res, dict) and d and res.get("id") == d["id"]:
            vision_want["drop"].pop(hub, None)
            _calibrated(hub, res)
    for hub, res in (body.get("sized") or {}).items():
        d = vision_want["size"].get(hub)
        if hub in game.ALLIANCES and isinstance(res, dict) and d and res.get("id") == d["id"]:
            vision_want["size"].pop(hub, None)
            _sized(hub, res)
    # config_rev: vision re-fetches /api/vision/config when it changes. now: its clock check.
    # want: what Setup -> Cameras is waiting for (pictures, a camera scan, a drop test).
    return {"record": rec, "config_rev": store.get("vision_rev") or 0, "now": time.time(),
            "want": {"frames": time.time() < vision_want["frames_until"], "scan": vision_want["scan"],
                     "drop": copy.deepcopy(vision_want["drop"]), "size": copy.deepcopy(vision_want["size"]),
                     "record": vision_want["record"], "check": copy.deepcopy(vision_want["check"]),
                     "preview": _previews()}}


def _previews():
    now = time.time()
    return {h: p["colour"] for h, p in vision_want["preview"].items() if p["until"] > now}


def _at_setup_size(area, size, cur):
    """A pixel area vision measured on a w x h picture, at the size the hub's outline was drawn on."""
    (w, h), (sw, sh) = size or [0, 0], cur.get("size") or [0, 0]
    return area * (sw * sh) / (w * h) if w and h and sw and sh else area


def _sized(hub, res):
    """Measure's estimate from vision (dropfit.estimate): saved as the hub's `measured`, which draws the
    ruler and checks the drop test. The ball size counted with stays the drop test's."""
    cur = CFG["vision"]["hubs"].get(hub)
    now = time.time()
    if res.get("error") or not res.get("area"):
        vision_size[hub] = {"ok": False, "msg": res.get("error") or "Nothing measured.", "t": now}
    elif not cur:
        vision_size[hub] = {"ok": False, "msg": "Pick this hub's camera in Setup first.", "t": now}
    else:
        area = _at_setup_size(res["area"], res.get("size"), cur)
        try:
            apply_settings(settings.merge(settings_entries(), settings.stamp(settings.clean(
                {f"vision.hubs.{hub}": {**cur, "measured": round(area, 1)}}), now)))
            cloud.poke()
            msg = (f"Measured: one ball ≈ {area:.0f} px ({res.get('blobs')} lone balls in {res.get('frames')} frames). "
                   "The ruler now shows ball widths while you draw. Calibrate still sets the size it counts with.")
            off = cur.get("ball_area") and not 0.67 <= area / cur["ball_area"] <= 1.5
            vision_size[hub] = {"ok": not off, "t": now, "msg": msg + (
                f" The drop test fitted {cur['ball_area']:.0f} px, which is far from this: check the balls were "
                "apart and still, and that the outline covers the whole opening." if off else "")}
        except ValueError as e:
            vision_size[hub] = {"ok": False, "msg": str(e), "t": now}
    mark_dirty()


def _calibrated(hub, res):
    """A drop test's fit from vision (vision/dropfit.py): saved as this hub's ball_area, at the outline's
    picture size, with counts per ball back to 1 (the ball size now does that job)."""
    cur = CFG["vision"]["hubs"].get(hub)
    now = time.time()
    if res.get("error") or not res.get("area"):
        vision_drop[hub] = {"ok": False, "msg": res.get("error") or "Nothing to fit.", "t": now}
    elif not cur:
        vision_drop[hub] = {"ok": False, "msg": "Pick this hub's camera in Setup first.", "t": now}
    else:
        area = _at_setup_size(res["area"], res.get("size"), cur)
        try:
            apply_settings(settings.merge(settings_entries(), settings.stamp(settings.clean(
                {f"vision.hubs.{hub}": {**cur, "ball_area": round(area, 1)}, f"vision.scale.{hub}": 1}), now)))
            cloud.poke()
            got, n = res.get("got"), res.get("n")
            msg = f"Calibrated: one ball = {area:.0f} px. Replaying the test with it counts {got} of the {n} balls dropped."
            off = got is not None and n and abs(got - n) > max(1, 0.1 * n)
            meas = cur.get("measured")
            far = meas and not 0.67 <= area / meas <= 1.5            # Measure's still balls disagree
            vision_drop[hub] = {"ok": not (off or far), "t": now, "msg": msg + (
                " That's not close: check the outline covers the whole opening and run the test again." if off else "") + (
                f" Measure said about {meas:.0f} px, far from this: check how many balls you typed, or Measure again." if far else "")}
        except ValueError as e:
            vision_drop[hub] = {"ok": False, "msg": str(e), "t": now}
    mark_dirty()


@app.post("/api/vision/frames")
def vision_frames_in(body: dict = Body(...), x_vision_key: str = Header(None)):
    """Hub camera pictures for Setup -> Cameras (about one a second while it's open)."""
    _vision_auth(x_vision_key)
    for hub, f in body.items():
        if hub in game.ALLIANCES and isinstance(f, dict) and f.get("jpeg"):
            vision_frames[hub] = {"jpeg": base64.b64decode(f["jpeg"]), "w": int(f.get("w") or 0),
                                  "h": int(f.get("h") or 0), "source": str(f.get("source", "")), "t": time.time(),
                                  "fuel": bool(f.get("fuel"))}
    return {"ok": True}


@app.post("/api/vision/cameras")
def vision_cameras_in(body: dict = Body(...), x_vision_key: str = Header(None)):
    """The result of Find cameras: a picture from each camera no hub is using."""
    _vision_auth(x_vision_key)
    cams = [{"source": str(c["source"]), "w": int(c.get("w") or 0), "h": int(c.get("h") or 0),
             "jpeg": base64.b64decode(c.get("jpeg") or "")} for c in body.get("cameras") or [] if c.get("source") is not None]
    vision_cams.update(id=body.get("id"), t=time.time(), cameras=cams, error=body.get("error"),
                       in_use={str(k): v for k, v in (body.get("in_use") or {}).items()})
    return {"ok": True}


@app.post("/api/vision/want")
def vision_want_post(body: dict = Body(...), x_fms_token: str = Header(None)):
    """Setup -> Cameras: {"frames": true} (keep pictures coming ~15 s), {"scan": true}, and the drop test:
    {"drop": hub} starts recording, {"drop": hub, "n": balls} ends it (vision fits), {"drop": hub, "cancel": true}.
    {"measure": hub} (5 s of still balls); Setup -> Run: {"unlock": true/false} for the running match;
    Setup -> Test: {"record": true/false}, {"check": {"dir", "name", "hub", "hand"}} or {"check": false}.
    {"preview": {hub: colour range or null}}: paint what that range takes as fuel magenta on the hub's
    pictures for the next 20 s (the page repeats it while the switch is on); nothing is saved."""
    need(x_fms_token)
    for hub, colour in (body.get("preview") or {}).items():
        if hub in game.ALLIANCES:
            if colour is None:
                vision_want["preview"].pop(hub, None)
                continue
            try:
                vision_want["preview"][hub] = {"colour": settings.fuel_colour(colour) or dict(settings.COLOUR_STANDARD),
                                               "until": time.time() + 20}
            except ValueError as e:
                raise HTTPException(400, str(e))
    if "unlock" in body:
        m = running_match()
        vision_unlock["key"] = m["key"] if m and body["unlock"] else None
    if (body.get("drop") in game.ALLIANCES and not body.get("cancel") or body.get("measure") in game.ALLIANCES) and cams_locked():
        raise HTTPException(409, LOCKED)
    hub = body.get("measure")
    if hub in game.ALLIANCES:
        vision_want["size"][hub] = {"id": int(time.time() * 1000)}
        vision_size.pop(hub, None)
    if "record" in body:
        vision_want["record"] = time.strftime("test_%Y%m%d_%H%M%S") if body["record"] else None
    chk = body.get("check")
    if chk is False:
        vision_want["check"] = None
        vision_check["progress"] = None
    elif isinstance(chk, dict):
        if running_match():
            raise HTTPException(409, "Not during a match: re-counting a recording slows the live count.")
        if chk.get("hub") not in game.ALLIANCES or chk.get("dir") not in ("test", "match") or not chk.get("name"):
            raise HTTPException(400, "Pick a recording and its hub")
        try:
            hand = int(chk["hand"]) if chk.get("hand") not in (None, "") else None
        except (TypeError, ValueError):
            hand = 0
        if hand is not None and not 1 <= hand <= 10000:
            raise HTTPException(400, "Enter how many balls went in by hand")
        vision_want["check"] = {"id": int(time.time() * 1000), "dir": chk["dir"], "name": str(chk["name"]),
                                "hub": chk["hub"], "hand": hand}
        vision_check.update(progress=None, result=None)
    if body.get("frames"):
        vision_want["frames_until"] = time.time() + 15
    if body.get("scan"):
        vision_want["scan"] = int(time.time() * 1000)
        vision_cams["error"] = None
    hub = body.get("drop")
    if hub in game.ALLIANCES:
        if body.get("cancel"):
            vision_want["drop"].pop(hub, None)
        elif body.get("n") is not None:
            try:
                n = int(body["n"])
            except (TypeError, ValueError):
                n = 0
            if not 1 <= n <= 1000:
                raise HTTPException(400, "Enter how many balls you dropped")
            if hub not in vision_want["drop"]:
                raise HTTPException(409, "No drop test running for that hub")
            vision_want["drop"][hub]["n"] = n
        else:
            vision_want["drop"][hub] = {"id": int(time.time() * 1000), "n": None}
            vision_drop.pop(hub, None)
    return vision_setup(x_fms_token)


@app.get("/api/vision/setup")
def vision_setup(x_fms_token: str = Header(None)):
    """What Setup -> Cameras draws: picture times and sizes (images come from /api/vision/image/...)."""
    need(x_fms_token)
    now = time.time()
    return {"frames": {h: {k: f[k] for k in ("w", "h", "source", "t", "fuel")} for h, f in vision_frames.items() if now - f["t"] < 30},
            "preview": _previews(),
            "scan": {"asked": vision_want["scan"], "id": vision_cams["id"], "t": vision_cams["t"],
                     "error": vision_cams["error"], "in_use": vision_cams["in_use"],
                     "cameras": [{k: c[k] for k in ("source", "w", "h")} for c in vision_cams["cameras"]]},
            "drop": {h: {"n": d["n"]} for h, d in vision_want["drop"].items()}, "drop_result": vision_drop,
            "size": {h: {} for h in vision_want["size"]}, "size_result": vision_size,
            "record": vision_want["record"], "files": vision_files,
            "check": vision_want["check"], "check_progress": vision_check["progress"], "check_result": vision_check["result"],
            "match": (lambda m: m["key"] if m else None)(running_match()), "locked": cams_locked(),
            # live, unlike /ws state (pushed only on changes when no match runs)
            "vision": {h: {**v, "age": round(now - v["seen"], 1)} for h, v in vision_status.items()}}


@app.get("/api/vision/image/{kind}/{name}")
def vision_image(kind: str, name: str, x_fms_token: str = Header(None)):
    need(x_fms_token)
    if kind == "hub" and name in vision_frames:
        return Response(vision_frames[name]["jpeg"], media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    if kind == "cam":
        for c in vision_cams["cameras"]:
            if c["source"] == name:
                return Response(c["jpeg"], media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    raise HTTPException(404, "no picture")


@app.get("/api/vision/config")
def vision_config(x_vision_key: str = Header(None)):
    """What Setup decides for the vision process: field feeds (null = its own file's), calibration, and
    each hub's camera, outline and ball size (null = its own file's)."""
    _vision_auth(x_vision_key)
    return {"rev": store.get("vision_rev") or 0, "feeds": CFG["field"]["feeds"],
            "scale": {h: float(CFG["vision"]["scale"].get(h) or 1) for h in game.ALLIANCES},
            "hubs": {h: CFG["vision"]["hubs"].get(h) for h in game.ALLIANCES}}


@app.post("/api/vision/replace")
async def vision_replace(body: dict = Body(...), x_vision_key: str = Header(None)):
    """Offline re-count from a recording: replaces one hub's events inside the match window."""
    _vision_auth(x_vision_key)
    m = _get(body["key"])
    _editable(m)
    ps = periods_of(m)
    if not ps:
        raise HTTPException(409, "Match never started")
    lo, hi = ps[0].start - 15, ps[-1].end + GRACE + 15
    store.replace_fuel(body["hub"], lo, hi, body["events"], body.get("source", "rescore"))
    mark_dirty()
    _, bd = compute(store.match(m["key"]))
    return {"ok": True, "fuel": {a: bd[a]["fuel"] for a in game.ALLIANCES}}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=CFG["server"]["host"], port=CFG["server"]["port"], log_level="warning")
