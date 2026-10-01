# Scrimmage FMS (REBUILT 2026)

Two independent processes on the MacBook. The FMS never touches the model; vision never touches scoring rules.

```
 hub cam RED ─┐                                     ┌─ ref phones    /ref     (fouls)
              ├─ vision/run_vision.py ──(t, hub, n)─▶ FMS server ─┼─ emcee phone  /emcee   (auto result, hub light cues)
 hub cam BLUE ┘   counter plugin + model   HTTP       SQLite     ├─ field TV / OBS  /display
                                                                   ├─ scorekeeper  /control (start, review, commit)
 stream cam ─▶ OBS ─▶ YouTube live                     │          └─▶ TBA outbox (retries when internet is back)
```

Vision only reports *timestamped fuel events*. The FMS decides which period each ball belongs to from the match timeline, so you can fix a late start click after the match and the whole score re-buckets.

## Setup (once)

```bash
cd frc-fms
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt            # FMS
pip install -r vision/requirements.txt     # vision (ultralytics, opencv)
python -m pytest -q                        # 10 tests, hand-computed scores
```

Edit `config/event.yaml`: PINs, `vision_key`, team list, TBA key. **Check every number under `game:` against the 2026 manual.**

## Run

```bash
# terminal 1 (repo root)
caffeinate -dimsu python -m fms.server

# terminal 2
cd vision && python run_vision.py --config ../config/vision.yaml --preview
```

Pages: `http://<mac-ip>:8000/` (find the IP with `ipconfig getifaddr en0`). On phones: open the page, Share → Add to Home Screen.

### No model yet? Rehearse everything anyway

```bash
cd vision && python run_vision.py --config ../config/vision.mock.yaml
```

Mock vision emits random fuel. Refs, emcee, commits, rankings and TBA all behave for real. You can also run with vision off entirely and type fuel counts into the review table.

## Plugging in the model

1. Copy weights to `vision/models/fuel_best.pt`.
2. `cd vision && python pick_roi.py 0` → paste the printed `roi:` into `config/vision.yaml` for that hub. Repeat for camera 1.
3. Pick a counter: `zone` (default, short tracks inside the ROI; built for the fragmentation problem you hit with ByteTrack), `linecross` (ByteTrack + line), `mock`, or your own class via `counter: "my_module:MyCounter"` (interface in `vision/counters/base.py`: `process(frame, t) -> new_count`).
4. Restart only the vision process. The FMS keeps running and loses nothing except the seconds vision was down.

Every match, vision records raw hub video + per-frame timestamps to `vision/recordings/`. After you train a better model:

```bash
python rescore.py --match qm7 --hub red --video recordings/qm7_<id>_red.mp4 --weights models/v2.pt
```

That replaces the hub's fuel for that match on the same clock. Reopen the match first if it's committed. Those recordings are also your best training data: they're from the real mount.

## Running a match

1. **Start**: click *Start match* on the field countdown. If you were late, fix it later with the ±0.5 s buttons (negative = clicked late). Optional *Teleop started* click re-anchors teleop if the field's teleop start drifted.
2. **Auto result**: 3 s after auto ends (the grace window), the FMS compares auto fuel. The alliance that scored more has its hub **off** in Shift 1. Tie = coin flip. The emcee's phone shows it with the hub light plan and buzzes 3 s before every hub change.
3. **Lock the call**: the emcee taps *Lights set*. Shift scoring follows the locked call, because the lights are what robots actually played to. If the auto margin is within `close_auto_margin`, both screens warn: get the head ref's call.
4. **Review**: after the match, enter tower levels, check fouls, type corrected fuel totals per period if vision was off. Vision data is never deleted; your number overrides it.
5. **Commit**: sends the match, rankings, and match video link to TBA, and advances to the next match.

## TBA

- Your offseason event must exist on TBA, and you need Trusted API keys for it (request write access for your event through TBA; approval can take days, so do it now). Put them in env vars `TBA_AUTH_ID` / `TBA_AUTH_SECRET`, set `tba.enabled: true`.
- Set the event's playoff type to 4-alliance double elimination on TBA. Keys used: `sf1m1`–`sf5m1`, `f1m1`–`f1m3`.
- Every write goes into a SQLite outbox and retries forever on network errors; 4xx rejections fail after 3 tries and show in Setup. Test with one match before the event.
- `send_score_breakdown` is off: TBA validates per-season breakdown keys, and totals-only is guaranteed to be accepted.
- Match videos: set the livestream's YouTube ID in Setup and click *Stream went live now* when OBS goes live. Each committed match gets `<id>?t=<seconds>` pointing 10 s before its start. No uploading.

## Network: the part most likely to break

- Event WiFi very often has **client isolation**: phones can't reach the laptop at all. Bring your own router, plug the MacBook in by Ethernet, give it a DHCP reservation. Test ref phones on that router before event day.
- macOS will ask to allow incoming connections for Python the first time: allow it.
- Internet can drop; the FMS doesn't need it. TBA updates queue and flush later.

## Not implemented (decide if you need them)

Yellow/red cards and DQs, surrogate display on TBA beyond the match field, backup robots in playoffs, FRC's exact tiebreakers (see `fms/bracket.py`). Playoff ties require the scorekeeper to pick who advances or reset and replay.
