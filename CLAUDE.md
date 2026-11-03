# CLAUDE.md: Watchtower FMS (FRC 2026 REBUILT; 2025 REEFSCAPE scored by hand)

Open-source (MIT) field management system for one-day FRC offseason events: quals, live alliance selection,
then a 4-alliance double-elimination playoff. Runs on one laptop on the event WiFi. First built for the
10th Street Showdown (Oct 2026); published for other teams, so nothing may assume one specific event.
Other teams use it through the website (watchtower.systemoverload.org): sign up, create an event, paste one
launch command; the FMS then runs on THEIR laptop. No desktop app (user's decision, 2026-10-30).

## Architecture (keep these separate)
- `fms/` FastAPI server + SQLite. Owns the match timeline, scoring, fouls, schedule, rankings, bracket, TBA.
- `vision/` Separate process. Hub cameras -> counter plugin (YOLO model) -> POSTs `(timestamp, hub, n)` to the FMS.
  The FMS never imports vision code or the model. Vision never contains scoring rules.
- `website/` The website on the Oracle server (141.148.162.105, host `blue-banner`, shared with other
  services: never touch their units or Caddy blocks). Accounts, events, launch scripts, the starting
  event.yaml, a synced copy of settings + status. It NEVER runs matches; it imports only `fms.settings`/`fms.config`.
  Deploy: `deploy/deploy.sh ubuntu@141.148.162.105` (systemd `watchtower-site`, port 8070, Caddy block between
  `# >>> watchtower-site` markers). `launch.py` runs one event folder (`~/Watchtower/events/<slug>`) on a laptop:
  the FMS plus vision on a generated `config/vision.cameras.yaml` (counter `colour`, hubs empty until
  Setup -> Cameras picks them; `--mock`, `--no-vision`, `--vision-config FILE`).
- Phone pages (`fms/static/`): `/ref` (fouls), `/emcee` (auto result + hub light cues), `/control`
  (scorekeeper), `/display` (field TV / OBS overlay; also the live alliance selection screen). Vanilla JS,
  no build step, no CDN (the venue may have no internet). Live state over one websocket `/ws`.
  Shared look in `style.css`; layouts must work at 390 px wide with no horizontal page scroll.

## Config (event-specific data never goes in git)
- Committed templates: `config/event.example.yaml`, `config/vision.example.yaml`, `config/vision.mock.yaml`.
- `config/event.yaml` and `config/vision.yaml` are gitignored. `python -m fms.init` creates them with random
  distinct PINs and a vision key (`run.sh` runs it on first use); it never overwrites.
- Live settings (`fms/settings.py`): the keys in `settings.FIELDS` (event basics, teams, TBA keys, PINs,
  `field.feeds`, `vision.scale.*`, `vision.hubs.*`) are edited in /control -> Setup, stored in SQLite as `{key: {v, t}}` and applied
  in place over event.yaml (never replace CFG's sub-dicts; G and tba.cfg hold them). Newest `t` wins per key, with
  tombstones for "back to event.yaml"; the website syncs with the same merge (`fms/cloud.py`). The TBA secret never
  goes to a browser. Tokens include the PIN. A data wipe keeps settings. Server-side paths (db, port) aren't settings.
- Vision fetches `/api/vision/config` when the FMS's `config_rev` changes: Setup's feeds replace the file's
  (null = use the file), and `vision.scale` (counts per ball) divides each hub's count before the feed and the FMS.
- Setup -> Cameras (`vision.hubs.<hub>` = {source, size, outline, ball_area}; null = the vision file's hub):
  `vision/hubsetup.py` merges it into the hub config and rescales an outline drawn at another picture size.
  The colour counter never counts on a guessed ball size: a hub with an outline but no `ball_area` waits for
  its drop test (`vision/dropfit.py`: record blobs near the outline, replay the counter at log-spaced areas,
  keep the area whose count matches the balls dropped). The FMS relays pictures, Find cameras and the drop
  test through the `want` field of its `/api/vision/events` reply (in memory only, never stored); pictures go
  to the browser via `/api/vision/image/...` with the control token. Saving a fitted size resets `vision.scale`
  for that hub to 1.
- `fms/config.py` refuses to start on a missing config, `CHANGE-ME`/blank PINs, or a control PIN shared
  with ref/emcee. Vision takes `vision_key` from `event.yaml` when its own config omits it, and `feeds:` from
  `vision.yaml` when its config has no `feeds` key (so mock runs feed bioarena too; `feeds: []` = off) (`vision/vconfig.py`).
- Tests and docs use generic teams and event keys, never a real event's.

## Invariants: do not break
- All scoring math lives in pure functions: `fms/game.py` (REBUILT) and `fms/reefscape.py` (REEFSCAPE). Every rule
  number comes from `config.GAME_RULES[event.game]` overlaid by `config/event.yaml` -> `game:`; never hardcode point
  values. Each `score_match` asserts two independent totals agree.
- `event.game` (`rebuilt` default | `reefscape`) is fixed when the event is made (the website's game picker). The
  server picks `RULES` (build_periods, period_names, TIEBREAKS) from it once at startup. `config.load` refuses a
  `game:` key the event's game doesn't have (a REBUILT section left in a REEFSCAPE event.yaml would win silently).
- REEFSCAPE has no vision, hubs, fuel, `first_inactive` or field feed: launch.py starts no counter, and the REBUILT-only
  endpoints (`adjust`, `climb`, `first_inactive`) answer 400. Its match `adjust` holds the scorekeeper's counts
  (`{alliance: {item: n}}`, items `reefscape.ITEMS`, `POST /api/match/count`) and `climbs` holds each robot's
  `{leave, barge}` (`POST /api/match/robot`). Pages branch on state `game_id`; rankings columns come from `rank_cols`.
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
- Alliance screen order is presentation only. Pages draw every red/blue pair in `FMS.sides()` order
  (`fms/static/common.js`; `FMS.placeSides` for static layouts). State `red_side` = Setup's override (kv `red_side`,
  `POST /api/red_side`) else `display.red_side` (default `right`); every page has a per-device flip in
  localStorage keyed by page path (`/display` has no login role; its button shows only on mouse movement). Never hardcode red-then-blue in a page; data, API payloads, CSV and TBA keep their red/blue keys.
- Live alliance selection stores only the frozen rank order and the pick list; `bracket.selection` replays
  it (serpentine, captain promotion), so undo is dropping the last pick.

## Game timeline (defaults; verify against the 2026 manual)
Auto 20 s -> 3 s gap -> Transition 10 s -> Shifts 1-4 at 25 s each (hubs alternate) -> Endgame 30 s.
Teleop = 140 s. Fuel landing within `score_grace_s` (3 s) after a hub deactivates still counts.
Unverified: teleop Level 1 climb (10 vs 15 in different sources), RP thresholds, foul values.
REEFSCAPE (2025 manual, regional RPs): Auto 15 s -> 3 s -> Teleop 135 s (last 20 s labelled Endgame). Leave 3; coral
auto 3/4/6/7, teleop 2/3/4/5; processor 6, net 4; park 2, shallow 6, deep 12; fouls 2/6. Coral RP 5 per level on 4
levels (3 with Coopertition: both alliances 2+ processor algae); Barge RP 14; Auto RP all robots leave + 1 auto coral.

## Run
Website flow (what other teams do): create an event at watchtower.systemoverload.org, run its launch command.
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
  `"module:Class"`. `colour` (alias `tbavid-colour`) is `vision/hubcounter/`, copied unchanged from
  YOLOv26-FRC-Model's `tbavid/` (MIT; the commit is in its `__init__.py`): fix counter bugs there and recopy, don't
  edit the copy. `vision/plugins/` is gitignored except `_example.py` (a working template; `_` files are
  skipped) and README.md. Required: `__init__(cfg)`,
  `process(frame, t) -> int`. Optional, defaulted: PLUGIN_API, NAME, DESCRIPTION, NEEDS, OPTIONS (misspelt keys
  warned), `status()` (detail/warning/error, shown in /control), `close()`. `--list-counters` prints them. Keep the
  registry free of cv2 (tests run without it). `zone` counts only the class named `fuel` (or `classes:`). The
  model blend `tbavid-combo` is not shipped (weights + GPU); it stays a plugin from that repo.
- Model weights go in `vision/models/` (gitignored). Restart only the vision process to swap models.
- Vision records raw hub video + per-frame timestamps per match; `vision/rescore.py` recounts a match offline
  and replaces that hub's events in the FMS on the same clock.
- macOS: OpenCV GUI calls must stay on the main thread (preview is done there).
- Optional live count feeds (`vision/count_feed.py`, `feeds:` / `--feed HOST:PORT`): cumulative red/blue
  over UDP to field systems that decide the auto winner themselves (bioarena protocol v1). Sent on every
  count, before the FMS POST. Their replies are relayed to the FMS; once a shift shows one hub dark the FMS
  adopts and locks that `first_inactive` once per match (the lights are the truth).
  bioarena `main` implements the receiver and Counted mode since 2026-10-07 (read at `c95a49b`, not yet
  run end to end). Only Counted mode uses the counts; in Random/Red/Blue the emcee locks `first_inactive`
  to what bioarena's lights showed.

## Event constraints
- Everyone (refs, emcee, scorekeeper) is on the venue WiFi; no hotspot. Some networks isolate clients;
  test phone -> Mac reachability at the venue. The Mac's IP changes per network.
- Count feeds to a field system (e.g. bioarena) normally run over the same venue WiFi, no wired link. The
  field accepts one source address, so its counter setting must hold the Mac's current IP, and `feeds:`
  must hold the field's IP; both change per network.
- Schedule generator (`fms/schedule.py`): greedy + annealing over restarts from one seed, a fixed move count
  (never a time budget) so a seed reproduces. Hard: back-to-backs = max(0, 12-N) per in-session pair (breaks
  = match numbers lunch follows), no three in a row (impossible below 9 teams), counts within 1, one
  surrogate per extra team on its 3rd match. `schedule.report` checks any schedule; kv `schedule_meta` holds
  the seed and breaks. 12 teams => bound 0 => two fixed groups alternate per session. Schedules need >= 6 teams; live alliance selection needs >= 12 (4 alliances of 3).
- Quals end by `lunch` by default. `quals_after_lunch` (or the Schedule tab checkbox) makes `schedule.add_times`
  skip the `lunch`–`lunch_end` break (no match's cycle overlaps it) and warn against `day_end` instead.

## Event-day history (10th Street Showdown, 2026-10-10): what bit us
- The colour counter counted each ball ~9x (ball_area 77/80 px in YOLOv26-FRC-Model's cams.json; fixed to
  693/720). Now: Setup -> Cameras' drop test fits the ball size (and counts per ball / Calibrate for other
  counters). Half/double the right size counted 302/46 against 112 on that recording: never guess it.
- bioarena's address had to be typed into vision.yaml by hand (field at 192.168.1.10, feed port 8411, web UI
  8080; bioarena accepts one source IP = the laptop's). Now: Setup -> Field system.
- A leftover mock vision process kept posting random fuel into the real match; a stray `on` line in event.yaml
  crashed startup. `main` was missing the quals-after-lunch branch on the day (merged since).
- Built live: per-match time edits, Shift times, Teams edit (replacement robots), Add/Delete match, `none` video,
  clearing TBA rankings.

## Not implemented yet
REEFSCAPE fouls that award an RP to the opponent; REEFSCAPE DCMP/CMP RP thresholds are config only (7 coral, 16 barge).
Yellow/red cards and DQs, playoff backup robots, FRC's exact ranking/playoff tiebreakers,
TBA `score_breakdown` (off by default; TBA validates per-season keys), alliance counts other than 4.

## Working style for this repo
Direct answers, uncomfortable truths first. Prove changes with tests or a mock run before calling them done.
Small commits; don't refactor across `fms/` and `vision/` in one change. Update README.md (and this file)
in the same commit as any change to setup, usage or invariants.
- Commits are Arnan Bajaj's only (`92697572+arnan-bajaj@users.noreply.github.com`): no Claude author/committer, no
  `Co-Authored-By`/`Claude-Session` lines, no "Generated with" footers in commits or PRs. `.claude/settings.json` turns
  attribution off and sets that git identity at session start; `.github/workflows/authors.yml` fails any commit that credits Claude.
- Versions are git tags (`git describe` is the version the website shows): v0.x = alphas before the first scrimmage,
  v1 = the build run at the 10th Street Showdown, v2 = the website + Setup revamp. Bump the major for a revamp, the
  minor for features, the patch for fixes. There is no desktop app build (removed in v2).
- `launch.py` chdirs into the event folder, so `fms.server`'s import-time `config.load()` and relative `data/` paths
  must keep working from whatever folder the process starts in.
