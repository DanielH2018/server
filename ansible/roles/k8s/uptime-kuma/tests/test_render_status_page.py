"""The sync writes a page only when the grouping actually changed, and writes it whole.

Every case pairs an input the renderer must pass over with one it must act on: a check that is
only ever observed passing is a check with no evidence it can fail.
"""

import json
import sys as _sys
from pathlib import Path as _Path

import pytest

ROLE = _Path(__file__).resolve().parents[1]
_sys.path.insert(0, str(ROLE / "files"))

from render_status_page import main  # noqa: E402

INDEX = {
    "grafana-k8s": "k3s Grafana",
    "daniel-pi-host": "Daniel Pi Host",
    "longhorn-backup": "k3s Longhorn Backup",
}

RULES = [
    {"name": "Services", "match": ["-k8s$"]},
    {"name": "Raspberry Pi", "match": ["^daniel-pi-"]},
    {"name": "Other", "match": [".*"]},
]

MONITORS = {
    "7": {"id": 7, "name": "k3s Grafana"},
    "8": {"id": 8, "name": "Daniel Pi Host"},
    "9": {"id": 9, "name": "k3s Longhorn Backup"},
}

LIVE_PAGE = {
    "slug": "containers",
    "title": "Homelab",
    "description": "hand-written, and the sync must not lose it",
    "published": True,
    "theme": "dark",
    "publicGroupList": [
        {
            "id": 1,
            "name": "Services",
            "weight": 1,
            "monitorList": [{"id": 7, "name": "k3s Grafana"}],
        },
        {
            "id": 2,
            "name": "Raspberry Pi",
            "weight": 2,
            "monitorList": [{"id": 8, "name": "Daniel Pi Host"}],
        },
        {
            "id": 3,
            "name": "Other",
            "weight": 3,
            "monitorList": [{"id": 9, "name": "k3s Longhorn Backup"}],
        },
    ],
}


def run(tmp_path, *, index=None, page=None, monitors=None, rules=None):
    """Run the renderer over one set of inputs; return the desired page, or None if unwritten."""
    paths = {}
    for name, payload in (
        ("index", INDEX if index is None else index),
        ("rules", RULES if rules is None else rules),
        ("page", LIVE_PAGE if page is None else page),
        ("monitors", MONITORS if monitors is None else monitors),
    ):
        paths[name] = tmp_path / f"{name}.json"
        paths[name].write_text(json.dumps(payload))

    out = tmp_path / "desired.json"
    code = main(
        [
            f"--index={paths['index']}",
            f"--rules={paths['rules']}",
            f"--page={paths['page']}",
            f"--monitors={paths['monitors']}",
            f"--out={out}",
        ]
    )
    assert code == 0
    return json.loads(out.read_text()) if out.exists() else None


def test_an_unchanged_grouping_is_clean(tmp_path):
    assert run(tmp_path) is None


def test_a_moved_monitor_is_flagged(tmp_path):
    """The B2 tile moves out of Other, which is the only difference from the live page."""
    rules = [
        {"name": "Services", "match": ["-k8s$"]},
        {"name": "Raspberry Pi", "match": ["^daniel-pi-"]},
        {"name": "Other", "match": [".*"]},
    ]
    rules[0]["match"].append("^longhorn-backup$")

    desired = run(tmp_path, rules=rules)
    assert desired is not None
    services = next(
        group for group in desired["publicGroupList"] if group["name"] == "Services"
    )
    assert [monitor["id"] for monitor in services["monitorList"]] == [7, 9]


def test_a_group_that_empties_is_dropped_from_the_page(tmp_path):
    page = json.loads(json.dumps(LIVE_PAGE))
    desired = run(tmp_path, index={"grafana-k8s": "k3s Grafana"}, page=page)
    assert [group["name"] for group in desired["publicGroupList"]] == ["Services"]


def test_the_pages_other_fields_survive(tmp_path):
    """`saveStatusPage` writes the whole object, so anything dropped here is blanked live."""
    desired = run(tmp_path, index={"grafana-k8s": "k3s Grafana"})
    assert desired["description"] == LIVE_PAGE["description"]
    assert desired["title"] == LIVE_PAGE["title"]
    assert desired["published"] is True
    assert desired["theme"] == "dark"


def test_existing_group_ids_are_kept(tmp_path):
    desired = run(
        tmp_path,
        index={"grafana-k8s": "k3s Grafana", "daniel-pi-host": "Daniel Pi Host"},
    )
    by_name = {group["name"]: group for group in desired["publicGroupList"]}
    assert by_name["Services"]["id"] == 1
    assert by_name["Raspberry Pi"]["id"] == 2


def test_a_group_kuma_has_never_seen_is_written_without_an_id(tmp_path):
    rules = [{"name": "Brand New", "match": [".*"]}]
    desired = run(tmp_path, rules=rules)
    assert "id" not in desired["publicGroupList"][0]


def test_a_declared_monitor_with_no_live_counterpart_fails(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        run(tmp_path, index=dict(INDEX, ghost="Not In Kuma"))
    assert "Not In Kuma" in str(excinfo.value)


def test_a_monitor_list_shaped_as_an_array_still_resolves(tmp_path):
    assert run(tmp_path, monitors=list(MONITORS.values())) is None


def test_an_unreadable_monitor_list_fails_rather_than_emptying_the_page(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        run(tmp_path, monitors={"7": "not an object", "8": "nor this"})
    assert "unreadable monitor list" in str(excinfo.value)


def test_server_echoed_monitor_fields_do_not_count_as_a_change(tmp_path):
    """Kuma returns `type` and `sendUrl` on each entry; judging on them would write every run."""
    page = json.loads(json.dumps(LIVE_PAGE))
    for group in page["publicGroupList"]:
        for monitor in group["monitorList"]:
            monitor.update({"type": "http", "sendUrl": 0})
    assert run(tmp_path, page=page) is None
