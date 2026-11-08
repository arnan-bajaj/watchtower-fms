# Changelog

Versions are git tags. v0.x were alphas built toward our first scrimmage; v1 is the build that ran the
10th Street Showdown (2026-10-10); v2 is the website revamp.

## v2.7.0: Match13 predictions in /control

- **Match13** (Setup → Match13, an API key from match13.com): the Match tab shows a prediction for the current
  match in small grey text after its time; hover for the details. Match13's own forecast when it has the match
  on TBA's schedule with the same teams, otherwise one from each team's season rating.
- Only Control sees it: never the field TV, the overlay or the phones. The key stays on the laptop.

## v2.6.0: run a bioarena field from /control

- **Field control** (Setup → Field control, plus a setting on the field): Start starts the field, and Watchtower's
  match clock begins when the field actually reaches Auto, so there's no start click to correct.
- A start or abort on the field's own page is followed too. An aborted match stops the clock and goes to review.
- **Field panel** on the Match tab: load a match's lineup, each station's connection lights and battery, bypass,
  e-stop, auto winner mode, and the field's own reason when it can't start.
- Start is refused if the field's lineup or match lengths don't match the event.
- The hub lights set the auto result directly, with or without vision running.

## v2.5.0: camera picture, fuel colour, and the setup guide on the page

- **Picture** per hub: frame rate, size, video format, exposure, white balance, brightness, contrast,
  saturation and gain for a USB camera, applied when it opens; settings the driver ignored are listed.
- **Fuel colour** per hub: **Show what counts as fuel** paints what the colour range takes as fuel magenta,
  live while you tune it; saved ranges are what the counter, drop test and Measure use.
- **Measure ball** works before an outline is drawn, so the ball-width ruler helps draw the first one.
- Each hub's card is now the setup guide: numbered steps with their own buttons, each shown once the step it
  needs is done. Changing the picture or fuel colour of a calibrated hub asks, then needs a new Calibrate.

## v2.4.0: the old Hub Counter page's tools, back in Setup

- **Exit line** per hub (Setup -> Cameras): drawn beside the outline; the hub counts the larger of the two,
  with the exits-against-outline check in its status.
- **Measure ball**: 5 s of still balls gives an estimate that draws a ball-width ruler while you draw the
  outline and is checked against the drop test. Only the drop test sets the size counted with.
- **Run**: live counts per hub, a practice count from zero, the field link, and the cameras locked while a
  match runs (Unlock for this match).
- **Test**: ball test (pass/fail, changes nothing), speed against the field feed's spec, Record the cameras
  into the event folder's `recordings/`, and Check a recording against a hand count.
- The Setup page's address list includes the stream overlay.

## v2.3.0: stream overlay

- **Stream overlay (`/overlay`)** for OBS: a score bar over the stream camera on an otherwise transparent page
  (Browser Source, 1920x1080; `?pos=top` for the top). It shows teams, live scores, the match, the clock and the
  period, a hub-active strip (REBUILT), then Final, the winner and quals RP after commit, and one line during
  alliance selection. `/display` stays the full-screen TV scoreboard.
- The overlay is listed on the FMS station page and on the website's event page, with its OBS address.
- The website's game picker no longer calls REEFSCAPE "scored by hand" (scorers count live on /score since v2.2.0).

## v2.2.0: live REEFSCAPE scorers

- **Scorer page (`/score`)** for REEFSCAPE: scorers on their phones tap coral (L1 to L4), processor and net for one
  alliance or both while the match runs, plus each robot's Leave and Barge. Taps add to the count, so several
  scorers can share an alliance; taps go to Auto until teleop starts, then Teleop (overridable), with Undo. The
  field display's score updates live. Control still reviews and commits.
- New **Scorer PIN** (`server.pins.scorer`). Website REEFSCAPE events and `fms.init` generate one; an older
  event.yaml without it still starts, with scorer logins off until Setup -> PINs sets one.
- **Scorekeeper is now Control** everywhere it names the `/control` page or its PIN (station list, Setup, the
  website, the launch banner), so it can't be mixed up with Scorer.

## v2.1.0: REEFSCAPE

- **REEFSCAPE (2025) can be picked** when you create an event. It's scored by hand: after each match the
  scorekeeper types each alliance's coral (L1 to L4) and algae (processor, net), split into auto and teleop, plus
  each robot's Leave and Barge result. Auto, Coral (with Coopertition) and Barge RPs are computed from them, and
  rankings use REEFSCAPE's tiebreakers. Refs, emcee, field display, TBA and playoffs work as for REBUILT.
- A REEFSCAPE event starts no hub counter, and /control hides cameras, calibration, the field link and the
  start-click correction.
- `event.game` in event.yaml selects the rules; a `game:` key from the other game stops startup with a message.

## v2.0.0 (2026-11-01): the website

Watchtower is now a website plus one launch command; there is nothing to clone or configure.

- **watchtower.systemoverload.org:** sign up, create an event (pick the game: REBUILT; REEFSCAPE and BIOCORE
  are listed, not yet selectable), and copy one command. It installs Watchtower on the scorekeeper's laptop
  (Mac, Linux or Windows) and runs the event there; the website never runs matches.
- **Everything in /control -> Setup:** event name, date, teams, TBA keys, PINs, field link (bioarena address)
  and hub calibration, all editable live. Settings sync both ways with the website.
- **Hub cameras built in:** the colour hub counter ships with Watchtower; no model or plugin to import. In
  Setup -> Cameras, pick each hub's camera, draw the hub's outline on its picture, and run a drop test (drop
  some balls, type how many) to fit the ball size. A hub never counts on a guessed ball size.
- `--mock` (fake fuel) and `--no-vision` for rehearsals and manual scoring.
- The desktop app builds are gone; the website and launch command replace them.

## v1.1.0 (2026-10-30): event-day tools

- Edit a match's time and teams (replacement robots), add and delete matches, mark a match as having no video.
- Field sides: choose which side red is drawn on, and flip it per device.
- Lunch as a time range, with an option to keep running quals after lunch.
- Schedule optimizer: back-to-backs held to the lower bound, lunch breaks respected, surrogate flags and a
  quality report.

## v1.0.0 (2026-10-10): 10th Street Showdown

The build that ran our first scrimmage: quals, live alliance selection and a 4-alliance double-elimination
playoff, with hub cameras counting fuel and a live count feed to the bioarena field.

- Mock (fake fuel) runs feed the field too, for rehearsing the field link.

## v0.1.1 (2026-10-05, alpha): desktop app

- Watchtower as a double-click app for Mac, Windows, Linux and Raspberry Pi, built on each tag. Removed in v2.

## v0.1.0 (2026-10-05, alpha): counter plugins

- Hub counters are plugins: drop one in a folder or `--add-plugin` a repo; /control shows each counter's status.
- The `zone` counter counts only fuel, so a model that also detects robots doesn't count robots.

## v0.0.3 (2026-10-01, alpha): field link

- Live hub counts to a bioarena field over UDP, and the FMS follows the field's hub lights for the auto result.

## v0.0.2 (2026-10-01, alpha): ready for other events

- Live alliance selection, a results viewer, data wipe with backup, and TBA takedowns.
- Phone-first restyle of every page.
- First-run setup for any event (example configs, random PINs) and the MIT-licensed public release.

## v0.0.1 (2026-09-30, alpha): first working FMS

- Match timer and REBUILT scoring, referee / emcee / scorekeeper / display pages, vision counting into the FMS.
