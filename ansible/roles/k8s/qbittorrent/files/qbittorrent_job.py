"""qbittorrent's two Flux Jobs: the verify checks, and the throughput preferences apply.

`templates/verify-inert/` holds both Jobs, which nothing renders while Ansible owns qbittorrent.
qbittorrent's flip PR renames the directory to `templates/verify/`, and the renderer then emits
them into `qbittorrent-verify` (target slice D5, *A verify conversion lands as inert code*).
`QBT_JOB` picks the Job:

- `verify` replaces `tasks/verify.yml`'s three proofs without an exec into the pod:
    - reach: the WebUI answers this pod, which sits in the pod CIDR as Traefik and the
      kubelet's probes do. The Mullvad kill-switch rejects everything that does not leave
      through wg0, except the ranges the role lists, so a refusal that outlasts the helper's
      60 seconds of retries means the kill-switch ate the cluster's return path.
    - modcache: the wireguard sidecar's mod cache on the config volume holds a tarball. The
      Job mounts the same claim and subPath read-only, so a tarball here is on the volume and
      survives a restart. A `.lock` is legitimate mid-download, so it only warns.
    - tunnel: qBittorrent's `last_external_address_v4`, the address peers and DHT nodes see,
      differs from this pod's own egress address. qBittorrent egresses through the tunnel
      and this pod through the node, so equal addresses mean traffic leaves in the clear.
- `apply-prefs` sets `DESIRED`, the throughput preferences `apply_prefs.py` holds, and reads
  them back. It writes only the keys that differ, so a second run writes nothing.

The Job census (`scripts/renderer/tests/_job_census.py`) admits no `urllib.request` and no
cookie jar, so this script connects only through `flux_job_connect` and carries the WebUI's
session cookie by hand. That is why it does not import `apply_prefs.py`, which the hand run
and the `qbittorrent-prefs-check` CronJob use. `DESIRED` is a copy, and
`tests/test_qbittorrent_job.py` holds it equal to `apply_prefs.DESIRED`.

Each Job logs in once and never retries the login, because every rejected login counts
against qBittorrent's `web_ui_max_auth_fail_count` of 5. Only the first connection retries,
and only while it is refused.

Environment:
    QBT_JOB          `verify` or `apply-prefs`.
    QBT_URL          the WebUI's base URL, through its Service.
    QBT_CREDENTIALS  the WebUI username, a newline, then the password (the
                     `qbittorrent-prefs-check` Secret's one key).
    EGRESS_URL       verify only: a service that echoes the caller's public address.
    MODCACHE_DIR     verify only: where the Job mounts the sidecar's mod cache.

Exit 0 when every check passes or the preferences match, 1 when one fails, naming it.

Run: python3 qbittorrent_job.py (in the Job; it takes no arguments)
     uv run pytest ansible/roles/k8s/qbittorrent/tests/test_qbittorrent_job.py
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
from collections.abc import Callable, Mapping

from flux_job_connect import Request, open_url, refused

# A copy of `apply_prefs.DESIRED`; that file carries the reason for each value.
DESIRED: dict[str, bool | int] = {
    "connection_speed": 100,
    "max_connec": 1000,
    "max_connec_per_torrent": 200,
    "announce_to_all_trackers": True,
    "hashing_threads": 2,
    "memory_working_set_limit": 1024,
    "checking_memory_use": 128,
    "preallocate_all": True,
    "enable_piece_extent_affinity": True,
}

# The socket timeout of each WebUI or egress request, in seconds, as `apply_prefs.py` uses.
TIMEOUT = 15
# How long the first connect retries a refusal, in seconds. It replaces verify.yml's 20 x 2s
# loop, which waited out kube-router learning a new pod's address.
REACH_RETRY_SECONDS = 60
# How long the tunnel check waits for qBittorrent to learn its external address, in seconds.
# A pod that has only just started reports an empty address until a DHT node or a tracker
# tells it what they see.
EXTERNAL_WAIT_SECONDS = 120
POLL_SECONDS = 5
# The WebUI's session cookie; 5.x names it QBT_SID_<port>.
SESSION_COOKIE = "QBT_SID"


class Failed(Exception):
    """A check failed; the message names what is wrong."""


def split_credentials(value: str) -> tuple[str, str]:
    """Returns (username, password) from the Secret's two-line value.

    Raises:
        Failed: The value is not two non-empty lines.
    """
    lines = value.split("\n")
    if len(lines) != 2 or not all(lines):
        raise Failed("QBT_CREDENTIALS is not a username line and a password line")
    return lines[0], lines[1]


class WebUI:
    """A WebUI API v2 client that holds the session cookie itself."""

    def __init__(self, base_url: str, opener: Callable = open_url) -> None:
        self.base_url = base_url.rstrip("/")
        self._open = opener
        self._cookie = ""

    def call(
        self,
        path: str,
        fields: Mapping[str, str] | None = None,
        retry_seconds: float = 0,
    ) -> str:
        """GETs `path`, or POSTs `fields` to it, and returns the body.

        Raises:
            urllib.error.URLError: The connect failed, or the WebUI answered an error status.
        """
        headers = {"Referer": self.base_url}
        if self._cookie:
            headers["Cookie"] = self._cookie
        data = None if fields is None else urllib.parse.urlencode(fields).encode()
        request = Request(f"{self.base_url}{path}", data=data, headers=headers)
        with self._open(request, timeout=TIMEOUT, retry_seconds=retry_seconds) as resp:
            for header in resp.headers.get_all("Set-Cookie") or []:
                pair = header.split(";", 1)[0].strip()
                if pair.startswith(SESSION_COOKIE):
                    self._cookie = pair
            return resp.read().decode()

    def login(self, username: str, password: str) -> None:
        """Logs in once, judged by the session cookie.

        qBittorrent 5.2.3 answers a good login with HTTP 204 and an empty body, and a bad
        password with 200 "Fails." and no cookie, so the cookie is the signal.

        Raises:
            Failed: No session cookie came back.
        """
        body = self.call(
            "/api/v2/auth/login", {"username": username, "password": password}
        )
        if not self._cookie:
            raise Failed(
                f"qBittorrent login failed: {body.strip() or 'no session cookie'}"
            )

    def preferences(self) -> dict[str, object]:
        return json.loads(self.call("/api/v2/app/preferences"))

    def set_preferences(self, values: Mapping[str, object]) -> None:
        self.call("/api/v2/app/setPreferences", {"json": json.dumps(values)})

    def public_address(self, echo_url: str) -> str:
        """Returns this pod's own public address, as the service at `echo_url` sees it."""
        with self._open(echo_url, timeout=TIMEOUT, retry_seconds=0) as resp:
            return resp.read().decode().strip()

    def external_address(self) -> str:
        """Returns the IPv4 address qBittorrent last learned peers see it as, or ''."""
        info = json.loads(self.call("/api/v2/transfer/info"))
        return str(info.get("last_external_address_v4") or "").strip()


def reach(webui: WebUI, retry_seconds: float = REACH_RETRY_SECONDS) -> str:
    """Proves the WebUI answers a pod in the pod CIDR; any HTTP answer counts.

    Raises:
        Failed: The connect was still refused after `retry_seconds`, or failed otherwise.
    """
    try:
        webui.call("/", retry_seconds=retry_seconds)
    except urllib.error.HTTPError as err:
        err.close()
    except OSError as err:
        why = (
            "the Mullvad kill-switch has eaten the cluster's return path, or the "
            "fence no longer admits this pod"
            if refused(err)
            else "the connect failed for a reason that proves nothing about the tunnel"
        )
        raise Failed(
            f"UNREACHABLE: {webui.base_url} from the pod network ({err}): {why}"
        ) from err
    return f"reach ok: {webui.base_url} answers the pod network"


def modcache(directory: str) -> list[str]:
    """Proves the mod cache on the config volume holds a tarball, and warns on a lock.

    Raises:
        Failed: The cache is missing or holds no tarball.
    """
    try:
        names = sorted(os.listdir(directory))
    except OSError as err:
        raise Failed(f"cannot list the mod cache at {directory}: {err}") from err
    tarballs = [name for name in names if name.endswith(".tar.xz")]
    if not tarballs:
        raise Failed(
            f"the mod cache at {directory} holds no mod tarball ({names or 'empty'}), so a "
            "failed lscr.io fetch would strand the wireguard sidecar, as it did for 9 hours "
            "on 2026-08-16"
        )
    lines = [f"modcache ok: {', '.join(tarballs)}"]
    locks = [name for name in names if name.endswith(".lock")]
    if locks:
        lines.append(
            f"WARNING: a modcache lock is present ({', '.join(locks)}). If no download is in "
            "flight it is stale, and later starts skip the mod; delete it from the volume."
        )
    return lines


def tunnel(
    webui: WebUI,
    egress_url: str,
    wait_seconds: float = EXTERNAL_WAIT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Proves qBittorrent's external address is not this pod's own egress address.

    Raises:
        Failed: Either address is unknown, or the two are equal.
    """
    deadline = clock() + wait_seconds
    external = webui.external_address()
    while not external and clock() < deadline:
        sleep(POLL_SECONDS)
        external = webui.external_address()
    if not external:
        raise Failed(
            f"qBittorrent reported no external address within {wait_seconds:g}s, so the "
            "tunnel cannot be compared; check the sidecar's wg0 and the DHT node count"
        )
    node = webui.public_address(egress_url)
    if not node:
        raise Failed(f"{egress_url} returned no address for this pod")
    if external == node:
        raise Failed(
            f"qBittorrent's external address ({external}) is this node's, so traffic is "
            "LEAVING IN THE CLEAR rather than through Mullvad. Scale qbittorrent to zero "
            "before doing anything else."
        )
    return f"tunnel ok: qBittorrent presents {external}, this node presents {node}"


def apply_prefs(webui: WebUI) -> list[str]:
    """Writes the keys of `DESIRED` that differ, then reads them back.

    Raises:
        Failed: A written key did not take.
    """
    current = webui.preferences()
    changes = {k: v for k, v in DESIRED.items() if current.get(k, "<absent>") != v}
    if not changes:
        return [f"{len(DESIRED)} preferences already match; nothing to do"]
    lines = [
        f"set {k}: {current.get(k, '<absent>')} -> {v}" for k, v in changes.items()
    ]
    webui.set_preferences(changes)
    # setPreferences answers 200 for a key it ignored, so only the read-back proves it.
    after = webui.preferences()
    missed = [k for k, v in DESIRED.items() if after.get(k, "<absent>") != v]
    if missed:
        raise Failed(f"NOT APPLIED: {', '.join(missed)} still differ after the write")
    return [*lines, f"applied {len(changes)}; all {len(DESIRED)} now match"]


def run(env: Mapping[str, str], opener: Callable = open_url) -> list[str]:
    """Runs the Job `QBT_JOB` names and returns its report lines.

    Raises:
        Failed: The first check that failed.
    """
    job = env.get("QBT_JOB", "")
    if job not in ("verify", "apply-prefs"):
        raise Failed(f"QBT_JOB is {job!r}, not verify or apply-prefs")
    username, password = split_credentials(env.get("QBT_CREDENTIALS", ""))
    webui = WebUI(env["QBT_URL"], opener)
    # The first connect waits out kube-router; the login after it then runs exactly once.
    lines = [reach(webui)]
    if job == "verify":
        lines += modcache(env["MODCACHE_DIR"])
        webui.login(username, password)
        lines.append(tunnel(webui, env["EGRESS_URL"]))
    else:
        webui.login(username, password)
        lines += apply_prefs(webui)
    return lines


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    env = os.environ if env is None else env
    tag = f"qbittorrent-{env.get('QBT_JOB', 'job')}"
    try:
        lines = run(env)
    except (Failed, OSError, ValueError) as err:
        print(f"{tag}: {err}", file=sys.stderr)
        return 1
    for line in lines:
        print(f"{tag}: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
