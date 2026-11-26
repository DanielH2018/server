"""lidarr's stall sweep: it removes only torrents that ran for hours with no data, and only armed.

The run tests drive `sweep` against one local server that answers qBittorrent's
`transfer/info` and `torrents/info` and Lidarr's `queue` and `queue/bulk`, and records each
DELETE it receives.

Run: uv run pytest k8s/roles/lidarr/tests/test_sweep_lidarr_stalls.py
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

import sweep_lidarr_stalls as sweep
from lib.repo_paths import REPO
from renderer.tests._job_census import script_offence

NOW = 1_800_000_000
HOUR = 3600
SCRIPT = REPO / "k8s/roles/lidarr/files/sweep_lidarr_stalls.py"


def _torrent(n: int, **over) -> dict:
    row = {
        "hash": f"{n:040x}",
        "name": f"Album {n}",
        "state": "stalledDL",
        "progress": 0.5,
        "time_active": 3 * HOUR,
        "last_activity": NOW - 2 * HOUR,
    }
    return {**row, **over}


def _dead(row: dict) -> bool:
    return sweep.is_dead(sweep.parse_torrents([row])[0], NOW)


def test_a_stalled_torrent_that_ran_for_hours_with_no_data_is_dead():
    assert _dead(_torrent(1))
    assert _dead(_torrent(1, state="metaDL", progress=0, last_activity=NOW - 9 * HOUR))


def test_a_torrent_that_just_left_the_queue_is_not_dead():
    # Grabbed nine hours ago, queued until a minute ago: only its running time counts.
    assert not _dead(
        _torrent(1, state="metaDL", time_active=60, last_activity=NOW - 9 * HOUR)
    )


def test_a_torrent_that_received_data_recently_is_not_dead():
    assert not _dead(_torrent(1, last_activity=NOW - 600))


def test_a_downloading_torrent_is_not_dead():
    assert not _dead(_torrent(1, state="downloading"))


def test_the_plan_removes_the_longest_idle_first_up_to_the_cap():
    rows = [
        _torrent(n, last_activity=NOW - (2 + n) * HOUR)
        for n in range(sweep.MAX_REMOVALS + 3)
    ]
    rows.append(_torrent(99))
    ids = {r["hash"]: n for n, r in enumerate(rows) if r["name"] != "Album 99"}
    result = sweep.plan(sweep.parse_torrents(rows), ids, NOW)
    assert [r.torrent.name for r in result.removals][:2] == ["Album 12", "Album 11"]
    assert len(result.removals) == sweep.MAX_REMOVALS
    assert (result.over_cap, result.untracked) == (3, 1)


def test_a_short_queue_page_is_refused():
    page = {"totalRecords": 2, "records": [{"id": 1, "downloadId": "AB"}]}
    with pytest.raises(sweep.SweepError):
        sweep.queue_ids(page)


def test_the_script_passes_the_job_census_helper_rules():
    assert script_offence(SCRIPT.name, SCRIPT.read_text()) == []


class _Apps:
    """Answers like qBittorrent and Lidarr on one port, and keeps the DELETEs it receives."""

    def __init__(self, torrents: list[dict], dht_nodes: int = 300):
        self.torrents = torrents
        self.dht_nodes = dht_nodes
        self.deletes: list[tuple[dict, dict]] = []

    def answer(
        self, method: str, path: str, query: dict, body: bytes, headers
    ) -> tuple[int, object]:
        if path.startswith("/api/v2/"):
            if headers.get("Authorization") != "Bearer qbt-key":
                return 403, "Forbidden"
            if path == "/api/v2/transfer/info":
                return 200, {"dht_nodes": self.dht_nodes}
            return 200, [
                t for t in self.torrents if query.get("category") == ["lidarr"]
            ]
        if headers.get("X-Api-Key") != "lidarr-key":
            return 401, {}
        if method == "DELETE" and path == "/api/v1/queue/bulk":
            self.deletes.append((query, json.loads(body)))
            return 200, None
        records = [
            {"id": n, "downloadId": t["hash"].upper()}
            for n, t in enumerate(self.torrents)
        ]
        return 200, {"totalRecords": len(records), "records": records}


@pytest.fixture
def serve():
    servers = []

    def start(apps: _Apps) -> dict[str, str]:
        class Handler(BaseHTTPRequestHandler):
            def _reply(self):
                url = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                status, payload = apps.answer(
                    self.command,
                    url.path,
                    parse_qs(url.query),
                    self.rfile.read(length),
                    self.headers,
                )
                data = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_DELETE = _reply

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        url = f"http://127.0.0.1:{server.server_port}"
        return {
            "LIDARR_URL": url,
            "LIDARR_API_KEY": "lidarr-key",
            "QBT_URL": url,
            "QBT_API_KEY": "qbt-key",
        }

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def test_an_armed_run_removes_the_dead_with_blocklist_and_research(serve, capsys):
    apps = _Apps([_torrent(1), _torrent(2, last_activity=NOW - 60)])
    env = {**serve(apps), "ARMED": "true"}
    assert sweep.sweep(env, NOW) == 0
    ((query, body),) = apps.deletes
    assert body == {"ids": [0]}
    assert query == {
        "removeFromClient": ["true"],
        "blocklist": ["true"],
        "skipRedownload": ["false"],
    }
    assert capsys.readouterr().out.startswith("lidarr stall sweep: 1 removed,")


def test_an_unarmed_run_reports_and_removes_nothing(serve, capsys):
    apps = _Apps([_torrent(1)])
    assert sweep.sweep(serve(apps), NOW) == 0
    assert apps.deletes == []
    assert "would remove: Album 1" in capsys.readouterr().out


def test_a_run_with_too_few_dht_nodes_refuses_before_any_removal(serve):
    apps = _Apps([_torrent(1)], dht_nodes=0)
    with pytest.raises(sweep.SweepError):
        sweep.sweep({**serve(apps), "ARMED": "true"}, NOW)
    assert apps.deletes == []
