# CLAUDE.md: Watchtower FMS (FRC 2026 REBUILT)

Open-source (MIT) field management system for one-day FRC offseason events: quals, live alliance selection,
then a 4-alliance double-elimination playoff. Runs on one laptop on the event WiFi. First built for the
10th Street Showdown (Oct 2026); published for other teams, so nothing may assume one specific event.

## Architecture (keep these separate)
- `fms/` FastAPI server + SQLite. Owns the match timeline, scoring, fouls, schedule, rankings, bracket, TBA.
- `vision/` Separate process. Hub cameras -> counter plugin (YOLO model) -> POSTs `(timestamp, hub, n)` to the FMS.
  The FMS never imports vision code or the model. Vision never contains scoring rules.
- Phone pages (`fms/static/`): `/ref` (fouls), `/emcee` (auto result + hub light cues), `/control`
  (scorekeeper), `/display` (field TV / OBS overlay; also the live alliance selection screen). Vanilla JS,
  no build step, no CDN (the venue may have no internet). Live state over one websocket `/ws`.
  Shared look in `style.css`; layouts must work at 390 px wide with no horizontal page scroll.

## Config (event-specific data never goes in git)
- Committed templates: `config/event.example.yaml`, `config/vision.example.yaml`, `config/vision.mock.yaml`.
- `config/event.yaml` and `config/vision.yaml` are gitignored. `python -m fms.init` creates them with random
  distinct PINs and a vision key (`run.sh` runs it on first use); it never overwrites.
- `fms/config.py` refuses to start on a missing config, `CHANGE-ME`/blank PINs, or a control PIN shared
  with ref/emcee. Vision takes `vision_key` from `event.yaml` when its own config omits it (`vision/vconfig.py`).
- Tests and docs use generic teams and event keys, never a real event's.

## Invariants: do not break
- All scoring math lives in `fms/game.py` as pure functions. Every rule number comes from `config/event.yaml`
  -> `game:`; never hardcode point values. `score_match` asserts two independent totals agree.
- `python -m pytest -q` must pass after every change. Tests use hand-computed expected values; add a
  hand-computed test for any scoring/timeline change. Zero tolerance for arithmetic errors.
- Fuel is stored as raw timestamped events and never deleted by scoring. Which period a ball counts toward is
  decided at score time from the timeline, so the start-click `offset` can be corrected after the match.
- Shift scoring follows the LOCKED `first_inactive` (what the hub lights actually showed), not a recount of auto.
  Alliance with MORE auto fuel has its hub INACTIVE in Shift 1; tie = coin flip.
- Manual review overrides are stored as per-period `adjust` deltas on top of vision; vision data stays intact.
- Committed matches are read-only until reopened. Playoff matches cannot commit while tied (scorekeeper picks).
- Every TBA write goes through the SQLite outbox (`fms/tba.py`); never call TBA directly from a request handler.
  Takedowns (`matches/delete`, `matches/delete_all`) go through it too.
- Live alliance selection stores only the frozen rank order and the pick list; `bracket.selection` replays
  it (serpentine, captain promotion), so undo is dropping the last pick.

## Game timeline (defaults; verify against the 2026 manual)
Auto 20 s -> 3 s gap -> Transition 10 s -> Shifts 1-4 at 25 s each (hubs alternate) -> Endgame 30 s.
Teleop = 140 s. Fuel landing within `score_grace_s` (3 s) after a hub deactivates still counts.
Unverified: teleop Level 1 climb (10 vs 15 in different sources), RP thresholds, foul values.

## Run
```bash
source .venv/bin/activate
./run.sh ../config/vision.mock.yaml     # FMS + fake fuel (no model/cameras)
./run.sh                                # FMS + real cameras per config/vision.yaml
python -m pytest -q
```
Pages at `http://<mac-ip>:8000` (`ipconfig getifaddr en0`); `localhost` only works on the Mac itself.
First run: `python -m fms.init`. TBA secrets come from env vars via `tba_secrets.sh` (gitignored). Keep
`tba.enabled: false` while rehearsing, or practice matches get pushed to the real event. Wipe practice data
from `/control` → Setup (backs up to `data/backups/` first) or `rm data/fms.sqlite3*` with the server stopped.
When testing, run a separate server on another port with a scratch config, and stop it by PID; never
pattern-kill `fms.server`/`run_vision.py` (it can hit the user's live processes).

## Vision
- Counter plugins (`vision/counters/__init__.py`, plugin API 1): `counter:` is a built-in (`zone`, `linecross`,
  `mock`), a short name registered under the `watchtower.counters` entry-point group (pip-installed, or a
  `plugin_paths:` folder's pyproject.toml), a class in `vision/plugins/*.py` (its NAME; found by parsing the
  source with ast, never importing it, so a broken file can't stop startup), a repo recorded there as
  `<name>.path` by `--add-plugin` (plain text, not a symlink, for Windows), a `plugins:` alias, or
  `"module:Class"`. `vision/plugins/` is gitignored except `_example.py` (a working template; `_` files are
  skipped) and README.md. Required: `__init__(cfg)`,
  `process(frame, t) -> int`. Optional, defaulted: PLUGIN_API, NAME, DESCRIPTION, NEEDS, OPTIONS (misspelt keys
  warned), `status()` (detail/warning/error, shown in /control), `close()`. `--list-counters` prints them. Keep the
  registry free of cv2 (tests run without it). `zone` counts only the class named `fuel` (or `classes:`). The
  YOLOv26-FRC-Model counters (tbavid-colour / tbavid-combo) are maintained there, not copied here.
- Model weights go in `vision/models/` (gitignored). Restart only the vision process to swap models.
- Vision records raw hub video + per-frame timestamps per match; `vision/rescore.py` recounts a match offline
  and replaces that hub's events in the FMS on the same clock.
- macOS: OpenCV GUI calls must stay on the main thread (preview is done there).
- Optional live count feeds (`vision/count_feed.py`, `feeds:` / `--feed HOST:PORT`): cumulative red/blue
  over UDP to field systems that decide the auto winner themselves (bioarena protocol v1). Sent on every
  count, before the FMS POST. Their replies are relayed to the FMS; once a shift shows one hub dark the FMS
  adopts and locks that `first_inactive` once per match (the lights are the truth).
  bioarena `main` (checked 2026-09-26) does NOT implement the receiver yet; it picks the winner at auto
  start. Until it does, the emcee locks `first_inactive` to what bioarena's lights showed.

## Event constraints
- Everyone (refs, emcee, scorekeeper) is on the venue WiFi; no hotspot. Some networks isolate clients;
  test phone -> Mac reachability at the venue. The Mac's IP changes per network.
- Count feeds to a field system (e.g. bioarena) normally run over the same venue WiFi, no wired link. The
  field accepts one source address, so its counter setting must hold the Mac's current IP, and `feeds:`
  must hold the field's IP; both change per network.
- With few teams (e.g. 12) and 6-team matches, some back-to-backs are unavoidable; the schedule optimizer
  minimizes them. Schedules need >= 6 teams; live alliance selection needs >= 12 (4 alliances of 3).

## Not implemented yet
Yellow/red cards and DQs, playoff backup robots, FRC's exact ranking/playoff tiebreakers,
TBA `score_breakdown` (off by default; TBA validates per-season keys), alliance counts other than 4.

## Working style for this repo
Direct answers, uncomfortable truths first. Prove changes with tests or a mock run before calling them done.
Small commits; don't refactor across `fms/` and `vision/` in one change. Update README.md (and this file)
in the same commit as any change to setup, usage or invariants.
- The Watchtower app (`.github/workflows/app.yml`) is built from YOLOv26-FRC-Model's `apps/watchtower/` (launcher,
  spec, smoke test) at the tag in `.github/hubcounter-release`, with this repo's code as the FMS. It runs fms.init
  into `~/Documents/Watchtower/config`, so `fms.server`'s import-time `config.load()` and relative `data/` paths must
  keep working from whatever folder the process chdirs to. Bump `.github/hubcounter-release` to pick up counter fixes.
