"""Reconcile's libvirt arm, against a stub `virsh` (H7.2c).

The clauses are H7's *Red proofs for the checks this slice adds*, libvirt arm. A refused undefine
is drift, so the run succeeds and the updater does not hold the host
(`test_reconcile_hold_way_out.py` drives the updater). Each red fixture is a copy of reconcile
with one rule removed (`_reconcile_arms.red`), and its scenario must fail against it.

Run: uv run pytest hosts/lib/tests/test_reconcile_libvirt_arm.py
"""

import hashlib

import pytest
from _reconcile_arms import base, metrics, red
from _reconcile_host import Node

ARM = "reconcile_lib/libvirt.py"
DIR = "/usr/lib/homelab/libvirt"
DRIFT = 'homelab_host_reconcile_drift{host="daniel-pi",arm="libvirt"}'
UUID = "6f1c8e2a-0b1d-4c3e-8a5f-1234567890ab"


def declare(
    node: Node, networks: tuple = (), nwfilters: tuple = (), body: str = ""
) -> None:
    """Ship each named object's XML and list it; `body` changes the XML to change the hash."""
    section = {"networks": [], "nwfilters": []}
    for kind, root, names in (
        ("networks", "network", networks),
        ("nwfilters", "filter", nwfilters),
    ):
        for name in names:
            path = f"{DIR}/{kind}/{name}.xml"
            target = node.root / path.lstrip("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                f"<{root}><name>{name}</name><uuid>{UUID}</uuid>{body}</{root}>\n"
            )
            section[kind].append({"name": name, "path": path})
    node.manifest["reconcilers"] = ["libvirt"]
    node.manifest["libvirt"] = section


def objects(node: Node, kind: str) -> dict:
    return node.state["libvirt"][kind]


def virsh_verbs(node: Node) -> list[str]:
    return [call["argv"][3] for call in node.calls("virsh")]


@pytest.fixture
def node(tmp_path) -> Node:
    return base(Node(tmp_path))


# What is and is not run.


def test_a_host_declaring_no_libvirt_object_runs_no_virsh_command_at_all(node):
    node.state["libvirt"] = {"networks": {"default": {"active": True}}, "nwfilters": {}}

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.calls("virsh") == []
    assert "libvirt" not in node.applied()
    assert 'arm="libvirt"' not in metrics(node)


def test_declared_objects_are_defined_the_network_is_started_and_a_second_run_writes_nothing(
    node,
):
    declare(node, networks=("staging",), nwfilters=("fence",))

    first = node.run()
    verbs = virsh_verbs(node)
    second = node.run()

    assert first.returncode == 0, first.stderr
    assert verbs.index("nwfilter-define") < verbs.index("net-define")
    assert objects(node, "networks")["staging"] == {"active": True, "autostart": True}
    record = node.applied()["libvirt"]
    digest = hashlib.sha256(
        (node.root / f"{DIR}/networks/staging.xml".lstrip("/")).read_bytes()
    ).hexdigest()
    assert record["networks"] == {"staging": digest}
    assert list(record["nwfilters"]) == ["fence"]
    assert second.returncode == 0, second.stderr
    assert set(virsh_verbs(node)) <= {"net-info", "nwfilter-dumpxml"}
    assert f"{DRIFT} 0" in metrics(node)


def test_changed_xml_is_defined_again(node):
    declare(node, networks=("staging",))
    assert node.run().returncode == 0
    declare(node, networks=("staging",), body="<bridge name='virbr-stage'/>")

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert "net-define" in virsh_verbs(node)


def test_an_object_libvirt_lost_is_defined_again_though_its_xml_did_not_change(node):
    declare(node, nwfilters=("fence",))
    assert node.run().returncode == 0
    del objects(node, "nwfilters")["fence"]

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert "fence" in objects(node, "nwfilters")


def test_a_failed_define_is_a_fault_and_the_next_run_retries_it(node):
    declare(node, networks=("staging",))
    node.state["libvirt"] = {"fail": ["net-define"]}

    result = node.run()

    assert result.returncode == 1
    assert node.applied()["result"]["ok"] is False
    assert node.applied()["libvirt"]["networks"] == {}
    node.state["libvirt"]["fail"] = []
    assert node.run().returncode == 0
    assert "staging" in node.applied()["libvirt"]["networks"]


# Undefining: only what the arm recorded, never by prefix.


def drop_one(node: Node) -> None:
    declare(node, networks=("staging", "staging-old"), nwfilters=("fence",))
    node.state["libvirt"] = {"networks": {"default": {"active": True}}, "nwfilters": {}}
    assert node.run().returncode == 0
    declare(node, networks=("staging",), nwfilters=("fence",))


def test_an_object_the_arm_recorded_and_no_longer_declares_is_undefined(node):
    drop_one(node)

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert sorted(objects(node, "networks")) == ["default", "staging"]
    assert "staging-old" not in node.applied()["libvirt"]["networks"]


def test_an_object_the_arm_never_recorded_is_never_touched(node):
    drop_one(node)

    node.run()

    undefined = [
        c["argv"][4] for c in node.calls("virsh") if "undefine" in c["argv"][3]
    ]
    assert undefined == ["staging-old"], "default, and the prefix match staging, stay"


def refused_undefine_is_drift(node: Node) -> None:
    drop_one(node)
    node.state["libvirt"]["refuse"] = ["staging-old"]

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.applied()["result"]["ok"] is True, (
        "a guest holding the object is no hold"
    )
    assert "staging-old" in node.applied()["libvirt"]["networks"]
    assert f"{DRIFT} 1" in metrics(node)
    node.state["libvirt"]["refuse"] = []
    assert node.run().returncode == 0
    assert "staging-old" not in objects(node, "networks")
    assert "staging-old" not in node.applied()["libvirt"]["networks"]


def test_a_refused_undefine_is_drift_the_run_succeeds_and_the_next_run_undefines(node):
    refused_undefine_is_drift(node)


def test_red_a_refusal_reported_as_a_failure_would_hold_the_host(tmp_path):
    red(
        tmp_path,
        refused_undefine_is_drift,
        ARM,
        "return ok, record, drift",
        "return ok and drift == 0, record, drift",
    )


def revert_undefines_nothing(node: Node) -> None:
    declare(node, networks=("staging",), nwfilters=("fence",))
    assert node.run().returncode == 0
    node.manifest.pop("libvirt")
    node.manifest["reconcilers"] = []

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert sorted(objects(node, "networks")) == ["staging"]
    assert sorted(objects(node, "nwfilters")) == ["fence"]
    assert "undefine" not in " ".join(virsh_verbs(node))
    assert f"{DRIFT} 2" in metrics(node)


def test_a_revert_that_declares_no_object_undefines_nothing_and_the_run_succeeds(node):
    revert_undefines_nothing(node)


def test_red_an_arm_with_no_floor_would_undefine_everything_on_a_revert(tmp_path):
    red(
        tmp_path,
        revert_undefines_nothing,
        ARM,
        "floor = not section or not any(section[kind] for kind in KINDS)",
        "floor = False",
    )
