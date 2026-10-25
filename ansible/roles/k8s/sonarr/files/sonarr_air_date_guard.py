"""Remove and blocklist Sonarr grabs whose release was published before the episode aired.

Sonarr v5 rejects such releases at decision time: a Release Profile's "do not download before
air date" setting (`AirDateSpecification`, Sonarr commit 8fa16e35) rejects a release whose
indexer publish date is earlier than each episode's `airDateUtc` plus a grace period. Sonarr
v4 has no such rule, so this job applies the same comparison after the grab. It reads the
queue, finds each download's publish date in its `grabbed` history event, and removes a
download that fails the rule with remove-from-client and blocklist.

Fakes race the real release: every fake episode this library was served in 2026 was
published days before air, and a real one almost never is. A release that fails here is
blocklisted, so Sonarr never grabs it again, and real releases published later still pass.

What it deliberately leaves alone:

- anime series (`--skip-series-type`). TVDB air dates for anime are off by hours to a week,
  so the rule rejected 29 real anime imports for every 2 fakes in a backtest over this
  library's history.
- a series carrying `--exempt-tag`, for a show whose real releases come out early.
- a download whose episode has no air date. v5 rejects one; this job removes and blocklists,
  so it skips and logs instead (DECIDED in the parity note on `decide`).
- a pending release (delay profile), which has no download yet.

Without `--apply` the run only logs what it would do.

Stdlib only, so the CronJob runs on a read-only root with nothing to install.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# Sonarr's EpisodeHistoryEventType.Grabbed.
GRABBED = 1
PAGE_SIZE = 200


@dataclass(frozen=True)
class Removal:
    queue_id: int
    title: str
    published: datetime
    air: datetime
    # True while the latest episode has not aired: Sonarr must not search for a replacement,
    # or it grabs the next fake and the next run removes that one too.
    skip_redownload: bool


class Api:
    """A thin X-Api-Key client. `opener` is the transport, so a test can record calls."""

    def __init__(self, base_url: str, api_key: str, opener=urllib.request.urlopen):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.opener = opener

    def request(self, method: str, path: str, params: dict | None = None):
        url = f"{self.base_url}/api/v3/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url, method=method, headers={"X-Api-Key": self.api_key}
        )
        with self.opener(req, timeout=30) as resp:
            body = resp.read()
        return json.loads(body) if body else None


def wait_for_api(
    api: Api, budget_seconds: int, interval_seconds: int, sleep=time.sleep
):
    """Retry the first call until it answers.

    kube-router admits a new pod's source IP a moment after the pod starts, so a Job's first
    connections can be dropped.
    """
    attempts = max(1, budget_seconds // interval_seconds)
    for attempt in range(1, attempts + 1):
        try:
            return api.request("GET", "system/status")
        except (urllib.error.URLError, OSError) as e:
            if attempt == attempts:
                raise
            print(f"sonarr not reachable yet (attempt {attempt}/{attempts}): {e}")
            sleep(interval_seconds)
    return None


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def list_queue(api: Api) -> list[dict]:
    records, page = [], 1
    while True:
        body = api.request(
            "GET",
            "queue",
            {
                "page": page,
                "pageSize": PAGE_SIZE,
                "includeSeries": "true",
                "includeEpisode": "true",
            },
        )
        records.extend(body["records"])
        if page * PAGE_SIZE >= body["totalRecords"]:
            return records
        page += 1


def published_at(api: Api, download_id: str) -> datetime | None:
    """Return the indexer publish date the download's `grabbed` event recorded, if any."""
    body = api.request(
        "GET",
        "history",
        {"downloadId": download_id, "eventType": GRABBED, "pageSize": 10},
    )
    for record in body["records"]:
        value = (record.get("data") or {}).get("publishedDate")
        if value:
            return parse_ts(value)
    return None


def decide(
    group: list[dict],
    published: datetime | None,
    now: datetime,
    grace: timedelta,
) -> tuple[Removal | None, str]:
    """Return the removal one download needs, or None, with a log line naming the reason.

    `group` is every queue record of one download: a multi-episode release has one record per
    episode. As in v5, the release fails if it was published before ANY of its episodes' air
    time less the grace, which is the same as failing against the latest air time.

    DECIDED: a missing air date skips the download, where v5 rejects it. v5's rejection only
    stops one grab; this job's removal blocklists the release forever, so it acts only on a
    comparison it can make.
    """
    first = group[0]
    title = first.get("title", "?")
    airs = [(r.get("episode") or {}).get("airDateUtc") for r in group]
    if not all(airs):
        return None, f"skip {title!r}: an episode has no air date"
    air = max(parse_ts(a) for a in airs if a)
    if published is None:
        # A release is published before it can be grabbed, so the grab time bounds the
        # publish date from above: a grab before the cutoff proves a publish before it.
        added = first.get("added")
        if added and parse_ts(added) < air - grace:
            published = parse_ts(added)
        else:
            return None, f"skip {title!r}: no publish date recorded"
    if published >= air - grace:
        return None, f"keep {title!r}: published {published:%Y-%m-%d %H:%M}Z"
    return (
        Removal(first["id"], title, published, air, skip_redownload=now < air),
        "",
    )


def plan(
    api: Api,
    queue: list[dict],
    now: datetime,
    grace: timedelta,
    skip_types: frozenset[str],
    exempt_tag_id: int | None,
) -> tuple[list[Removal], list[str]]:
    groups: dict[str, list[dict]] = {}
    for record in queue:
        download_id = record.get("downloadId")
        if download_id:
            groups.setdefault(download_id, []).append(record)
    removals, lines = [], []
    for download_id, group in groups.items():
        series = group[0].get("series") or {}
        title = group[0].get("title", "?")
        if series.get("seriesType") in skip_types:
            lines.append(f"skip {title!r}: series type {series['seriesType']}")
            continue
        if exempt_tag_id is not None and exempt_tag_id in series.get("tags", []):
            lines.append(f"skip {title!r}: series carries the exempt tag")
            continue
        removal, line = decide(group, published_at(api, download_id), now, grace)
        if removal:
            removals.append(removal)
        else:
            lines.append(line)
    return removals, lines


def find_tag_id(api: Api, label: str) -> int | None:
    for tag in api.request("GET", "tag"):
        if tag["label"] == label:
            return tag["id"]
    return None


def apply_removals(
    api: Api, removals: list[Removal], apply: bool, max_removals: int
) -> None:
    for i, r in enumerate(removals):
        if i >= max_removals:
            # A sudden burst means air dates or publish dates went wrong, not a burst of fakes.
            print(
                f"deferred {len(removals) - i} removals: --max-removals {max_removals} reached"
            )
            return
        hours = (r.air - r.published).total_seconds() / 3600
        verb = "remove" if apply else "would remove"
        print(
            f"{verb} {r.title!r}: published {r.published:%Y-%m-%d %H:%M}Z, "
            f"{hours:.1f}h before the episode airs at {r.air:%Y-%m-%d %H:%M}Z"
        )
        if apply:
            api.request(
                "DELETE",
                f"queue/{r.queue_id}",
                {
                    "removeFromClient": "true",
                    "blocklist": "true",
                    "skipRedownload": "true" if r.skip_redownload else "false",
                },
            )


def main(argv=None, opener=urllib.request.urlopen, now=None, sleep=time.sleep) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--grace-hours",
        type=float,
        required=True,
        help="Accept a release published up to this many hours before air.",
    )
    p.add_argument("--skip-series-type", action="append", default=[])
    p.add_argument("--exempt-tag", default="")
    p.add_argument("--max-removals", type=int, required=True)
    p.add_argument("--connect-budget-seconds", type=int, default=120)
    p.add_argument("--connect-interval-seconds", type=int, default=5)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args(argv)

    api = Api(os.environ["SONARR_URL"], os.environ["SONARR_API_KEY"], opener)
    now = now or datetime.now(UTC)
    wait_for_api(api, args.connect_budget_seconds, args.connect_interval_seconds, sleep)

    exempt_tag_id = find_tag_id(api, args.exempt_tag) if args.exempt_tag else None
    queue = list_queue(api)
    removals, lines = plan(
        api,
        queue,
        now,
        timedelta(hours=args.grace_hours),
        frozenset(args.skip_series_type),
        exempt_tag_id,
    )
    print("mode: " + ("apply" if args.apply else "dry run (no changes)"))
    print(f"queue records: {len(queue)}, removals due: {len(removals)}")
    for line in lines:
        print(line)
    apply_removals(api, removals, args.apply, args.max_removals)
    return 0


if __name__ == "__main__":
    sys.exit(main())
