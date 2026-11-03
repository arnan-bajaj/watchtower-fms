# Changelog

Versions are git tags. v0.x were alphas built toward our first scrimmage; v1 is the build that ran the
10th Street Showdown (2026-10-10); v2 is the website revamp.

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
