"""The DNS rollup's session handling, rows and check.

Pi-hole's API answers only with the admin password, which no agent reads, so these responses
are built from the response schemas Pi-hole publishes at `/api/docs/specs/stats.yaml` and
`auth.yaml`, served by the deployed image, with the examples those schemas give.
"""

import io
import json
import urllib.parse
from datetime import date, datetime, timezone

import pytest

from dags.homelab_warehouse import dns_history as dh

DAY = date(2026, 11, 19)
DAY_START = int(datetime(2026, 11, 19, tzinfo=timezone.utc).timestamp())
ANSWERS = {
    "stats/database/summary": {
        "sum_queries": 29160,
        "sum_blocked": 6379,
        "percent_blocked": 21.9,
        "total_clients": 10,
    },
    "stats/database/top_clients": {
        "clients": [
            {"ip": "192.168.0.44", "name": "raspberrypi.lan", "count": 5896},
            {"ip": "192.168.0.52", "name": "", "count": 210},
        ],
        "total_queries": 29160,
        "blocked_queries": 6379,
    },
    "stats/database/top_clients?blocked=true": {
        "clients": [
            {"ip": "192.168.0.44", "name": "raspberrypi.lan", "count": 1200},
            {"ip": "192.168.0.60", "name": "tv.lan", "count": 33},
        ],
        "total_queries": 29160,
        "blocked_queries": 6379,
    },
    "stats/database/top_domains": {
        "domains": [{"domain": "pi-hole.net", "count": 8516}],
        "total_queries": 29160,
        "blocked_queries": 6379,
    },
}


class FakePihole:
    """Answers the API as Pi-hole does, recording each request's method, path and query."""

    def __init__(self, valid: bool = True):
        self.valid = valid
        self.calls = []

    def __call__(self, request, timeout):
        url = urllib.parse.urlparse(request.full_url)
        path = url.path.removeprefix("/api/")
        self.calls.append(
            (request.get_method(), path, urllib.parse.parse_qs(url.query))
        )
        if path == "auth" and request.get_method() == "POST":
            body = {"session": {"valid": self.valid, "sid": "s1d", "message": "ok"}}
        elif path == "auth":
            body = {}
        else:
            assert request.get_header("X-ftl-sid") == "s1d"
            blocked = urllib.parse.parse_qs(url.query).get("blocked", [""])[0]
            body = ANSWERS.get(f"{path}?blocked={blocked}", ANSWERS[path])
        return io.BytesIO(json.dumps(body).encode())


def rows_through(fake: FakePihole) -> list[tuple]:
    with dh.PiholeSession("http://pihole", "pw", opener=fake) as pihole:
        return dh.instance_rows(DAY, "pihole", pihole.get)


def test_a_day_gives_totals_clients_and_both_kinds_of_domain():
    rows = {(r[2], r[3]): (r[4], r[5]) for r in rows_through(FakePihole())}

    assert rows[("total", "queries")] == ("", 29160)
    assert rows[("total", "blocked")] == ("", 6379)
    assert rows[("client", "192.168.0.44")] == ("raspberrypi.lan", 7096)
    assert rows[("client", "192.168.0.52")] == ("", 210)
    assert rows[("client", "192.168.0.60")] == ("tv.lan", 33)
    assert rows[("domain", "pi-hole.net")] == ("", 8516)
    assert rows[("blocked_domain", "pi-hole.net")] == ("", 8516)


def test_an_address_seen_under_two_names_gives_one_client_row(monkeypatch):
    monkeypatch.setitem(
        ANSWERS,
        "stats/database/top_clients",
        {
            "clients": [
                {"ip": "192.168.0.44", "name": "", "count": 5896},
                {"ip": "192.168.0.44", "name": "raspberrypi.lan", "count": 210},
            ]
        },
    )
    monkeypatch.setitem(
        ANSWERS, "stats/database/top_clients?blocked=true", {"clients": []}
    )

    clients = [r[3:] for r in rows_through(FakePihole()) if r[2] == "client"]

    assert clients == [("192.168.0.44", "raspberrypi.lan", 6106)]


def test_every_read_covers_the_utc_day_and_the_session_is_logged_out():
    fake = FakePihole()

    rows_through(fake)

    reads = [query for method, path, query in fake.calls if path.startswith("stats/")]
    assert all(
        q["from"] == [str(DAY_START)] and q["until"] == [str(DAY_START + 86400)]
        for q in reads
    )
    for top in ("top_clients", "top_domains"):
        assert [q["blocked"] for _, p, q in fake.calls if p.endswith(top)] == [
            ["false"],
            ["true"],
        ]
    assert fake.calls[0][:2] == ("POST", "auth")
    assert fake.calls[-1][:2] == ("DELETE", "auth")


def test_a_refused_login_raises_before_any_read():
    fake = FakePihole(valid=False)

    with pytest.raises(RuntimeError, match="refused the login"):
        rows_through(fake)
    assert len(fake.calls) == 1


def as_dicts(rows):
    return [dict(zip(dh.COUNTS_COLUMNS, row, strict=True)) for row in rows]


def test_both_instances_answering_pass_the_check():
    rows = as_dicts(rows_through(FakePihole()))
    rows += [{**row, "instance": "pihole-2"} for row in rows]

    assert dh.quality_problems(DAY, rows) == []


def test_a_missing_instance_or_overcounted_clients_fail_the_check():
    rows = as_dicts(rows_through(FakePihole()))
    rows.append(
        {"instance": "pihole", "kind": "client", "key": "x", "label": "", "n": 30000}
    )

    assert dh.quality_problems(DAY, rows) == [
        f"{DAY}: pihole's clients sum to 37339, over its 29160 queries",
        f"{DAY}: pihole-2 answered 0 queries, under 1000",
    ]


def test_the_backfill_window_stays_a_day_inside_ftls_retention():
    now = datetime(2026, 11, 20, 6, tzinfo=timezone.utc)

    assert dh.in_backfill_window(date(2026, 8, 23), now)
    assert not dh.in_backfill_window(date(2026, 8, 22), now)
    assert not dh.in_backfill_window(date(2026, 11, 20), now)
