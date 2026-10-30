#!/bin/sh
# Deploy (or update) the Watchtower website to a server you can ssh into with sudo:
#   deploy/deploy.sh ubuntu@141.148.162.105
# Builds the download from the committed tree, copies the code, installs/updates the venv, the
# systemd unit and the Caddy block (Caddyfile backed up first, validated before reload).
# Leaves everything else on the server alone. Data (accounts, events) lives in /srv/watchtower/data.
set -eu
HOST="$1"
cd "$(dirname "$0")/.."
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
deploy/build.sh "$TMP/dist" >/dev/null
git archive --format=tar HEAD fms website requirements-run.txt LICENSE | gzip > "$TMP/app.tgz"
scp -q "$TMP/app.tgz" "$TMP/dist/watchtower.tar.gz" "$TMP/dist/version" deploy/watchtower-site.service deploy/Caddyfile.site "$HOST:/tmp/"
ssh "$HOST" 'sudo sh -eu' <<'REMOTE'
id watchtower >/dev/null 2>&1 || useradd --system --home /srv/watchtower --shell /usr/sbin/nologin watchtower
mkdir -p /srv/watchtower/app.new /srv/watchtower/dist /srv/watchtower/data
tar -xzf /tmp/app.tgz -C /srv/watchtower/app.new
rm -rf /srv/watchtower/app.old; [ -d /srv/watchtower/app ] && mv /srv/watchtower/app /srv/watchtower/app.old
mv /srv/watchtower/app.new /srv/watchtower/app
mv /tmp/watchtower.tar.gz /tmp/version /srv/watchtower/dist/
[ -x /srv/watchtower/venv/bin/python ] || python3 -m venv /srv/watchtower/venv
/srv/watchtower/venv/bin/pip install -q --disable-pip-version-check -r /srv/watchtower/app/requirements-run.txt
chown -R watchtower:watchtower /srv/watchtower/data
chmod 700 /srv/watchtower/data
install -m 644 /tmp/watchtower-site.service /etc/systemd/system/watchtower-site.service
systemctl daemon-reload
systemctl enable -q watchtower-site
systemctl restart watchtower-site
# Caddy: replace our block between markers, keep everything else
CF=/etc/caddy/Caddyfile
NEW="$(mktemp)"
cp "$CF" "$CF.bak-watchtower-$(date +%Y%m%d%H%M%S)"
awk '/^# >>> watchtower-site/{skip=1} !skip{print} /^# <<< watchtower-site/{skip=0}' "$CF" > "$NEW"
{ echo "# >>> watchtower-site (deploy/deploy.sh; edits here are replaced)"; cat /tmp/Caddyfile.site; echo "# <<< watchtower-site"; } >> "$NEW"
caddy validate --adapter caddyfile --config "$NEW" >/dev/null 2>&1 || { echo "Caddyfile invalid; left unchanged"; caddy validate --adapter caddyfile --config "$NEW"; exit 1; }
install -m 644 "$NEW" "$CF"
systemctl reload caddy
rm -f /tmp/app.tgz /tmp/watchtower-site.service /tmp/Caddyfile.site "$NEW"
sleep 2
curl -fsS http://127.0.0.1:8070/healthz && echo " website up ($(cat /srv/watchtower/dist/version))"
REMOTE
