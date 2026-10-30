"""Link to the Watchtower website (event.yaml `cloud:`, written by the website's launch command).

Every 15 s, or right after a Setup change, the FMS sends the website its live settings and a short
status (current match, phone addresses), and takes back any settings edited on the website. Both
sides merge per key, newest wins (fms/settings.py). Matches, fuel and scores never leave this
machine, and nothing here is on the match path: with no internet the FMS runs exactly as before
and catches up when the link comes back.
"""
from __future__ import annotations

import socket
import threading
import time

import requests

status: dict = {"linked": False, "last_ok": None, "error": None}
_wake = threading.Event()
PERIOD_S = 15


def poke():
    _wake.set()


def exchange(cloud_cfg: dict, entries: dict, st: dict):
    """One sync. Returns the website's settings entries, or None (status says why)."""
    url = f"{cloud_cfg['url'].rstrip('/')}/api/link/{cloud_cfg['event']}/sync"
    try:
        r = requests.post(url, timeout=8, headers={"X-Watchtower-Token": cloud_cfg["token"]},
                          json={"settings": entries, "status": st})
        if r.status_code in (401, 404):
            status.update(linked=False, error="the website doesn't know this event any more (deleted, or a new launch command)")
            return None
        r.raise_for_status()
        status.update(linked=True, last_ok=time.time(), error=None)
        return r.json().get("settings") or {}
    except (requests.RequestException, ValueError) as e:
        status.update(linked=False, error=f"website unreachable ({type(e).__name__})")
        return None


def run_forever(sync):
    while True:
        try:
            sync()
        except Exception as e:                           # noqa: BLE001  never take the FMS down
            status.update(linked=False, error=f"sync failed: {e}")
        _wake.wait(PERIOD_S)
        _wake.clear()


def lan_urls(port: int) -> list[str]:
    """Addresses phones on the same network can open (best guess; the website shows them)."""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))      # no packet is sent; picks the outgoing interface
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return [f"http://{ip}:{port}" for ip in ips if not ip.startswith("127.")]
