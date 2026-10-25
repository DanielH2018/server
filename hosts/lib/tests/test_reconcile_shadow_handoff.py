"""A packaged unit takes over from the Ansible-era copy under /etc/systemd/system that shadows it.

H7.41's builder units are already named `homelab-*`, so the package ships the same names as the
copies in /etc/systemd/system, and those copies win. Reconcile removes the shadowing file, runs
one daemon-reload, and restarts the packaged unit whose fragment changed. A `{path:}` entry
whose unit the package does not ship stays in place and counts as drift.

Run: uv run pytest hosts/lib/tests/test_reconcile_shadow_handoff.py
"""

import pytest
from _reconcile_host import Node

UNIT = "homelab-builder.service"
ETC = "etc/systemd/system"


@pytest.fixture
def node(tmp_path) -> Node:
    node = Node(tmp_path)
    node.unit("homelab-updater.service", policy="manual")
    node.unit("homelab-reconcile.service", policy="manual")
    return node


def shadow(node: Node, relative: str, text: str = "[Service]\nOld=1\n") -> None:
    path = node.root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def reloads(node: Node) -> list[int]:
    """The positions of daemon-reload among the systemctl calls."""
    verbs = [call["argv"][1] for call in node.calls("systemctl")]
    return [i for i, verb in enumerate(verbs) if verb == "daemon-reload"]


@pytest.mark.parametrize(
    "legacy",
    [{"path": f"/{ETC}/{UNIT}"}, {"unit": UNIT}],
    ids=["path-entry", "service-entry"],
)
def test_a_shadowing_copy_of_a_packaged_unit_is_replaced_by_one_reload_and_a_restart(
    node, legacy
):
    node.unit(
        UNIT, "[Service]\nNew=1\n", active="active", enabled="enabled", linked=True
    )
    assert node.run().returncode == 0  # the first run restarts the new units
    shadow(node, f"{ETC}/{UNIT}")
    node.manifest["legacy"].append(legacy)

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert not (node.root / ETC / UNIT).exists()
    assert len(reloads(node)) == 1
    assert node.actions() == [["restart", UNIT]]
    assert node.applied()["drift"]["legacy"] == 0
    assert len(node.applied()["retired"]) == 1
    verbs = [call["argv"][1] for call in node.calls("systemctl")]
    assert verbs.index("daemon-reload") < verbs.index("restart")


def test_two_shadows_share_one_daemon_reload_and_a_wants_link_alone_restarts_nothing(
    node,
):
    other = "homelab-signer.service"
    node.unit(
        UNIT, "[Service]\nNew=1\n", active="active", enabled="enabled", linked=True
    )
    node.unit(
        other, "[Service]\nSame=1\n", active="active", enabled="enabled", linked=True
    )
    assert node.run().returncode == 0
    shadow(node, f"{ETC}/{UNIT}")
    shadow(node, f"{ETC}/{other}", "[Service]\nSame=1\n")
    shadow(node, f"{ETC}/multi-user.target.wants/{other}", "")
    for name in (
        f"{ETC}/{UNIT}",
        f"{ETC}/{other}",
        f"{ETC}/multi-user.target.wants/{other}",
    ):
        node.manifest["legacy"].append({"path": f"/{name}"})

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert len(reloads(node)) == 1
    # The signer's copy has the packaged bytes, so only the builder's fragment changed.
    assert node.actions() == [["restart", UNIT]]
    assert not (node.root / ETC / "multi-user.target.wants" / other).exists()


def test_a_path_under_etc_whose_unit_the_package_does_not_ship_is_left_and_reported(
    node,
):
    shadow(node, f"{ETC}/{UNIT}")
    node.manifest["legacy"].append({"path": f"/{ETC}/{UNIT}"})

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert (node.root / ETC / UNIT).exists()
    assert reloads(node) == []
    assert node.actions() == []
    assert node.applied()["drift"]["legacy"] == 1
    assert node.applied()["retired"] == []
    assert f"keeping /{ETC}/{UNIT}" in result.stderr + result.stdout


def test_the_packages_enable_link_survives_the_handoff_of_the_ansible_era_link(node):
    # H7.41b: Ansible enabled homelab-builder.timer with a link under /etc, and the package
    # enables it with its own link. Removing the /etc link must not disable or stop the timer.
    timer = "homelab-builder.timer"
    node.unit(
        timer, "[Timer]\nNew=1\n", active="active", enabled="enabled", linked=True
    )
    assert node.run().returncode == 0
    shadow(node, f"{ETC}/{timer}", "[Timer]\nOld=1\n")
    shadow(node, f"{ETC}/timers.target.wants/{timer}", "")
    for name in (f"{ETC}/{timer}", f"{ETC}/timers.target.wants/{timer}"):
        node.manifest["legacy"].append({"path": f"/{name}"})

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert not (node.root / ETC / "timers.target.wants" / timer).exists()
    assert node.applied()["enable_links"] == [timer]
    assert node.actions() == [["restart", timer]]
    verbs = {call["argv"][1] for call in node.calls("systemctl")}
    assert not {"disable", "stop", "mask"} & verbs
