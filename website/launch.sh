#!/bin/sh
# Watchtower for "__NAME__" -- from __SITE__/e/__EVENT__
#   curl -fsSL __SITE__/run/<token> | sh                  FMS + hub cameras (pick them in /control -> Setup)
#   curl -fsSL __SITE__/run/<token> | sh -s -- --mock     FMS + fake fuel instead (rehearsal)
# Installs into ~/Watchtower (WATCHTOWER_HOME to change it). Nothing outside that folder changes,
# except uv (a Python installer, ~/.local/bin) if this computer has no Python 3.10 or newer.
set -eu
SITE='__SITE__'
EVENT='__EVENT__'
TOKEN='__TOKEN__'
WT="${WATCHTOWER_HOME:-$HOME/Watchtower}"
APP="$WT/app"
EV="$WT/events/$EVENT"
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
need_counter=1
for a in "$@"; do case "$a" in --no-vision) need_counter=0 ;; esac; done
mkdir -p "$WT" "$EV/config"

# 1. Watchtower itself (only when the website has a newer one)
V="$(curl -fsSL "$SITE/download/version")"
if [ ! -f "$APP/.version" ] || [ "$(cat "$APP/.version")" != "$V" ]; then
  say "Downloading Watchtower ($V)"
  curl -fsSL "$SITE/download/watchtower.tar.gz" -o "$WT/app.tgz"
  rm -rf "$APP.new" && mkdir -p "$APP.new"
  tar -xzf "$WT/app.tgz" -C "$APP.new" && rm -f "$WT/app.tgz"
  rm -rf "$APP" && mv "$APP.new" "$APP" && echo "$V" > "$APP/.version"
fi

# 2. Python 3.10+ in its own folder
PYV="$WT/venv/bin/python"
if [ ! -x "$PYV" ]; then
  PY=""
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys, venv; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then PY="$c"; break; fi
  done
  if [ -n "$PY" ]; then
    say "Setting up Python ($("$PY" --version))"
    "$PY" -m venv "$WT/venv"
  else
    say "Installing Python 3.12 with uv (this computer has no Python 3.10+)"
    command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
    UV="$(command -v uv 2>/dev/null || echo "$HOME/.local/bin/uv")"
    "$UV" venv --seed --python 3.12 "$WT/venv"
  fi
fi

# 3. Packages (again only when the list changed)
install() {  # $1 = requirements file
  stamp="$WT/venv/.$(basename "$1").$(cksum < "$1" | cut -d' ' -f1)"
  if [ ! -f "$stamp" ]; then
    say "Installing packages ($(basename "$1"))"
    "$PYV" -m pip install -q --disable-pip-version-check -r "$1" || return 1
    touch "$stamp"
  fi
}
install "$APP/requirements-run.txt"
if [ "$need_counter" = 1 ]; then
  install "$APP/vision/requirements-counter.txt" || say "OpenCV didn't install, so the hub cameras are off. Watchtower still runs."
fi

# 4. This event's settings (first time), or just the website link (new launch command)
curl -fsSL -H "X-Watchtower-Token: $TOKEN" "$SITE/api/link/$EVENT/event.yaml" -o "$EV/config/event.yaml.new"
if [ -f "$EV/config/event.yaml" ]; then
  "$PYV" - "$EV/config/event.yaml" "$EV/config/event.yaml.new" <<'PY'
import sys, yaml
old, new = (yaml.safe_load(open(p)) or {} for p in sys.argv[1:3])
if old.get("cloud") != new.get("cloud"):
    old["cloud"] = new["cloud"]
    yaml.safe_dump(old, open(sys.argv[1], "w"), sort_keys=False)
PY
  rm -f "$EV/config/event.yaml.new"
else
  mv "$EV/config/event.yaml.new" "$EV/config/event.yaml"
fi

# 5. A short command for next time
cat > "$WT/start" <<'SH'
#!/bin/sh
# Start an event's Watchtower again:  ~/Watchtower/start EVENT [--mock | --no-vision]
WT="$(cd "$(dirname "$0")" && pwd)"
[ -n "${1:-}" ] || { echo "usage: $0 EVENT [--mock|--no-vision]"; ls "$WT/events"; exit 1; }
ev="$1"; shift
exec "$WT/venv/bin/python" "$WT/app/launch.py" "$WT/events/$ev" "$@"
SH
chmod +x "$WT/start"
say "Next time you can also run:  $WT/start $EVENT $*"

exec "$PYV" "$APP/launch.py" "$EV" "$@"
