"""Roll one day of each Pi-hole's query database up into `dns.counts_daily`, and check it.

Both Pi-hole instances keep every query in their FTL database for 91 days. Each row is one
count over the day, per instance: the day's total and blocked queries and client count, each
client's queries, and the most-queried permitted and blocked domains.

Pi-hole v6 has no read-only credential, so `PiholeSession` logs in with the admin password
and always logs out, because Pi-hole caps how many sessions may be open at once.

Stdlib only, so the repo's tests import it without Airflow.

Typical usage example:

    with PiholeSession(url, password) as pihole:
        rows = instance_rows(day, name, pihole.get)
"""

import json
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone

from .prom_rollup import read_json

INSTANCES = {
    "pihole": "http://pihole.homelab.svc.cluster.local",
    "pihole-2": "http://pihole-2-web.homelab.svc.cluster.local",
}
# FTL's default database.maxDBdays, which the pihole role does not change.
PIHOLE_KEEP_DAYS = 91
TOP_DOMAINS = 200
TOP_CLIENTS = 1000
# Each instance answered about 51,000 queries a day on 2026-11-20.
MIN_DAILY_QUERIES = 1000

SCHEMA = "dns"
COUNTS_TABLE = f"{SCHEMA}.counts_daily"
COUNTS_COLUMNS = ("day", "instance", "kind", "key", "label", "n")
DDL = (
    f"CREATE TABLE IF NOT EXISTS {COUNTS_TABLE} (day date NOT NULL, instance text NOT NULL, "
    "kind text NOT NULL, key text NOT NULL, label text NOT NULL, n bigint NOT NULL, "
    "PRIMARY KEY (day, instance, kind, key))",
)


def _start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def in_backfill_window(day: date, now: datetime) -> bool:
    """Whether `day` is complete and a day inside FTL's retention, so wholly held."""
    start = _start(day)
    return (
        now - timedelta(days=PIHOLE_KEEP_DAYS - 1) < start
        and start + timedelta(days=1) <= now
    )


class PiholeSession:
    """An authenticated Pi-hole API session, logged out when the `with` block ends.

    Attributes:
      base_url: The instance's web address, by ClusterIP.
    """

    def __init__(
        self,
        base_url: str,
        password: str,
        opener: Callable = urllib.request.urlopen,
    ):
        self.base_url = base_url
        self._password = password
        self._opener = opener
        self._sid = ""

    def __enter__(self) -> "PiholeSession":
        request = urllib.request.Request(
            f"{self.base_url}/api/auth",
            data=json.dumps({"password": self._password}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        session = read_json(request, self._opener)["session"]
        if not session.get("valid"):
            raise RuntimeError(
                f"{self.base_url} refused the login: {session.get('message')}"
            )
        self._sid = session["sid"]
        return self

    def get(self, path: str, params: Mapping[str, object]) -> Mapping:
        """GET an API path with the session's id, returning the parsed body."""
        request = urllib.request.Request(
            f"{self.base_url}/api/{path}?{urllib.parse.urlencode(params)}",
            headers={"X-FTL-SID": self._sid},
        )
        return read_json(request, self._opener, timeout=120)

    def __exit__(self, *exc) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/api/auth",
            headers={"X-FTL-SID": self._sid},
            method="DELETE",
        )
        with self._opener(request, timeout=30):
            pass


def instance_rows(
    day: date, instance: str, get: Callable[[str, Mapping[str, object]], Mapping]
) -> list[tuple]:
    """Every `counts_daily` row for one instance and day.

    Args:
      day: The UTC day.
      instance: The value of the `instance` column.
      get: `PiholeSession.get`, or a stub that answers the same paths.
    """
    window = {
        "from": int(_start(day).timestamp()),
        "until": int((_start(day) + timedelta(days=1)).timestamp()),
    }
    summary = get("stats/database/summary", window)
    rows = [
        (day, instance, "total", "queries", "", summary["sum_queries"]),
        (day, instance, "total", "blocked", "", summary["sum_blocked"]),
        (day, instance, "total", "clients", "", summary["total_clients"]),
    ]
    clients = get("stats/database/top_clients", {**window, "count": TOP_CLIENTS})
    rows += [
        (day, instance, "client", ip, label, n)
        for ip, (label, n) in _clients_by_address(clients["clients"]).items()
    ]
    for kind, blocked in (("domain", "false"), ("blocked_domain", "true")):
        domains = get(
            "stats/database/top_domains",
            {**window, "blocked": blocked, "count": TOP_DOMAINS},
        )
        rows += [
            (day, instance, kind, d["domain"], "", d["count"])
            for d in domains["domains"]
        ]
    return rows


def _clients_by_address(clients: Sequence[Mapping]) -> dict[str, tuple[str, int]]:
    """Each address's label and query count, one entry per address.

    FTL's database keeps one client record per address and name it has seen, and
    `top_clients` counts queries per record, so an address seen under two names comes back
    twice. Its counts add up, and the label is the first non-empty name, the busiest one,
    because FTL lists clients by count.
    """
    merged: dict[str, tuple[str, int]] = {}
    for client in clients:
        label, n = merged.get(client["ip"], ("", 0))
        merged[client["ip"]] = (label or client.get("name") or "", n + client["count"])
    return merged


def quality_problems(
    day: date, rows: Sequence[Mapping], instances: Sequence[str] = tuple(INSTANCES)
) -> list[str]:
    """Every reason the day's `counts_daily` rows are not trustworthy; empty when they are.

    Each instance answers DNS for the whole LAN all day, so a quiet instance lost its
    database, and clients that add up to more than the total were counted twice.
    """
    totals = {
        (row["instance"], row["key"]): row["n"]
        for row in rows
        if row["kind"] == "total"
    }
    client_sums: dict[str, int] = {}
    for row in rows:
        if row["kind"] == "client":
            client_sums[row["instance"]] = (
                client_sums.get(row["instance"], 0) + row["n"]
            )
    problems = []
    for instance in instances:
        queries = totals.get((instance, "queries"), 0)
        if queries < MIN_DAILY_QUERIES:
            problems.append(
                f"{day}: {instance} answered {queries} queries, under {MIN_DAILY_QUERIES}"
            )
        if client_sums.get(instance, 0) > queries:
            problems.append(
                f"{day}: {instance}'s clients sum to {client_sums[instance]}, "
                f"over its {queries} queries"
            )
    return problems
