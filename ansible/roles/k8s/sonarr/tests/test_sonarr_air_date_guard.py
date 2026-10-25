"""The air-date guard's rule, driven through main() against a recording fake of Sonarr's API.

Every test runs the script end to end and asserts on the requests that reach the transport,
so the apply gate is checked where a DELETE would actually be sent.

Run: uv run pytest ansible/roles/k8s/sonarr/tests/test_sonarr_air_date_guard.py
"""

import io
import json
import urllib.parse
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

import sonarr_air_date_guard

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
ARGS = [
    "--grace-hours=24",
    "--skip-series-type=anime",
    "--exempt-tag=allow-early",
    "--max-removals=5",
]
EXEMPT_TAG_ID = 7


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def record(
    queue_id,
    download_id,
    air,
    *,
    series_type="standard",
    tags=(),
    added=None,
):
    """One queue record, as `queue?includeSeries=true&includeEpisode=true` returns it."""
    return {
        "id": queue_id,
        "downloadId": download_id,
        "title": f"Show {download_id}",
        "added": iso(added or NOW - timedelta(hours=1)),
        "series": {"seriesType": series_type, "tags": list(tags)},
        "episode": {"airDateUtc": iso(air) if air else None},
    }


class FakeSonarr:
    """Serves a queue and per-download grab history, and records every request."""

    def __init__(self, queue, published):
        self.queue = queue
        self.published = published  # downloadId -> publish datetime, or None
        self.calls = []

    @contextmanager
    def __call__(self, req, timeout):
        url = urllib.parse.urlparse(req.full_url)
        path = url.path.removeprefix("/api/v3/")
        query = urllib.parse.parse_qs(url.query)
        self.calls.append((req.get_method(), path, query))
        body = None
        if path == "system/status":
            body = {"version": "4.0.20.3014"}
        elif path == "tag":
            body = [{"id": EXEMPT_TAG_ID, "label": "allow-early"}]
        elif path == "queue":
            body = {"totalRecords": len(self.queue), "records": self.queue}
        elif path == "history":
            assert query["eventType"] == ["1"]
            published = self.published.get(query["downloadId"][0])
            data = {"publishedDate": iso(published)} if published else {}
            body = {"records": [{"eventType": "grabbed", "data": data}]}
        yield io.BytesIO(json.dumps(body).encode() if body is not None else b"")

    def deletes(self):
        return [(p, q) for m, p, q in self.calls if m == "DELETE"]


class StatefulSonarr(FakeSonarr):
    """A FakeSonarr whose DELETE removes the download's records, so a second run sees it."""

    @contextmanager
    def __call__(self, req, timeout):
        with super().__call__(req, timeout) as body:
            yield body
        if req.get_method() == "DELETE":
            queue_id = int(self.calls[-1][1].split("/")[1])
            gone = next(r["downloadId"] for r in self.queue if r["id"] == queue_id)
            self.queue = [r for r in self.queue if r["downloadId"] != gone]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SONARR_URL", "http://sonarr.homelab.svc.cluster.local:8989")
    monkeypatch.setenv("SONARR_API_KEY", "k")


def run(api, *extra):
    return sonarr_air_date_guard.main([*ARGS, *extra], opener=api, now=NOW)


def test_a_release_published_days_before_air_is_removed_and_blocklisted():
    # Today's VisionQuest S01E05 shape: published 3 days before an episode that airs later.
    air = NOW + timedelta(days=3)
    api = FakeSonarr([record(11, "fake", air)], {"fake": NOW - timedelta(hours=2)})
    assert run(api, "--apply") == 0
    assert api.deletes() == [
        (
            "queue/11",
            {
                "removeFromClient": ["true"],
                "blocklist": ["true"],
                "skipRedownload": ["true"],
            },
        )
    ]


def test_a_release_published_inside_the_grace_is_kept():
    air = NOW + timedelta(hours=6)
    api = FakeSonarr([record(11, "real", air)], {"real": NOW - timedelta(hours=1)})
    run(api, "--apply")
    assert api.deletes() == []


def test_a_release_published_after_air_is_kept():
    air = NOW - timedelta(hours=3)
    api = FakeSonarr([record(11, "real", air)], {"real": air + timedelta(hours=1)})
    run(api, "--apply")
    assert api.deletes() == []


def test_a_pre_air_release_grabbed_after_air_lets_sonarr_search_again():
    # The episode has aired, so a replacement search finds real releases, not the next fake.
    air = NOW - timedelta(hours=1)
    api = FakeSonarr([record(11, "fake", air)], {"fake": air - timedelta(days=2)})
    run(api, "--apply")
    assert [q["skipRedownload"] for _, q in api.deletes()] == [["false"]]


@pytest.mark.parametrize(
    "queued",
    [
        pytest.param({"series_type": "anime"}, id="anime-series"),
        pytest.param({"tags": [EXEMPT_TAG_ID]}, id="exempt-tag"),
    ],
)
def test_anime_and_exempt_series_are_left_alone(queued):
    air = NOW + timedelta(days=3)
    api = FakeSonarr(
        [record(11, "fake", air, **queued)], {"fake": NOW - timedelta(hours=2)}
    )
    run(api, "--apply")
    assert api.deletes() == []


def test_a_missing_air_date_skips_the_download_rather_than_blocklisting_it(capsys):
    api = FakeSonarr([record(11, "tba", None)], {"tba": NOW - timedelta(days=5)})
    run(api, "--apply")
    assert api.deletes() == []
    assert "skip 'Show tba': an episode has no air date" in capsys.readouterr().out


def test_a_multi_episode_release_fails_on_its_latest_episode_and_is_removed_once():
    # E01 aired a week ago; E02 airs in two days. Published yesterday, it fails on E02.
    api = FakeSonarr(
        [
            record(11, "pack", NOW - timedelta(days=7)),
            record(12, "pack", NOW + timedelta(days=2)),
        ],
        {"pack": NOW - timedelta(days=1)},
    )
    run(api, "--apply")
    assert [p for p, _ in api.deletes()] == ["queue/11"]


def test_without_a_publish_date_a_grab_before_the_cutoff_still_proves_the_fake():
    air = NOW + timedelta(days=3)
    queued = record(11, "fake", air, added=NOW - timedelta(hours=1))
    api = FakeSonarr([queued], {"fake": None})
    run(api, "--apply")
    assert [p for p, _ in api.deletes()] == ["queue/11"]


def test_without_a_publish_date_a_grab_after_the_cutoff_is_kept():
    air = NOW - timedelta(hours=2)
    api = FakeSonarr([record(11, "unknown", air)], {"unknown": None})
    run(api, "--apply")
    assert api.deletes() == []


def test_a_dry_run_sends_no_delete(capsys):
    air = NOW + timedelta(days=3)
    api = FakeSonarr([record(11, "fake", air)], {"fake": NOW - timedelta(hours=2)})
    run(api)
    assert api.deletes() == []
    assert "would remove 'Show fake'" in capsys.readouterr().out


def test_max_removals_caps_removals_per_run_and_reports_the_rest(capsys):
    air = NOW + timedelta(days=3)
    queue = [record(i, f"fake{i}", air) for i in range(7)]
    api = FakeSonarr(queue, {f"fake{i}": NOW - timedelta(hours=2) for i in range(7)})
    run(api, "--apply")
    assert len(api.deletes()) == 5
    assert "deferred 2 removals: --max-removals 5 reached" in capsys.readouterr().out


def test_a_second_run_removes_nothing():
    """The rerun-safe test sonarr-verify's gate Job names (`flux_cronjob_gates`).

    The gate copy and a scheduled firing can overlap, and the gate re-runs on every sonarr
    revision, so a run straight after an applied one must send no DELETE.
    """
    # fact: ansible/roles/k8s/sonarr/CLAUDE.md#Notable
    air = NOW + timedelta(days=3)
    api = StatefulSonarr(
        [record(11, "fake", air), record(12, "real", NOW - timedelta(days=1))],
        {"fake": NOW - timedelta(hours=2), "real": NOW - timedelta(hours=20)},
    )
    run(api, "--apply")
    assert [p for p, _ in api.deletes()] == ["queue/11"]
    api.calls.clear()
    run(api, "--apply")
    assert api.deletes() == []
