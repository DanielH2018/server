#!/usr/bin/env python3
r"""Re-assert qBittorrent's throughput-relevant preferences.

WHY THIS EXISTS. Every setting below lives in `qBittorrent.conf` on the
`qbittorrent-config` Longhorn PVC, and the role templates none of it — the LinuxServer
image applies only `WEBUI_PORT` and `TORRENTING_PORT` from the environment. So these are
live state: a WebUI change no Ansible run reproduces, and a Longhorn restore silently
reverts. This script is the repo-side source of truth for them.

WHY IT IS NOT WIRED INTO THE DEPLOY. `deploy.sh --tags qbittorrent` renders manifests,
which fires the central rollout-restart (roles/k8s/manifests/tasks/main.yml). The
replacement pod is gated by the wireguard sidecar's startupProbe — failureThreshold 60 x
5s, so up to five minutes before qBittorrent's container starts at all. A prefs task in
the deploy path would have to wait out that window every time, and an lscr.io hiccup
during it (the 9h/107-restart incident in this role's CLAUDE.md) would surface as a
FAILED DEPLOY rather than as the mod-fetch problem it is. Run this by hand instead —
after a PVC restore, or when changing a value here.

REACHING THE WEBUI. The pod's NetworkPolicy admits only traefik, sonarr, radarr,
homepage and the reach probe, and the public route is Authelia-gated — but the WebUI is
reachable directly from the node over the pod IP or the ClusterIP, which is what this
script uses. Verified 2026-08-26: `curl http://<pod-ip>:8080/` returns 200 from
daniel-box.

Credentials come from the environment, never from a SOPS call inside this script, so
nothing here can print a secret:

    QBT_USERNAME=$(sops -d --extract '["qbittorrent_username"]' ansible/vars/secrets.yml) \
    QBT_PASSWORD=$(sops -d --extract '["qbittorrent_password"]' ansible/vars/secrets.yml) \
    uv run python ansible/roles/k8s/qbittorrent/files/apply_prefs.py --dry-run
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# Exit codes. 0/1/2 predate this constant and are read by nothing but a human's shell prompt
# today (verified 2026-08-27: no caller in the repo shells out to this script), so widening the
# contract here is additive, not a break. EXIT_DRIFT is what a scheduled --dry-run caller
# branches on — see templates/prefs-check.sh.j2 — because a bare `--dry-run` cannot otherwise
# tell "nothing to do" from "found changes but wrote none" without parsing stdout.
EXIT_OK = 0
EXIT_UNREACHABLE = 1
EXIT_BAD_ARGS = 2
EXIT_DRIFT = 3

# The desired values, keyed by the qBittorrent WebUI API v2 preference names.
#
# Every one is chosen for a client that CANNOT ACCEPT INBOUND CONNECTIONS. Mullvad
# removed port forwarding on 2023-07-01 and this pod egresses through a Mullvad tunnel,
# so qBittorrent only ever pairs with peers it dials itself. That single fact is what
# makes the connection-related values worth raising: reach is bounded by how fast and how
# widely we dial, not by how many peers find us.
DESIRED: dict[str, object] = {
    # Outbound connection attempts per second. The ramp rate for a dial-only client, and
    # the largest single win here — 30 is the stock value and assumes peers dial back.
    "connection_speed": 100,
    # Global and per-torrent connection ceilings. Raised because only a minority of peers
    # accept inbound, so more attempts are needed to find them. Costs almost nothing:
    # measured 5m of a 2000m CPU limit while downloading.
    "max_connec": 1000,
    "max_connec_per_torrent": 200,
    # Announce to every tracker in a tier rather than stopping at the first that answers,
    # which widens the peer set on multi-tracker torrents. Matters more while portless.
    "announce_to_all_trackers": True,
    # Piece verification is single-threaded at the stock value, which shows on the large
    # remuxes this instance handles. The container's CPU LIMIT is 2 and its REQUEST is
    # 100m, so 2 is the ceiling worth setting and it is burst capacity, not two reserved
    # cores.
    "hashing_threads": 2,
    # INERT ON LINUX, kept only so a WebUI edit cannot set something worse. Traced in
    # qBittorrent's source 2026-08-29: applyMemoryWorkingSetLimit() (app/application.cpp) is
    # compiled under `QBT_USES_LIBTORRENT2 && !Q_OS_LINUX && !Q_OS_MACOS`, so on this image the
    # function does not exist and is never called — setMemoryWorkingSetLimit() only persists the
    # value to QSettings. Even the unreachable Q_OS_UNIX branch says "has no effect on linux" and
    # implements it as setrlimit(RLIMIT_RSS), which the kernel stopped enforcing in the 2.4 era.
    # So there is NO application-side bound on libtorrent's mmap page cache; the cgroup memory
    # limit is the only ceiling, and page cache expands to fill whatever it is given. That is why
    # the container limit stays at 2048Mi: a working set pinned at 99.9% of it with zero restarts
    # and zero OOM events is reclaimable cache, not unmet demand, and raising the limit would just
    # move where it pins. The earlier comment here read this setting as a working control and told
    # the next reader to raise the container limit "before going past this" — it isn't, and doing
    # so buys nothing.
    "memory_working_set_limit": 1024,
    # Blocks held in memory while hash-checking, in MiB (sessionimpl.cpp multiplies by 64 to reach
    # libtorrent's count of 16 KiB blocks). 32 is stock. Raised because piece verification here
    # runs over multi-GB remuxes and is the same bottleneck `hashing_threads: 2` above was set to
    # lift — two threads reading through a 32 MiB window stay window-bound. Consumed in
    # torrent.cpp's start_checking(), which is backend-agnostic: `checking_mem_usage` appears
    # nowhere in mmap_disk_io.cpp, so libtorrent 2.0's mmap path does not bypass it. Costs up to
    # ~96 MiB more RSS during a recheck only, against the 2048Mi limit.
    "checking_memory_use": 128,
    # fallocate the full file up front on ext4. Avoids fragmentation across a multi-hour
    # 60GB+ download and fails fast on out-of-space instead of part-way through.
    "preallocate_all": True,
    # Group piece requests by file extent, reducing write scatter. Modest on NVMe, free.
    "enable_piece_extent_affinity": True,
}


def diff_prefs(
    current: dict[str, object], desired: dict[str, object]
) -> dict[str, tuple[object, object]]:
    """Return {key: (current_value, desired_value)} for keys that need changing.

    A key absent from `current` counts as needing a change — an older qBittorrent may not
    expose it, and sending it is harmless. Comparison is by value, so a second run against
    an already-applied instance returns an empty dict.
    """
    changes: dict[str, tuple[object, object]] = {}
    for key, want in desired.items():
        have = current.get(key, "<absent>")
        if have != want:
            changes[key] = (have, want)
    return changes


class QbtClient:
    """Minimal authenticated WebUI API v2 client."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)
        )

    def _post(self, path: str, fields: dict[str, str]) -> str:
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers={"Referer": self.base_url}
        )
        with self._opener.open(req, timeout=15) as resp:
            return resp.read().decode()

    def _get(self, path: str) -> str:
        req = urllib.request.Request(f"{self.base_url}{path}")
        with self._opener.open(req, timeout=15) as resp:
            return resp.read().decode()

    def login(self, username: str, password: str) -> None:
        # SUCCESS IS THE SESSION COOKIE, NOT THE BODY. qBittorrent 5.2.3 answers a good
        # login with **HTTP 204 and an empty body**; older builds answered 200 with the
        # literal string "Ok.". Checking for "Ok." therefore rejects a login that in fact
        # succeeded — measured against 5.2.3 on 2026-08-26, and the reason this comment
        # exists. A bad password answers 200 with "Fails." and sets no cookie, so the
        # cookie is the one signal that means the same thing across versions.
        body = self._post(
            "/api/v2/auth/login", {"username": username, "password": password}
        )
        if not any(c.name.startswith("QBT_SID") for c in self._jar):
            detail = body.strip() or "no session cookie returned"
            raise SystemExit(f"qBittorrent login failed: {detail}")

    def preferences(self) -> dict[str, object]:
        return json.loads(self._get("/api/v2/app/preferences"))

    def set_preferences(self, values: dict[str, object]) -> None:
        self._post("/api/v2/app/setPreferences", {"json": json.dumps(values)})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--url",
        default=os.environ.get(
            "QBT_URL", "http://qbittorrent.homelab.svc.cluster.local:8080"
        ),
        help="qBittorrent WebUI base URL (env QBT_URL). Use the pod IP or ClusterIP from "
        "the node; the public route is Authelia-gated.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything. Exits "
        f"{EXIT_DRIFT} if a preference has drifted, {EXIT_OK} if none has.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    username = os.environ.get("QBT_USERNAME")
    password = os.environ.get("QBT_PASSWORD")
    if not username or not password:
        print(
            "QBT_USERNAME and QBT_PASSWORD must be set — see this file's docstring.",
            file=sys.stderr,
        )
        return EXIT_BAD_ARGS

    client = QbtClient(args.url)
    try:
        client.login(username, password)
        current = client.preferences()
    except urllib.error.URLError as exc:
        print(f"cannot reach qBittorrent at {args.url}: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE

    changes = diff_prefs(current, DESIRED)
    if not changes:
        print(f"{len(DESIRED)} preferences already match; nothing to do.")
        return EXIT_OK

    for key, (have, want) in sorted(changes.items()):
        verb = "would set" if args.dry_run else "set"
        print(f"{verb} {key}: {have} -> {want}")

    if args.dry_run:
        return EXIT_DRIFT

    client.set_preferences({key: DESIRED[key] for key in changes})

    # Read back rather than trusting the POST — setPreferences returns 200 for keys it
    # silently ignored, so the write is only proven by what the server reports afterwards.
    remaining = diff_prefs(client.preferences(), DESIRED)
    if remaining:
        for key, (have, want) in sorted(remaining.items()):
            print(f"NOT APPLIED {key}: still {have}, wanted {want}", file=sys.stderr)
        return EXIT_UNREACHABLE

    print(f"applied {len(changes)} preference(s); all {len(DESIRED)} now match.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
