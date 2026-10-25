"""`hosts/lib/homelab-reconcile`'s restart contract and secrets step (H4.1b).

Each test is one of H4's *Verification* reconcile cases (docs/target-architecture/host/
h4-secrets-unit.md), run against the stub `systemctl` of `_reconcile_host.py`. The fixture is
daniel-server's shape: a package that ships `secrets-unit`, and `nut-monitor`, another package's
unit, which a persisted output feeds.

Run: uv run pytest hosts/lib/tests/test_homelab_reconcile_restart.py
"""

import json

import pytest
from _reconcile_arms import live_unit
from _reconcile_host import SECRETS_UNIT, Node

from renderer.renderer_lib import host as host_mode

NUT = "nut-monitor.service"
UPSMON = "etc/nut/upsmon.conf"
SECRETS_RESTART = f"restart {SECRETS_UNIT}"
# What a successful `homelab-secrets` run leaves: the persisted output, and its dependent queued.
WRITES_UPSMON = {
    "write": {f"/var/lib/homelab-secrets/{UPSMON}": "rendered\n"},
    "queue": [NUT],
}


@pytest.fixture
def node(tmp_path) -> Node:
    node = Node(tmp_path)
    node.unit("homelab-updater.service", policy="manual")
    node.unit("homelab-reconcile.service", policy="manual")
    node.unit(NUT, active="active", enabled="enabled")
    node.write_dropin(
        NUT, "[Service]\nEnvironment=NUT_CONFPATH=/var/lib/homelab-secrets\n"
    )
    node.secrets(
        outputs=[{"destination": UPSMON, "persist": True, "dependents": [NUT]}]
    )
    node.state["effects"] = {SECRETS_RESTART: WRITES_UPSMON}
    return node


def settled(node: Node) -> None:
    result = node.run()
    assert result.returncode == 0, result.stderr


def names(node: Node, unit: str) -> list[list[str]]:
    return [call for call in node.writes() if call[-1] == unit]


# The secrets step and the restart set.


def test_the_secrets_step_runs_first_and_each_dependent_is_restarted_once_without_no_block(
    node,
):
    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.writes() == [
        ["restart", SECRETS_UNIT],
        ["reset-failed", NUT],
        ["restart", NUT],
    ]
    assert all("--no-block" not in call["argv"] for call in node.calls("systemctl"))
    assert node.applied()["secrets_inputs_sha256"] == "a" * 64
    assert node.pending() == []


def test_unchanged_secrets_inputs_and_an_active_unit_make_no_secrets_call(node):
    settled(node)

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.writes() == []


def test_a_changed_digest_or_an_inactive_unit_reruns_the_secrets_step(node):
    settled(node)
    node.manifest["secrets_inputs_sha256"] = "b" * 64
    assert node.run().returncode == 0
    assert node.writes() == [
        ["restart", SECRETS_UNIT],
        ["reset-failed", NUT],
        ["restart", NUT],
    ]

    node.state["units"][SECRETS_UNIT]["active"] = "inactive"
    assert node.run().returncode == 0
    assert node.writes() == [
        ["restart", SECRETS_UNIT],
        ["reset-failed", NUT],
        ["restart", NUT],
    ]


def test_a_changed_secrets_unit_file_restarts_it_once_and_not_in_step_3(node):
    settled(node)
    node.write_unit(SECRETS_UNIT, "[Service]\nType=oneshot\nTimeoutStartSec=90\n")

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert names(node, SECRETS_UNIT) == [["restart", SECRETS_UNIT]]


def test_a_pending_unit_is_restarted_once_although_this_runs_secrets_step_added_nothing(
    node,
):
    # A run killed after the secrets step left it queued; the next run finds nothing to add.
    settled(node)
    node.queue(NUT)

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.writes() == [["reset-failed", NUT], ["restart", NUT]]
    assert node.pending() == []


def test_a_pending_unit_that_is_neither_enabled_nor_active_is_dropped_unrestarted(node):
    node.unit("homelab-pi-sd.service")
    settled(node)
    node.queue("homelab-pi-sd.service")

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.writes() == []
    assert node.pending() == []


def test_a_pending_manual_unit_is_dropped_unrestarted(node):
    node.unit("homelab-thing.service", active="active", linked=True, policy="manual")
    settled(node)
    node.queue("homelab-thing.service")

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.writes() == []
    assert node.pending() == []


# One call per unit, and the reset before it.


def test_a_failed_enabled_unit_whose_hash_changed_gets_one_restart_after_a_reset(node):
    node.unit("homelab-compose.service", active="failed", linked=True)
    settled(node)
    node.write_unit("homelab-compose.service", "[Service]\nExecStart=/bin/true\n")
    node.state["units"]["homelab-compose.service"]["active"] = "failed"

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert names(node, "homelab-compose.service") == [
        ["reset-failed", "homelab-compose.service"],
        ["restart", "homelab-compose.service"],
    ]


def test_a_failed_unit_at_its_start_limit_is_reset_before_the_start_and_starts(node):
    node.unit("homelab-compose.service", active="active", linked=True)
    settled(node)
    node.state["units"]["homelab-compose.service"].update(active="failed", limit=True)

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert names(node, "homelab-compose.service") == [
        ["reset-failed", "homelab-compose.service"],
        ["start", "homelab-compose.service"],
    ]
    assert node.state["units"]["homelab-compose.service"]["active"] == "active"


def test_a_restart_that_fails_queues_the_unit_and_the_next_run_restarts_it_once(node):
    settled(node)
    node.write_dropin(NUT, "[Service]\nEnvironment=CHANGED=1\n")
    node.state["fail"] = [f"restart {NUT}"]

    failed = node.run()

    assert failed.returncode == 1
    assert node.pending() == [NUT]

    node.state["fail"] = []
    retried = node.run()

    assert retried.returncode == 0, retried.stderr
    assert names(node, NUT) == [["reset-failed", NUT], ["restart", NUT]]
    assert node.pending() == []


# The windowed exemption.


def test_a_windowed_unit_is_never_reset_started_or_restarted_and_leaves_the_queue(node):
    node.unit("k3s.service", active="activating", enabled="enabled", policy="windowed")
    settled(node)
    node.write_dropin("k3s.service", "[Service]\nEnvironment=CHANGED=1\n")
    node.queue("k3s.service")

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert names(node, "k3s.service") == []
    assert node.pending() == []


def test_step_2_skips_a_linked_windowed_unit_that_is_not_active(node):
    node.unit("k3s.service", active="activating", linked=True, policy="windowed")

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert names(node, "k3s.service") == []


# The existence gate.


def test_a_failed_secrets_step_on_a_fresh_host_skips_the_dependent_and_queues_it(node):
    node.state["fail"] = [SECRETS_RESTART]

    first = node.run()

    assert first.returncode == 1
    assert names(node, NUT) == []
    assert node.pending() == [NUT]
    assert "secrets_inputs_sha256" not in node.applied()

    # The next run writes the output; no hash changed, and the queue alone restarts the unit.
    node.state["fail"] = []
    second = node.run()

    assert second.returncode == 0, second.stderr
    assert names(node, NUT) == [["reset-failed", NUT], ["restart", NUT]]


def test_a_failed_secrets_step_restarts_a_dependent_whose_persisted_output_exists(node):
    settled(node)
    node.write_dropin(NUT, "[Service]\nEnvironment=CHANGED=1\n")
    node.state["units"][SECRETS_UNIT]["active"] = "failed"
    node.state["fail"] = [SECRETS_RESTART]
    node.output_file(UPSMON, persist=True)
    node.manifest["legacy"].append({"path": "/etc/nut/upsmon.conf"})
    (node.root / "etc/nut").mkdir(parents=True)
    (node.root / "etc/nut/upsmon.conf").write_text("old\n")

    result = node.run()

    assert result.returncode == 1
    assert names(node, NUT) == [["reset-failed", NUT], ["restart", NUT]]
    assert (node.root / "etc/nut/upsmon.conf").exists()
    assert node.applied()["retired"] == []


def test_step_2_does_not_start_a_linked_unit_whose_output_is_missing(node):
    node.secrets(
        outputs=[
            {
                "destination": "app/env",
                "persist": False,
                "dependents": ["homelab-app.service"],
            }
        ]
    )
    node.unit("homelab-app.service", linked=True)
    node.state["effects"] = {}
    settled_run = node.run()
    assert settled_run.returncode == 1
    assert names(node, "homelab-app.service") == []
    assert node.pending() == ["homelab-app.service"]

    node.output_file("app/env")
    result = node.run()

    assert result.returncode == 0, result.stderr
    assert names(node, "homelab-app.service") == [
        ["reset-failed", "homelab-app.service"],
        ["restart", "homelab-app.service"],
    ]


# Legacy entries wait for the secrets.


def test_legacy_entries_wait_for_a_credential_and_retire_once_it_exists(node):
    node.secrets(
        outputs=[{"destination": UPSMON, "persist": True, "dependents": [NUT]}],
        credentials=["kuma_push_token"],
    )
    node.manifest["legacy"].append({"path": "/usr/local/bin/old"})
    (node.root / "usr/local/bin").mkdir(parents=True)
    (node.root / "usr/local/bin/old").write_text("")

    held = node.run()

    assert held.returncode == 1
    assert node.applied()["retired"] == []
    assert (node.root / "usr/local/bin/old").exists()

    node.credential("kuma_push_token")
    released = node.run()

    assert released.returncode == 0, released.stderr
    assert len(node.applied()["retired"]) == 1
    assert not (node.root / "usr/local/bin/old").exists()


def test_a_secrets_json_reconcile_cannot_read_fails_the_run_and_keeps_the_legacy_entries(
    node,
):
    (node.root / "usr/lib/homelab/secrets.json").write_text("not json")
    node.manifest["legacy"].append({"path": "/usr/local/bin/old"})

    result = node.run()

    assert result.returncode == 1
    assert node.applied()["retired"] == []


# A package without `secrets-unit`: the shape every host package had before H4.2.


def test_a_manifest_without_secrets_unit_runs_no_secrets_step(tmp_path):
    node = Node(tmp_path)
    node.unit("homelab-updater.service", policy="manual")
    node.unit("homelab-pi-sd.service", active="active")
    node.unit("homelab-pi-sd.timer", active="active", linked=True)
    node.manifest["legacy"].append({"path": "/usr/local/bin/old"})
    (node.root / "usr/local/bin").mkdir(parents=True)
    (node.root / "usr/local/bin/old").write_text("")
    # applied.json in H3's format: no secrets_inputs_sha256, and an older hash for the service.
    node.applied_path.parent.mkdir(parents=True)
    node.applied_path.write_text(
        json.dumps(
            {"units": {"homelab-pi-sd.service": {"file": "old", "dropin": None}}}
        )
    )
    node.state["fail"] = [SECRETS_RESTART]
    assert not (node.root / "run/homelab-secrets").exists()

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert all(SECRETS_UNIT not in call["argv"] for call in node.calls("systemctl"))
    assert ["reset-failed", "homelab-pi-sd.service"] in node.writes()
    assert node.writes().count(["restart", "homelab-pi-sd.service"]) == 1
    assert not (node.root / "usr/local/bin/old").exists()
    assert (node.root / "run/homelab-secrets").is_dir()
    assert (node.root / "run/homelab-secrets").stat().st_mode & 0o777 == 0o755
    assert "secrets_inputs_sha256" not in node.applied()


def test_the_pis_real_manifest_runs_the_secrets_step_once_for_its_two_credentials(
    tmp_path,
):
    package = host_mode.render_host("daniel-pi")
    node = Node(tmp_path)
    for dst, (content, _) in package.files.items():
        path = node.root / dst.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    node.manifest = json.loads(package.files["/usr/lib/homelab/manifest.json"][0])
    for unit in node.manifest["units"]:
        node.state["units"][unit] = {
            "load": "loaded",
            "active": "inactive",
            "enabled": "disabled",
        }
    assert "secrets_inputs_sha256" in node.manifest
    credentials = json.loads(package.files["/usr/lib/homelab/secrets.json"][0])[
        "credentials"
    ]
    assert credentials == ["pi_recovery_push_token", "pi_sd_health_push_token"]
    for key in credentials:
        node.credential(key)
    # H5.7: the Compose env file is an output, which the secrets unit renders before reconcile
    # restarts homelab-compose.service.
    outputs = json.loads(package.files["/usr/lib/homelab/secrets.json"][0])["outputs"]
    for output in outputs:
        node.output_file(output["destination"], persist=output["persist"])
    # H7.27: the line arm edits conffiles that exist on a real host.
    for unit in ("ssh.service", "systemd-journald.service", "auditd.service"):
        live_unit(node, unit)
    node.state["users"] = {
        "node_exporter": 999
    }  # the account the live Pi already holds
    (node.root / "var/lib/node-exporter-textfile").mkdir(parents=True)
    for edit in package.lines:
        target = node.root / edit["path"].lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a") as handle:
            handle.write(edit["line"] + "\n")

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.writes().count(["restart", SECRETS_UNIT]) == 1
