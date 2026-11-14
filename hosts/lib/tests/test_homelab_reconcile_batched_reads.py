"""`homelab-reconcile` asks dpkg and systemd about many items in one command each.

A run reads the owners of every legacy path with one `dpkg -S`, and the state of every unit
with one `systemctl show`. These tests hold the batching and the cases where one command
cannot answer for all of its items.

Run: uv run pytest hosts/lib/tests/test_homelab_reconcile_batched_reads.py
"""

import pytest
from _reconcile_host import Node


@pytest.fixture
def node(tmp_path) -> Node:
    node = Node(tmp_path)
    node.unit("homelab-updater.service", policy="manual")
    node.unit("homelab-updater.timer")
    node.unit("homelab-reconcile.service", policy="manual")
    return node


def settled(node: Node) -> None:
    """Run reconcile once, then forget its calls, so the next run's calls stand alone."""
    result = node.run()
    assert result.returncode == 0, result.stderr
    node.stub.joinpath("calls.marshal").unlink()


def show_calls(node: Node) -> list[list[str]]:
    return [c["argv"][4:] for c in node.calls("systemctl") if c["argv"][1] == "show"]


def test_one_systemctl_show_reads_every_units_state_and_no_unit_is_asked_alone(node):
    for name in ("homelab-a.timer", "homelab-b.timer", "homelab-c.timer"):
        node.unit(name, linked=True)
    settled(node)
    node.state["units"]["homelab-b.timer"]["active"] = "inactive"

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.actions() == [["start", "homelab-b.timer"]]
    assert [c for c in node.calls("systemctl") if c["argv"][1] == "is-active"] == []
    assert show_calls(node) == [
        ["homelab-*"],
        [
            "homelab-a.timer",
            "homelab-b.timer",
            "homelab-c.timer",
            "homelab-reconcile.service",
            "homelab-updater.service",
            "homelab-updater.timer",
        ],
    ]


def test_a_unit_an_earlier_start_pulled_in_is_not_started_again(node):
    node.unit("homelab-a.service", linked=True)
    node.unit("homelab-b.service", linked=True)
    settled(node)
    for name in ("homelab-a.service", "homelab-b.service"):
        node.state["units"][name]["active"] = "inactive"
    node.state["effects"] = {
        "start homelab-a.service": {"pulls": ["homelab-b.service"]}
    }

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.actions() == [["start", "homelab-a.service"]]


def test_a_template_in_the_manifest_does_not_hide_the_other_units_state(node):
    node.unit("claude-land@.service")
    node.unit("homelab-pi-sd.timer", linked=True, load="masked")

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.actions() == []
    assert all("claude-land@.service" not in argv for argv in show_calls(node))


def test_a_unit_systemctl_refuses_leaves_the_others_read_one_at_a_time(node):
    node.unit("homelab-a.timer", linked=True)
    node.unit("homelab-pi-sd.timer", linked=True, load="masked")
    node.state["fail"].append("show homelab-a.timer")

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert ["restart", "homelab-pi-sd.timer"] not in node.actions()
    assert ["homelab-pi-sd.timer"] in show_calls(node)


def test_a_run_after_every_legacy_entry_retired_asks_dpkg_nothing(node):
    for name in ("owned", "stray"):
        path = node.root / "usr/local/bin" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
        node.manifest["legacy"].append({"path": f"/usr/local/bin/{name}"})
    node.manifest["legacy"] += [{"cron_file": "gc"}, {"unit": "pi-sd-health.timer"}]
    node.state["owned"] = ["/usr/local/bin/owned"]

    settled(node)
    result = node.run()

    assert not (node.root / "usr/local/bin/stray").exists()
    assert (node.root / "usr/local/bin/owned").exists()
    assert result.returncode == 0, result.stderr
    assert node.calls("dpkg") == []


def test_the_first_run_asks_dpkg_once_for_every_legacy_path(node):
    node.manifest["legacy"] += [
        {"path": "/usr/local/bin/stray"},
        {"cron_file": "gc"},
        {"unit": "pi-sd-health.timer"},
        {"cron": "pi-sd-health", "user": "ubuntu"},
    ]

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert [c["argv"] for c in node.calls("dpkg")] == [
        [
            "dpkg",
            "-S",
            "/etc/cron.d/gc",
            "/etc/systemd/system/pi-sd-health.timer",
            "/usr/local/bin/stray",
        ]
    ]


def test_a_dpkg_that_cannot_answer_leaves_every_legacy_path_in_place(node):
    stray = node.root / "usr/local/bin/stray"
    stray.parent.mkdir(parents=True)
    stray.write_text("")
    node.manifest["legacy"].append({"path": "/usr/local/bin/stray"})
    node.state["fail"].append("dpkg -S")

    result = node.run()

    assert result.returncode == 1
    assert stray.exists()
    assert node.applied()["retired"] == []
