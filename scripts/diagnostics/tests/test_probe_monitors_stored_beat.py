"""A long-interval push tile's stored DOWN must survive a Kuma restart in both probes (#4541).

Kuma's exporter emits a monitor's `monitor_status` series only after a beat lands since the
process started. A weekly tile whose last push was a DOWN, followed by the weekly reboot, has
no series for a week. Kuma still holds that beat, and serves it from
`/api/status-page/heartbeat/<slug>`.

These run the real subcommands against the real static-monitors template. Prometheus and
Kuma's two status-page endpoints are stubbed at `subprocess.run`, the curl underneath `core`,
each answering with the whole body its real endpoint returns. kubectl (Kuma's pod age) and sops
(the domain and the gating push tokens) are stubbed at the same seam.
"""

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from diagnostics.probe_lib import cli_parser, monitors
from lib.proc_testing import fake_bin, path_with

# Kuma up 55631s, as on 2026-10-11: past every daily tile's interval, inside the weekly ones.
KUMA_AGE = 55631
STORED_DOWN_TIME = "2026-10-11 02:23:45.734"
LONG_INTERVAL_TILES = ["Homelab Evals", "etcd Restore Drill (full)"]


def _declared():
    with open(monitors.STATIC_MONITORS_PATH) as f:
        return monitors.parse_declared_monitors(f.read())


def _stub_kuma(monkeypatch, tmp_path, silent_tile, stored_status):
    """Prometheus exports every declared tile but `silent_tile`, all UP.

    Kuma's status page holds every declared tile with an id. Its heartbeat endpoint holds one
    beat per tile: UP for all of them, and `stored_status` for `silent_tile`.
    """
    names = sorted(_declared())
    ids = {name: 100 + i for i, name in enumerate(names)}
    prom = json.dumps(
        {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {
                            "__name__": "monitor_status",
                            "job": "uptime-kuma",
                            "monitor_name": name,
                        },
                        "value": [1760170000, "1"],
                    }
                    for name in names
                    if name != silent_tile
                ],
            },
        }
    )
    page = json.dumps(
        {
            "config": {"slug": "homelab", "title": "Homelab", "published": True},
            "incident": None,
            "publicGroupList": [
                {
                    "id": 1,
                    "name": "Everything",
                    "weight": 1,
                    "monitorList": [
                        {"id": ids[n], "name": n, "sendUrl": 0, "type": "push"}
                        for n in names
                    ],
                }
            ],
            "maintenanceList": [],
        }
    )

    def beat(name):
        status = stored_status if name == silent_tile else 1
        return {
            "status": status,
            "time": STORED_DOWN_TIME
            if name == silent_tile
            else "2026-10-11 07:40:00.000",
            "msg": "eval regressed" if status == 0 else "OK",
            "ping": None,
            "important": status == 0,
            "duration": 0,
        }

    heartbeats = json.dumps(
        {
            "heartbeatList": {str(ids[n]): [beat(n)] for n in names},
            "uptimeList": {f"{ids[n]}_24": 1 for n in names},
        }
    )

    def body_for(url):
        if "/api/status-page/heartbeat/" in url:
            return heartbeats
        if "/api/status-page/" in url:
            return page
        return prom

    started = (datetime.now(timezone.utc) - timedelta(seconds=KUMA_AGE)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    kubectl_out = {
        "nodes": {"items": [{"metadata": {"name": "daniel-box"}}]},
        "pods": {
            "items": [
                {
                    "metadata": {"name": "uptime-kuma-0"},
                    "status": {"phase": "Running", "startTime": started},
                }
            ]
        },
    }

    def fake_run(argv, *args, **kwargs):
        tool = os.path.basename(argv[0])
        if tool == "curl":
            # get_status asks curl to append the status code on its own line.
            out = body_for(argv[-1]) + ("\n200" if "-w" in argv else "")
        elif tool == "sops":
            # The domain and every gating push token decrypt to a non-empty value.
            out = "example.test\n"
        elif tool == "kubectl":
            out = json.dumps(kubectl_out["nodes" if "nodes" in argv else "pods"])
        else:
            raise AssertionError(f"unexpected subprocess in a probe test: {argv}")
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    # A kubectl on PATH and a readable kubeconfig, so lib.kubectl reaches `subprocess.run`.
    stubs = fake_bin(tmp_path / "bin", kubectl="exit 1\n")
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    monkeypatch.setenv("PATH", path_with(stubs))
    monkeypatch.setenv("KUBECONFIG", str(kubeconfig))
    monkeypatch.setattr(subprocess, "run", fake_run)


def _line_naming(out, tile):
    return [line for line in out.splitlines() if tile in line]


@pytest.mark.parametrize("tile", LONG_INTERVAL_TILES)
def test_monitors_lists_a_long_interval_tiles_stored_down_after_a_restart(
    monkeypatch, capsys, tmp_path, tile
):
    # The Verify-by: the tile has no series, its last stored beat is status 0, and
    # `monitors` must list it as DOWN rather than drop it out of the total.
    # The fixture puts the tile inside its not-yet-due window.
    assert _declared()[tile]["interval"] > KUMA_AGE
    _stub_kuma(monkeypatch, tmp_path, tile, stored_status=0)
    code = monitors.run_monitors(cli_parser._build_parser().parse_args(["monitors"]))
    out = capsys.readouterr().out
    assert any("DOWN" in line for line in _line_naming(out, tile)), out
    assert code == 1


def test_monitors_labels_a_stored_down_apart_from_a_live_one(
    monkeypatch, capsys, tmp_path
):
    # A stored beat predates the restart, so its line must not read like a live series' DOWN.
    tile = "Homelab Evals"
    _stub_kuma(monkeypatch, tmp_path, tile, stored_status=0)
    monitors.run_monitors(cli_parser._build_parser().parse_args(["monitors"]))
    lines = _line_naming(capsys.readouterr().out, tile)
    assert any("DOWN" in line for line in lines), lines
    assert f"  {tile}: DOWN" not in lines


def test_kuma_drift_names_the_stored_down_of_a_not_yet_due_tile(
    monkeypatch, capsys, tmp_path
):
    # kuma-drift files the tile as "no beat due yet", which is right about drift and must still
    # say its last stored beat was a DOWN.
    tile = "Homelab Evals"
    _stub_kuma(monkeypatch, tmp_path, tile, stored_status=0)
    ns = cli_parser._build_parser().parse_args(["kuma-drift"])
    monitors.run_kuma_drift(ns)
    lines = _line_naming(capsys.readouterr().out, tile)
    assert any("DOWN" in line for line in lines), lines
