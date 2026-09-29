"""The GitOps tick's staging arm stays retired, and the manual way in stays shipped.

#2859 removed the deployer's staging consultation: no `STAGING_*` key in the rendered
config, no staging module under the role's `files/`, and no marker for the tick ledger or
the one-tick override. The cluster itself is not retired — an operator still drives
`scripts/deploy_tools/staging_gate.py` by hand, and `roles/setup/hypervisor` still builds
the guest and the network the monthly etcd drill needs.

Both halves are asserted here, because each without the other is the wrong retirement: a
check on the arm alone would pass just as well if somebody deleted the staging cluster, and
a check on the cluster alone would pass while the arm grew back.

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
# What an operator still runs by hand, and what the monthly etcd drill depends on.
KEPT = (
    "scripts/deploy_tools/staging_gate.py",
    "scripts/deploy_tools/staging_expectations.py",
    "ansible/roles/setup/hypervisor/templates/staging-network.xml.j2",
    "ansible/roles/setup/hypervisor/templates/staging-vm.xml.j2",
    "docs/staging-cluster.md",
)


def test_the_rendered_config_carries_no_staging_key():
    text = CONFIG_TEMPLATE.read_text()
    present = [key for key in RETIRED_KEYS if f"{key}=" in text]
    assert not present, f"the tick would read the gate again through {present}"


def test_the_role_defaults_declare_no_staging_budget():
    """The key that survives is the ssh identity an operator's own run authenticates with."""
    text = DEFAULTS.read_text()
    budgets = re.findall(r"^gitops_deploy_staging_\w+:", text, re.MULTILINE)
    assert budgets == ["gitops_deploy_staging_gate_key_path:"], budgets


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


def test_the_manual_staging_path_is_still_shipped():
    """The rejecting half: retiring the arm must not read as retiring the cluster.

    Every path here is what an operator or the monthly etcd drill reaches for, so a sweep
    that took the cluster with the gate fails by name rather than leaving these tests
    passing over a tree that can no longer run staging at all.
    """
    missing = [path for path in KEPT if not (REPO / path).exists()]
    assert not missing, f"the manual staging path lost {missing}"
