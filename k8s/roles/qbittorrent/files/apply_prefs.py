#!/usr/bin/env python3
r"""Re-assert qBittorrent's throughput-relevant preferences.

WHY THIS EXISTS. Every setting below lives in `qBittorrent.conf` on the
`qbittorrent-config` Longhorn PVC, and the role templates none of it — the LinuxServer
image applies only `WEBUI_PORT` and `TORRENTING_PORT` from the environment. So these are
live state: a WebUI change no Ansible run reproduces, and a Longhorn restore silently
reverts. This script is the repo-side source of truth for them.

WHY THIS IS NOT THE DEPLOY'S APPLY. While Ansible owned qbittorrent, a prefs task in the
deploy path would have waited out the wireguard sidecar's startupProbe — failureThreshold
60 x 5s, so up to five minutes before qBittorrent's container starts at all — and an
lscr.io hiccup during it (the 9h/107-restart incident in this role's CLAUDE.md) would have
surfaced as a FAILED DEPLOY rather than as the mod-fetch problem it is. Flux owns the role
since its flip, and the apply on a roll is the Job below. Run this by hand after a PVC
restore, or to check drift without waiting for the next roll.

REACHING THE WEBUI. The pod's NetworkPolicy admits only traefik, sonarr, radarr,
homepage, the reach probe and the qbittorrent-prefs-check CronJob, and the public route is
Authelia-gated — but the WebUI is reachable directly from the node over the pod IP or the
ClusterIP, which is what a hand run uses. Verified 2026-08-26: `curl
http://<pod-ip>:8080/` returns 200 from daniel-box.

UNDER FLUX, A JOB RUNS A COPY. templates/verify/prefs.yaml.j2 applies DESIRED from
files/qbittorrent_job.py on every revision that rolls the pod, after the pod is Ready. The Job
census refuses this file's cookie jar and opener, so that script holds its own copy of DESIRED,
and tests/test_qbittorrent_job.py holds the two equal. Change a value in both.

THE SCHEDULED CALLER. The qbittorrent-prefs-check CronJob runs files/prefs_check.py, which
calls main() with `--dry-run --connect-wait 60` and logs the verdict. It never writes.

Credentials come from the environment, never from a SOPS call inside this script, so
nothing here can print a secret:

    QBT_USERNAME=$(sops -d --extract '["qbittorrent_username"]' secrets/secrets.yml) \
    QBT_PASSWORD=$(sops -d --extract '["qbittorrent_password"]' secrets/secrets.yml) \
    uv run python k8s/roles/qbittorrent/files/apply_prefs.py --dry-run
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping

EXIT_OK = 0
EXIT_UNREACHABLE = 1
EXIT_BAD_ARGS = 2
EXIT_DRIFT = 3

# The patterns behind `excluded_file_names` below; that key's comment says why each is here.
EXCLUDED_FILE_NAMES = (
    "*.exe",
    "*.scr",
    "*.com",
    "*.pif",
    "*.bat",
    "*.cmd",
    "*.msi",
    "*.lnk",
    "*.vbs",
    "*.ps1",
    "*.7z",
    "*.zip",
    "*.zipx",
    "*.arj",
    "*.iso",
    "*.rar",
    "*.r[0-9][0-9]",
)

# Chosen for a dial-only client: Mullvad has no port forwarding, so inbound never arrives.
DESIRED: dict[str, bool | int | str] = {
    "connection_speed": 100,
    "max_connec": 1000,
    "max_connec_per_torrent": 200,
    "announce_to_all_trackers": True,
    # Burst capacity, not reserved cores: the CPU limit is 2 and the request 100m.
    "hashing_threads": 2,
    # Inert on Linux: only persisted, never applied; the cgroup limit is the only memory bound.
    "memory_working_set_limit": 1024,
    # MiB held while hash-checking, on top of the session; raised so hashing is not window-bound.
    "checking_memory_use": 128,
    "preallocate_all": True,
    # Group piece requests by file extent, reducing write scatter. Modest on NVMe, free.
    "enable_piece_extent_affinity": True,
    # Not throughput: skips fake-release executables/archives and RARs, which nothing here unpacks.
    "excluded_file_names_enabled": True,
    # The WebUI API joins and splits this list on "\n" (appcontroller.cpp).
    "excluded_file_names": "\n".join(EXCLUDED_FILE_NAMES),
}


def diff_prefs(
    current: Mapping[str, object], desired: Mapping[str, object]
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

    def answers(self) -> bool:
        """Returns whether the WebUI sends any HTTP response to an unauthenticated GET.

        An error status is an answer too: it proves the connection carried. Only a
        connection that got no response at all, refused, reset or timed out, returns False.
        """
        try:
            self._get("/")
        except urllib.error.HTTPError as exc:
            exc.close()
            return True
        except urllib.error.URLError, OSError:
            return False
        return True

    def login(self, username: str, password: str) -> None:
        """Logs in and raises SystemExit if qBittorrent did not set a session cookie.

        Success is judged by the session cookie, not the response body or status:
        qBittorrent 5.2.3 answers a good login with HTTP 204 and an empty body, so
        checking for the older "Ok." string would wrongly reject a successful login.

        Args:
            username: The qBittorrent WebUI username.
            password: The qBittorrent WebUI password.

        Raises:
            SystemExit: If no QBT_SID session cookie was set after the login POST.
        """
        # Success is the session cookie, not the body: 5.2.3 answers 204 empty, older builds 200
        # "Ok.".
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


def wait_until_answering(
    client: QbtClient,
    bound_s: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    interval_s: float = 5.0,
) -> bool:
    """Polls `client.answers()` until it is True or `bound_s` seconds have passed.

    The poll sends no credentials, which is why it may repeat: a CronJob pod's first dials
    can be dropped even through an open fence, because kube-router learns a new pod's labels
    late. The login that follows runs once, since every answered bad login counts against
    `web_ui_max_auth_fail_count` (5).

    Args:
        client: The WebUI client to poll.
        bound_s: How long to keep polling, in seconds. 0 polls once.
        sleep: Waits between polls; injected by tests.
        clock: A monotonic clock in seconds; injected by tests.
        interval_s: The wait between polls, in seconds.

    Returns:
        True once the WebUI answered, False if it never did within the bound.
    """
    deadline = clock() + bound_s
    while True:
        if client.answers():
            return True
        if clock() >= deadline:
            return False
        sleep(min(interval_s, max(deadline - clock(), 0.0)))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parses --url and --dry-run, defaulting --url from the QBT_URL environment variable.

    Args:
        argv: Argument list to parse; defaults to sys.argv[1:] when None.

    Returns:
        The parsed Namespace.
    """
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
    parser.add_argument(
        "--connect-wait",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="Before logging in, retry a connection that gets no response for up to this "
        "many seconds. The login itself is never retried. Default 0, one attempt.",
    )
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    client_factory: Callable[[str], QbtClient] = QbtClient,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Logs into qBittorrent, diffs live preferences against DESIRED, and applies drift.

    Reads QBT_USERNAME/QBT_PASSWORD from the environment. In --dry-run mode, reports drift
    without writing. Otherwise applies the changed keys and reads preferences back to
    confirm the write took, since setPreferences returns 200 even for keys it silently
    ignored.

    Args:
        argv: Argument list to parse; defaults to sys.argv[1:] when None.
        client_factory: Builds the WebUI client from the base URL; injected by tests.
        sleep: Waits between --connect-wait polls; injected by tests.
        clock: A monotonic clock for --connect-wait; injected by tests.

    Returns:
        EXIT_OK if nothing needed changing or the apply succeeded and confirmed, EXIT_BAD_ARGS
        if the qBittorrent credentials are missing, EXIT_UNREACHABLE if the WebUI could not
        be reached or a write did not take, or EXIT_DRIFT if --dry-run found drift.
    """
    args = parse_args(argv)

    username = os.environ.get("QBT_USERNAME")
    password = os.environ.get("QBT_PASSWORD")
    if not username or not password:
        print(
            "QBT_USERNAME and QBT_PASSWORD must be set — see this file's docstring.",
            file=sys.stderr,
        )
        return EXIT_BAD_ARGS

    client = client_factory(args.url)
    if args.connect_wait > 0 and not wait_until_answering(
        client, args.connect_wait, sleep=sleep, clock=clock
    ):
        print(
            f"cannot reach qBittorrent at {args.url}: no response within "
            f"{args.connect_wait:g}s",
            file=sys.stderr,
        )
        return EXIT_UNREACHABLE
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

    # Read back: setPreferences returns 200 for keys it silently ignored.
    remaining = diff_prefs(client.preferences(), DESIRED)
    if remaining:
        for key, (have, want) in sorted(remaining.items()):
            print(f"NOT APPLIED {key}: still {have}, wanted {want}", file=sys.stderr)
        return EXIT_UNREACHABLE

    print(f"applied {len(changes)} preference(s); all {len(DESIRED)} now match.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
