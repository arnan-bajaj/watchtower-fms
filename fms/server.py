"""FMS server. Run:  python -m fms.server  (from the repo root)"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import pathlib
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import Body, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import bracket, config, game, schedule
from .store import Store
from .tba import TBA

CFG = config.load()
G = CFG["game"]
GRACE = G["score_grace_s"]
STATIC = pathlib.Path(__file__).parent / "static"
store = Store(CFG["server"]["db"])
tba = TBA(CFG, store)
SECRET = store.get("secret") or secrets.token_hex(16)
store.set("secret", SECRET)

clients: set[WebSocket] = set()
vision_status: dict = {}
feed_status: list = []        # live count feeds to field systems, relayed by vision (count_feed.py)
feed_seen = 0.0
_field_applied: set = set()   # (match key, auto_start) whose auto result came from a field's lights
_dirty = threading.Event()  # set from any thread; tick loop broadcasts


# ------------------------------------------------------------------ auth
def token_for(role):
    return hashlib.sha256(f"{SECRET}:{role}".encode()).hexdigest()[:32]


def role_of(tok):
    for r in ("control", "ref", "emcee"):
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
    return game.build_periods(G, m["auto_start"], m.get("teleop_start"),
                              m.get("first_inactive"), m.get("offset") or 0.0)


def compute(m):
    ps = periods_of(m)
    if not ps:
        return None, None
    events = store.fuel_between(ps[0].start, ps[-1].end + GRACE)
    playoff = m["comp_level"] != "qm"
    bd = game.score_match(G, ps, events, m.get("adjust"), m.get("climbs"), store.fouls(m["key"]),
                          playoff=playoff, winner_override=m.get("winner_override") if playoff else None)
    return ps, bd


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
        "event": {k: CFG["event"][k] for k in ("name", "teams", "tba_event_key", "lunch", "qual_start", "cycle_min")},
        "game": G,
        "current": match_public(m, True) if m else None,
        "matches": [match_public(x) for x in ms],
        "rankings": bracket.rankings(CFG["event"]["teams"], store.matches("qm")),
        "alliances": store.get("alliances"),
        "champion": bracket.champion({x["key"]: x for x in ms}),
        "vision": {h: {**v, "age": round(time.time() - v["seen"], 1)} for h, v in vision_status.items()},
        "feeds": feeds_public(),
        "tba": {"configured": tba.configured, "key": tba.key, **store.outbox_stats()},
        "webcast": store.get("webcast") or {},
        "selection": selection_public(),
        "display": store.get("display") or "match",
    }


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
            if m.get("first_inactive") is None and now >= ps[0].end + GRACE:
                _, bd = compute(m)
                fi, coin = game.decide_first_inactive(bd["red"]["auto_fuel"], bd["blue"]["auto_fuel"])
                store.update_match(m["key"], first_inactive=fi, fi_coin=int(coin))
                push = True
            # With a fed field (e.g. bioarena), its hub lights ARE the auto result: adopt them
            # once per match, as soon as a shift shows them. Later manual overrides still stick.
            fi = field_first_inactive() if now >= ps[0].end else None
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


for _p in ("control", "ref", "emcee", "display"):
    app.add_api_route(f"/{_p}", (lambda n=_p: page(n)), methods=["GET"])


@app.get("/manifest/{role}.json")
def manifest(role: str):
    names = {"ref": "FMS Ref", "emcee": "FMS Emcee", "control": "FMS Control", "display": "FMS Display"}
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


@app.post("/api/login")
def login(body: dict = Body(...)):
    role, pin = body.get("role"), str(body.get("pin", ""))
    if role not in CFG["server"]["pins"] or pin != str(CFG["server"]["pins"][role]):
        raise HTTPException(401, "Wrong PIN")
    return {"token": token_for(role), "role": role}


# ------------------------------------------------------------------ schedule
def _schedule_locked():
    return any(m["auto_start"] is not None or m["status"] == "committed" for m in store.matches("qm"))


def _preview(matches, start, cycle):
    if not all(m.get("scheduled") for m in matches):
        schedule.add_times(matches, start, cycle)
    end = (datetime.strptime(matches[-1]["scheduled"], "%H:%M") + timedelta(minutes=cycle)).strftime("%H:%M")
    return {"matches": matches, "quals_end": end, "fits_before_lunch": end <= CFG["event"]["lunch"]}


@app.post("/api/schedule/preview")
def schedule_preview(body: dict = Body(...), x_fms_token: str = Header(None)):
    need(x_fms_token)
    mpt = int(body.get("matches_per_team", 6))
    start = body.get("start") or CFG["event"]["qual_start"]
    cycle = float(body.get("cycle_min") or CFG["event"]["cycle_min"])
    seed = body.get("seed")
    if len(CFG["event"]["teams"]) < 6:
        raise HTTPException(400, "Add at least 6 team numbers to event.teams in config/event.yaml, then restart the server")
    matches, st = schedule.generate(CFG["event"]["teams"], mpt, seed=seed)
    out = _preview(matches, start, cycle)
    out["stats"] = st
    return out


@app.post("/api/schedule/import")
def schedule_import(body: dict = Body(...), x_fms_token: str = Header(None)):
    need(x_fms_token)
    matches = schedule.parse_csv(body.get("csv", ""))
    if not matches:
        raise HTTPException(400, "No rows parsed. Format: match,red1,red2,red3,blue1,blue2,blue3[,HH:MM]")
    unknown = sorted({t for m in matches for t in m["red"] + m["blue"]} - set(CFG["event"]["teams"]))
    out = _preview(matches, CFG["event"]["qual_start"], float(CFG["event"]["cycle_min"]))
    out["unknown_teams"] = unknown
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
    store.set("current", "qm1")
    mark_dirty()
    return {"ok": True, "count": len(body["matches"])}


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
    m = _get(body.get("key"))
    _editable(m)
    a = body.get("alliance") or m.get("first_inactive")
    if a not in game.ALLIANCES:
        raise HTTPException(409, "Auto result not decided yet")
    if role == "emcee" and a != m.get("first_inactive"):
        raise HTTPException(403, "Only the scorekeeper can override the auto result")
    store.update_match(m["key"], first_inactive=a, fi_locked=int(bool(body.get("lock", True))))
    mark_dirty()
    return {"ok": True}


@app.post("/api/match/adjust")
async def adjust(body: dict = Body(...), x_fms_token: str = Header(None)):
    """Send {"total": n} to set the final count for a period (server computes the delta),
    or {"delta": n} to nudge it."""
    need(x_fms_token)
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
    store.update_match(m["key"], video=(body.get("youtube") or "").strip() or None)
    if m["status"] == "committed" and body.get("youtube"):
        tba.video(m["key"], body["youtube"].strip())
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
        tba.rankings(bracket.rankings(CFG["event"]["teams"], store.matches("qm")))
    vid = m.get("video") or suggested_video(m)
    if vid:
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
    order = [r["team"] for r in bracket.rankings(CFG["event"]["teams"], store.matches("qm"))]
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


# ------------------------------------------------------------------ wipe
@app.post("/api/admin/wipe")
async def wipe(body: dict = Body(...), x_fms_token: str = Header(None)):
    need(x_fms_token)
    if str(body.get("pin", "")) != str(CFG["server"]["pins"]["control"]):
        raise HTTPException(403, "Wrong scorekeeper PIN")
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
    tba.rankings(bracket.rankings(CFG["event"]["teams"], store.matches("qm")))
    if store.get("alliances"):
        tba.alliances(store.get("alliances"))
    return {"ok": True}


@app.post("/api/tba/rankings")
def push_rankings(x_fms_token: str = Header(None)):
    need(x_fms_token)
    tba.rankings(bracket.rankings(CFG["event"]["teams"], store.matches("qm")))
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
        tba.rankings(bracket.rankings(CFG["event"]["teams"], store.matches("qm")))
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
    names = game.period_names(G)
    head = ["match", "status", "red1", "red2", "red3", "blue1", "blue2", "blue3", "red", "blue"]
    for a in ("red", "blue"):
        head += [f"{a}_{n}" for n in names] + [f"{a}_tower", f"{a}_fouls_drawn", f"{a}_rp"]
    wr.writerow(head)
    for m in store.matches():
        bd = m.get("breakdown")
        row = [m["key"], m["status"], *m["red"], *m["blue"],
               bd["red"]["total"] if bd else "", bd["blue"]["total"] if bd else ""]
        for a in ("red", "blue"):
            if bd:
                row += [bd[a]["fuel"][n] for n in names] + [bd[a]["tower_points"],
                                                            bd[a]["foul_points"], bd[a]["rp"]]
            else:
                row += [""] * (len(names) + 3)
        wr.writerow(row)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv")


# ------------------------------------------------------------------ vision
def _vision_auth(key):
    if key != CFG["server"]["vision_key"]:
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
    return {"record": rec}


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
