"""Remove dead music torrents through Lidarr, so Lidarr blocklists each release and searches again.

Lidarr never fails a stalled download, so a torrent whose swarm has no peers stays in its queue
forever and no other release is searched. This script, run by the `lidarr-stall-sweep` CronJob,
reads qBittorrent's `lidarr` category, picks the dead torrents, and removes their Lidarr queue
records with `removeFromClient`, `blocklist` and re-search.

A torrent is dead when all of these hold (`is_dead`):

- qBittorrent reports it `stalledDL` or `metaDL`;
- it has been running, not queued, for at least `MIN_ACTIVE_SECONDS` (`time_active`);
- it has received no data for at least `MIN_IDLE_SECONDS` (`last_activity`).

`time_active` is what makes the rule safe: a torrent that sat queued for hours and has just
started counts only its running time, so it is never removed in its first minutes.

The run refuses to remove anything while qBittorrent reports fewer than `MIN_DHT_NODES` DHT
nodes, because a dropped VPN tunnel stalls every torrent at once. It removes at most
`MAX_REMOVALS` per run, oldest first. Unless `ARMED` is `true`, it only reports what it would
remove.

Environment:
    LIDARR_URL      Lidarr's base URL, through its Service.
    LIDARR_API_KEY  Lidarr's API key.
    QBT_URL         qBittorrent's WebUI base URL, through its Service.
    QBT_API_KEY     qBittorrent's WebUI API key, sent as a bearer header.
    ARMED           `true` to remove; anything else reports only.

Exit 0 when the sweep ran, removing or not; 1 when it could not read either app, refused on
the DHT check, or a removal failed.

Run: python3 sweep_lidarr_stalls.py (in the Job; it takes no arguments)
     uv run pytest k8s/roles/lidarr/tests/test_sweep_lidarr_stalls.py
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
from dataclasses import dataclass

from flux_job_connect import Request, open_url

CATEGORY = "lidarr"
DEAD_STATES = frozenset({"stalledDL", "metaDL"})
MIN_ACTIVE_SECONDS = 2 * 3600
MIN_IDLE_SECONDS = 3600
MAX_REMOVALS = 10
MIN_DHT_NODES = 10
# Above the queue Lidarr holds after a Spotify playlist import, about 100 albums.
QUEUE_PAGE_SIZE = 1000
HTTP_TIMEOUT = 30


class SweepError(Exception):
    """A condition the run must fail on rather than report as swept."""


@dataclass(frozen=True)
class Torrent:
    """One qBittorrent torrent in the `lidarr` category.

    Attributes:
        hash: The info-hash, lowercase, as qBittorrent reports it.
        name: The torrent's name.
        state: qBittorrent's state, such as `stalledDL`.
        progress: The fraction downloaded, 0 to 1.
        time_active: Seconds the torrent has spent running, not queued.
        last_activity: Epoch seconds of the last data it received or sent.
    """

    hash: str
    name: str
    state: str
    progress: float
    time_active: int
    last_activity: int


@dataclass(frozen=True)
class Removal:
    """One dead torrent matched to the Lidarr queue record that removes it.

    Attributes:
        queue_id: The Lidarr queue record's id.
        torrent: The dead torrent.
    """

    queue_id: int
    torrent: Torrent


@dataclass(frozen=True)
class Plan:
    """What one run removes, and what it found dead but leaves.

    Attributes:
        removals: The records to remove, oldest idle first, at most `MAX_REMOVALS`.
        over_cap: Dead torrents left for a later run by the cap.
        untracked: Dead torrents with no Lidarr queue record, which the sweep cannot remove.
    """

    removals: list[Removal]
    over_cap: int
    untracked: int


def parse_torrents(rows: object) -> list[Torrent]:
    """Torrents from qBittorrent's `torrents/info` rows.

    Raises:
        SweepError: The answer is not a list of rows.
    """
    if not isinstance(rows, list):
        raise SweepError("qBittorrent's torrents/info answered no list")
    return [
        Torrent(
            hash=row["hash"].lower(),
            name=row["name"],
            state=row["state"],
            progress=float(row["progress"]),
            time_active=int(row["time_active"]),
            last_activity=int(row["last_activity"]),
        )
        for row in rows
    ]


def queue_ids(page: object) -> dict[str, int]:
    """{lowercase info-hash: queue record id} from one page of Lidarr's `queue`.

    Raises:
        SweepError: The answer is not a page, or the page holds fewer records than Lidarr's
            total, so a dead torrent's record could be missing from it.
    """
    if not isinstance(page, dict):
        raise SweepError("Lidarr's queue answered no page")
    records = page["records"]
    if page["totalRecords"] > len(records):
        raise SweepError(
            f"Lidarr's queue holds {page['totalRecords']} records and returned {len(records)}"
        )
    return {r["downloadId"].lower(): r["id"] for r in records if r.get("downloadId")}


def is_dead(torrent: Torrent, now: float) -> bool:
    """Whether `torrent` has run long enough with no data that its swarm is dead."""
    return (
        torrent.state in DEAD_STATES
        and torrent.time_active >= MIN_ACTIVE_SECONDS
        and now - torrent.last_activity >= MIN_IDLE_SECONDS
    )


def plan(torrents: list[Torrent], ids: dict[str, int], now: float) -> Plan:
    """The removals for one run: dead torrents with a queue record, oldest idle first, capped."""
    dead = sorted(
        (t for t in torrents if is_dead(t, now)), key=lambda t: t.last_activity
    )
    tracked = [Removal(ids[t.hash], t) for t in dead if t.hash in ids]
    return Plan(
        removals=tracked[:MAX_REMOVALS],
        over_cap=max(0, len(tracked) - MAX_REMOVALS),
        untracked=len(dead) - len(tracked),
    )


def request(
    url: str, headers: dict[str, str], method: str = "GET", body: dict | None = None
) -> object:
    """One API call, returning the parsed JSON body, or None for an empty one.

    Raises:
        SweepError: The server answered with an error status or could not be reached.
    """
    data = json.dumps(body).encode() if body is not None else None
    req = Request(url, data=data, method=method)
    for name, value in headers.items():
        req.add_header(name, value)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with open_url(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        raise SweepError(f"{method} {url} returned HTTP {exc.code}") from exc
    except OSError as exc:
        raise SweepError(f"{method} {url} could not connect: {exc}") from exc
    return json.loads(raw) if raw.strip() else None


def describe(removal: Removal, now: float) -> str:
    """One line naming a removal and the numbers that made it dead."""
    t = removal.torrent
    return (
        f"{t.name} ({t.state}, {t.progress:.0%}, running {t.time_active / 3600:.1f}h, "
        f"no data for {(now - t.last_activity) / 3600:.1f}h)"
    )


def sweep(env: dict[str, str], now: float) -> int:
    """Run one sweep against the apps `env` names; print the count line, then each removal."""
    qbt = {"Authorization": f"Bearer {env['QBT_API_KEY']}"}
    lidarr = {"X-Api-Key": env["LIDARR_API_KEY"]}
    armed = env.get("ARMED") == "true"

    transfer = request(f"{env['QBT_URL']}/api/v2/transfer/info", qbt)
    if not isinstance(transfer, dict):
        raise SweepError("qBittorrent's transfer/info answered no object")
    if transfer["dht_nodes"] < MIN_DHT_NODES:
        raise SweepError(
            f"qBittorrent reports {transfer['dht_nodes']} DHT nodes, under {MIN_DHT_NODES}; "
            "the tunnel may be down, so every torrent would read as dead"
        )
    category = urllib.parse.urlencode({"category": CATEGORY})
    torrents = parse_torrents(
        request(f"{env['QBT_URL']}/api/v2/torrents/info?{category}", qbt)
    )
    page = request(
        f"{env['LIDARR_URL']}/api/v1/queue?page=1&pageSize={QUEUE_PAGE_SIZE}"
        "&includeUnknownArtistItems=true",
        lidarr,
    )
    result = plan(torrents, queue_ids(page), now)

    verb = "removed" if armed else "would remove"
    print(
        f"lidarr stall sweep: {len(result.removals)} {verb}, {result.over_cap} over the cap, "
        f"{result.untracked} untracked, {len(torrents)} checked"
    )
    if armed and result.removals:
        query = urllib.parse.urlencode(
            {"removeFromClient": "true", "blocklist": "true", "skipRedownload": "false"}
        )
        request(
            f"{env['LIDARR_URL']}/api/v1/queue/bulk?{query}",
            lidarr,
            method="DELETE",
            body={"ids": [r.queue_id for r in result.removals]},
        )
    for removal in result.removals:
        print(f"  {verb}: {describe(removal, now)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the sweep from the Job's environment."""
    argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0]).parse_args(
        argv
    )
    missing = [
        k
        for k in ("LIDARR_URL", "LIDARR_API_KEY", "QBT_URL", "QBT_API_KEY")
        if not os.environ.get(k)
    ]
    if missing:
        print(
            f"lidarr stall sweep: missing environment {', '.join(missing)}",
            file=sys.stderr,
        )
        return 1
    try:
        return sweep(dict(os.environ), time.time())
    except SweepError as exc:
        print(f"lidarr stall sweep: refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
