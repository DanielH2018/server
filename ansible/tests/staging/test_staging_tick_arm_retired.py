"""The staging cluster stays retired, and the hypervisor substrate the etcd drill needs stays.

Two retirements, one ratchet. #2859 removed the deployer's staging consultation: no `STAGING_*`
key in the rendered config, no staging module under the role's `files/`, and no marker for the
tick ledger or the one-tick override. #2941 then removed the cluster itself — the operator
decided no manual staging sessions continue, so the `daniel-stage` guest, the gate that deployed
to it, its inventory entry and its own secrets file are gone.

What survives is the libvirt substrate the monthly etcd restore drill runs in:
`roles/setup/hypervisor`, the staging network and the egress fence. That half is asserted here
too, because each check without the other is the wrong retirement — a check on what is gone
would pass just as well if somebody deleted the drill's substrate, and a check on the substrate
alone would pass while the arm or the guest grew back.

Run: uv run pytest ansible/tests/staging/test_staging_tick_arm_retired.py
"""

import re

from _helpers import REPO, SETUP_ROLES

ROLE = SETUP_ROLES / "gitops_deploy"
CONFIG_TEMPLATE = ROLE / "templates" / "config.env.j2"
UNIT = ROLE / "templates" / "gitops-deploy.service.j2"
DEFAULTS = ROLE / "defaults" / "main.yml"
MARKERS = ROLE / "files" / "gitops_markers.py"

# The keys the tick read the gate through. Named rather than matched by a `STAGING_` prefix:
# a prefix check passes on an empty file, and these are the four a revival would re-add.
RETIRED_KEYS = (
    "STAGING_GATE",
    "STAGING_GATE_BLOCKING",
    "STAGING_GATE_TIMEOUT_S",
    "STAGING_EXPECT_TIMEOUT_S",
)
RETIRED_MARKERS = ("staging_alerted", "staging_ticks", "staging_override")
# What the monthly etcd drill depends on. Named rather than globbed: a glob over the role's
# templates returns an empty set the moment the directory is renamed, and an `all()` over
# nothing passes.
KEPT = (
    "ansible/roles/setup/hypervisor/tasks/etcd_drill.yml",
    "ansible/roles/setup/hypervisor/templates/staging-network.xml.j2",
    "ansible/roles/setup/hypervisor/templates/staging-nwfilter.xml.j2",
    "ansible/roles/setup/hypervisor/templates/etcd-drill-vm.xml.j2",
)
# The daniel-stage surface #2941 removed, one entry per thing a revival would have to re-add.
GONE = (
    "ansible/inventory/host_vars/daniel-stage.yml",
    "ansible/vars/secrets-staging.yml",
    "ansible/roles/setup/hypervisor/tasks/guest.yml",
    "ansible/roles/setup/hypervisor/templates/staging-vm.xml.j2",
    "ansible/roles/setup/hypervisor/templates/staging-gate-dispatch.sh.j2",
    "ansible/roles/setup/hypervisor/files/staging-gate.pub",
    "scripts/deploy_tools/staging_gate.py",
    "scripts/deploy_tools/staging_gate_remote.sh",
    "scripts/deploy_tools/staging_expectations.py",
    "scripts/deploy_tools/verify_staging_gate_key.sh",
)


def test_the_rendered_config_carries_no_staging_key():
    text = CONFIG_TEMPLATE.read_text()
    present = [key for key in RETIRED_KEYS if f"{key}=" in text]
    assert not present, f"the tick would read the gate again through {present}"


def test_the_role_defaults_declare_no_staging_budget():
    """Nothing survives: the gate's ssh identity went with the gate (#2941), and the role now
    only reaps the path it used to write."""
    text = DEFAULTS.read_text()
    budgets = re.findall(r"^gitops_deploy_staging_\w+:", text, re.MULTILINE)
    assert budgets == [], budgets


def test_no_staging_module_ships_with_the_deployer():
    assert not list((ROLE / "files").glob("deploy_staging*.py"))


def test_the_marker_table_holds_no_staging_marker():
    text = MARKERS.read_text()
    present = [name for name in RETIRED_MARKERS if f'"{name}":' in text]
    assert not present, f"the marker table still names {present}"


def test_the_unit_budget_no_longer_counts_a_staging_pair():
    """The removed budgets were additive to the k8s pair inside one activation."""
    unit = UNIT.read_text()
    assert "STAGING_GATE_TIMEOUT_S (600)" not in unit
    assert "STAGING_EXPECT_TIMEOUT_S (120)" not in unit


def test_the_drill_substrate_is_still_shipped():
    """The rejecting half: retiring the cluster must not take the etcd drill's guest with it.

    Every path here is what the monthly drill reaches for, so a sweep that went one step too
    far fails by name rather than leaving these tests passing over a tree that can no longer
    run the drill at all.
    """
    missing = [path for path in KEPT if not (REPO / path).exists()]
    assert not missing, f"the etcd drill's substrate lost {missing}"


def test_the_daniel_stage_surface_is_gone():
    """The cluster itself, retired by operator decision on 2026-09-28 (#2941).

    Nothing consults the guest, so 8 GiB of daniel-server's RAM and a 100 GB qcow2 sat
    allocated for a host nothing drove. A file reappearing here is a revival, which is a
    decision rather than a side effect of a refactor.
    """
    present = [path for path in GONE if (REPO / path).exists()]
    assert not present, f"the retired daniel-stage surface is back: {present}"


def test_the_inventory_declares_no_staging_host():
    hosts = (REPO / "ansible" / "inventory" / "hosts.ini").read_text()
    assert "daniel-stage" not in hosts, (
        "hosts.ini names daniel-stage again. The guest is undefined and its disk reclaimed "
        "(roles/setup/hypervisor/tasks/reap_staging.yml), so an inventory entry points at "
        "nothing and every play that reaches it hangs on ssh."
    )


def test_the_role_reaps_the_guest_rather_than_only_forgetting_it():
    """Deleting guest.yml alone would leave daniel-stage running as an orphan.

    has_hypervisor stays TRUE on daniel-server for the drill, so teardown.yml never runs
    there — the reap has to sit on the install path or the host never converges.
    """
    reap = (
        REPO
        / "ansible"
        / "roles"
        / "setup"
        / "hypervisor"
        / "tasks"
        / "reap_staging.yml"
    ).read_text()
    assert "--remove-all-storage" in reap, (
        "reap_staging.yml no longer reclaims the guest's disk, so the 100 GB qcow2 is "
        "orphaned in /var/lib/libvirt/images with nothing that knows how to start it."
    )
    install = (
        REPO / "ansible" / "roles" / "setup" / "hypervisor" / "tasks" / "install.yml"
    ).read_text()
    assert "reap_staging.yml" in install, (
        "install.yml no longer includes reap_staging.yml. teardown.yml never runs on "
        "daniel-server, so nothing would undefine the guest."
    )
